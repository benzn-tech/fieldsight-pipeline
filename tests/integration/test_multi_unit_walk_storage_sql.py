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


# ---- Task 9: one checklist report per walk -------------------------------------------

import json  # noqa: E402

import checklist_reports as cr  # noqa: E402
import units  # noqa: E402

S1 = [{"from": "10:00:00", "to": "10:20:00"}]
S2 = [{"from": "13:00:00", "to": "13:15:00"}]


def _reports(db, cid):
    return db.execute("SELECT session, start_at, segments, request_id, walk_key "
                      "FROM checklist_reports WHERE company_id = %s ORDER BY start_at",
                      (cid,)).fetchall()


def test_a_walk_row_takes_over_a_plain_report_of_the_same_check_without_aborting(db):
    """Pass 1 saw an ordinary check; pass 2 sees the walk. The walk row lands
    on checklist_reports_once's triple -- the plain row is its part and goes."""
    cid, uid, tid = _setup(db)
    key = units.walk_key(cid, FOLDER, DATE, tid, "10:00:00")
    plain = {"name": "pre-line", "template_id": tid, "start_at": "10:00:00",
             "end_at": "10:20:00", "segments": S1}
    later = dict(plain, start_at="13:00:00", end_at="13:15:00", segments=S2)
    cr._record(db, cid, FOLDER, DATE, "sidA", plain, "Pre-line", "r0", "k0")
    cr._record(db, cid, FOLDER, DATE, "sidB", later, "Pre-line", "rB", "kB")  # R13 claims it later
    assert not cr.already_made(db, "sidA", tid, "10:00:00", "10:20:00", S1, walk_key=key)

    walk = dict(plain, walk_key=key, parts=[("sidA", "10:00:00")])
    cr._record(db, cid, FOLDER, DATE, "sidA", walk, "Pre-line", "r1", "k1")
    assert [(r[0], r[1], r[3], r[4]) for r in _reports(db, cid)] == [
        ("sidA", "10:00:00", "r1", key), ("sidB", "13:00:00", "rB", None)]
    assert cr.already_made(db, "sidA", tid, "10:00:00", "10:20:00", S1, walk_key=key)
    assert not cr.already_made(db, "sidB", tid, "10:00:00", "13:15:00", S1 + S2, walk_key=key)

    # the second recording: the same row, now under sidB with both stretches
    both = dict(walk, end_at="13:15:00", segments=S1 + S2,
                parts=[("sidA", "10:00:00"), ("sidB", "13:00:00")])
    cr._record(db, cid, FOLDER, DATE, "sidB", both, "Pre-line", "r2", "k2")
    rows = _reports(db, cid)
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        ("sidB", "10:00:00", S1 + S2, "r2", key)]
    assert cr.already_made(db, "sidA", tid, "10:00:00", "13:15:00", S1 + S2, walk_key=key)
    assert db.execute("SELECT 1").fetchone() == (1,)            # the transaction is alive


def test_auto_generate_queues_one_report_for_a_walk_over_two_recordings(db):
    cid, _, tid = _setup(db)
    folder = "walk_%s" % uuid.uuid4().hex[:8]
    db.execute("INSERT INTO users (company_id, email, global_role, folder_name, cognito_sub) "
               "VALUES (%s, %s, 'gm', %s, %s)",
               (cid, "%s@example.test" % uuid.uuid4().hex, folder, "sub-%s" % folder))
    key = units.walk_key(cid, folder, DATE, tid, "10:00:00")
    opening = {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3}
    a = dict(_window("pre-line units 1 to 3", "10:00:00", tid, "10:20:00", opening,
                     [{"unit": "Unit 1", "at": "10:01", "at_s": "10:01:00"},
                      {"unit": "Unit 2", "at": "10:10", "at_s": "10:10:00"}], key))
    b = dict(_window("pre-line carry on", "13:00:00", tid, "13:15:00", None,
                     [{"unit": "Unit 3", "at": "13:01", "at_s": "13:01:00"}], key))
    iw.replace_for_session(db, cid, folder, DATE, "sidA", [a])
    iw.replace_for_session(db, cid, folder, DATE, "sidB", [b])
    calls = []

    def generate(conn, caller, date, event):
        calls.append(json.loads(event["body"]))
        return {"statusCode": 202, "body": json.dumps({"requestId": "r%d" % len(calls)})}

    out = cr.auto_generate(db, cid, folder, DATE, "sidB", [b], generate=generate)
    assert out == [("pre-line units 1 to 3", "r1")]
    body = calls[0]
    assert body["segments"] == S1 + S2 and (body["from"], body["to"]) == ("10:00:00", "13:15:00")
    assert body["walk"]["key"] == key and body["walk"]["units"] == opening
    assert [s["unit"] for s in body["walk"]["stretches"]] == [None, "Unit 1", "Unit 2", None, "Unit 3"]
    assert [(r[0], r[1], r[2], r[4]) for r in _reports(db, cid)] == [
        ("sidB", "10:00:00", S1 + S2, key)]
    # re-extracting the first recording: made already
    assert cr.auto_generate(db, cid, folder, DATE, "sidA", [a], generate=generate) == [
        ("pre-line units 1 to 3", "already made")]
    # an on-site answer: re-queued anyway
    assert cr.requeue_walk(db, cid, folder, DATE, key, generate=generate) == "r2"
    assert calls[1] == calls[0]
    assert [(r[0], r[3]) for r in _reports(db, cid)] == [("sidA", "r2")]   # the opening's recording
