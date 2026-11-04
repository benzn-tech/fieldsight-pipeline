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


def test_a_walk_that_turns_back_into_a_plain_check_loses_its_walk_key(db):
    """Re-extraction finds no walk any more: the plain upsert lands on the walk
    row's (session, template, start) and must not leave it tagged as a walk."""
    cid, _, tid = _setup(db)
    key = units.walk_key(cid, FOLDER, DATE, tid, "10:00:00")
    plain = {"name": "pre-line", "template_id": tid, "start_at": "10:00:00",
             "end_at": "10:20:00", "segments": S1}
    cr._record(db, cid, FOLDER, DATE, "sidA", dict(plain, walk_key=key,
                                                  parts=[("sidA", "10:00:00")]),
               "Pre-line", "r1", "k1")
    cr._record(db, cid, FOLDER, DATE, "sidA", plain, "Pre-line", "r2", "k2")
    assert [(r[0], r[3], r[4]) for r in _reports(db, cid)] == [("sidA", "r2", None)]
    assert not cr.already_made(db, "sidA", tid, "10:00:00", "10:20:00", S1, walk_key=key)
    assert cr.already_made(db, "sidA", tid, "10:00:00", "10:20:00", S1)


def test_walk_companies_are_the_walks_windows_for_that_folder_and_day(db):
    cid, _, tid = _setup(db)
    other, _, otid = _setup(db)
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [
        _window("pre-line", "10:00:00", tid, "10:20:00", walk_key="wk-co")])
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidB", [
        _window("pre-line on", "13:00:00", tid, "13:20:00", walk_key="wk-co")])
    iw.replace_for_session(db, other, "Someone_Else", DATE, "sidC", [
        _window("pre-line", "10:00:00", otid, "10:20:00", walk_key="wk-other")])
    assert iw.walk_companies(db, FOLDER, DATE, "wk-co") == [str(cid)]
    assert iw.walk_companies(db, FOLDER, "2026-11-04", "wk-co") == []
    assert iw.walk_companies(db, FOLDER, DATE, "wk-other") == []          # not this folder
    assert iw.walk_companies(db, "Someone_Else", DATE, "wk-other") == [str(other)]



# ---- Task 12: the phone settles a walk (org-api routes over real SQL) --------------

import io  # noqa: E402

import lambda_org_api as org  # noqa: E402


class _NoCommit:
    """The request connection, minus commit: the test's work is rolled back after it."""
    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass


def test_the_walks_report_row_is_read_in_its_folders_day_only(db):
    cid, _, tid = _setup(db)
    key = units.walk_key(cid, FOLDER, DATE, tid, "10:00:00")
    walk = {"name": "pre-line", "template_id": tid, "start_at": "10:00:00", "end_at": "10:20:00",
            "segments": S1, "walk_key": key, "parts": [("sidA", "10:00:00")]}
    cr._record(db, cid, FOLDER, DATE, "sidA", walk, "Pre-line", "r1", "k1")
    row = cr.for_walk(db, FOLDER, DATE, key)
    assert (row["result_key"], row["walk_key"], row["session"]) == ("k1", key, "sidA")
    assert row["created_at"] is not None
    assert cr.for_walk(db, "Someone_Else", DATE, key) is None
    assert cr.for_walk(db, FOLDER, "2026-11-04", key) is None


def test_a_phone_answer_settles_its_item_on_the_open_list(db, monkeypatch):
    cid, _, tid = _setup(db)
    folder = "walk_%s" % uuid.uuid4().hex[:8]
    uid = db.execute("INSERT INTO users (company_id, email, global_role, folder_name, cognito_sub) "
                     "VALUES (%s, %s, 'worker', %s, %s) RETURNING id",
                     (cid, "%s@example.test" % uuid.uuid4().hex, folder,
                      "sub-%s" % folder)).fetchone()[0]
    key = units.walk_key(cid, folder, DATE, tid, "10:00:00")
    iw.replace_for_session(db, cid, folder, DATE, "sidA", [
        _window("pre-line units 1 and 2", "10:00:00", tid, "10:20:00",
                {"names": ["Unit 1", "Unit 2"], "count": 2}, [], key)])
    result_key = "session_report_results/%s/%s/day/r1.json" % (folder, DATE)
    cr._record(db, cid, folder, DATE, "sidA",
               {"name": "pre-line", "template_id": tid, "start_at": "10:00:00",
                "end_at": "10:20:00", "segments": S1, "walk_key": key,
                "parts": [("sidA", "10:00:00")]}, "Pre-line", "r1", result_key)
    # The report was queued a minute ago (one transaction here: now() would tie).
    db.execute("UPDATE checklist_reports SET created_at = now() - interval '1 minute' "
               "WHERE walk_key = %s", (key,))
    stored = {"status": "done", "walk": {
        "key": key, "conflicts": [], "unmatched_answers": [], "summary": None,
        "units": [{"unit": u, "report": {"gaps": {"not_covered": [{"n": 1, "text": "Insulation"}],
                                                  "failed": []}}}
                  for u in ("Unit 1", "Unit 2")]}}

    class S3:
        def get_object(self, Bucket, Key):
            assert Key == result_key
            return {"Body": io.BytesIO(json.dumps(stored).encode())}

    requeued = []
    monkeypatch.setattr(org, "_resolve_org_media_folder", lambda conn, c, u, what="media": (u, None))
    monkeypatch.setattr(org, "_own_rows_visible", lambda conn, c, f, d, rows, field="session": rows)
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, c: {"self_folder": folder})
    monkeypatch.setattr(org, "s3", lambda: S3())
    monkeypatch.setattr(org.checklist_reports, "requeue_walk",
                        lambda conn, co, f, d, k: requeued.append((co, f, d, k)) or "b" * 32)
    caller = {"id": str(uid), "company_id": str(cid), "global_role": "worker",
              "folder_name": folder}
    q = {"queryStringParameters": {"user": folder}}
    conn = _NoCommit(db)

    res = org.get_walk_open_items(conn, caller, DATE, key, q)
    assert res["statusCode"] == 200
    assert [len(u["items"]) for u in json.loads(res["body"])["units"]] == [1, 1]

    res = org.post_walk_answer(conn, caller, DATE, key, dict(q, body=json.dumps(
        {"unit": "unit 2", "item_text": "insulation", "answer": "N/A"})))
    assert res["statusCode"] == 201, res["body"]
    saved = db.execute("SELECT company_id, unit, item_text, answer, source, answered_by "
                       "FROM walk_answers WHERE walk_key = %s", (key,)).fetchall()
    assert saved == [(cid, "Unit 2", "Insulation", "N/A", "phone", uid)]
    assert requeued == [(str(cid), folder, DATE, key)]

    res = org.get_walk_open_items(conn, caller, DATE, key, q)
    assert json.loads(res["body"])["units"] == [
        {"unit": "Unit 1", "items": [{"item_text": "Insulation", "kind": "not_checked",
                                      "comment": ""}]},
        {"unit": "Unit 2", "items": []}]

    # Another folder's day never reaches this walk.
    res = org.get_walk_open_items(conn, caller, DATE, key, {"queryStringParameters": {"user": "X"}})
    assert res["statusCode"] == 404


def test_an_answer_keeps_the_kind_of_question_it_answers(db):
    """R36: a read-back answer (confirm_no) is the inspector's choice; the
    column says which question an answer was given to."""
    cid, uid, _ = _setup(db)
    wa.add(db, _answer(cid, uid, "wk-kind"))
    wa.add(db, _answer(cid, uid, "wk-kind", answer="No", asked_kind="confirm_no",
                       answered_at="10:05:00"))      # one transaction: now() ties
    assert [r["asked_kind"] for r in wa.for_walk(db, [cid], "wk-kind")] == [
        "not_checked", "confirm_no"]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            wa.add(db, _answer(cid, uid, "wk-kind", asked_kind="general"))


def test_a_walk_requeue_prepends_the_result_it_replaces(db):
    """R39: re-queueing a walk replaces its result_key; the old key and when it
    was queued are prepended to previous_results, newest first, at most 5."""
    from datetime import datetime
    cid, _, tid = _setup(db)
    key = units.walk_key(cid, FOLDER, DATE, tid, "10:00:00")
    walk = {"name": "pre-line", "template_id": tid, "start_at": "10:00:00", "end_at": "10:20:00",
            "segments": S1, "walk_key": key, "parts": [("sidA", "10:00:00")]}
    cr._record(db, cid, FOLDER, DATE, "sidA", walk, "Pre-line", "r1", "k1")
    assert cr.for_walk(db, FOLDER, DATE, key)["previous_results"] in (None, [])
    db.execute("UPDATE checklist_reports SET created_at = now() - interval '5 minutes' "
               "WHERE walk_key = %s", (key,))
    queued_1 = cr.for_walk(db, FOLDER, DATE, key)["created_at"]
    cr._record(db, cid, FOLDER, DATE, "sidA", walk, "Pre-line", "r2", "k2")
    row = cr.for_walk(db, FOLDER, DATE, key)
    assert row["result_key"] == "k2"
    assert [p["key"] for p in row["previous_results"]] == ["k1"]
    assert datetime.fromisoformat(row["previous_results"][0]["queued_at"]) == queued_1
    for n in range(3, 9):
        cr._record(db, cid, FOLDER, DATE, "sidA", walk, "Pre-line", "r%d" % n, "k%d" % n)
    row = cr.for_walk(db, FOLDER, DATE, key)
    assert row["result_key"] == "k8"
    assert [p["key"] for p in row["previous_results"]] == ["k7", "k6", "k5", "k4", "k3"]
    # an ordinary check's row carries no previous walk results
    plain = {"name": "pre-line", "template_id": tid, "start_at": "10:00:00",
             "end_at": "10:20:00", "segments": S1}
    cr._record(db, cid, FOLDER, DATE, "sidA", plain, "Pre-line", "r9", "k9")
    assert db.execute("SELECT previous_results FROM checklist_reports "
                      "WHERE company_id = %s", (cid,)).fetchall() == [(None,)]


# ---- R41: walk_final_checks -------------------------------------------------------

from repositories import walk_final_checks as wfc  # noqa: E402


def test_the_latest_final_check_is_read_within_the_walks_company(db):
    cid, _, _ = _setup(db)
    other, _, _ = _setup(db)
    row = {"company_id": cid, "user_folder": FOLDER, "report_date": DATE, "walk_key": "wk-fc",
           "questions": [{"kind": "confirm_no", "unit": "Unit 1", "item_text": "Insulation",
                          "comment": "gaps"}]}
    first = wfc.add(db, row)
    db.execute("UPDATE walk_final_checks SET created_at = now() - interval '2 minutes' "
               "WHERE id = %s", (first,))
    second = wfc.add(db, dict(row, questions=[]))
    got = wfc.latest(db, [cid], "wk-fc")
    assert (got["id"], got["questions"]) == (second, [])
    assert wfc.latest(db, [other], "wk-fc") is None
    assert wfc.latest(db, [], "wk-fc") is None                  # no company reads nothing
    assert db.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'walk_final_checks' "
                      "AND indexdef LIKE '%%(walk_key, created_at)%%'").fetchone() is not None


def test_final_checks_are_not_in_the_company_records_export():
    import company_records_export as cre
    assert "walk_final_checks" in dict(cre.NOT_EXPORTED)
