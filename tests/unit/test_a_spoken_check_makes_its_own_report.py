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
    monkeypatch.setattr(cr, "already_made", lambda c, s, t, a: (s, t, a) in st["made"])

    def record(conn, co, folder, date, session, w, name, rid, key):
        st["recorded"].append((session, w["template_id"], w["start_at"], rid, key))
        st["made"].add((session, w["template_id"], w["start_at"]))
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
    assert body["title"].startswith("Concrete Pre-pour Inspection Checklist -- level 2 pre-pour")
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
    assert 'if extraction.get("tier") == "final" and stored_inspections:' in src[rebind:auto + 10]


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
    monkeypatch.setattr(org, "_checklist_report_status", lambda key: ("done", None))
    caller = {"id": "u", "company_id": "c-1", "global_role": "gm"}
    res = org.get_day_checklist_reports(None, caller, "2026-10-06", {"queryStringParameters": {"user": "Ben_Lin_test2"}})
    body = json.loads(res["body"])
    assert body["reports"][0]["status"] == "done" and body["reports"][0]["requestId"] == "r1"
    assert body["unmatched"] == [{"checkName": "steel inspection", "startAt": "11:02:21",
                                  "endAt": "11:02:41", "kind": "steel"}]
    src = open(org.__file__, encoding="utf-8").read()
    assert 're.match(r"^/days/([^/]+)/checklist-reports$", route)' in src
    assert 'route == "/checklist-reports/recent" and method == "GET"' in src
