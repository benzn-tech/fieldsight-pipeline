"""Multi-unit walks in storage, against Postgres (migration 0096).

A window carries the units its opening named, the units he entered, and the
walk_key that ties the day's stretches of one walk together. walk_answers holds
the answers given on site, read back only within the caller's companies.
"""
import uuid

import psycopg
import pytest

from repositories import inspection_windows as iw
from repositories import report_templates as rt
from repositories import walk_answers as wa

pytestmark = pytest.mark.integration

FOLDER, DATE = "Ben_Lin_Test", "2026-11-03"
CHECKLIST = {"sections": [{"key": "c", "title": "Pre-line", "purpose": "p", "kind": "checklist",
                           "items": ["Insulation"]}],
             "catch_all": {"key": "o", "title": "Other", "purpose": "o"},
             "excluded_subjects": [], "style": []}


def _setup(db):
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     ("Walks %s" % uuid.uuid4().hex[:6],)).fetchone()[0]
    uid = db.execute("INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, 'gm') "
                     "RETURNING id", (cid, "%s@example.test" % uuid.uuid4().hex)).fetchone()[0]
    t = rt.create(db, cid, "org", None, "pre-line", "Pre-line Inspection", "", "custom", uid)
    rt.add_version(db, t["id"], CHECKLIST, "first", uid)
    return cid, uid, t["id"]


def _window(name, start, tid, end=None, units=None, entered=None, walk_key=None):
    return {"name": name, "kind": "", "start_at": start, "end_at": end,
            "end_source": "said" if end else "recording_stop", "start_quote": "q",
            "template_id": tid, "match_score": 1.0, "units": units, "entered": entered,
            "walk_key": walk_key}


def _answer(cid, uid, walk_key, **kw):
    row = {"company_id": cid, "user_folder": FOLDER, "report_date": DATE, "walk_key": walk_key,
           "unit": "Unit 1", "item_text": "Insulation", "answer": "Yes", "comment": "",
           "move_to_unit": None, "source": "device", "quote": "insulation is fine",
           "voice_note_id": None, "answered_at": "10:00:00", "answered_by": uid}
    row.update(kw)
    return row


def test_window_units_and_walk_key_round_trip(db):
    cid, uid, tid = _setup(db)
    units = {"names": ["Unit 1", "Unit 2"], "count": 2}
    entered = [{"unit": "Unit 1", "at": "10:01:00", "at_s": 60.0, "quote": "into unit one"},
               {"unit": "Unit 2", "at": "10:20:00", "at_s": 1200.0, "quote": "unit two now"}]
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [
        _window("pre-line", "10:00:00", tid, "10:40:00", units, entered, "wk-1"),
        _window("pre-line again", "11:00:00", tid, None,
                {"names": [], "count": None}, [], None)])           # a single-unit check
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidB", [
        _window("pre-line cont", "13:00:00", tid, None, {"names": ["Unit 3"], "count": None},
                [], "wk-1")])

    day = iw.for_day(db, cid, FOLDER, DATE)
    assert [(r["name"], r["units"], r["entered"], r["walk_key"]) for r in day] == [
        ("pre-line", units, entered, "wk-1"),
        ("pre-line again", None, None, None),                       # empty is stored as NULL
        ("pre-line cont", {"names": ["Unit 3"], "count": None}, None, "wk-1")]

    sess = iw.for_session(db, [cid], FOLDER, "sidA")
    assert [(r["name"], r["units"], r["entered"], r["walk_key"]) for r in sess] == [
        ("pre-line", units, entered, "wk-1"),
        ("pre-line again", None, None, None)]

    walks = iw.open_walks(db, cid, FOLDER, DATE, tid)               # every session of the day
    assert [(w["walk_key"], w["start_at"], w["units"], w["session"]) for w in walks] == [
        ("wk-1", "10:00:00", units, "sidA"),
        ("wk-1", "13:00:00", {"names": ["Unit 3"], "count": None}, "sidB")]
    assert iw.open_walks(db, cid, FOLDER, DATE, uuid.uuid4()) == []
    assert iw.open_walks(db, cid, FOLDER, "2026-11-04", tid) == []

    # one checklist report per walk
    db.execute("INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
               "template_id, template_name, check_name, start_at, request_id, result_key, walk_key) "
               "VALUES (%s, %s, %s, 'sidA', %s, 'Pre-line', 'pre-line', '10:00:00', 'r1', 'k1', 'wk-1')",
               (cid, FOLDER, DATE, tid))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute("INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
                       "template_id, template_name, check_name, start_at, request_id, result_key, "
                       "walk_key) VALUES (%s, %s, %s, 'sidB', %s, 'Pre-line', 'pre-line', "
                       "'13:00:00', 'r2', 'k2', 'wk-1')", (cid, FOLDER, DATE, tid))


def test_walk_answers_read_only_within_the_company(db):
    cid, uid, _ = _setup(db)
    other, other_uid, _ = _setup(db)
    first = wa.add(db, _answer(cid, uid, "wk-1", answered_at="10:00:00"))
    second = wa.add(db, _answer(cid, uid, "wk-1", item_text="Wiring", answer="No",
                                comment="loose", answered_at="10:05:00", source="phone"))
    wa.add(db, _answer(other, other_uid, "wk-1", item_text="Other co"))
    wa.add(db, _answer(cid, uid, "wk-2", item_text="Other walk"))
    assert uuid.UUID(first) and uuid.UUID(second)

    rows = wa.for_walk(db, [cid], "wk-1")
    assert [(r["id"], r["item_text"], r["answer"], r["comment"], r["source"]) for r in rows] == [
        (first, "Insulation", "Yes", "", "device"),
        (second, "Wiring", "No", "loose", "phone")]
    assert str(rows[0]["company_id"]) == str(cid)
    assert {r["item_text"] for r in wa.for_walk(db, [cid, other], "wk-1")} == {
        "Insulation", "Wiring", "Other co"}
    assert wa.for_walk(db, [], "wk-1") == []
    assert wa.for_walk(db, None, "wk-1") == []


def test_a_move_row_needs_no_answer_but_needs_a_target(db):
    cid, uid, _ = _setup(db)
    moved = wa.add(db, _answer(cid, uid, "wk-1", answer=None, move_to_unit="Unit 2"))
    assert [(r["id"], r["answer"], r["move_to_unit"]) for r in wa.for_walk(db, [cid], "wk-1")] == [
        (moved, None, "Unit 2")]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            wa.add(db, _answer(cid, uid, "wk-1", answer=None, move_to_unit=None))
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            wa.add(db, _answer(cid, uid, "wk-1", answer="Maybe"))
