"""
Lambda: decision-scan -- the site decision loop's emitters and hub loop (P0, shadow).

Spec docs/superpowers/specs/2026-11-05-site-decision-loop-design.md (rev 5), plan Task 4.

Event:  {"scan": "checklist_gap" | "todo_overdue" | "nightly_tick" | "process"}
Result: {"emitted", "duplicates", "skipped_ambiguous_site", "skipped_pending", "processed",
         "cards", "faults", "errors"}  (counts), or {"skipped": "flag off"} when
         SITE_DECISION_LOOP is not "shadow" -- returned before any DB connection.

checklist_gap  every checklist_reports row of the last 48 h not yet seen under its
               result_key: its S3 result is read; a done result emits one checklist.gap
               event per gap item on the session's ONE site (zero or several sites ->
               skipped, counted, never guessed), supersedes the open cards of gaps that are
               gone, and marks the report seen in the same transaction.
todo_overdue   one todo.overdue event per live open action item past its deadline (bucket =
               the scan's NZ day, so one event per item per day).
nightly_tick   one nightly.due event per site whose local time is 01:00-04:59 and whose
               night (the previous local date) has none yet (nightly_rematch.due_sites);
               bucket = the night, so a second tick or an EventBridge retry is a
               duplicate. The hourly schedule exists only while NIGHTLY_REMATCH != off.
process        claims due queued events (FOR UPDATE SKIP LOCKED) and runs them through the
               hub: auth -> too_deep -> match -> late -> merge -> ledger -> open card.
               A run-rule (Rule.run, e.g. the nightly job) skips the ledger and the card:
               after the merge the hub calls rule.run(conn, event, RunContext) inside the
               run; its outcome ('done' / 'nothing') is both the run outcome and the event
               status, and a raise is a failed attempt (backoff, fault at 3, stop at 5).
               NIGHTLY_REMATCH gates every run-rule: unless it is "shadow" or "on", the
               event ends 'nothing' (run outcome 'nothing') WITHOUT calling run, so a
               leftover event can never act once the flag is turned off.

Each emit scan runs process right after it, in the same invocation and connection, and
returns the summed counts. There is no process schedule: Aurora pauses after 600 s idle
and a 5-minute tick would keep it awake (tests/unit/test_sweep_cadence_vs_autopause.py).
{"scan": "process"} alone stays valid for a manual invoke. An event that failed and is
backing off is retried at the next scan (checklist_gap every 30 minutes).

P0 SENDS NOTHING: no SES, email, push, voice, Lambda invoke or model call happens here.
Cards are routed (recipients computed) and stored with status 'open'.

Transactions: never conn.commit() here. Each unit of work is `with conn.transaction()`:
under the handler's connection that is a real BEGIN/COMMIT, under a caller's open
transaction a savepoint. process() claims and handles a batch in ONE transaction, each
event in its own savepoint; a Lambda crash rolls the whole batch back, so no row is ever
left 'running'. Run-rule events are the exception: each is claimed and run in its OWN
transaction (claim limit 1), before the batches, so a timeout mid-way never rolls back a
site whose run already finished -- and whose S3 request artifacts, which no rollback
undoes, are already written.

Hub functions are imported BY NAME and called unqualified: mutation tests patch
`lambda_decision_scan.<name>`.
"""
import logging
import os
from datetime import datetime, time, timedelta, timezone

import checklist_reports
import decision_rules
import nightly_rematch
import nz_time
import tenancy_status
from db.connection import get_connection
from decision_hub import (FAULT_AT, FAULT_RULE_ID, RUN_OUTCOMES, STOP_AT, Event, RunContext,
                          backoff_seconds, card_key, dedupe_key, fault_card, fault_card_key,
                          is_late, match, recipients, too_deep, too_deep_card)
from repositories import action_items, decision_cards, decision_ledger, event_runs
from repositories import memberships, recordings, site_events, users

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FLAG = "SITE_DECISION_LOOP"
RUN_FLAG = "NIGHTLY_REMATCH"         # gates every run-rule; "shadow" | "on" run, else nothing
RUN_FLAG_VALUES = ("shadow", "on")
RESULT_KEYS = ("emitted", "duplicates", "skipped_ambiguous_site", "skipped_pending",
               "processed", "cards", "faults", "errors")

BATCH = 50          # events claimed per transaction
MAX_BATCHES = 20    # per invocation; the next schedule picks up the rest
MAX_RUN_EVENTS = 200       # run-rule events per invocation, one transaction each
# Start no new run-rule event this long after process() began: the Lambda Timeout is 120 s and
# one nightly run is bounded by nightly_rematch.RUN_BUDGET_S (45 s).
RUN_EVENTS_WINDOW_S = 30.0
NIGHTLY_TYPE = nightly_rematch.EVENT_TYPE
GAP_TYPE = "checklist.gap"
OVERDUE_TYPE = "todo.overdue"


def _counts():
    return dict.fromkeys(RESULT_KEYS, 0)


def _utc(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _insert_event(conn, ev, today_nz):
    """Insert the hub Event as a queued site_events row. True when new, False when its
    dedupe key was already live (stored as a 'duplicate' row)."""
    rule = match(ev)
    bucket = rule.bucket(ev, today_nz) if rule is not None else ""
    _, is_new = site_events.insert(conn, {
        "event_id": ev.event_id, "company_id": ev.company_id, "site_id": ev.site_id,
        "source": ev.source, "type": ev.type, "subject_ref": ev.subject_ref,
        "bucket": bucket, "occurred_at": ev.occurred_at, "received_at": ev.received_at,
        "summary": None, "link": None, "payload": ev.payload, "chain_id": ev.chain_id,
        "chain_depth": ev.chain_depth, "dedupe_key": dedupe_key(ev, today_nz),
        "status": "queued"})
    return is_new


def _new_event(company_id, site_id, source, type_, subject_ref, occurred_at, now, payload):
    import uuid
    return Event(event_id=uuid.uuid4(), company_id=company_id, site_id=site_id,
                 source=source, type=type_, subject_ref=subject_ref,
                 occurred_at=occurred_at, received_at=now, payload=payload,
                 chain_id=None, chain_depth=0)


# --------------------------------------------------------------------------- emitters

def emit_todo_overdue(conn, now):
    """One todo.overdue event per live open item whose deadline is before the NZ day of
    `now` (derived here, so the bucket can never disagree with received_at).
    occurred_at = received_at = now (the scan); company = the item's site's company.
    Each item in its own savepoint: one bad item is counted in errors and the rest of the
    day's events are kept."""
    out = _counts()
    today_nz = nz_time.to_nz(now).date()
    for item in action_items.overdue_open(conn, today_nz):
        try:
            with conn.transaction():
                _emit_overdue_item(conn, item, today_nz, now, out)
        except Exception as exc:  # noqa: BLE001 - one bad item never stops the scan
            out["errors"] += 1
            logger.warning("decision-scan overdue: item %s not emitted: %r",
                           item.get("stable_id"), exc)
    logger.info("decision-scan todo_overdue %s", out)
    return out


def _emit_overdue_item(conn, item, today_nz, now, out):
    deadline = item.get("deadline")
    ev = _new_event(
        item["company_id"], item["site_id"], "action_items", OVERDUE_TYPE,
        str(item["stable_id"]), now, now,
        {"text": item.get("text"),
         "deadline": deadline.isoformat() if deadline is not None else None,
         "responsible": item.get("responsible")})
    if _insert_event(conn, ev, today_nz):
        out["emitted"] += 1
    else:
        out["duplicates"] += 1


def emit_nightly_due(conn, now):
    """One nightly.due event per due site (nightly_rematch.due_sites): source nightly_scan,
    subject_ref = the site id, occurred_at = received_at = now, payload {night}; the rule's
    bucket is the night. Each site in its own savepoint: one bad site is counted in errors."""
    out = _counts()
    today_nz = nz_time.to_nz(now).date()
    for due in nightly_rematch.due_sites(conn, now):
        try:
            with conn.transaction():
                ev = _new_event(due["company_id"], due["site_id"], nightly_rematch.SOURCE,
                                NIGHTLY_TYPE, str(due["site_id"]), now, now,
                                {"night": due["night"].isoformat()})
                if _insert_event(conn, ev, today_nz):
                    out["emitted"] += 1
                else:
                    out["duplicates"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad site never stops the tick
            out["errors"] += 1
            logger.warning("decision-scan nightly: site %s not emitted: %r",
                           due.get("site_id"), exc)
    logger.info("decision-scan nightly_tick %s", out)
    return out


def _report_midnight_utc(report_date):
    """report_date 00:00 in NZ, tz-aware UTC."""
    if isinstance(report_date, str):
        report_date = datetime.fromisoformat(report_date[:10]).date()
    return nz_time.from_nz(datetime.combine(report_date, time(0, 0))).replace(
        tzinfo=timezone.utc)


def _gap_items(gaps):
    for key, kind in (("notCovered", "not_covered"), ("failed", "failed")):
        for g in (gaps or {}).get(key) or []:
            if isinstance(g, dict) and g.get("n") is not None:
                yield kind, g


def _emit_report(conn, rep, gaps, company_id, site_id, now, out):
    """One report's events, supersedes and seen-mark (caller holds the savepoint)."""
    session = rep["session"]
    report_date = rep["report_date"]
    occurred_at = _report_midnight_utc(report_date)
    today_nz = nz_time.to_nz(now).date()
    rule = None
    current = set()
    titles = list((gaps or {}).get("checklists") or [])
    for kind, g in _gap_items(gaps):
        title = g.get("checklist") or ""
        if title not in titles:
            titles.append(title)
        payload = {"checklist": title, "item": g.get("text"), "kind": kind,
                   "comment": g.get("comment"), "session": session,
                   "report_date": str(report_date)[:10], "n": g.get("n"),
                   "report_id": str(rep["id"])}
        if g.get("quote"):
            payload["quote"] = g["quote"]
        if g.get("responsible"):
            payload["responsible"] = g["responsible"]
        ev = _new_event(company_id, site_id, "checklist_reports", GAP_TYPE,
                        f"{session}|{title}|{g['n']}|{g.get('text')}", occurred_at, now,
                        payload)
        rule = rule or match(ev)
        current.add(card_key(ev, rule))
        if _insert_event(conn, ev, today_nz):
            out["emitted"] += 1
        else:
            out["duplicates"] += 1
    # A gap that was on THIS report's checklist and is gone now: its open card is
    # superseded. Only checklists the result names with known gaps are judged, and only
    # cards whose originating event came from this report (report_id), so two reports of
    # one template in one session never close each other's cards, whatever the scan order.
    for title in titles:
        for card in decision_cards.open_for_subject_prefix(
                conn, company_id, site_id, GAP_TYPE, f"{session}|{title}|",
                report_id=str(rep["id"])):
            if card["card_key"] not in current:
                decision_cards.supersede(conn, company_id, site_id, card["card_key"])
    checklist_reports.mark_seen(conn, rep["id"], rep["result_key"])


def emit_checklist_gaps(conn, read_result, now):
    """checklist.gap events from every unseen report. `read_result(result_key)` returns
    (status, error, gaps) -- checklist_reports.read_result_status bound to an S3 client.
    Not done -> skipped_pending; any exception -> errors; neither is marked seen, so the
    next scan reads it again. Zero or several sites -> skipped_ambiguous_site."""
    out = _counts()
    for rep in checklist_reports.unseen_reports(conn):
        ref = {"report": str(rep["id"]), "result_key": rep["result_key"]}
        try:
            sites_ = recordings.session_site_companies(
                conn, rep["user_folder"], str(rep["report_date"]), rep["session"])
            if len(sites_) != 1:
                out["skipped_ambiguous_site"] += 1
                logger.warning("decision-scan gap: %d sites for session, skipped %s",
                               len(sites_), ref)
                continue
            site_id, company_id = sites_[0]
            status, _err, gaps = read_result(rep["result_key"])
            if status != "done":
                out["skipped_pending"] += 1
                continue
            with conn.transaction():
                _emit_report(conn, rep, gaps, company_id, site_id, now, out)
        except Exception as exc:  # noqa: BLE001 - one bad report never stops the scan
            out["errors"] += 1
            logger.warning("decision-scan gap: report not scanned %s: %r", ref, exc)
    logger.info("decision-scan checklist_gap %s", out)
    return out


# --------------------------------------------------------------------------- process

def _event_of(row):
    """The hub Event of a claimed row. ValueError on a row the hub cannot read (payload not
    an object, received_at not a timestamp)."""
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


def _card_row(draft, key, rule_id, purpose, subject_ref, ev, recips):
    return {"card_key": key, "rule_id": rule_id, "purpose": purpose,
            "subject_ref": str(subject_ref), "kind": draft.kind, "question": draft.question,
            "consequence": draft.consequence, "options": draft.options,
            "evidence": draft.evidence, "urgency": draft.urgency,
            "deadline_at": draft.deadline_at, "timeout_default": draft.timeout_default,
            "recipients": recips, "event_id": ev.event_id, "company_id": ev.company_id,
            "site_id": ev.site_id}


def _route(conn, draft, company_id, site_id):
    """recipients() over the v1 sources, always the SITE's company."""
    roles = set(draft.recipient_roles or ())
    members = (memberships.members_for_site(conn, company_id, site_id)
               if roles & {"pm", "site_manager"} else [])
    company = users.list_company_users(conn, company_id) if "company_admin" in roles else []
    ops = ([u for u in users.list_all_users(conn) if u.get("global_role") == "platform_admin"]
           if "ops" in roles else [])
    return recipients(draft, members, company, ops)


def _handle(conn, event, rule, now):
    """The per-event work after dispatch: ledger, then the open card. Returns
    (event status, run outcome, card row or None). Tests patch this seam."""
    if decision_ledger.decided(conn, event.company_id, event.site_id, event.subject_ref):
        return "done", "nothing", None
    draft = rule.build_card(event)
    card = decision_cards.insert_open(conn, _card_row(
        draft, card_key(event, rule), rule.rule_id, rule.purpose, event.subject_ref, event,
        _route(conn, draft, event.company_id, event.site_id)))
    if card is None:
        return "nothing", "nothing", None
    return "done", "done", card


def _lock_queued(conn, event_ids):
    """The subset of event_ids still 'queued' and not held by another transaction,
    locked until ours ends -- so a merge never overwrites an event another scan has
    claimed meanwhile."""
    if not event_ids:
        return set()
    rows = conn.execute(
        "SELECT event_id FROM site_events WHERE event_id = ANY(%s) AND status = 'queued' "
        "FOR UPDATE SKIP LOCKED", (list(event_ids),)).fetchall()
    return {r[0] for r in rows}


def _run_job(conn, ev, rule, run_id, now, job):
    """A run-rule's work: rule.run inside the open run. No ledger lookup (a run-rule's
    subject_ref is a site, not an item) and no card. Flag off -> 'nothing' without calling
    run. An outcome outside RUN_OUTCOMES raises (a failed attempt like any other)."""
    flag, s3_client, bucket = job
    if flag not in RUN_FLAG_VALUES:
        logger.info("decision-scan process: run-rule %s event %s not run (%s off)",
                    rule.rule_id, ev.event_id, RUN_FLAG)
        return "nothing", "nothing", None
    outcome = rule.run(conn, ev, RunContext(
        run_id=run_id, company_id=ev.company_id, site_id=ev.site_id, now_utc=now,
        flag=flag, s3_client=s3_client, bucket=bucket))
    if outcome not in RUN_OUTCOMES:
        # The type only: a bad return value may carry item text (never logged).
        raise ValueError(f"run-rule {rule.rule_id} returned a {type(outcome).__name__}, "
                         f"not one of {RUN_OUTCOMES}")
    return outcome, outcome, None


def _dispatch(conn, row, ev, rule, now, job=(None, None, None)):
    """Merge, run, handle -- inside the event's savepoint. Returns (status, outcome, card).
    job = (NIGHTLY_REMATCH value, s3_client, bucket), used only by run-rules."""
    cid, sid = ev.company_id, ev.site_id
    others = [r["event_id"] for r in site_events.queued_same_subject(
        conn, cid, sid, ev.type, ev.subject_ref) if r["event_id"] != ev.event_id]
    free = _lock_queued(conn, others)
    merged = [e for e in others if e in free]
    run = event_runs.start(conn, cid, sid, [ev.event_id], row["attempts"] + 1, rule.rule_id)
    for eid in merged:
        site_events.set_status(conn, eid, cid, sid, "merged", rule_id=rule.rule_id)
        event_runs.add_event(conn, run["run_id"], eid, cid, sid)
    if rule.run is not None:
        status, outcome, card = _run_job(conn, ev, rule, run["run_id"], now, job)
    else:
        status, outcome, card = _handle(conn, ev, rule, now)
    site_events.set_status(conn, ev.event_id, cid, sid, status, rule_id=rule.rule_id)
    event_runs.finish(conn, run["run_id"], cid, sid, outcome)
    return status, outcome, card


def _insert_fault(conn, ev, draft, purpose, subject_ref):
    key = fault_card_key(ev.company_id, ev.site_id, subject_ref, purpose)
    return decision_cards.insert_open(conn, _card_row(
        draft, key, FAULT_RULE_ID, purpose, subject_ref, ev,
        _route(conn, draft, ev.company_id, ev.site_id)))


def _fail(conn, row, ev, exc, now, out):
    """Record a failed attempt in the OUTER transaction (the event's savepoint is already
    rolled back): a failed run row, attempts + 1, backoff or terminal 'failed', and the
    fault card at FAULT_AT, bumped at STOP_AT."""
    cid, sid = ev.company_id, ev.site_id
    attempts = (row["attempts"] or 0) + 1
    err = f"{type(exc).__name__}: {exc}"[:1000]
    logger.warning("decision-scan process: event %s attempt %d failed: %s",
                   ev.event_id, attempts, err)
    run = event_runs.start(conn, cid, sid, [ev.event_id], attempts,
                           row.get("rule_id") or ev.type)
    event_runs.finish(conn, run["run_id"], cid, sid, "failed", error=err)
    if attempts >= STOP_AT:
        site_events.set_status(conn, ev.event_id, cid, sid, "failed", attempts=attempts,
                               last_error=err)
    else:
        site_events.set_status(conn, ev.event_id, cid, sid, "queued", attempts=attempts,
                               last_error=err,
                               not_before=now + timedelta(seconds=backoff_seconds(attempts)))
    if attempts == FAULT_AT or attempts >= STOP_AT:
        # Own savepoint: a fault-card failure (e.g. a recipient lookup) must never undo the
        # run row / attempts / backoff above, or the batch would stall on this event.
        try:
            with conn.transaction():
                _fault_step(conn, ev, attempts, out)
        except Exception:  # noqa: BLE001 - logged; the attempt itself stays recorded
            logger.exception("decision-scan process: fault card for event %s (attempt %d) "
                             "not written", ev.event_id, attempts)


def _fault_step(conn, ev, attempts, out):
    """The fault card at FAULT_AT; its bump (or a stopped card if none is open) at STOP_AT."""
    cid, sid = ev.company_id, ev.site_id
    if attempts == FAULT_AT:
        if _insert_fault(conn, ev, fault_card(ev, attempts, False), "fault", ev.event_id):
            out["faults"] += 1
    elif attempts >= STOP_AT:
        stopped = fault_card(ev, attempts, True)
        key = fault_card_key(cid, sid, ev.event_id, "fault")
        card = decision_cards.find_open(conn, cid, sid, key)
        if card is not None:
            decision_cards.bump(conn, card["card_id"], cid, sid, stopped.consequence)
        elif _insert_fault(conn, ev, stopped, "fault", ev.event_id):
            out["faults"] += 1


def _bare_event(row, now):
    """A minimal Event for recording the failure of a row _event_of could not read."""
    return Event(event_id=row["event_id"], company_id=row["company_id"],
                 site_id=row["site_id"], source=str(row.get("source") or ""),
                 type=str(row.get("type") or ""), subject_ref=str(row.get("subject_ref") or ""),
                 occurred_at=None, received_at=now, payload={}, chain_id=row.get("chain_id"),
                 chain_depth=0)


def _process_one(conn, row, now, out, job=(None, None, None)):
    """A row _event_of cannot read (malformed payload/timestamps) is a failed attempt like
    a handler failure: it goes through _fail (backoff, fault at 3, stop at 5)."""
    try:
        ev = _event_of(row)
    except Exception as exc:  # noqa: BLE001
        _fail(conn, row, _bare_event(row, now), exc, now, out)
        return
    cid, sid = ev.company_id, ev.site_id
    try:
        with conn.transaction():
            today_nz = nz_time.to_nz(ev.received_at).date()
            # Pre-dispatch outcomes start no run.
            if tenancy_status.company_status(conn, cid) != tenancy_status.ACTIVE:
                site_events.set_status(conn, ev.event_id, cid, sid, "blocked")
                return
            if too_deep(ev):
                site_events.set_status(conn, ev.event_id, cid, sid, "too_deep")
                chain = ev.chain_id or ev.event_id
                if _insert_fault(conn, ev, too_deep_card(ev), "too_deep", chain):
                    out["faults"] += 1
                return
            rule = match(ev)
            if rule is None:
                site_events.set_status(conn, ev.event_id, cid, sid, "unmatched")
                return
            if is_late(ev, rule, today_nz):
                site_events.set_status(conn, ev.event_id, cid, sid, "late",
                                       rule_id=rule.rule_id)
                return
            _status, _outcome, card = _dispatch(conn, row, ev, rule, now, job)
            if card is not None:
                out["cards"] += 1
    except Exception as exc:  # noqa: BLE001 - recorded, retried with backoff
        _fail(conn, row, ev, exc, now, out)


def _run_types():
    """Event types whose rule is a run-rule (read at call time: tests register rules)."""
    return sorted(t for t, r in decision_rules.RULES.items() if r.run is not None)


def process(conn, now, *, s3_client=None, bucket=None, clock=None):
    """Claim due events and run each through the hub. Run-rule events first, one per
    transaction; then the rest, one transaction per batch, each event in its own
    savepoint. Returns counts (processed, cards, faults). s3_client and bucket only reach
    run-rules (RunContext); NIGHTLY_REMATCH is read once per call."""
    import time
    clock = clock or time.monotonic
    out = _counts()
    job = (os.environ.get(RUN_FLAG), s3_client, bucket)
    run_types = _run_types()
    started = clock()
    for _ in range(MAX_RUN_EVENTS if run_types else 0):
        if clock() - started > RUN_EVENTS_WINDOW_S:
            break
        with conn.transaction():
            rows = site_events.claim_due(conn, now, 1, only_types=run_types)
            for row in rows:
                out["processed"] += 1
                _process_one(conn, row, now, out, job)
        if not rows:
            break
    for _ in range(MAX_BATCHES):
        with conn.transaction():
            rows = site_events.claim_due(conn, now, BATCH, exclude_types=run_types)
            for row in rows:
                out["processed"] += 1
                _process_one(conn, row, now, out, job)
        if len(rows) < BATCH:
            break
    logger.info("decision-scan process %s", out)
    return out


# --------------------------------------------------------------------------- handler

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3")
    return _s3_client


def lambda_handler(event, context):
    if os.environ.get(FLAG) != "shadow":
        return {"skipped": "flag off"}
    scan = (event or {}).get("scan")
    if scan not in ("checklist_gap", "todo_overdue", "nightly_tick", "process"):
        raise ValueError(f"decision-scan: unknown scan {scan!r}")
    bucket = os.environ.get("LAKE_BUCKET", "")
    if scan in ("checklist_gap", "nightly_tick") and not bucket:
        # Fail loudly once, not as a per-report error on every scan forever.
        raise RuntimeError("decision-scan: LAKE_BUCKET is not set")
    now = datetime.now(timezone.utc)
    result = _counts()
    with get_connection(autocommit=True) as conn:
        runs = []
        if scan == "todo_overdue":
            runs.append(emit_todo_overdue(conn, now))
        elif scan == "nightly_tick":
            runs.append(emit_nightly_due(conn, now))
        elif scan == "checklist_gap":
            runs.append(emit_checklist_gaps(
                conn, lambda key: checklist_reports.read_result_status(key, _s3(), bucket),
                now))
        # Always process, right after the emit: no separate schedule wakes the database.
        runs.append(process(conn, now, s3_client=_s3() if bucket else None,
                            bucket=bucket or None))
    for got in runs:
        for k in RESULT_KEYS:
            result[k] += got.get(k, 0)
    return result
