"""Task 12: the phone settles a multi-unit walk on site.

GET  /api/org/days/{date}/inspections/walks/{walkKey}/open?user=<folder>
     -- what is still open, per unit, from the latest walk report, with the
        answers given since it was queued laid over it;
POST /api/org/days/{date}/inspections/walks/{walkKey}/answers?user=<folder>
     -- one answer (or a move), stored as walk_answers (source 'phone', by the
        caller, in the walk's company), then the walk report queued again.

Spec: docs/superpowers/specs/2026-11-03-multi-unit-inspection-design.md
Plan: docs/superpowers/plans/2026-11-03-multi-unit-inspection.md, Task 12
"""
import contextlib
import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

import check_done
import units

org = pytest.importorskip("lambda_org_api")

FOLDER, DATE = "Ben", "2026-11-04"
SITE_CO, HOME_CO = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
TID = "33333333-3333-3333-3333-333333333333"
KEY = units.walk_key(SITE_CO, FOLDER, DATE, TID, "10:00:00")
ITEMS = ["Insulation", "Building wrap", "Window flashings"]
TEMPLATE = {"sections": [{"key": "c", "title": "Pre-line", "purpose": "p", "kind": "checklist",
                          "items": ITEMS}]}
QUEUED = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
RESULT_KEY = "session_report_results/%s/%s/day/r1.json" % (FOLDER, DATE)
CALLER = {"id": "44444444-4444-4444-4444-444444444444", "company_id": HOME_CO,
          "global_role": "worker", "folder_name": FOLDER}


def _unit(name, not_covered=(), failed=(), conflicts=()):
    return {"unit": name, "report": {
        "items": len(ITEMS), "answered": 0, "dropped": [], "evidence": {},
        "gaps": {"not_covered": [{"n": ITEMS.index(t) + 1, "text": t} for t in not_covered],
                 "failed": [{"n": ITEMS.index(t) + 1, "text": t, "comment": c,
                             "responsible": "", "due": ""} for t, c in failed]},
        "conflicts": list(conflicts)}}


def _result(units_, conflicts=()):
    return {"status": "done", "requestId": "r1", "docKey": "x.docx",
            "walk": {"key": KEY, "units": units_, "summary": None,
                     "conflicts": list(conflicts), "unmatched_answers": []}}


def _answer(unit, item, answer="Yes", move=None, at=QUEUED + timedelta(minutes=1)):
    return {"id": "a", "unit": unit, "item_text": item, "answer": answer, "move_to_unit": move,
            "created_at": at, "answered_at": "10:30:00"}


class Conn:
    def __init__(self, log):
        self.log = log

    @contextlib.contextmanager
    def transaction(self):
        yield

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    class info:                       # psycopg's IDLE
        transaction_status = 0


class S3:
    def __init__(self, objects):
        self.objects, self.reads = objects, []

    def get_object(self, Bucket, Key):
        self.reads.append(Key)
        if Key not in self.objects:
            raise org.ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


@pytest.fixture
def world(monkeypatch):
    """One walk of Ben's on 2026-11-04 on SITE_CO's site, opened for Unit 1 and
    Unit 2 and entering Unit 3; a report queued at QUEUED."""
    w = {"log": [], "what": [], "added": [], "requeued": [], "answers": [],
         "report": {"session": "sidA", "result_key": RESULT_KEY, "created_at": QUEUED,
                    "walk_key": KEY},
         "s3": S3({RESULT_KEY: _result([_unit("Unit 1"), _unit("Unit 2"), _unit("Unit 3")])}),
         "visible": True, "self_folder": FOLDER, "versions": [], "bodies": {}, "final": None}

    def resolve(conn, caller, user, what="media"):
        w["what"].append(what)
        return (user or FOLDER), None

    def for_day(conn, company, folder, date):
        w["log"].append(("for_day", company, folder, date))
        return ([{"session": "sidA", "walk_key": KEY, "template_id": TID, "start_at": "10:00:00"}]
                if w["visible"] and (folder, date) == (FOLDER, DATE) else [])

    rows = [{"company_id": SITE_CO, "report_date": DATE, "session": "sidA", "template_id": TID,
             "start_at": "10:00:00", "units": {"names": ["Unit 1", "Unit 2"], "count": 2},
             "entered": [{"unit": "Unit 3", "at": "10:40:00"}]}]
    monkeypatch.setattr(org, "_resolve_org_media_folder", resolve)
    monkeypatch.setattr(org, "_own_rows_visible", lambda conn, caller, f, d, rows, field="session": rows)
    monkeypatch.setattr(org.inspection_windows, "for_day", for_day)
    monkeypatch.setattr(org.inspection_windows, "walk_companies",
                        lambda conn, f, d, k: [SITE_CO] if (f, d, k) == (FOLDER, DATE, KEY) else [])
    monkeypatch.setattr(org.inspection_windows, "walk_rows",
                        lambda conn, f, k: rows if (f, k) == (FOLDER, KEY) else [])
    def get_version(conn, tid, v):
        w["versions"].append(v)
        return {"body": w["bodies"].get(v, TEMPLATE)} if str(tid) == TID else None
    monkeypatch.setattr(org.report_templates, "get_version", get_version)
    monkeypatch.setattr(org.checklist_reports, "for_walk",
                        lambda conn, f, d, k: w["report"] if (f, d, k) == (FOLDER, DATE, KEY) else None)
    monkeypatch.setattr(org.walk_final_checks, "latest",
                        lambda conn, cos, k: w["final"] if (list(cos), k) == ([SITE_CO], KEY) else None)
    monkeypatch.setattr(org.walk_answers, "for_walk",
                        lambda conn, cos, k: list(w["answers"]) if list(cos) == [SITE_CO] else [])

    def add(conn, row):
        w["log"].append("add")
        w["added"].append(row)
        return "new-id"
    monkeypatch.setattr(org.walk_answers, "add", add)

    def requeue(conn, company_id, folder, date, key):
        w["log"].append("requeue")
        w["requeued"].append((company_id, folder, date, key))
        if w.get("requeue_raises"):
            raise RuntimeError("boom")
        return "a" * 32
    monkeypatch.setattr(org.checklist_reports, "requeue_walk", requeue)
    monkeypatch.setattr(org, "s3", lambda: w["s3"])
    monkeypatch.setattr(org.scope, "visible_scope",
                        lambda conn, caller: {"self_folder": w["self_folder"]})
    w["conn"] = Conn(w["log"])
    return w


def _get(w, date=DATE, key=KEY, user=FOLDER, caller=CALLER):
    res = org.get_walk_open_items(w["conn"], caller, date, key,
                                  {"queryStringParameters": {"user": user}})
    return res["statusCode"], json.loads(res["body"])


def _post(w, body, date=DATE, key=KEY, user=FOLDER, caller=CALLER):
    res = org.post_walk_answer(w["conn"], caller, date, key,
                               {"queryStringParameters": {"user": user}, "body": json.dumps(body)})
    return res["statusCode"], json.loads(res["body"])


# ---- the routes ------------------------------------------------------------------

def test_both_routes_are_dispatched_date_scoped_next_to_the_inspections_route():
    src = open(org.__file__, encoding="utf-8").read()
    assert 're.match(r"^/days/([^/]+)/inspections/walks/([^/]+)/open$", route)' in src
    assert 're.match(r"^/days/([^/]+)/inspections/walks/([^/]+)/answers$", route)' in src


# ---- access ----------------------------------------------------------------------

def test_open_items_need_the_same_access_as_the_inspections_route(world, monkeypatch):
    # The read check is the inspections route's own: _resolve_org_media_folder, "inspections".
    assert _get(world)[0] == 200
    assert _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"})[0] == 201
    assert world["what"] == ["inspections", "inspections"]

    # Another company's caller: refused by that check, exactly as the inspections route.
    refused = org.error("not permitted to view this user's inspections", 403)
    monkeypatch.setattr(org, "_resolve_org_media_folder", lambda conn, c, u, what="media": (None, refused))
    for status, body in (_get(world), _post(world, {"unit": "Unit 1", "item_text": "Insulation",
                                                    "answer": "Yes"})):
        assert (status, body) == (403, {"error": "not permitted to view this user's inspections"})
    world["s3"].reads.clear()
    world["added"].clear()

    # The folder may be read, but none of the walk's windows is visible to this caller
    # (the inspections route's company pin / inactive-site filter): 404, nothing read.
    monkeypatch.setattr(org, "_resolve_org_media_folder", lambda conn, c, u, what="media": (u, None))
    world["visible"] = False
    assert _get(world) == (404, {"error": "walk not found"})
    assert _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"}) == (
        404, {"error": "walk not found"})
    assert world["s3"].reads == [] and world["added"] == []

    # Not this folder's walk on this day (another day, a forged key): 404.
    world["visible"] = True
    assert _get(world, date="2026-11-05")[0] == 404
    assert _get(world, key="walk:" + "f" * 40)[0] == 404
    assert _get(world, key="../etc")[0] == 404
    assert _get(world, date="04-11-2026")[0] == 400
    assert world["s3"].reads == [] and world["added"] == []


def test_the_company_pin_is_the_inspections_routes(world, monkeypatch):
    other = dict(CALLER, folder_name="Someone", global_role="admin")
    _get(world, caller=other)
    assert ("for_day", HOME_CO, FOLDER, DATE) in world["log"]      # pinned, like _read_company
    world["log"].clear()
    _get(world)
    assert ("for_day", None, FOLDER, DATE) in world["log"]         # own folder: unrestricted


def test_only_the_walker_may_answer(world):
    """R38: the answers are rendered as on-site evidence, so only the person
    who walked it gives them -- no admin, gm or platform_admin path."""
    world["self_folder"] = "Someone_Else"         # anyone who may read Ben's day
    for role in ("pm", "site_manager", "admin", "gm", "platform_admin"):
        other = dict(CALLER, global_role=role, folder_name="Someone_Else")
        assert _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"},
                     caller=other) == (403, {"error": "only the person who did the walk can "
                                                      "answer for it"}), role
    assert world["added"] == [] and world["requeued"] == []
    world["self_folder"] = FOLDER
    assert _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"})[0] == 201


# ---- POST ------------------------------------------------------------------------

def test_an_answer_for_a_unit_outside_the_walk_is_refused(world):
    for body, msg in (
            ({"unit": "Unit 9", "item_text": "Insulation", "answer": "Yes"}, "unit not in walk"),
            ({"unit": "Kitchen", "item_text": "Insulation", "answer": "Yes"}, "unit not in walk"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": None,
              "move_to_unit": "Unit 9"}, "move_to_unit not in walk"),
            ({"unit": "Unit 1", "item_text": "Roof", "answer": "Yes"}, "item not in checklist"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": "Maybe"}, "answer invalid"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": None},
             "answer or move_to_unit required"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": "No",
              "move_to_unit": "Unit 2"}, "answer with a move_to_unit"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes",
              "voice_note_id": "not-a-uuid"}, "voice_note_id invalid"),
            ({"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes",
              "asked_kind": "general"}, "asked_kind invalid"),
            ({"unit": 3, "item_text": "Insulation", "answer": "Yes"}, "unit invalid")):
        assert _post(world, body) == (400, {"error": msg}), body
    assert world["added"] == [] and world["requeued"] == []


def test_a_phone_answer_is_stored_with_the_caller(world, monkeypatch):
    monkeypatch.setattr(org.nz_time, "nz_now",
                        lambda: datetime(2026, 11, 4, 10, 31, 7))
    status, body = _post(world, {"unit": "unit one", "item_text": "  insulation ", "answer": "No",
                                 "voice_note_id": "55555555-5555-5555-5555-555555555555",
                                 # never taken from the body:
                                 "company_id": HOME_CO, "user_folder": "X", "answered_by": "Y",
                                 "source": "device", "walk_key": "walk:" + "0" * 40})
    assert (status, body) == (201, {"status": "saved", "id": "new-id", "walkKey": KEY,
                                    "requeued": True})
    assert world["added"] == [{
        "company_id": SITE_CO, "user_folder": FOLDER, "report_date": DATE, "walk_key": KEY,
        "unit": "Unit 1", "item_text": "Insulation", "answer": "No", "comment": "",
        "move_to_unit": None, "source": "phone", "quote": "",
        "voice_note_id": "55555555-5555-5555-5555-555555555555",
        "answered_at": "10:31:07", "answered_by": CALLER["id"], "asked_kind": "not_checked"}]
    # The answer is durable before the report is queued again, in the walk's company.
    assert world["log"][-3:] == ["add", "commit", "requeue"]
    assert world["requeued"] == [(SITE_CO, FOLDER, DATE, KEY)]


def test_the_question_it_answers_is_stored(world):
    """R36: an answer to a read-back No is the inspector's choice."""
    assert _post(world, {"unit": "Unit 1", "item_text": "Building wrap", "answer": "Yes",
                         "asked_kind": "confirm_no"})[0] == 201
    assert world["added"][0]["asked_kind"] == "confirm_no"


def test_a_move_names_the_walks_unit(world):
    status, _ = _post(world, {"unit": "Unit 1", "item_text": "Building wrap", "answer": None,
                              "move_to_unit": "unit 3"})
    assert status == 201
    assert (world["added"][0]["answer"], world["added"][0]["move_to_unit"]) == (None, "Unit 3")


def test_a_requeue_failure_still_keeps_the_answer(world, caplog):
    world["requeue_raises"] = True
    with caplog.at_level(logging.INFO):
        status, body = _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"})
    assert (status, body["id"], body["requeued"]) == (201, "new-id", False)
    assert world["log"].index("commit") < world["log"].index("requeue")
    # counts only, never what was answered
    assert "Insulation" not in caplog.text
    # one traceback, not two, and no second line about the same failure
    assert sum(1 for r in caplog.records if r.exc_info) == 1
    assert sum(1 for r in caplog.records if r.levelno >= logging.WARNING) == 1


def test_a_requeue_that_says_why_logs_no_traceback(world, caplog, monkeypatch):
    monkeypatch.setattr(org.checklist_reports, "requeue_walk",
                        lambda conn, co, f, d, k: "template gone")
    with caplog.at_level(logging.INFO):
        status, body = _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"})
    assert (status, body["requeued"]) == (201, False)
    assert "template gone" in caplog.text
    assert not any(r.exc_info for r in caplog.records)


def test_an_ambiguous_walk_company_is_refused(world, monkeypatch):
    rows = [{"company_id": SITE_CO, "report_date": DATE, "session": "s", "template_id": TID,
             "start_at": "09:00:00", "units": None, "entered": [{"unit": "Unit 1"}]},
            {"company_id": HOME_CO, "report_date": DATE, "session": "t", "template_id": TID,
             "start_at": "09:30:00", "units": None, "entered": []}]
    monkeypatch.setattr(org.inspection_windows, "walk_rows", lambda conn, f, k: rows)
    assert _post(world, {"unit": "Unit 1", "item_text": "Insulation", "answer": "Yes"}) == (
        409, {"error": "walk company unclear"})
    assert world["added"] == []


# ---- GET -------------------------------------------------------------------------

def test_before_any_report_it_is_not_ready(world):
    world["report"] = None
    assert _get(world) == (404, {"error": "not ready"})
    world["report"] = {"session": "sidA", "result_key": RESULT_KEY.replace("r1", "r9"),
                       "created_at": QUEUED, "walk_key": KEY}
    assert _get(world) == (404, {"error": "not ready", "status": "pending"})


def _key(n):
    return RESULT_KEY.replace("r1", n)


def _iso(t):
    return t.isoformat()                       # as jsonb_build_object writes a timestamptz


def test_two_quick_answers_still_read_the_last_finished_report(world):
    """R39: each requeue prepends the result it replaces, written or not. Two
    answers before the first new result lands: the current result and the
    newest previous one are unwritten, and the list comes from the last
    finished one, with every answer since IT was queued laid over it."""
    t0 = QUEUED - timedelta(minutes=10)
    world["s3"].objects = {_key("r0"): _result(
        [_unit("Unit 1", not_covered=["Insulation", "Window flashings"]), _unit("Unit 2")])}
    world["report"] = {"session": "sidA", "result_key": _key("r9"), "created_at": QUEUED,
                       "walk_key": KEY,
                       "previous_results": [{"key": _key("r8"), "queued_at": _iso(t0 + timedelta(minutes=1))},
                                            {"key": _key("r0"), "queued_at": _iso(t0)}]}
    world["answers"] = [_answer("Unit 1", "Insulation", "Yes", at=t0 + timedelta(seconds=30)),
                        _answer("Unit 2", "Insulation", "No", at=t0 + timedelta(minutes=2))]
    status, body = _get(world)
    assert status == 200
    assert body["units"] == [
        {"unit": "Unit 1", "items": [
            {"item_text": "Window flashings", "kind": "not_checked", "comment": ""}]},
        {"unit": "Unit 2", "items": []}]
    assert world["s3"].reads == [_key("r9"), _key("r8"), _key("r0")]


def test_a_failed_newest_report_falls_back_to_the_previous_finished_one(world):
    world["s3"].objects = {_key("r9"): {"status": "error", "error": "x"},
                           _key("r0"): _result([_unit("Unit 1", not_covered=["Insulation"])])}
    world["report"] = {"session": "sidA", "result_key": _key("r9"), "created_at": QUEUED,
                       "walk_key": KEY,
                       "previous_results": [{"key": _key("r0"),
                                             "queued_at": _iso(QUEUED - timedelta(minutes=5))}]}
    status, body = _get(world)
    assert status == 200
    assert body["units"][0]["items"] == [
        {"item_text": "Insulation", "kind": "not_checked", "comment": ""}]


def test_none_finished_is_not_ready_with_the_newest_status(world):
    world["s3"].objects = {_key("r8"): {"status": "error", "error": "x"}}
    world["report"] = {"session": "sidA", "result_key": _key("r9"), "created_at": QUEUED,
                       "walk_key": KEY,
                       "previous_results": [{"key": _key("r8"), "queued_at": _iso(QUEUED)}]}
    assert _get(world) == (404, {"error": "not ready", "status": "pending"})
    world["report"]["previous_results"] = None
    assert _get(world) == (404, {"error": "not ready", "status": "pending"})


def test_at_most_five_previous_results_are_read_and_never_outside_the_day(world):
    old = [{"key": _key("p%d" % i), "queued_at": _iso(QUEUED - timedelta(minutes=i))}
           for i in range(1, 7)]
    old[1]["key"] = "session_report_results/Other/%s/day/p2.json" % DATE
    world["s3"].objects = {_key("p6"): _result([_unit("Unit 1")])}     # only the 6th is done
    world["report"] = {"session": "sidA", "result_key": _key("r9"), "created_at": QUEUED,
                       "walk_key": KEY, "previous_results": old}
    assert _get(world)[0] == 404
    assert world["s3"].reads == [_key("r9"), _key("p1"), _key("p3"), _key("p4"), _key("p5")]


def test_an_item_of_the_reports_template_version_may_be_answered(world):
    """The phone shows the items of the version the stored report used; the
    POST must take them even when the current version dropped one."""
    world["bodies"][1] = {"sections": [{"key": "c", "title": "Pre-line", "kind": "checklist",
                                        "items": ["Insulation", "Old item"]}]}
    res = _result([_unit("Unit 1")])
    res["templateVersion"] = 1
    world["s3"].objects[RESULT_KEY] = res
    status, body = _post(world, {"unit": "Unit 1", "item_text": "old ITEM", "answer": "Yes"})
    assert status == 201, body
    assert world["added"][0]["item_text"] == "Old item"
    assert _post(world, {"unit": "Unit 1", "item_text": "Roof", "answer": "Yes"}) == (
        400, {"error": "item not in checklist"})


def test_the_items_are_the_template_version_the_report_used(world):
    v1 = {"sections": [{"key": "c", "title": "Pre-line", "kind": "checklist",
                        "items": ["Insulation"]}]}
    world["bodies"][1] = v1
    res = _result([_unit("Unit 1")])
    res["templateVersion"] = 1
    world["s3"].objects[RESULT_KEY] = res
    assert _get(world)[0] == 200
    assert world["versions"] == [1]
    # no version recorded: the current one
    world["versions"].clear()
    world["s3"].objects[RESULT_KEY] = _result([_unit("Unit 1")])
    _get(world)
    assert world["versions"] == [None]


def test_a_result_key_outside_the_folders_day_is_never_read(world):
    world["report"] = dict(world["report"], result_key="session_report_results/Other/%s/day/r1.json" % DATE)
    assert _get(world)[0] == 404
    assert world["s3"].reads == []


def test_open_items_by_unit_from_the_latest_walk_report(world):
    world["s3"].objects[RESULT_KEY] = _result(
        [_unit("Unit 1", not_covered=["Window flashings", "Insulation"],
               failed=[("Building wrap", "torn at the corner")]),
         _unit("Unit 2", failed=[("Insulation", "gaps (on site, 10:20)")]),
         _unit("Unit 3", conflicts=[{"unit": "Unit 3", "item": "Building wrap",
                                     "on_site": "Yes", "recording": "No"}])],
        conflicts=[{"unit": "Unit 3", "item": "Building wrap", "on_site": "Yes", "recording": "No"}])
    # Unit 2's No was decided on site before the report was queued.
    world["answers"] = [_answer("Unit 2", "Insulation", "No", at=QUEUED - timedelta(minutes=5)),
                        _answer("Unit 3", "Building wrap", "Yes", at=QUEUED - timedelta(minutes=4))]
    status, body = _get(world)
    assert status == 200
    assert body == {"walkKey": KEY, "source": "report", "units": [
        {"unit": "Unit 1", "items": [
            {"item_text": "Insulation", "kind": "not_checked", "comment": ""},
            {"item_text": "Window flashings", "kind": "not_checked", "comment": ""},
            {"item_text": "Building wrap", "kind": "confirm_no", "comment": "torn at the corner"}]},
        {"unit": "Unit 2", "items": []},
        {"unit": "Unit 3", "items": [
            {"item_text": "Building wrap", "kind": "confirm_no", "comment": ""}]}]}


def test_r51_a_unit_seen_only_in_the_reports_rows_is_not_listed(world):
    """The phone asks the units the device asks (R51): the walk's units --
    opened with (Units 1, 2) or entered (Unit 3). A unit the report has only
    from the recording's rows (Unit 9) is not listed: its answer is refused."""
    world["s3"].objects[RESULT_KEY] = _result(
        [_unit("Unit 1"), _unit("Unit 2"), _unit("Unit 3", not_covered=["Insulation"]),
         _unit("Unit 9", not_covered=["Insulation", "Building wrap"])])
    status, body = _get(world)
    assert status == 200
    assert [u["unit"] for u in body["units"]] == ["Unit 1", "Unit 2", "Unit 3"]
    assert body["units"][2]["items"] == [
        {"item_text": "Insulation", "kind": "not_checked", "comment": ""}]
    status, _ = _post(world, {"unit": "Unit 9", "item_text": "Insulation", "answer": "Yes"})
    assert status == 400                                    # the accept-set agrees


def test_open_items_drop_what_was_answered(world):
    world["s3"].objects[RESULT_KEY] = _result(
        [_unit("Unit 1", not_covered=["Insulation", "Window flashings"],
               failed=[("Building wrap", "torn")]),
         _unit("Unit 2", not_covered=["Building wrap"])])
    world["answers"] = [
        _answer("Unit 1", "insulation", "Yes"),                       # after the report
        _answer("Unit 1", "Building wrap", None, move="Unit 2"),      # the No was Unit 2's
    ]
    status, body = _get(world)
    assert status == 200
    assert body["units"] == [
        {"unit": "Unit 1", "items": [
            {"item_text": "Window flashings", "kind": "not_checked", "comment": ""},
            # moved away: not checked here now (the origin item becomes not checked)
            {"item_text": "Building wrap", "kind": "not_checked", "comment": ""}]},
        {"unit": "Unit 2", "items": []}]                              # decided by the move


def test_a_pending_or_failed_report_is_not_read_as_all_done(world):
    world["s3"].objects[RESULT_KEY] = {"status": "error", "error": "x"}
    assert _get(world) == (404, {"error": "not ready", "status": "error"})
    world["s3"].objects[RESULT_KEY] = {"status": "done"}               # an ordinary report
    assert _get(world) == (404, {"error": "not ready", "status": "done"})


def test_an_s3_refusal_is_not_an_empty_list(world, monkeypatch):
    class Denied(S3):
        def get_object(self, Bucket, Key):
            raise org.ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
    monkeypatch.setattr(org, "s3", lambda: Denied({}))
    with pytest.raises(org.ClientError):
        _get(world)


# ---- the pure part, reused from the final check -----------------------------------

def test_the_open_list_uses_the_final_checks_questions(monkeypatch):
    seen = []
    real = check_done.walk_questions
    monkeypatch.setattr(check_done, "walk_questions",
                        lambda walk, items, **kw: seen.append(items) or real(walk, items, **kw))
    out = check_done.open_from_result(
        {"key": KEY, "units": [_unit("Unit 1", not_covered=["Insulation"])], "conflicts": []},
        ITEMS, [], QUEUED)
    assert seen == [ITEMS]
    assert out == [{"unit": "Unit 1", "items": [
        {"item_text": "Insulation", "kind": "not_checked", "comment": ""}]}]


# ---- R41: the final check's own list, until a newer report ---------------------

def _final(at, questions=None):
    return {"id": "fc", "created_at": at, "questions": questions if questions is not None else [
        {"kind": "not_checked", "unit": "Unit 1", "item_text": "Insulation", "comment": ""},
        {"kind": "not_checked", "unit": "Unit 3", "item_text": "Window flashings", "comment": ""},
        {"kind": "confirm_no", "unit": "Unit 3", "item_text": "Building wrap",
         "comment": "torn"}]}


def test_right_after_a_final_check_the_list_is_served_with_no_report(world):
    world["report"] = None
    at = QUEUED
    world["final"] = _final(at)
    world["answers"] = [_answer("Unit 3", "Building wrap", "Yes", at=at + timedelta(seconds=20)),
                        _answer("Unit 1", "Insulation", "No", at=at - timedelta(minutes=1))]
    status, body = _get(world)
    assert status == 200
    assert body == {"walkKey": KEY, "source": "final_check", "units": [
        {"unit": "Unit 1", "items": [
            {"item_text": "Insulation", "kind": "not_checked", "comment": ""}]},  # answered before it
        {"unit": "Unit 2", "items": []},
        {"unit": "Unit 3", "items": [
            {"item_text": "Window flashings", "kind": "not_checked", "comment": ""}]}]}
    assert world["s3"].reads == []


def test_a_newer_final_check_wins_over_an_older_report(world):
    world["final"] = _final(QUEUED + timedelta(minutes=5),
                            [{"kind": "not_checked", "unit": "Unit 2", "item_text": "Insulation",
                              "comment": ""}])
    status, body = _get(world)
    assert (status, body["source"]) == (200, "final_check")
    assert [u["items"] for u in body["units"]] == [
        [], [{"item_text": "Insulation", "kind": "not_checked", "comment": ""}], []]


def test_an_older_final_check_loses_to_a_newer_report(world):
    world["final"] = _final(QUEUED - timedelta(minutes=5))
    status, body = _get(world)
    assert (status, body["source"]) == (200, "report")
    assert all(u["items"] == [] for u in body["units"])         # the report's list: nothing open


def test_a_final_check_is_served_while_no_report_has_finished(world):
    world["report"] = dict(world["report"], result_key=RESULT_KEY.replace("r1", "r9"))
    world["final"] = _final(QUEUED - timedelta(minutes=30))
    status, body = _get(world)
    assert (status, body["source"]) == (200, "final_check")


def test_a_read_back_answered_on_the_phone_drops_out_of_the_final_checks_list(world):
    world["report"] = None
    world["final"] = _final(QUEUED)
    world["answers"] = [dict(_answer("Unit 3", "Building wrap", "No",
                                     at=QUEUED + timedelta(seconds=5)), asked_kind="confirm_no")]
    body = _get(world)[1]
    assert body["units"][2]["items"] == [
        {"item_text": "Window flashings", "kind": "not_checked", "comment": ""}]


def test_neither_a_report_nor_a_final_check_is_not_ready(world):
    world["report"], world["final"] = None, None
    assert _get(world) == (404, {"error": "not ready"})


def test_a_newer_final_check_with_nothing_open_empties_the_list(world):
    """The device said all covered: an older report's open items are not shown."""
    world["s3"].objects[RESULT_KEY] = _result(
        [_unit("Unit 1", not_covered=["Insulation"]), _unit("Unit 2"), _unit("Unit 3")])
    world["final"] = _final(QUEUED + timedelta(minutes=1), [])
    status, body = _get(world)
    assert (status, body["source"]) == (200, "final_check")
    assert body["units"] == [{"unit": "Unit 1", "items": []}, {"unit": "Unit 2", "items": []},
                             {"unit": "Unit 3", "items": []}]
