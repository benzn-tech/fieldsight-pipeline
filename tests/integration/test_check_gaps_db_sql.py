"""Check done (step A): the DB hop's two new queries against Postgres.

The unit doubles record SQL and never parse it, so the session pick's interval
arithmetic and the uuid[] filter are only proven here.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from repositories import inspection_windows as iw
from repositories import meeting_session as ms
from repositories import report_templates as rt

pytestmark = pytest.mark.integration

PRESSED = datetime(2026, 10, 19, 21, 5, 0, tzinfo=timezone.utc)
CHECKLIST = {"sections": [{"key": "c", "title": "Pre-pour", "purpose": "p", "kind": "checklist",
                           "items": ["Formwork"]}],
             "catch_all": {"key": "o", "title": "Other", "purpose": "o"},
             "excluded_subjects": [], "style": []}


def _company(db):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                      ("Gaps %s" % uuid.uuid4().hex[:6],)).fetchone()[0]


def _user(db, cid):
    return db.execute("INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, 'gm') "
                      "RETURNING id", (cid, "%s@example.test" % uuid.uuid4().hex)).fetchone()[0]


def _session(db, cid, uid, opened, last=None):
    sid = uuid.uuid4().hex
    db.execute("INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
               "opened_at, last_segment_at) VALUES (%s, %s, %s, 'audio', 'open', %s, %s)",
               (sid, cid, uid, opened, last))
    return sid


def test_the_callers_most_recently_active_session_is_picked(db):
    cid = _company(db)
    me, other = _user(db, cid), _user(db, cid)
    older = _session(db, cid, me, PRESSED - timedelta(minutes=30), PRESSED - timedelta(minutes=4))
    live = _session(db, cid, me, PRESSED - timedelta(minutes=20), PRESSED - timedelta(seconds=10))
    _session(db, cid, other, PRESSED - timedelta(minutes=5), PRESSED)        # newer, not mine
    row = ms.latest_for_user_near(db, me, int(PRESSED.timestamp()))
    assert row["session_id"] == live and row["session_id"] != older


def test_a_session_idle_for_longer_than_five_minutes_is_not_live(db):
    cid = _company(db)
    me = _user(db, cid)
    _session(db, cid, me, PRESSED - timedelta(minutes=40), PRESSED - timedelta(minutes=6))
    assert ms.latest_for_user_near(db, me, int(PRESSED.timestamp())) is None


def test_a_session_with_no_chunk_yet_is_found_by_its_open_time(db):
    cid = _company(db)
    me = _user(db, cid)
    fresh = _session(db, cid, me, PRESSED - timedelta(seconds=40), None)
    assert ms.latest_for_user_near(db, me, int(PRESSED.timestamp()))["session_id"] == fresh


def test_live_windows_are_this_folders_and_this_companys_only(db):
    cid, other = _company(db), _company(db)
    uid = _user(db, cid)
    t = rt.create(db, cid, "org", None, "pre-pour", "Pre-pour Inspection Checklist", "", "custom", uid)
    rt.add_version(db, t["id"], CHECKLIST, "first", uid)
    row = {"name": "pre-pour", "kind": "", "start_at": "10:01:00", "end_at": None,
           "end_source": "recording_stop", "start_quote": "q", "template_id": t["id"], "match_score": 1.0}
    iw.replace_for_session(db, cid, "Ben", "2026-10-20", "sidA", [row])
    # Raw insert: replace_for_session replaces by (company, session), so a second
    # folder under the same session base can only be planted by hand.
    db.execute("INSERT INTO inspection_windows (company_id, user_folder, report_date, session, name, "
               "start_at, end_source) VALUES (%s, 'Someone', '2026-10-20', 'sidA', 'theirs', "
               "'10:02:00', 'recording_stop')", (cid,))
    iw.replace_for_session(db, other, "Ben", "2026-10-20", "sidA", [dict(row, name="other co", template_id=None)])
    got = iw.for_session(db, [cid], "Ben", "sidA")
    assert [(r["name"], str(r["template_id"]), str(r["report_date"])) for r in got] == [
        ("pre-pour", str(t["id"]), "2026-10-20")]
    assert len(iw.for_session(db, [cid, other], "Ben", "sidA")) == 2
    assert iw.for_session(db, [cid], "Ben", "sidB") == []


# ---- Task 11: the hop's one write (walk_answers), against Postgres -------------

import lambda_check_gaps_db as hop
import units
from repositories import walk_answers as wa

WALK_BODY = {"sections": [{"key": "c", "title": "Framing", "kind": "checklist",
                           "items": ["Wall bracing", "Nogging"]}],
             "catch_all": {"key": "o", "title": "Other", "purpose": "o"},
             "excluded_subjects": [], "style": []}


def _walk(db, folder="Ben"):
    home, site_co = _company(db), _company(db)
    sub = uuid.uuid4().hex
    uid = db.execute("INSERT INTO users (company_id, email, global_role, cognito_sub, folder_name) "
                     "VALUES (%s, %s, 'gm', %s, %s) RETURNING id",
                     (home, "%s@example.test" % sub, sub, folder)).fetchone()[0]
    t = rt.create(db, site_co, "org", None, "framing", "Framing", "", "custom", uid)
    rt.add_version(db, t["id"], WALK_BODY, "first", uid)
    key = units.walk_key(str(site_co), folder, "2026-11-04", str(t["id"]), "10:00:00")
    row = {"name": "framing", "kind": "", "start_at": "10:00:00", "end_at": "10:30:00",
           "end_source": "recording_stop", "start_quote": "q", "template_id": t["id"],
           "match_score": 1.0, "units": {"names": ["Unit 1", "Unit 2"], "count": 2},
           "entered": [{"unit": "unit 3", "at": "10:20"}], "walk_key": key}
    iw.replace_for_session(db, site_co, folder, "2026-11-04", "sidW", [row])
    return {"sub": sub, "uid": uid, "site_co": site_co, "home": home, "key": key}


def _answer(w, **kw):
    e = {"action": "write_walk_answer", "caller_sub": w["sub"], "walk_key": w["key"],
         "unit": "Unit 3", "item_text": "wall bracing", "answer": "No",
         "comment": "two straps short", "move_to_unit": None, "quote": "fail two straps short",
         "answered_at": "10:31:02"}
    e.update(kw)
    return e


def test_the_hop_writes_one_answer_under_the_walks_company(db):
    w = _walk(db)
    out = hop.write_walk_answer(db, _answer(w))
    assert out["status"] == "saved" and out["company_id"] == str(w["site_co"])
    assert (out["folder"], str(out["date"]), out["walk_key"]) == ("Ben", "2026-11-04", w["key"])
    rows = wa.for_walk(db, [w["site_co"]], w["key"])
    assert [(r["unit"], r["item_text"], r["answer"], r["source"], str(r["answered_by"]), r["quote"])
            for r in rows] == [("Unit 3", "Wall bracing", "No", "device", str(w["uid"]),
                                "fail two straps short")]
    assert wa.for_walk(db, [w["home"]], w["key"]) == []


def test_the_hop_writes_a_move_to_a_named_unit(db):
    w = _walk(db)
    assert hop.write_walk_answer(db, _answer(w, answer=None, move_to_unit="unit two"))["status"] == "saved"
    r = wa.for_walk(db, [w["site_co"]], w["key"])[0]
    assert (r["answer"], r["move_to_unit"]) == (None, "Unit 2")


def test_another_folders_walk_is_not_found_and_nothing_is_written(db):
    w = _walk(db)
    other = _walk(db, folder="Someone")
    out = hop.write_walk_answer(db, _answer(w, walk_key=other["key"]))
    assert out == {"error": "walk not found"}
    assert wa.for_walk(db, [w["site_co"], other["site_co"]], other["key"]) == []


def test_a_unit_or_item_outside_the_walk_writes_nothing(db):
    w = _walk(db)
    assert hop.write_walk_answer(db, _answer(w, unit="Unit 7"))["error"] == "unit not in walk"
    assert hop.write_walk_answer(db, _answer(w, item_text="Plumbing"))["error"] == "item not in checklist"
    assert wa.for_walk(db, [w["site_co"]], w["key"]) == []


# ---- R41: the hop's second write, the final check's questions ---------------------

from repositories import walk_final_checks as wfc  # noqa: E402


def _store(w, **kw):
    e = {"action": "store_final_check", "caller_sub": w["sub"], "walk_key": w["key"],
         "questions": [{"kind": "not_checked", "unit": "unit 3", "item_text": "wall bracing",
                        "comment": ""}]}
    e.update(kw)
    return e


def test_the_hop_stores_the_final_check_under_the_walks_company(db):
    w = _walk(db)
    out = hop.store_final_check(db, _store(w))
    assert out["status"] == "saved" and out["questions"] == 1
    row = wfc.latest(db, [w["site_co"]], w["key"])
    assert row["questions"] == [{"kind": "not_checked", "unit": "Unit 3",
                                 "item_text": "Wall bracing", "comment": ""}]
    assert row["created_at"] is not None
    assert wfc.latest(db, [w["home"]], w["key"]) is None
    got = db.execute("SELECT company_id, user_folder, report_date::text FROM walk_final_checks "
                     "WHERE walk_key = %s", (w["key"],)).fetchall()
    assert got == [(w["site_co"], "Ben", "2026-11-04")]


def test_a_refused_final_check_stores_nothing_in_postgres(db):
    w = _walk(db)
    other = _walk(db, folder="Someone")
    assert hop.store_final_check(db, _store(w, walk_key=other["key"])) == {"error": "walk not found"}
    bad = [{"kind": "not_checked", "unit": "Unit 7", "item_text": "wall bracing", "comment": ""}]
    assert hop.store_final_check(db, _store(w, questions=bad)) == {
        "error": "no question fits the walk", "skipped": 1}
    assert db.execute("SELECT count(*) FROM walk_final_checks WHERE walk_key IN (%s, %s)",
                      (w["key"], other["key"])).fetchone() == (0,)


def test_an_unfit_question_is_skipped_and_the_rest_stored_in_postgres(db):
    w = _walk(db)
    qs = [{"kind": "not_checked", "unit": "Unit 7", "item_text": "wall bracing", "comment": ""},
          {"kind": "confirm_no", "unit": "unit 1", "item_text": "wall bracing", "comment": "c"}]
    out = hop.store_final_check(db, _store(w, questions=qs))
    assert (out["questions"], out["skipped"]) == (1, 1)
    assert wfc.latest(db, [w["site_co"]], w["key"])["questions"] == [
        {"kind": "confirm_no", "unit": "Unit 1", "item_text": "Wall bracing", "comment": "c"}]
    db.execute("UPDATE walk_final_checks SET created_at = now() - interval '1 minute' "
               "WHERE walk_key = %s", (w["key"],))          # one transaction: now() ties
    assert hop.store_final_check(db, _store(w, questions=[]))["questions"] == 0
    assert wfc.latest(db, [w["site_co"]], w["key"])["questions"] == []
