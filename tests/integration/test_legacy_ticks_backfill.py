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


def test_a_real_sql_failure_is_recorded_cleanly_and_the_run_continues(db):
    co, s, folder, _ = _seed(db, texts=("Fix the gate", "Book crane"))
    db.execute("CREATE FUNCTION tick_boom() RETURNS trigger LANGUAGE plpgsql AS $$ "
               "BEGIN IF OLD.text = 'Fix the gate' THEN RAISE EXCEPTION 'boom'; END IF; "
               "RETURN NEW; END $$")
    db.execute("CREATE TRIGGER tick_boom BEFORE UPDATE ON action_items "
               "FOR EACH ROW EXECUTE FUNCTION tick_boom()")
    rep = lt.run(db, [_row(folder, "Fix the gate"), _row(folder, "Book crane", i=1)], apply=True)
    assert len(rep["failed"]) == 1 and "boom" in rep["failed"][0]["error"]
    assert "rollback" not in rep["failed"][0]["error"].lower()
    assert len(rep["applied"]) == 1 and rep["applied"][0]["text"] == "Book crane"
    assert _ai(db, "Fix the gate")[0][1] == "open" and _ai(db, "Book crane")[0][1] == "done"
    assert len(_edits(db, co)) == 1


def test_a_vanished_item_is_failed_not_found(db, monkeypatch):
    co, s, folder, _ = _seed(db)
    monkeypatch.setattr(action_items, "get_action_item", lambda *a, **k: None)
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert rep["applied"] == [] and rep["failed"][0]["error"] == "action item not found"


def test_reopened_item_is_superseded_not_reclosed(db):
    """open today only because someone re-opened it after the legacy tick."""
    co, s, folder, _ = _seed(db)
    [(aid, _, _)] = _ai(db, "Fix the gate")
    content_edits.append_content_edit(db, co["id"], "action_items", aid, "status",
                                      "done", "open", None, "pm")
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert rep["applied"] == [] and len(rep["superseded"]) == 1
    assert rep["superseded"][0]["status"] == "open"
    assert rep["superseded"][0]["action_item_id"] == str(aid)
    assert _ai(db, "Fix the gate")[0][1] == "open" and len(_edits(db, co)) == 1


def test_in_progress_item_is_superseded(db):
    co, s, folder, _ = _seed(db)
    db.execute("UPDATE action_items SET status='in_progress' WHERE text='Fix the gate'")
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply=True)
    assert rep["applied"] == [] and rep["superseded"][0]["status"] == "in_progress"
    assert _ai(db, "Fix the gate")[0][1] == "in_progress" and _edits(db, co) == []


def test_same_text_same_day_in_another_company_is_untouched(db):
    co1, s1, f1, _ = _seed(db)
    co2, s2, f2, _ = _seed(db)
    rep = lt.run(db, [_row(f1, "Fix the gate")], apply=True)
    assert len(rep["applied"]) == 1 and len(_edits(db, co1)) == 1
    assert _edits(db, co2) == []
    other = db.execute("SELECT a.status FROM action_items a JOIN sites s ON s.id=a.site_id "
                       "WHERE s.company_id=%s AND a.text='Fix the gate'", (co2["id"],)).fetchone()
    assert other[0] == "open"


def test_two_ticks_on_one_item_in_a_batch_write_once(db):
    co, s, folder, _ = _seed(db)
    rows = [_row(folder, "Fix the gate"), _row(folder, "fix  the GATE", i=5)]
    rep = lt.run(db, rows, apply=True)
    assert len(rep["applied"]) == 1 and len(rep["already_done"]) == 1
    assert len(_edits(db, co)) == 1


def test_sk_folder_vs_user_folder_conflict_is_not_applied(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [{**_row(folder, "Fix the gate"), "user_folder": "Somebody_Else"}], apply=True)
    assert len(rep["folder_conflict"]) == 1 and rep["applied"] == []
    assert _edits(db, co) == []


def test_a_string_apply_is_a_dry_run_even_when_called_directly(db):
    co, s, folder, _ = _seed(db)
    rep = lt.run(db, [_row(folder, "Fix the gate")], apply="true")
    assert rep["apply"] is False and _edits(db, co) == []


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
