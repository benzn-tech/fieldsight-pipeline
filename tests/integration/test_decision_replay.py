"""Integration: the decision-loop replay judge and SQL invariants (plan Task 7, spec §12).

The judge (tools/decision_replay.py) re-runs site_events in seq order through the PURE hub
and must reproduce the stored outcomes and decision_cards; the invariants are SQL checks.
Rows are written only through the rollback `db` fixture (decision_ledger rows can never be
cleaned up, so none is ever committed). Skipped without TEST_DATABASE_URL -- a skip is NOT
a pass.
"""
import datetime as dt
import os
import sys
import uuid

import psycopg
import pytest

import lambda_decision_scan as lds
from tests.integration.decision_scripted_day import DAY2, _direct, run_scripted_day
from tests.integration.test_decision_scan import NOW, _overdue_item, _world

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "tools"))
import decision_replay  # noqa: E402

pytestmark = pytest.mark.integration


def _statuses(conn, company_ids):
    return dict(conn.execute(
        "SELECT status, count(*) FROM site_events WHERE company_id = ANY(%s::uuid[]) "
        "GROUP BY status", ([str(c) for c in company_ids],)).fetchall())


def test_replay_of_a_scripted_day_is_clean(db):
    day = run_scripted_day(db)
    scope = day["companies"]
    # the day really exercised every path the judge looks at
    st = _statuses(db, scope)
    for status in ("done", "nothing", "duplicate", "late", "merged", "failed", "unmatched",
                   "too_deep"):
        assert st.get(status, 0) >= 1, (status, st)
    card_status = dict(db.execute(
        "SELECT status, count(*) FROM decision_cards WHERE company_id = ANY(%s::uuid[]) "
        "GROUP BY status", ([str(c) for c in scope],)).fetchall())
    assert card_status.get("open", 0) >= 5 and card_status.get("superseded", 0) == 1
    fence = db.execute("SELECT attempts, status FROM site_events WHERE subject_ref = %s "
                       "ORDER BY seq LIMIT 1", (day["fence_ref"],)).fetchone()
    assert fence == (5, "failed")
    # the external recorder's gap events are under the SITE's company only
    assert db.execute("SELECT count(*) FROM site_events WHERE company_id = %s "
                      "AND type = 'checklist.gap' AND status <> 'late'",
                      (day["home"]["id"],)).fetchone()[0] == 0

    assert decision_replay.replay(db, company_id=scope) == []
    assert decision_replay.invariants(db, company_ids=scope) == []
    # unscoped (what the CLI does): the SQL without a company filter runs, and none of its
    # findings (rows other tests may have left behind) is about this day's rows
    ours = {str(r[0]) for r in db.execute(
        "SELECT event_id FROM site_events WHERE company_id = ANY(%s::uuid[]) UNION "
        "SELECT card_id FROM decision_cards WHERE company_id = ANY(%s::uuid[])",
        ([str(c) for c in scope], [str(c) for c in scope])).fetchall()}
    unscoped = decision_replay.replay(db) + decision_replay.invariants(db)
    assert not [f for f in unscoped if any(i in f for i in ours)], unscoped


def test_replay_finds_a_tampered_card_and_status(db):
    """The judge is not vacuous: hand-edited rows are reported."""
    day = run_scripted_day(db)
    scope = day["companies"]
    db.execute("UPDATE site_events SET status = 'done' WHERE status = 'late' "
               "AND company_id = ANY(%s::uuid[])", ([str(c) for c in scope],))
    out = decision_replay.replay(db, company_id=scope)
    assert any("hub says late" in m for m in out), out


def test_invariants_find_a_cross_company_row(db):
    day = run_scripted_day(db)
    home, co = day["home"], day["co"]
    db.execute("UPDATE decision_cards SET company_id = %s WHERE card_id = "
               "(SELECT card_id FROM decision_cards WHERE company_id = %s LIMIT 1)",
               (home["id"], co["id"]))
    out = decision_replay.invariants(db, company_ids=day["companies"])
    assert any("decision_cards" in m and "company" in m for m in out), out


def _set_status(conn, company_id, status, changed_at):
    conn.execute("UPDATE companies SET status = %s, status_changed_at = %s WHERE id = %s",
                 (status, changed_at, company_id))


def _later(conn):
    """A status_changed_at after everything this transaction wrote (runs start at now(),
    events were received at NOW)."""
    return conn.execute("SELECT GREATEST(now(), %s) + interval '1 second'",
                        (NOW,)).fetchone()[0]


def test_blocked_mismatch_is_known_false_only_after_a_status_change(db, monkeypatch):
    """`blocked` is recomputed from the company's CURRENT status. A mismatch is KNOWN_FALSE
    only when companies.status_changed_at is after the event's last run (or its
    received_at); with no change since, it is a REAL finding."""
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    co, site, pm, admin = _world(db)
    _overdue_item(db, site)
    _set_status(db, co["id"], "suspended", NOW - dt.timedelta(days=1))
    lds.emit_todo_overdue(db, NOW)
    lds.process(db, NOW)
    assert decision_replay.replay(db, company_id=co["id"]) == []      # consistent now
    # reactivated AFTER the event was processed: known false
    _set_status(db, co["id"], "active", _later(db))
    out = decision_replay.replay(db, company_id=co["id"])
    assert len(out) == 1 and out[0].startswith(decision_replay.KNOWN_FALSE), out
    assert decision_replay.real(out) == []
    assert decision_replay.invariants(db, company_ids=[co["id"]]) == []
    # the same mismatch with the change dated BEFORE the event: a real finding
    _set_status(db, co["id"], "active", NOW - dt.timedelta(hours=1))
    out = decision_replay.replay(db, company_id=co["id"])
    assert len(decision_replay.real(out)) == 1 and "blocked" in out[0], out

    # the other way round: processed while active, exited since (after its run)
    co2, site2, _, _ = _world(db)
    _overdue_item(db, site2)
    lds.emit_todo_overdue(db, NOW)
    lds.process(db, NOW)
    _set_status(db, co2["id"], "exited", _later(db))
    out = decision_replay.replay(db, company_id=co2["id"])
    assert out and all(m.startswith(decision_replay.KNOWN_FALSE) for m in out), out
    # never-changed status (status_changed_at NULL) -> real
    _set_status(db, co2["id"], "exited", None)
    out = decision_replay.replay(db, company_id=co2["id"])
    assert out and decision_replay.real(out) == out, out


def test_merged_event_is_not_gated(db):
    """Merge absorbs same-subject queued events without the pre-dispatch gates: a merged
    event the hub alone would call too_deep is judged only by its run_events link."""
    co, site, pm, admin = _world(db)
    subject = str(uuid.uuid4())
    absorbed = _direct(db, co, site, "todo.overdue", subject, NOW, chain_id=uuid.uuid4(),
                       chain_depth=4, not_before=DAY2 + dt.timedelta(days=1))
    _direct(db, co, site, "todo.overdue", subject, DAY2)
    lds.process(db, DAY2)
    st = db.execute("SELECT status FROM site_events WHERE event_id = %s",
                    (absorbed["event_id"],)).fetchone()[0]
    assert st == "merged"
    assert decision_replay.replay(db, company_id=co["id"]) == []
    assert decision_replay.invariants(db, company_ids=[co["id"]]) == []
    # a merged event outside every run is reported
    db.execute("DELETE FROM run_events WHERE event_id = %s", (absorbed["event_id"],))
    out = decision_replay.replay(db, company_id=co["id"])
    assert any("merged but in no run" in m for m in out), out


def test_replay_understands_run_rule_events(db, monkeypatch):
    """A run-rule event has no card by design: done / nothing (incl. flag off) and a
    failing one are all clean; a card naming it or a run outcome that disagrees with the
    status is reported."""
    import decision_rules
    from repositories import decision_cards as dc
    from repositories import sites as sites_repo
    outcomes = {}

    def run(conn, event, ctx):
        o = outcomes[str(event.site_id)]
        if o == "raise":
            raise RuntimeError("job exploded")
        return o
    monkeypatch.setitem(decision_rules.RULES, "test.run", decision_rules.Rule(
        rule_id="test.run", bucket=lambda event, today_nz: today_nz.isoformat(),
        late_after_days=None, purpose="run", run=run))
    co, site_done, pm, admin = _world(db)
    site_nothing, site_raise, site_off = (
        sites_repo.create_site(db, co["id"], f"S-rr-{uuid.uuid4().hex[:6]}")
        for _ in range(3))
    outcomes.update({str(site_done["id"]): "done", str(site_nothing["id"]): "nothing",
                     str(site_raise["id"]): "raise"})
    evs = {}
    for name, s in (("done", site_done), ("nothing", site_nothing), ("raise", site_raise)):
        evs[name] = _direct(db, co, s, "test.run", str(s["id"]), NOW)
    monkeypatch.setenv("NIGHTLY_REMATCH", "shadow")
    t = NOW
    for _ in range(3):                                    # the raising one reaches FAULT_AT
        lds.process(db, t)
        t += dt.timedelta(hours=2)
    monkeypatch.delenv("NIGHTLY_REMATCH")
    evs["off"] = _direct(db, co, site_off, "test.run", str(site_off["id"]), t)
    lds.process(db, t)
    st = {k: db.execute("SELECT status, attempts FROM site_events WHERE event_id = %s",
                        (e["event_id"],)).fetchone() for k, e in evs.items()}
    # the raising event is due again in the flag-off pass: the flag is a kill switch for a
    # retrying run-rule event too (it ends 'nothing' after its 3 failed runs)
    assert st == {"done": ("done", 0), "nothing": ("nothing", 0), "raise": ("nothing", 3),
                  "off": ("nothing", 0)}, st
    assert decision_replay.replay(db, company_id=co["id"]) == []
    assert decision_replay.invariants(db, company_ids=[co["id"]]) == []

    # tamper 1: the run outcome disagrees with the event status
    run_of = ("UPDATE event_runs SET outcome = %s WHERE run_id = "
              "(SELECT run_id FROM run_events WHERE event_id = %s)")
    db.execute(run_of, ("done", evs["nothing"]["event_id"]))
    out = decision_replay.replay(db, company_id=co["id"])
    assert any("run-rule event nothing with run outcome(s) ['done']" in m for m in out), out
    db.execute(run_of, ("nothing", evs["nothing"]["event_id"]))
    assert decision_replay.replay(db, company_id=co["id"]) == []
    # tamper 2: a card names a run-rule event
    dc.insert_open(db, {"card_key": f"tamper|{uuid.uuid4()}", "rule_id": "test.run",
                        "purpose": "run", "subject_ref": str(site_done["id"]),
                        "kind": "choose", "question": "q", "consequence": "c",
                        "options": [], "evidence": [], "urgency": "normal",
                        "deadline_at": None, "timeout_default": None, "recipients": [],
                        "event_id": evs["done"]["event_id"], "company_id": co["id"],
                        "site_id": site_done["id"]})
    out = decision_replay.replay(db, company_id=co["id"])
    assert any("run-rule event done but 1 card(s) name it" in m for m in out), out


def test_cli_connection_is_one_read_only_snapshot(migrated_db_url):
    conn = decision_replay.connect_read_only(migrated_db_url)
    other = psycopg.connect(migrated_db_url, autocommit=True)
    probe = None
    try:
        assert conn.autocommit is False
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        before = conn.execute("SELECT count(*) FROM companies").fetchone()[0]
        probe = other.execute("INSERT INTO companies (name) VALUES ('replay-snapshot-probe') "
                              "RETURNING id").fetchone()[0]
        # same transaction: the concurrent commit is invisible (one snapshot)
        assert conn.execute("SELECT count(*) FROM companies").fetchone()[0] == before
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO companies (name) VALUES (%s)", ("replay-write-probe",))
        conn.rollback()
        # tenancy_status's conn.transaction() works inside judge()'s transaction
        rep, inv = decision_replay.judge(conn, company_ids=[str(probe)])
        assert rep == [] and inv == []
    finally:
        conn.close()
        if probe is not None:
            other.execute("DELETE FROM companies WHERE id = %s", (probe,))
        other.close()


def test_cli_main_reports_and_exits(migrated_db_url, capsys):
    """Read-only run of the CLI against the shared test database; exit 0 when clean."""
    code = decision_replay.main(["--database-url", migrated_db_url])
    out = capsys.readouterr().out
    assert "replay:" in out and "invariants:" in out
    last = out.strip().splitlines()[-1]
    assert code == (0 if last == "0 real finding(s)" else 1), out


@pytest.mark.parametrize("rep,inv,code", [
    (["event x: stored done, hub says late"], [], 1),
    ([], ["merged event not in run_events: 1 row(s)"], 1),
    ([decision_replay.KNOWN_FALSE + "event y: stored done, hub says blocked"], [], 0),
    ([], [], 0),
])
def test_cli_exit_code(migrated_db_url, monkeypatch, capsys, rep, inv, code):
    monkeypatch.setattr(decision_replay, "replay", lambda conn, ids=None: list(rep))
    monkeypatch.setattr(decision_replay, "invariants", lambda conn, ids=None: list(inv))
    assert decision_replay.main(["--database-url", migrated_db_url]) == code
    out = capsys.readouterr().out
    for f in rep + inv:
        assert f in out
