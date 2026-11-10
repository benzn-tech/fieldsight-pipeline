"""Replay judge and SQL invariants for the site decision loop (P0 engineering gate).

Spec docs/superpowers/specs/2026-11-05-site-decision-loop-design.md §12 (rev 5), plan Task 7.
The idea (replay an append-only event log through a pure kernel and diff it against what
was stored; invariants; planted-fault mutation tests) is borrowed from
github.com/KKKKhazix/agent-autoloop `sim/` (MIT). No code was copied from it.

READ-ONLY. Nothing here writes: the CLI connects with default_transaction_read_only=on, and
the library functions only SELECT. It imports the PURE hub (decision_hub, decision_rules)
and never lambda_decision_scan, so a mutant patched into the scan module can never be
shared by the judge.

    replay(conn, company_id=None) -> list[str]
    invariants(conn, company_ids=None) -> list[str]
    real(findings) -> list[str]          findings without the KNOWN_FALSE ones

Scope: company_id / company_ids is None (every company), one id, or an iterable of ids.

replay() contract
-----------------
* Rows are read in seq order. EVERY row's dedupe_key and bucket are recomputed from its
  stored fields with the event's today_nz = nz_time.to_nz(received_at).date() and compared
  (a 'duplicate' row's '|dup|<event_id>' suffix is stripped first). A 'duplicate' row must
  follow a live row holding its key; a live row whose recomputed key an earlier live row
  already holds should have been a duplicate.
* Rows in 'queued' / 'running' are not judged further (their outcome is not decided yet).
* Pre-dispatch outcomes (late, unmatched, blocked, too_deep) are recomputed in the hub's
  order (blocked -> too_deep -> unmatched -> late -> dispatch) and compared one to one;
  done / nothing / merged / failed all mean "dispatched".
* `merged` events are the hub's absorb path (_dispatch merges same-subject queued events
  into the run without gating them): no pre-dispatch outcome is compared for them; they
  must be in run_events and named by no card.
* `blocked` is recomputed from the company's CURRENT companies.status (the status at
  processing time is not stored). A mismatch where the stored or the recomputed outcome is
  'blocked' gets the prefix KNOWN_FALSE ("KNOWN_FALSE: ...") only when
  companies.status_changed_at (migration 0092, set by every status change) is later than
  the event's last run start, or its received_at when it has no run; if the column is
  missing, every such mismatch is KNOWN_FALSE. A blocked mismatch with no status change
  since is a REAL finding. A real blocked-gate bug on a company whose status changed since
  still surfaces only as a KNOWN_FALSE line; real() drops them, the CLI prints them but
  does not fail on them.
* Rule cards (rule_id != 'hub.fault'): every 'done' event whose run outcome is 'done' has
  exactly one card naming it, with card_key / kind / purpose / rule_id / subject_ref /
  tenant recomputed from the event; a 'done' event without a card needs a ledger decision
  for its subject; a 'nothing' event needs an earlier event's card under the same card_key;
  every card names a 'done' event in scope. Per card_key: done-events-with-card = cards
  (exactly one carding done event per card), at most one card open, and every card but the
  newest is 'superseded'. Card status is otherwise NOT recomputed: supersede is decided by
  the checklist scan from S3 content, which the database does not hold. Recipients are not
  compared (membership changes over time).
* Runs: a pre-dispatch or duplicate event has no run; a done / nothing / merged event has
  exactly one non-failed run, and a 'done' event's run outcome is 'done' iff it has a card.
* Fault cards are judged through run_events / failed-run counts, not the hub: an event with
  >= FAULT_AT failed runs has exactly one 'fault' card (key fault_card_key(..., event_id,
  'fault')), marked stopped (version > 1 or 'stopped' in the consequence) once >= STOP_AT;
  a 'failed' event has >= STOP_AT failed runs; every too_deep event's chain has exactly one
  'too_deep' card; every fault card points at such an event / chain.

Consistency: judge(conn) runs replay() and invariants() in ONE transaction; the CLI's
connection is REPEATABLE READ READ ONLY with autocommit off (plus
default_transaction_read_only=on as a second guard), so both read a single snapshot and a
concurrent process() commit on a live database cannot produce a false finding.

CLI:  python tools/decision_replay.py --database-url URL [--company-id ID ...]
      prints every finding (KNOWN_FALSE ones labelled); exit 1 when any REAL finding
      (replay mismatch or invariant failure), else 0.
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import decision_hub  # noqa: E402
import tenancy_status  # noqa: E402
from decision_hub import FAULT_AT, FAULT_RULE_ID, STOP_AT, Event  # noqa: E402

KNOWN_FALSE = "KNOWN_FALSE: "

PRE_DISPATCH = ("late", "unmatched", "blocked", "too_deep")
DISPATCHED = ("done", "nothing", "merged", "failed")
UNDECIDED = ("queued", "running", "received")
TENANT_TABLES = ("site_events", "event_runs", "run_events", "decision_cards",
                 "decision_ledger")


def real(findings):
    """The findings that are not labelled KNOWN_FALSE."""
    return [f for f in findings if not f.startswith(KNOWN_FALSE)]


def _scope(company_id):
    if company_id is None:
        return None
    if isinstance(company_id, (str, bytes)) or not hasattr(company_id, "__iter__"):
        return [str(company_id)]
    return [str(c) for c in company_id]


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _where(ids, col):
    return (f" WHERE {col} = ANY(%s::uuid[])", (ids,)) if ids is not None else ("", ())


def _utc(d):
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def _event(row):
    payload = row["payload"] if row["payload"] is not None else {}
    if not isinstance(payload, dict):
        raise ValueError(f"payload is {type(payload).__name__}, not an object")
    if not isinstance(row["received_at"], datetime):
        raise ValueError("received_at is not a timestamp")
    return Event(event_id=row["event_id"], company_id=row["company_id"],
                 site_id=row["site_id"], source=row["source"], type=row["type"],
                 subject_ref=row["subject_ref"], occurred_at=row["occurred_at"],
                 received_at=_utc(row["received_at"]), payload=payload,
                 chain_id=row["chain_id"], chain_depth=row["chain_depth"] or 0)


def _predict(ev, today, statuses):
    """The hub's pre-dispatch outcome, in lambda_decision_scan._process_one's order."""
    if statuses.get(str(ev.company_id), tenancy_status.ACTIVE) != tenancy_status.ACTIVE:
        return "blocked", None
    if decision_hub.too_deep(ev):
        return "too_deep", None
    rule = decision_hub.match(ev)
    if rule is None:
        return "unmatched", None
    if decision_hub.is_late(ev, rule, today):
        return "late", rule
    return "dispatch", rule


def _status_changed_at(conn):
    """{str(company id): companies.status_changed_at}; None when the column is not there
    (migration 0092 missing), in which case every blocked mismatch is KNOWN_FALSE."""
    try:
        with conn.transaction():
            rows = conn.execute("SELECT id, status_changed_at FROM companies").fetchall()
    except Exception as exc:  # noqa: BLE001 - narrowed below
        if getattr(exc, "sqlstate", None) in ("42703", "42P01"):
            return None
        raise
    return {str(r[0]): r[1] for r in rows}


def _status_changed_since(changed_at, company_id, processed_at):
    """True when the company's status may have changed after the event was processed:
    status_changed_at is later than the event's last run start (or, for an event with no
    run, its received_at -- processing is never earlier). Missing column -> True."""
    if changed_at is None:
        return True
    at = changed_at.get(company_id)
    if at is None:
        return False
    return _utc(at) > _utc(processed_at)


def _decided(conn, ev):
    return conn.execute(
        "SELECT 1 FROM decision_ledger WHERE company_id = %s AND site_id = %s "
        "AND subject_ref = %s AND kind IN ('decided', 'declined') LIMIT 1",
        (ev.company_id, ev.site_id, ev.subject_ref)).fetchone() is not None


def replay(conn, company_id=None):
    """Human-readable mismatches between the pure hub and what is stored; [] when
    consistent. See the module docstring for the contract (and KNOWN_FALSE)."""
    ids = _scope(company_id)
    w, p = _where(ids, "company_id")
    events = _rows(conn, "SELECT * FROM site_events" + w + " ORDER BY seq", p)
    cards = _rows(conn, "SELECT card_id, event_id, company_id, site_id, rule_id, subject_ref, "
                        "purpose, card_key, kind, status, version, consequence "
                        "FROM decision_cards" + w + " ORDER BY created_at, card_id", p)
    wr, pr = _where(ids, "re.company_id")
    runs = defaultdict(list)                       # event_id -> [outcome]
    last_run = {}                                  # event_id -> latest run started_at
    for r in _rows(conn, "SELECT re.event_id, r.outcome, r.started_at FROM run_events re "
                         "JOIN event_runs r ON r.run_id = re.run_id" + wr, pr):
        eid_ = str(r["event_id"])
        runs[eid_].append(r["outcome"])
        if r["started_at"] is not None and (eid_ not in last_run
                                            or r["started_at"] > last_run[eid_]):
            last_run[eid_] = r["started_at"]
    statuses = tenancy_status.company_statuses(conn)
    changed_at = _status_changed_at(conn)

    out = []
    by_id = {str(e["event_id"]): e for e in events}
    rule_cards = defaultdict(list)                 # event_id -> [card]
    card_seqs = defaultdict(list)                  # card_key -> [seq of the card's event]
    fault_cards = defaultdict(list)                # card_key -> [card]
    for c in cards:
        if c["rule_id"] == FAULT_RULE_ID:
            fault_cards[c["card_key"]].append(c)
            continue
        rule_cards[str(c["event_id"])].append(c)
        ev_row = by_id.get(str(c["event_id"]))
        if ev_row is not None:
            card_seqs[c["card_key"]].append(ev_row["seq"])

    live = {}                                      # recomputed key -> event_id
    live_stored = set()                            # stored keys of live rows so far
    done_with_card = defaultdict(int)              # card_key -> done events that carded
    too_deep_chains = set()

    for row in events:
        eid, status, seq = str(row["event_id"]), row["status"], row["seq"]
        tag = f"event {eid} (seq {seq}, {row['type']})"
        try:
            ev = _event(row)
        except ValueError as exc:
            if status not in UNDECIDED and status != "failed":
                out.append(f"{tag}: unreadable ({exc}) but stored {status}")
            continue
        today = decision_hub.today_nz_of(ev)
        want = decision_hub.dedupe_key(ev, today)
        rule = decision_hub.match(ev)
        want_bucket = rule.bucket(ev, today) if rule is not None else ""
        if (row["bucket"] or "") != want_bucket:
            out.append(f"{tag}: bucket {row['bucket']!r}, hub says {want_bucket!r}")
        stored = row["dedupe_key"]
        if status == "duplicate":
            suffix = f"|dup|{eid}"
            if not stored.endswith(suffix):
                out.append(f"{tag}: duplicate without its |dup|<event_id> suffix: {stored!r}")
            else:
                stored = stored[:-len(suffix)]
            if stored not in live_stored:
                out.append(f"{tag}: duplicate of {stored!r}, but no earlier live event "
                           "holds that dedupe_key")
        if stored != want:
            out.append(f"{tag}: dedupe_key {stored!r}, hub recomputes {want!r}")
        if status == "duplicate":
            if runs.get(eid):
                out.append(f"{tag}: duplicate event has {len(runs[eid])} run(s)")
            continue
        if want in live:
            out.append(f"{tag}: stored {status}, but its recomputed dedupe_key is already "
                       f"held by event {live[want]} (hub says duplicate)")
        else:
            live[want] = eid
        live_stored.add(row["dedupe_key"])
        if status in UNDECIDED:
            continue

        if status == "merged":
            # The absorb path: _dispatch merges same-subject queued events into the run
            # without gating them, so no pre-dispatch outcome is compared. Only membership
            # in run_events is judged (and that no card names it).
            if not runs.get(eid):
                out.append(f"{tag}: merged but in no run (run_events)")
            if rule_cards.get(eid):
                out.append(f"{tag}: merged but {len(rule_cards[eid])} card(s) name it")
            continue
        pred, prule = _predict(ev, today, statuses)
        actual = "dispatch" if status in DISPATCHED else status
        if pred != actual:
            msg = f"{tag}: stored {status}, hub says {pred}"
            if "blocked" in (pred, actual) and _status_changed_since(
                    changed_at, str(ev.company_id), last_run.get(eid) or ev.received_at):
                msg = (KNOWN_FALSE + msg + " (blocked is recomputed from the company's "
                       "CURRENT status, which changed after the event was processed)")
            out.append(msg)
        if status == "too_deep":
            too_deep_chains.add((str(ev.company_id), str(ev.site_id),
                                 str(ev.chain_id or ev.event_id)))

        outcomes = runs.get(eid, [])
        ok_runs = [o for o in outcomes if o != "failed"]
        failed_runs = len(outcomes) - len(ok_runs)
        own = rule_cards.get(eid, [])
        if status in PRE_DISPATCH:
            if outcomes:
                out.append(f"{tag}: pre-dispatch outcome {status} but {len(outcomes)} run(s)")
            if own:
                out.append(f"{tag}: pre-dispatch outcome {status} but {len(own)} card(s)")
            continue
        if status == "failed" and failed_runs < STOP_AT:
            out.append(f"{tag}: failed with {failed_runs} failed run(s), stop is at {STOP_AT}")
        if status in ("done", "nothing") and len(ok_runs) != 1:
            out.append(f"{tag}: {status} with {len(ok_runs)} non-failed run(s), expected 1")
        if status in ("nothing", "failed") and own:
            out.append(f"{tag}: {status} but {len(own)} card(s) name it")
        rule = rule or prule
        if rule is None or status not in ("done", "nothing"):
            continue
        key = decision_hub.card_key(ev, rule)
        if status == "nothing":
            if not any(s < seq for s in card_seqs.get(key, [])):
                out.append(f"{tag}: nothing, but no earlier event's card holds {key!r}")
            continue
        carded = "done" in ok_runs
        if not own:
            if carded:
                out.append(f"{tag}: run outcome done but no card names the event")
            elif not _decided(conn, ev):
                out.append(f"{tag}: done without a card and without a ledger decision "
                           f"for {ev.subject_ref!r}")
            continue
        if not carded:
            out.append(f"{tag}: has a card but its run outcomes are {ok_runs}")
        if len(own) != 1:
            out.append(f"{tag}: {len(own)} cards name the event, expected 1")
        draft = rule.build_card(ev)
        for c in own:
            for col, exp in (("card_key", key), ("kind", draft.kind),
                             ("rule_id", rule.rule_id), ("purpose", rule.purpose),
                             ("subject_ref", str(ev.subject_ref)),
                             ("company_id", str(ev.company_id)),
                             ("site_id", str(ev.site_id))):
                if str(c[col]) != exp:
                    out.append(f"{tag}: card {c['card_id']} {col} {c[col]!r}, "
                               f"hub says {exp!r}")
        done_with_card[key] += 1

    # cards -> events
    per_key = defaultdict(list)
    for eid, cs in rule_cards.items():
        for c in cs:
            ev_row = by_id.get(eid)
            if ev_row is None:
                out.append(f"card {c['card_id']} ({c['card_key']}): its event {eid} is not "
                           "a site_events row in scope")
                continue
            if ev_row["status"] != "done":
                out.append(f"card {c['card_id']} ({c['card_key']}): its event {eid} is "
                           f"{ev_row['status']}, not done")
            if c["status"] not in ("open", "superseded"):
                out.append(f"card {c['card_id']}: status {c['status']} (P0 has only open / "
                           "superseded)")
            per_key[c["card_key"]].append((ev_row["seq"], c))
    for key, lst in per_key.items():
        if done_with_card.get(key, 0) != len(lst):
            out.append(f"card_key {key!r}: {len(lst)} card(s) but "
                       f"{done_with_card.get(key, 0)} done event(s) that carded")
        lst.sort(key=lambda x: (x[0], str(x[1]["card_id"])))
        if sum(1 for _, c in lst if c["status"] == "open") > 1:
            out.append(f"card_key {key!r}: more than one open card")
        for _, c in lst[:-1]:
            if c["status"] != "superseded":
                out.append(f"card_key {key!r}: older card {c['card_id']} is {c['status']}, "
                           "not superseded")

    # fault cards, through failed-run counts
    for eid, row in by_id.items():
        failed_runs = sum(1 for o in runs.get(eid, []) if o == "failed")
        if failed_runs < FAULT_AT:
            continue
        key = decision_hub.fault_card_key(row["company_id"], row["site_id"], eid, "fault")
        fc = fault_cards.get(key, [])
        if len(fc) != 1:
            out.append(f"event {eid}: {failed_runs} failed runs, {len(fc)} fault card(s), "
                       "expected 1")
        elif failed_runs >= STOP_AT and not (
                fc[0]["version"] > 1 or "stopped" in (fc[0]["consequence"] or "").lower()):
            out.append(f"event {eid}: {failed_runs} failed runs but fault card "
                       f"{fc[0]['card_id']} does not say retrying stopped")
    for cid, sid, chain in too_deep_chains:
        n = len(fault_cards.get(decision_hub.fault_card_key(cid, sid, chain, "too_deep"), []))
        if n != 1:
            out.append(f"chain {chain}: too_deep events, {n} too_deep card(s), expected 1")
    for cs in fault_cards.values():
        for c in cs:
            ref = str(c["subject_ref"])
            exp = decision_hub.fault_card_key(c["company_id"], c["site_id"], ref, c["purpose"])
            if c["card_key"] != exp or c["kind"] != "fault":
                out.append(f"fault card {c['card_id']}: key/kind {c['card_key']!r}/{c['kind']} "
                           f"!= {exp!r}/fault")
            if c["purpose"] == "fault":
                n = sum(1 for o in runs.get(ref, []) if o == "failed")
                if ref not in by_id or n < FAULT_AT:
                    out.append(f"fault card {c['card_id']}: event {ref} has {n} failed runs "
                               f"(< {FAULT_AT}) or is not in scope")
            elif c["purpose"] == "too_deep":
                if (str(c["company_id"]), str(c["site_id"]), ref) not in too_deep_chains:
                    out.append(f"too_deep card {c['card_id']}: no too_deep event in chain {ref}")
            else:
                out.append(f"fault card {c['card_id']}: unknown purpose {c['purpose']!r}")
    return out


def _check(conn, out, label, sql, params=()):
    rows = conn.execute(sql, params).fetchall()
    if rows:
        sample = "; ".join(" ".join(str(v) for v in r) for r in rows[:5])
        out.append(f"{label}: {len(rows)} row(s), e.g. {sample}")


def invariants(conn, company_ids=None):
    """SQL invariant checks; human-readable failures, [] when all hold."""
    ids = _scope(company_ids)
    out = []

    def scoped(col, extra_col=None):
        if ids is None:
            return "", ()
        if extra_col is None:
            return f" AND {col} = ANY(%s::uuid[])", (ids,)
        return (f" AND ({col} = ANY(%s::uuid[]) OR {extra_col} = ANY(%s::uuid[]))",
                (ids, ids))

    s, p = scoped("c.company_id")
    _check(conn, out, "card without an event of the same company and site",
           "SELECT c.card_id FROM decision_cards c LEFT JOIN site_events e "
           "ON e.event_id = c.event_id AND e.company_id = c.company_id "
           "AND e.site_id = c.site_id WHERE e.event_id IS NULL" + s, p)
    for table in TENANT_TABLES:
        s, p = scoped("t.company_id", "si.company_id")
        _check(conn, out, f"{table}: site belongs to another company than the row's company_id",
               f"SELECT t.company_id, t.site_id, si.company_id FROM {table} t "
               "JOIN sites si ON si.id = t.site_id WHERE si.company_id <> t.company_id" + s, p)
    s, p = scoped("company_id")
    _check(conn, out, "more than one open card per card_key",
           "SELECT card_key, count(*) FROM decision_cards WHERE status = 'open'" + s +
           " GROUP BY card_key HAVING count(*) > 1", p)
    _check(conn, out, "decision_ledger row of kind defaulted (none in P0)",
           "SELECT entry_id FROM decision_ledger WHERE kind = 'defaulted'" + s, p)
    s, p = scoped("e.company_id")
    _check(conn, out, "merged event not in run_events",
           "SELECT e.event_id FROM site_events e WHERE e.status = 'merged' "
           "AND NOT EXISTS (SELECT 1 FROM run_events re WHERE re.event_id = e.event_id)" + s, p)
    failed = ("SELECT re.event_id, count(*) AS n FROM run_events re "
              "JOIN event_runs r ON r.run_id = re.run_id WHERE r.outcome = 'failed' "
              "GROUP BY re.event_id")
    fault_count = ("(SELECT count(*) FROM decision_cards c WHERE c.rule_id = %s "
                   "AND c.purpose = 'fault' AND c.subject_ref = e.event_id::text "
                   "AND c.company_id = e.company_id AND c.site_id = e.site_id)")
    _check(conn, out, f"event with >= {FAULT_AT} failed runs without exactly one fault card",
           f"SELECT e.event_id, f.n, {fault_count} FROM site_events e "
           f"JOIN ({failed}) f ON f.event_id = e.event_id "
           f"WHERE f.n >= %s AND {fault_count} <> 1" + s,
           (FAULT_RULE_ID, FAULT_AT, FAULT_RULE_ID) + p)
    _check(conn, out, f"event with more than {STOP_AT} failed runs",
           f"SELECT e.event_id, f.n FROM site_events e JOIN ({failed}) f "
           f"ON f.event_id = e.event_id WHERE f.n > %s" + s, (STOP_AT,) + p)
    _check(conn, out, "site_events.attempts differs from its failed-run count",
           f"SELECT e.event_id, e.attempts, COALESCE(f.n, 0) FROM site_events e "
           f"LEFT JOIN ({failed}) f ON f.event_id = e.event_id "
           f"WHERE e.attempts <> COALESCE(f.n, 0)" + s, p)
    _check(conn, out, "too_deep chain without exactly one too_deep card",
           "SELECT DISTINCT e.company_id, e.site_id, COALESCE(e.chain_id, e.event_id) "
           "FROM site_events e WHERE e.status = 'too_deep' AND (SELECT count(*) "
           "FROM decision_cards c WHERE c.rule_id = %s AND c.purpose = 'too_deep' "
           "AND c.subject_ref = COALESCE(e.chain_id, e.event_id)::text "
           "AND c.company_id = e.company_id AND c.site_id = e.site_id) <> 1" + s,
           (FAULT_RULE_ID,) + p)
    s, p = scoped("re.company_id", "e.company_id")
    _check(conn, out,
           "run_events company_id/site_id differ from its event_runs or site_events row",
           "SELECT re.run_id, re.event_id FROM run_events re "
           "JOIN event_runs r ON r.run_id = re.run_id "
           "JOIN site_events e ON e.event_id = re.event_id "
           "WHERE ((re.company_id, re.site_id) <> (r.company_id, r.site_id) "
           "   OR (re.company_id, re.site_id) <> (e.company_id, e.site_id))" + s, p)
    return out


def connect_read_only(url):
    """Autocommit OFF, every transaction REPEATABLE READ READ ONLY (psycopg session
    settings), and default_transaction_read_only=on at connect time as a second guard.
    tenancy_status's conn.transaction() inside it becomes a savepoint."""
    import psycopg
    conn = psycopg.connect(url, autocommit=False,
                           options="-c default_transaction_read_only=on")
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    return conn


def judge(conn, company_ids=None):
    """(replay findings, invariant findings), both read inside ONE transaction (one
    snapshot under REPEATABLE READ) that is always rolled back -- nothing is written."""
    with conn.transaction(force_rollback=True):
        return replay(conn, company_ids), invariants(conn, company_ids)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Site decision loop replay judge (read-only).")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                    help="postgres URL (default: $DATABASE_URL)")
    ap.add_argument("--company-id", action="append", default=None,
                    help="limit to this company (repeatable); default every company")
    args = ap.parse_args(argv)
    if not args.database_url:
        ap.error("--database-url (or DATABASE_URL) is required")
    conn = connect_read_only(args.database_url)
    try:
        rep, inv = judge(conn, args.company_id)
    finally:
        conn.close()
    known = [f for f in rep if f.startswith(KNOWN_FALSE)]
    bad = real(rep) + inv
    print(f"replay: {len(real(rep))} mismatch(es), {len(known)} known false")
    for f in rep:
        print("  " + f)
    print(f"invariants: {len(inv)} failure(s)")
    for f in inv:
        print("  " + f)
    print(f"{len(bad)} real finding(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
