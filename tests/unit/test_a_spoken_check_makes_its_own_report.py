"""A spoken check that matched a checklist makes its own report (owner,
2026-10-06): no dialog, no picking topics -- the filled checklist is waiting.

THE test is `a matched check is sent to the worker once, as the recorder,
with its template and its stretches`.
"""
import json

import pytest

import checklist_reports as cr

USER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "folder_name": "Ben_Lin_test2"}
TPL = {"id": "t-1", "name": "Concrete Pre-pour Inspection Checklist", "current_version": 3}
SEGS = [{"from": "11:02:03", "to": "11:02:21"}, {"from": "11:02:41", "to": "11:03:21"}]
WINDOWS = [
    {"name": "level 2 pre-pour", "start_at": "11:02:03", "end_at": "11:03:21",
     "template_id": "t-1", "segments": SEGS},
    {"name": "steel inspection", "start_at": "11:02:21", "end_at": "11:02:41",
     "template_id": None, "segments": [{"from": "11:02:21", "to": "11:02:41"}]},
]


@pytest.fixture
def wired(monkeypatch):
    st = {"made": set(), "recorded": [], "calls": []}
    monkeypatch.setattr(cr.users, "get_by_folder_name", lambda c, co, f: USER)
    monkeypatch.setattr(cr.users, "get_user_by_sub", lambda c, sub: dict(USER, global_role="gm"))
    monkeypatch.setattr(cr.report_templates, "get_any", lambda c, tid: TPL)
    monkeypatch.setattr(cr.topics_repo, "list_day_topics_for_binding", lambda c, f, d: [])
    monkeypatch.setattr(cr, "already_made",
                        lambda c, s, t, a, e=None, g=None: (s, t, a, e) in st["made"])

    def record(conn, co, folder, date, session, w, name, rid, key):
        st["recorded"].append((session, w["template_id"], w["start_at"], rid, key))
        st["made"].add((session, w["template_id"], w["start_at"], w.get("end_at")))
    monkeypatch.setattr(cr, "_record", record)

    def generate(conn, caller, date, event):
        st["calls"].append((caller["id"], date, event["queryStringParameters"]["user"],
                            json.loads(event["body"])))
        return {"statusCode": 200, "body": json.dumps({"status": "queued", "requestId": "r%d" % len(st["calls"])})}
    st["generate"] = generate
    return st


def test_THE_a_matched_check_is_sent_to_the_worker_once_as_the_recorder(wired):
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS,
                           generate=wired["generate"])
    assert out == [("level 2 pre-pour", "r1")]
    who, date, folder, body = wired["calls"][0]
    assert (who, date, folder) == ("u-1", "2026-10-06", "Ben_Lin_test2")
    assert body["templateId"] == "t-1" and body["templateVersion"] == 3
    assert (body["from"], body["to"], body["segments"]) == ("11:02:03", "11:03:21", SEGS)
    assert body["deliver"] == "download"
    assert body["title"] == "Concrete Pre-pour Inspection Checklist"
    assert wired["recorded"][0][3:] == ("r1", "session_report_results/Ben_Lin_test2/2026-10-06/day/r1.json")
    # re-extracted: the same check is not made again
    assert cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS,
                            generate=wired["generate"]) == [("level 2 pre-pour", "already made")]
    assert len(wired["calls"]) == 1


def test_an_unmatched_check_never_fills_a_form(wired):
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS[1:],
                     generate=wired["generate"])
    assert wired["calls"] == []


def test_a_refusal_is_reported_not_recorded(wired):
    def refuse(conn, caller, date, event):
        return {"statusCode": 400, "body": json.dumps({"error": "no topics in scope"})}
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS[:1], generate=refuse)
    assert out == [("level 2 pre-pour", "refused: no topics in scope")]
    assert wired["recorded"] == []


def test_a_failure_never_escapes(wired):
    def boom(conn, caller, date, event):
        raise RuntimeError("S3 down")
    assert cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS[:1],
                            generate=boom) == [("level 2 pre-pour", "error")]


def test_one_stretch_sends_no_segments(wired):
    one = [dict(WINDOWS[0], segments=[SEGS[0]], end_at="11:02:21")]
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", one, generate=wired["generate"])
    assert "segments" not in wired["calls"][0][3]


def test_item_writer_makes_them_on_a_final_pass_after_the_photos():
    lam = pytest.importorskip("lambda_item_writer")
    src = open(lam.__file__, encoding="utf-8").read()
    rebind = src.index("photo_rebind.rebind_day_photos(")
    auto = src.index("checklist_reports.auto_generate(")
    assert rebind < auto
    assert 'if (extraction.get("tier") == "final" and stored_inspections' in src[rebind:auto + 10]
    assert 'and not extraction.get("incomplete")):' in src[rebind:auto + 10]


def test_the_endpoints_list_reports_with_status_and_unmatched_checks(monkeypatch):
    import datetime
    import uuid
    org = pytest.importorskip("lambda_org_api")
    monkeypatch.setattr(org, "_resolve_org_media_folder", lambda c, caller, u, what: ("Ben_Lin_test2", None))
    monkeypatch.setattr(org.checklist_reports, "for_day", lambda c, co, f, d: [{
        "id": uuid.UUID(int=1), "report_date": datetime.date(2026, 10, 6), "session": "sidA",
        "template_id": uuid.UUID(int=2), "template_name": TPL["name"], "check_name": "level 2 pre-pour",
        "start_at": "11:02:03", "end_at": "11:03:21", "segments": SEGS, "request_id": "r1",
        "result_key": "session_report_results/x/2026-10-06/day/r1.json"}])
    monkeypatch.setattr(org.inspection_windows, "for_day", lambda c, co, f, d: [
        {"name": "steel inspection", "start_at": "11:02:21", "end_at": "11:02:41", "kind": "steel",
         "template_id": None}])
    monkeypatch.setattr(org, "_checklist_report_status", lambda key: ("done", None, None))
    caller = {"id": "u", "company_id": "c-1", "global_role": "gm"}
    res = org.get_day_checklist_reports(None, caller, "2026-10-06", {"queryStringParameters": {"user": "Ben_Lin_test2"}})
    body = json.loads(res["body"])
    assert body["reports"][0]["status"] == "done" and body["reports"][0]["requestId"] == "r1"
    assert body["unmatched"] == [{"checkName": "steel inspection", "startAt": "11:02:21",
                                  "endAt": "11:02:41", "kind": "steel"}]
    src = open(org.__file__, encoding="utf-8").read()
    assert 're.match(r"^/days/([^/]+)/checklist-reports$", route)' in src
    assert 'route == "/checklist-reports/recent" and method == "GET"' in src


def test_a_rerun_over_the_full_recording_makes_the_report_again(wired):
    """TEST 2026-10-06: the first final saw 80 seconds of the check; the re-run
    saw all of it. The report must follow the fuller window."""
    early = [dict(WINDOWS[0], end_at="11:02:21", segments=[SEGS[0]])]
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", early, generate=wired["generate"])
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", WINDOWS[:1], generate=wired["generate"])
    assert [c[3]["to"] for c in wired["calls"]] == ["11:02:21", "11:03:21"]



# The owner's TEST recording, 2026-10-06 17:12: the topics as extraction stamped them.
TOPICS_1006 = [
    {"id": "formwork", "title": "Formwork Setup and Cleanliness", "time_range": "17:12 – 17:14"},
    {"id": "booking", "title": "Level Two Pour Booking and Approvals", "time_range": "17:12 – 17:12"},
    {"id": "reo", "title": "Slab Reinforcement and Cover", "time_range": "17:14 – 17:16"},
    {"id": "scaffold", "title": "Friday Scaffold Delivery Coordination", "time_range": "17:14 – 17:14"},
    {"id": "steel", "title": "Stair Core Steel Check", "time_range": "17:14 – 17:14"},
    {"id": "pour", "title": "Pour Logistics and Edge Protection", "time_range": "17:16 – 17:16"},
    {"id": "services", "title": "Electrical and Plumbing Rough-In", "time_range": "17:16 – 17:16"},
]
PREPOUR = {"name": "concrete pre-pour check for the level two slab", "kind": "pre-pour",
           "start_at": "17:12:52", "end_at": "17:17:33",
           "segments": [{"from": "17:12:52", "to": "17:15:27"}, {"from": "17:15:42", "to": "17:17:33"}]}
STEEL = {"name": "steel inspections for the stair core", "kind": "steel",
         "start_at": "17:15:27", "end_at": "17:15:42"}


def test_THE_a_checks_report_takes_its_own_topics_and_not_the_other_checks():
    """Stamped by the minute, the pour-booking talk (17:12:52-17:13:30) was
    '17:12 - 17:12' and the steel check (17:15:27) '17:14': judged by the
    window the report lost the first and took the second."""
    assert cr.check_topics(PREPOUR, [STEEL], TOPICS_1006) == [
        "formwork", "booking", "reo", "scaffold", "pour", "services"]


def test_the_queued_request_names_them(wired, monkeypatch):
    monkeypatch.setattr(cr.topics_repo, "list_day_topics_for_binding", lambda c, f, d: [
        dict(t, source_s3_key="extractions/Ben_Lin_test2/2026-10-06/sidA.json") for t in TOPICS_1006])
    w = dict(PREPOUR, template_id="t-1")
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA",
                     [w, dict(STEEL, template_id=None)], generate=wired["generate"])
    assert "steel" not in wired["calls"][0][3]["topicRowIds"]
    assert "booking" in wired["calls"][0][3]["topicRowIds"]


# ---- a multi-unit walk: one report across its recordings (spec 2026-11-03, Task 9) ----

import units  # noqa: E402

WDATE = "2026-11-03"
WKEY = units.walk_key("c-1", "Ben_Lin_test2", WDATE, "t-1", "10:00:00")
WALK_A = {"id": "wa", "session": "sidA", "name": "pre-line check units 1 to 3", "kind": "pre-line",
          "start_at": "10:00:00", "end_at": "10:20:00", "template_id": "t-1",
          "segments": [{"from": "10:00:00", "to": "10:20:00"}],
          "units": {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3},
          "entered": [{"unit": "Unit 1", "at": "10:01", "at_s": "10:01:00", "quote": "unit one"},
                      {"unit": "Unit 2", "at": "10:10", "at_s": "10:10:00", "quote": "unit two"}],
          "walk_key": WKEY}
WALK_B = {"id": "wb", "session": "sidB", "name": "pre-line carry on", "kind": "pre-line",
          "start_at": "13:00:00", "end_at": "13:15:00", "template_id": "t-1",
          "segments": [{"from": "13:00:00", "to": "13:15:00"}], "units": None,
          "entered": [{"unit": "Unit 3", "at": "13:01", "at_s": "13:01:00", "quote": "unit three"}],
          "walk_key": WKEY}
BOTH_SEGS = WALK_A["segments"] + WALK_B["segments"]
BOTH_STRETCHES = [{"unit": None, "from": "10:00:00", "to": "10:01:00"},
                  {"unit": "Unit 1", "from": "10:01:00", "to": "10:10:00"},
                  {"unit": "Unit 2", "from": "10:10:00", "to": "10:20:00"},
                  {"unit": None, "from": "13:00:00", "to": "13:01:00"},
                  {"unit": "Unit 3", "from": "13:01:00", "to": "13:15:00"}]


@pytest.fixture
def walked(wired, monkeypatch):
    """`wired`, plus the day's windows (for_day) and a walk's stored segments."""
    wired["day"] = [WALK_A, WALK_B]
    wired["walk_made"] = {}
    wired["walk_recorded"] = []
    monkeypatch.setattr(cr.inspection_windows, "for_day", lambda c, co, f, d: list(wired["day"]))

    def made(c, s, t, a, e=None, g=None, walk_key=None):
        if walk_key:
            return walk_key in wired["walk_made"] and wired["walk_made"][walk_key] == g
        return (s, t, a, e) in wired["made"]
    monkeypatch.setattr(cr, "already_made", made)

    def record(conn, co, folder, date, session, w, name, rid, key):
        wired["recorded"].append((session, w["template_id"], w["start_at"], rid, key))
        if w.get("walk_key"):
            wired["walk_made"][w["walk_key"]] = w.get("segments")
            wired["walk_recorded"].append(dict(w, session=session, request_id=rid))
    monkeypatch.setattr(cr, "_record", record)
    return wired


def test_a_walk_over_two_recordings_is_queued_once_with_both_stretches(walked):
    # sidB's final extraction: its window carries on the walk sidA opened
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                           generate=walked["generate"])
    assert out == [("pre-line check units 1 to 3", "r1")]
    assert len(walked["calls"]) == 1
    body = walked["calls"][0][3]
    assert (body["from"], body["to"], body["segments"]) == ("10:00:00", "13:15:00", BOTH_SEGS)
    assert body["walk"] == {"key": WKEY, "units": WALK_A["units"],
                            "entered": WALK_A["entered"] + WALK_B["entered"],
                            "stretches": BOTH_STRETCHES}
    rec = walked["walk_recorded"][0]
    assert (rec["session"], rec["start_at"], rec["walk_key"], rec["segments"]) == (
        "sidB", "10:00:00", WKEY, BOTH_SEGS)


def test_two_windows_of_one_walk_in_one_recording_make_one_report(walked):
    a2 = dict(WALK_A, id="wa2", name="pre-line again", start_at="10:30:00", end_at="10:40:00",
              segments=[{"from": "10:30:00", "to": "10:40:00"}], units=None, entered=[])
    walked["day"] = [WALK_A, a2]
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidA", [WALK_A, a2],
                           generate=walked["generate"])
    assert out == [("pre-line check units 1 to 3", "r1")]
    assert walked["calls"][0][3]["segments"] == WALK_A["segments"] + a2["segments"]


def test_a_second_recording_regenerates_the_walk_report(walked):
    walked["day"] = [WALK_A]
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidA", [WALK_A],
                     generate=walked["generate"])
    assert walked["calls"][0][3]["to"] == "10:20:00"
    assert "segments" not in walked["calls"][0][3]          # one stretch, as an ordinary check
    walked["day"] = [WALK_A, WALK_B]                       # the second recording lands
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                           generate=walked["generate"])
    assert out == [("pre-line check units 1 to 3", "r2")]
    assert walked["calls"][1][3]["segments"] == BOTH_SEGS
    # re-extracting either recording now finds it made
    assert cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidA", [WALK_A],
                            generate=walked["generate"]) == [
        ("pre-line check units 1 to 3", "already made")]
    assert len(walked["calls"]) == 2


def test_a_single_unit_check_is_queued_exactly_as_before(walked, monkeypatch):
    def no_day(*a):
        raise AssertionError("a single-unit check reads no walk")
    monkeypatch.setattr(cr.inspection_windows, "for_day", no_day)
    plain = [dict(w, walk_key=None) for w in WINDOWS]
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", "2026-10-06", "sidA", plain,
                           generate=walked["generate"])
    assert out == [("level 2 pre-pour", "r1")]
    assert walked["calls"][0][3] == {
        "templateId": "t-1", "templateVersion": 3,
        "title": "Concrete Pre-pour Inspection Checklist", "deliver": "download",
        "from": "11:02:03", "to": "11:03:21", "segments": SEGS}
    assert walked["recorded"] == [("sidA", "t-1", "11:02:03", "r1",
                                   "session_report_results/Ben_Lin_test2/2026-10-06/day/r1.json")]


def test_a_walk_with_no_opening_names_sends_empty_units(walked):
    """Entering two units opens a walk with no opening list (units NULL): the
    request still names `units` the way org-api validates it."""
    key = units.walk_key("c-1", "Ben_Lin_test2", WDATE, "t-1", "13:00:00")
    b = dict(WALK_B, walk_key=key, entered=WALK_B["entered"] + [
        {"unit": "Unit 4", "at": "13:08", "at_s": "13:08:00", "quote": "unit four"}])
    walked["day"] = [b]
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [b], generate=walked["generate"])
    assert walked["calls"][0][3]["walk"]["units"] == {"names": [], "count": None}


def test_a_recording_still_running_is_left_out_of_the_walk(walked):
    live = dict(WALK_B, end_at=None, segments=[{"from": "13:00:00", "to": None}])
    walked["day"] = [WALK_A, live]
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidA", [WALK_A],
                     generate=walked["generate"])
    body = walked["calls"][0][3]
    assert (body["from"], body["to"]) == ("10:00:00", "10:20:00")
    assert all(s["to"] for s in body["walk"]["stretches"])


def test_a_blocked_recorder_queues_no_walk(walked, monkeypatch):
    monkeypatch.setattr(cr.tenancy_status, "caller_blocked", lambda c, caller, where: "company_inactive")
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                           generate=walked["generate"])
    assert out == [("pre-line carry on", "company_inactive")]
    assert walked["calls"] == []


def test_requeue_walk_queues_again_even_when_made(walked):
    cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                     generate=walked["generate"])
    rid = cr.requeue_walk(None, "c-1", "Ben_Lin_test2", WDATE, WKEY, generate=walked["generate"])
    assert rid == "r2"
    assert walked["calls"][1][3] == walked["calls"][0][3]     # the same per-walk body
    assert walked["walk_recorded"][1]["request_id"] == "r2"
    assert cr.requeue_walk(None, "c-1", "Ben_Lin_test2", WDATE, "walk:" + "0" * 40,
                           generate=walked["generate"]) == "no such walk"


def test_requeue_walk_for_a_blocked_recorder_queues_nothing(walked, monkeypatch):
    monkeypatch.setattr(cr.tenancy_status, "caller_blocked", lambda c, caller, where: "company_inactive")
    assert cr.requeue_walk(None, "c-1", "Ben_Lin_test2", WDATE, WKEY,
                           generate=walked["generate"]) == "company_inactive"
    assert walked["calls"] == [] and walked["walk_recorded"] == []


def _org_accepts(walked):
    """The walked generate, but every body must pass org-api's own walk check."""
    org = pytest.importorskip("lambda_org_api")

    def generate(conn, caller, date, event):
        walk, err = org._walk_from_body(json.loads(event["body"]))
        assert err is None and walk is not None, err
        return walked["generate"](conn, caller, date, event)
    return generate


def test_a_malformed_mark_is_dropped_before_the_body_is_built(walked, caplog):
    bad = {"unit": "Unit 3", "at": "10:15", "at_s": "9:01:00", "quote": "unit three"}
    none = {"unit": "Unit 3", "quote": "no time at all"}
    a = dict(WALK_A, entered=WALK_A["entered"] + [bad, none])
    walked["day"] = [a, WALK_B]
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                           generate=_org_accepts(walked))
    assert out == [("pre-line check units 1 to 3", "r1")]
    walk = walked["calls"][0][3]["walk"]
    assert walk["entered"] == WALK_A["entered"] + WALK_B["entered"]
    assert walk["stretches"] == BOTH_STRETCHES
    assert "dropped 2 of 5 mark" in caplog.text
    # Counts only: a mark's quote is his words, and the unit is his too.
    assert "unit three" not in caplog.text and "no time at all" not in caplog.text
    assert "Unit 3" not in caplog.text


@pytest.mark.parametrize("marks", [
    [{"unit": "Unit %d" % i, "at_s": "10:11:%02d" % i} for i in range(41)],    # 41 units
    [{"unit": "Unit %d" % (i % 2 + 1), "at_s": "10:%02d:%02d" % (11 + i // 60, i % 60)}
     for i in range(201)],                                                        # 201 marks
])
def test_a_walk_over_the_caps_is_never_sent(walked, marks, caplog):
    a = dict(WALK_A, entered=WALK_A["entered"] + marks)
    walked["day"] = [a, WALK_B]
    out = cr.auto_generate(None, "c-1", "Ben_Lin_test2", WDATE, "sidB", [WALK_B],
                           generate=_org_accepts(walked))
    assert walked["calls"] == [] and walked["walk_recorded"] == []
    assert out[0][0] == "pre-line check units 1 to 3"
    assert out[0][1].startswith("walk not sent: walk has more than")
    assert "walk not sent" in caplog.text


# ---- org-api: the walk travels to the report worker ----------------------------------

GOOD_WALK = {"key": WKEY, "units": {"names": ["Unit 1", "Unit 2"], "count": 2},
             "entered": [{"unit": "Unit 1", "at": "10:01"}],
             "stretches": [{"unit": None, "from": "10:00:00", "to": "10:01:00"},
                           {"unit": "Unit 1", "from": "10:01:00", "to": "10:20:00"}]}


@pytest.fixture
def org_gen(monkeypatch):
    org = pytest.importorskip("lambda_org_api")
    st = {"put": [], "answers_read": []}
    monkeypatch.setattr(org, "_assemble_day_report", lambda c, caller, d, ev, sel: (
        {"folder": "Ben_Lin_test2", "topic_row_ids": [], "sessionIds": ["sidA"],
         "mirrorFolders": [], "title": "t", "participants": [], "siteIds": []}, None))
    monkeypatch.setattr(org, "_generation_request", lambda body, deliver, c, caller: (
        ({"templateId": body.get("templateId")}, None) if body.get("templateId") else (None, None)))
    monkeypatch.setattr(org, "_excluded_topics_for", lambda *a, **k: [])
    monkeypatch.setattr(org, "_report_facts", lambda *a, **k: {})
    monkeypatch.setattr(org, "_report_email_suppression", lambda *a, **k: None)
    monkeypatch.setattr(org, "_lake_company_for_sites", lambda *a, **k: None)
    monkeypatch.setattr(org, "_lake_user_for_folder", lambda *a, **k: None)
    monkeypatch.setattr(org.lake_objects, "record_best_effort", lambda *a, **k: None)

    class S3:
        def put_object(self, **kw):
            st["put"].append(json.loads(kw["Body"]))
    monkeypatch.setattr(org, "s3", lambda: S3())

    def for_walk(conn, company_ids, key):
        st["answers_read"].append((company_ids, key))
        return st.get("answers", [])
    monkeypatch.setattr(org.walk_answers, "for_walk", for_walk)

    st["walk_companies"] = ["c-1"]          # the walk's windows' company (the site's)
    st["walk_lookups"] = []

    def walk_companies(conn, folder, date, key):
        st["walk_lookups"].append((folder, date, key))
        return list(st["walk_companies"])
    monkeypatch.setattr(org.inspection_windows, "walk_companies", walk_companies)
    st["org"] = org

    class Conn:                     # the answers are read in a savepoint
        def transaction(self):
            import contextlib
            return contextlib.nullcontext()

    def call(body):
        return org.day_report_generate(Conn(), {"id": "u-1", "company_id": "c-1"}, WDATE, {
            "queryStringParameters": {"user": "Ben_Lin_test2"}, "body": json.dumps(body)})
    st["call"] = call
    return st


BASE_BODY = {"templateId": "t-1", "deliver": "download", "from": "10:00:00", "to": "10:20:00"}


@pytest.mark.parametrize("walk", [
    dict(GOOD_WALK, key="walk:XYZ"),
    dict(GOOD_WALK, key="wk-1"),
    dict(GOOD_WALK, key="walk:" + "a" * 39),
    dict(GOOD_WALK, key=None),
    "walk:" + "a" * 40,
])
def test_a_malformed_walk_key_is_refused_400(org_gen, walk):
    res = org_gen["call"](dict(BASE_BODY, walk=walk))
    assert res["statusCode"] == 400 and "walk" in json.loads(res["body"])["error"]
    assert org_gen["put"] == []


@pytest.mark.parametrize("walk", [
    dict(GOOD_WALK, stretches=[{"unit": "Unit 1", "from": "10:01", "to": "10:20:00"}]),
    dict(GOOD_WALK, stretches=[{"unit": "Unit 1", "from": "10:01:00", "to": None}]),
    dict(GOOD_WALK, stretches=[{"unit": 2, "from": "10:01:00", "to": "10:20:00"}]),
    dict(GOOD_WALK, stretches="none"),
    dict(GOOD_WALK, stretches=[{"unit": "Unit %d" % i, "from": "10:00:00", "to": "10:01:00"}
                               for i in range(41)]),
    dict(GOOD_WALK, units={"names": ["Unit %d" % i for i in range(41)], "count": None}),
    dict(GOOD_WALK, entered=[{"unit": "Unit 1", "at": "10:01"}] * 201),
    dict(GOOD_WALK, entered="Unit 1"),
    dict(GOOD_WALK, units=None),
    dict(GOOD_WALK, units={"names": "Unit 1", "count": None}),
    dict(GOOD_WALK, units={"names": [], "count": "2"}),
    dict(GOOD_WALK, units={"names": [], "count": True}),
])
def test_a_malformed_walk_is_refused_400(org_gen, walk):
    res = org_gen["call"](dict(BASE_BODY, walk=walk))
    assert res["statusCode"] == 400 and "walk" in json.loads(res["body"])["error"]
    assert org_gen["put"] == []


def test_a_walk_goes_to_the_worker_with_its_on_site_answers(org_gen):
    import datetime
    import uuid
    org_gen["answers"] = [{
        "id": "a1", "company_id": uuid.UUID(int=7), "user_folder": "Ben_Lin_test2",
        "report_date": datetime.date(2026, 11, 3), "walk_key": WKEY, "unit": "Unit 1",
        "item_text": "Insulation", "answer": "No", "comment": "gap", "move_to_unit": None,
        "source": "device", "quote": "insulation no", "voice_note_id": None,
        "answered_at": "10:30:00", "answered_by": uuid.UUID(int=9),
        "created_at": datetime.datetime(2026, 11, 3, 10, 30, tzinfo=datetime.timezone.utc)}]
    res = org_gen["call"](dict(BASE_BODY, walk=GOOD_WALK))
    assert res["statusCode"] == 202
    art = org_gen["put"][0]
    assert org_gen["walk_lookups"] == [("Ben_Lin_test2", WDATE, WKEY)]
    assert org_gen["answers_read"] == [(["c-1"], WKEY)]      # the walk's company
    a = art["walk"]["answers"][0]
    assert (a["company_id"], a["answered_by"], a["report_date"], a["created_at"]) == (
        str(uuid.UUID(int=7)), str(uuid.UUID(int=9)), "2026-11-03", "2026-11-03T10:30:00+00:00")
    assert {k: art["walk"][k] for k in ("key", "units", "entered", "stretches")} == GOOD_WALK
    assert art["window"] == {"from": "10:00:00", "to": "10:20:00"}


def test_answers_are_read_in_the_walks_company_not_the_callers(org_gen):
    """R22: a recorder from another company walks a site of c-site. The answers
    were written under the walk's company, so that is where they are read."""
    org_gen["walk_companies"] = ["c-site"]
    res = org_gen["call"](dict(BASE_BODY, walk=GOOD_WALK))
    assert res["statusCode"] == 202
    assert org_gen["answers_read"] == [(["c-site"], WKEY)]


def test_a_walk_that_is_not_this_folders_day_is_refused(org_gen):
    org_gen["walk_companies"] = []
    res = org_gen["call"](dict(BASE_BODY, walk=GOOD_WALK))
    assert res["statusCode"] == 400 and "walk" in json.loads(res["body"])["error"]
    assert org_gen["put"] == [] and org_gen["answers_read"] == []


def test_entered_marks_up_to_200_are_accepted(org_gen):
    walk = dict(GOOD_WALK, entered=[{"unit": "Unit 1", "at": "10:01"}] * 200)
    assert org_gen["call"](dict(BASE_BODY, walk=walk))["statusCode"] == 202


def test_answers_that_cannot_be_read_still_queue_the_walk(org_gen, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(org_gen["org"].walk_answers, "for_walk", boom)
    res = org_gen["call"](dict(BASE_BODY, walk=GOOD_WALK))
    assert res["statusCode"] == 202
    assert org_gen["put"][0]["walk"]["answers"] == []


def test_without_a_walk_the_artifact_is_todays(org_gen):
    res = org_gen["call"](dict(BASE_BODY))
    assert res["statusCode"] == 202
    assert "walk" not in org_gen["put"][0] and org_gen["answers_read"] == []
    org_gen["call"](dict(BASE_BODY, walk=None))
    assert "walk" not in org_gen["put"][1]


def _hms(s):
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def test_a_walk_window_takes_up_to_200_stretches_an_ordinary_one_20():
    org = pytest.importorskip("lambda_org_api")
    start = 10 * 3600
    segs = [{"from": _hms(start + 20 * i), "to": _hms(start + 20 * i + 10)} for i in range(201)]
    body = {"from": segs[0]["from"], "to": segs[199]["to"], "segments": segs[:200]}
    assert "segments" not in org._report_window(body)
    assert org._report_window(dict(body, segments=segs[:20]))["segments"] == segs[:20]
    assert org._report_window(dict(body, walk=GOOD_WALK))["segments"] == segs[:200]
    assert "segments" not in org._report_window(dict(body, walk=GOOD_WALK, segments=segs))
