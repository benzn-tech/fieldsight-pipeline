"""Integration: the legacy tick backfill against real Postgres.

The matcher is by TEXT within (date, recorder folder) -- legacy ticks were
positional and the positions have since moved. The ambiguous / duplicate case is
the one that must write nothing.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import legacy_ticks_backfill as lt
from repositories import action_items, companies, content_edits, redactions, sites, topics

pytestmark = pytest.mark.integration

DATE = "2026-08-17"
CHECKED_AT = "2026-08-19T00:30:00Z"   # 12:30 NZST on 2026-08-19, far from any midnight


def _seed(db, texts=("Fix the gate", "Order scaffolding", "Order scaffolding"), date=DATE):
    co = companies.create_company(db, f"Tick-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Tick-Site")
    folder = f"TickF-{uuid.uuid4().hex[:8]}"
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (co["id"], f"{folder.lower()}@example.test", folder)).fetchone()[0]
    t = topics.upsert_topic(db, s["id"], date, "Gate", user_id=uid,
                            action_items=[{"text": x} for x in texts])
    return co, s, folder, t


def _row(folder, text, i=0, checked=True, checked_at=CHECKED_AT, date=DATE, sk=None):
    r = {"PK": f"ACTIONS#{date}", "SK": sk or f"USER#{folder}#TOPIC#0#ACTION#{i}",
         "action_text": text, "checked": checked, "checked_by": "Ben"}
    if checked_at is not None:
        r["checked_at"] = checked_at
    return r


def _ai(db, text):
    return db.execute("SELECT id, status, updated_by FROM action_items WHERE text=%s "
                      "ORDER BY id", (text,)).fetchall()


def _edits(db, co):
    return db.execute("SELECT field, before_text, after_text, actor_user_id, actor_role, "
                      "created_at, row_id FROM content_edits WHERE company_id=%s",
                      (co["id"],)).fetchall()


def test_unambiguous_tick_is_applied_with_an_audit_row(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert len(rep["applied"]) == 1 and rep["failed"] == []
    [(aid, status, updated_by)] = _ai(db, "Fix the gate")
    assert status == "done" and updated_by is None
    assert rep["applied"][0]["action_item_id"] == str(aid)
    [e] = _edits(db, co)
    assert e[:5] == ("status", "open", "done", None, "legacy_tick_backfill")
    assert e[6] == aid


def test_duplicate_text_is_ambiguous_and_writes_nothing(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [_row(folder, "order  SCAFFOLDING", i=1)], apply=True)
    assert len(rep["ambiguous"]) == 1 and rep["applied"] == []
    assert {r[1] for r in _ai(db, "Order scaffolding")} == {"open"}
    assert _edits(db, co) == []


def test_matches_by_text_not_position(db):
    """The tick says ACTION#2; the text lives at index 0 today. Position must not matter."""
    co, s, folder, _ = _seed(db, texts=("Fix the gate", "Book crane"))
    rep = lt.run(db, [_row(folder, "Book crane", i=0)], apply=True)
    assert len(rep["applied"]) == 1
    assert _ai(db, "Book crane")[0][1] == "done" and _ai(db, "Fix the gate")[0][1] == "open"


def test_unmatched_findings_no_folder_unchecked(db):
    co, s, folder, _ = _seed(db)
    rows = [_row(folder, "Nonexistent"),
            _row(folder, "Fix the gate", sk=f"USER#{folder}#TOPIC#0#ACTION#flag_0"),
            _row(folder, "Fix the gate", sk="TOPIC#0#ACTION#0"),
            _row(folder, "Fix the gate", checked=False)]
    rep = lt.run(db, rows, apply=True)
    assert [len(rep[k]) for k in ("unmatched", "findings", "no_folder", "applied")] == [1, 1, 1, 0]
    assert rep["unchecked"] == 1
    assert _edits(db, co) == []


def test_second_apply_reports_already_done_and_writes_nothing_new(db):
    co, s, folder, _ = _seed(db)
    rows = [_row(folder, "Fix the gate")]
    lt.run(db, rows, apply=True)
    rep = lt.run(db, rows, apply=True)
    assert len(rep["already_done"]) == 1 and rep["applied"] == []
    assert len(_edits(db, co)) == 1


def test_dry_run_writes_nothing_but_reports_what_it_would_do(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=False)
    assert rep["apply"] is False and len(rep["applied"]) == 1
    assert _ai(db, "Fix the gate")[0][1] == "open" and _edits(db, co) == []


def test_checked_row_without_a_usable_timestamp_is_never_applied(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [_row(folder, "Fix the gate", checked_at=None),
                      _row(folder, "Fix the gate", checked_at="garbage")], apply=True)
    assert len(rep["no_timestamp"]) == 2 and rep["applied"] == []
    assert _ai(db, "Fix the gate")[0][1] == "open" and _edits(db, co) == []


def test_deleted_topics_are_not_candidates(db):
    co, s, folder, t = _seed(db)
    redactions.create_redaction(db, co["id"], t["id"], "test", None, "admin", scope="deleted")
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert len(rep["unmatched"]) == 1 and _edits(db, co) == []


def test_failed_update_is_recorded_not_applied(db, monkeypatch):
    co, s, folder, _ = _seed(db)
    monkeypatch.setattr(action_items, "update_action_item_fields", lambda *a, **k: None)
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert rep["applied"] == [] and len(rep["failed"]) == 1 and _edits(db, co) == []


def test_closure_is_attributed_to_the_checked_at_day_not_today(db):
    co, s, folder, _ = _seed(db)
    lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    tz = content_edits.CLOSURE_TZ
    on_day = content_edits.count_action_closures_by_day(
        db, [s["id"]], "2026-08-19", "2026-08-19", company_id=co["id"], tz=tz)
    assert sum(on_day.values()) == 1, on_day
    today = datetime.now(timezone.utc).date()
    on_today = content_edits.count_action_closures_by_day(
        db, [s["id"]], str(today - timedelta(days=1)), str(today + timedelta(days=1)),
        company_id=co["id"], tz=tz)
    assert on_today == {}, on_today
