"""Integration: the decision-loop replay judge and SQL invariants (plan Task 7, spec §12).

The judge (tools/decision_replay.py) re-runs site_events in seq order through the PURE hub
and must reproduce the stored outcomes and decision_cards; the invariants are SQL checks.
Rows are written only through the rollback `db` fixture (decision_ledger rows can never be
cleaned up, so none is ever committed). Skipped without TEST_DATABASE_URL -- a skip is NOT
a pass.
"""
import os
import sys

import psycopg
import pytest

import lambda_decision_scan as lds
from tests.integration.decision_scripted_day import run_scripted_day
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
    assert any("late" in m for m in out), out


def test_invariants_find_a_cross_company_row(db):
    day = run_scripted_day(db)
    home, co = day["home"], day["co"]
    db.execute("UPDATE decision_cards SET company_id = %s WHERE card_id = "
               "(SELECT card_id FROM decision_cards WHERE company_id = %s LIMIT 1)",
               (home["id"], co["id"]))
    out = decision_replay.invariants(db, company_ids=day["companies"])
    assert any("decision_cards" in m and "company" in m for m in out), out


def test_blocked_mismatch_is_labelled_known_false(db, monkeypatch):
    """`blocked` is recomputed from the company's CURRENT status: a company reactivated
    since shows as a KNOWN_FALSE mismatch, never as a real one."""
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    co, site, pm, admin = _world(db)
    _overdue_item(db, site)
    db.execute("UPDATE companies SET status = 'suspended' WHERE id = %s", (co["id"],))
    lds.emit_todo_overdue(db, NOW)
    lds.process(db, NOW)
    assert decision_replay.replay(db, company_id=co["id"]) == []      # consistent now
    db.execute("UPDATE companies SET status = 'active' WHERE id = %s", (co["id"],))
    out = decision_replay.replay(db, company_id=co["id"])
    assert len(out) == 1 and out[0].startswith(decision_replay.KNOWN_FALSE), out
    assert decision_replay.real(out) == []
    assert decision_replay.invariants(db, company_ids=[co["id"]]) == []

    # and the other way round: processed while active, suspended since
    co2, site2, _, _ = _world(db)
    _overdue_item(db, site2)
    lds.emit_todo_overdue(db, NOW)
    lds.process(db, NOW)
    db.execute("UPDATE companies SET status = 'exited' WHERE id = %s", (co2["id"],))
    out = decision_replay.replay(db, company_id=co2["id"])
    assert out and all(m.startswith(decision_replay.KNOWN_FALSE) for m in out), out


def test_cli_connection_is_read_only(migrated_db_url):
    conn = decision_replay.connect_read_only(migrated_db_url)
    try:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("INSERT INTO companies (name) VALUES (%s)", ("replay-write-probe",))
    finally:
        conn.close()


def test_cli_main_reports_and_exits(migrated_db_url, capsys):
    """Read-only run of the CLI against the shared test database; exit 0 when clean."""
    code = decision_replay.main(["--database-url", migrated_db_url])
    out = capsys.readouterr().out
    assert "replay:" in out and "invariants:" in out
    last = out.strip().splitlines()[-1]
    assert code == (0 if last == "0 real finding(s)" else 1), out
