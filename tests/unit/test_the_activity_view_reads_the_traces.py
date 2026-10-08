"""The platform admin reads the traces back: one day's steps, and the funnel.

Owner, 2026-10-05: admins see, per customer, what the pipeline did with each
recording and how far recordings get. Computed from the traces
(pipeline_trace) on read -- nothing else stores it.

THE test is `the funnel counts how far each folder's recordings got`.
"""
import io
import json

import pytest

import trace_views as tv

org = pytest.importorskip("lambda_org_api")


def ev(step, result="ok", folder="Ben_Lin_Test", session="sid1", at="2026-10-05T00:00:00",
       company="c-1", **kw):
    e = {"v": 1, "trace_id": "%s/2026-10-05/%s" % (folder, session), "step": step,
         "result": result, "user_folder": folder, "session": session, "at": at,
         "company_id": company, "detail": {}, "prompt_tokens": None,
         "completion_tokens": None, "seconds": None}
    e.update(kw)
    return e


DAY = [
    ev("llm_call", at="2026-10-05T01:00:00", prompt_tokens=1000, completion_tokens=100, seconds=20.0),
    ev("extraction", at="2026-10-05T01:00:01",
       detail={"pass": "live", "topics": 3, "location_markers": 2, "markers_timed": 2}),
    ev("llm_call", at="2026-10-05T01:10:00", prompt_tokens=3800, completion_tokens=900, seconds=98.0),
    ev("extraction", at="2026-10-05T01:10:01",
       detail={"pass": "final", "topics": 4, "location_markers": 5, "markers_timed": 5}),
    # a recording whose final pass has not run yet: provisional, not counted
    ev("extraction", at="2026-10-05T01:05:00", session="sid2",
       detail={"pass": "live", "topics": 2, "location_markers": 1, "markers_timed": 1}),
    ev("photo_placed", "location", at="2026-10-05T01:10:05", detail={"photo": "a.jpg"}),
    ev("photo_placed", "clock", at="2026-10-05T01:10:05", detail={"photo": "b.jpg"}),
    ev("photo_placed", "unbound", at="2026-10-05T01:10:05", detail={"photo": "c.jpg"}),
    # the day re-bound later: the same photos again, counted once
    ev("photo_placed", "location", at="2026-10-05T02:00:00", detail={"photo": "a.jpg"}),
    ev("template_report", at="2026-10-05T03:00:00", session="sidR"),
    ev("checklist", "answered", at="2026-10-05T03:00:00", session="sidR"),
    ev("checklist", "no_match", at="2026-10-05T03:00:00", session="sidR"),
    ev("glossary", "applied", at="2026-10-05T03:00:00", session="sidR"),
    ev("extraction", at="2026-10-05T04:00:00", folder="Neil", session="sidN", company="c-2",
       detail={"pass": "final", "topics": 0}),
]


def reader(by_date):
    return lambda d: list(by_date.get(d, []))


def test_THE_the_funnel_counts_how_far_each_folders_recordings_got():
    out = tv.funnel(reader({"2026-10-05": DAY}), "2026-10-04", "2026-10-05")
    ben = next(f for f in out["folders"] if f["folder"] == "Ben_Lin_Test")
    t = ben["total"]
    assert (t["recordings_extracted"], t["recordings_with_topics"]) == (1, 1), "final passes only"
    assert (t["location_markers"], t["markers_timed"]) == (5, 5)
    assert (t["photos"], t["photos_by_location"], t["photos_unbound"]) == (3, 1, 1)
    assert (t["template_reports"], t["checklists"], t["checklists_answered"]) == (1, 2, 1)
    assert t["glossary_applied"] == 1
    assert (t["llm_calls"], t["prompt_tokens"], t["completion_tokens"]) == (2, 4800, 1000)
    assert t["llm_seconds"] == 118.0
    assert ben["company_id"] == "c-1" and list(ben["daily"]) == ["2026-10-05"]
    neil = next(f for f in out["folders"] if f["folder"] == "Neil")
    assert (neil["total"]["recordings_extracted"], neil["total"]["recordings_with_topics"]) == (1, 0)


def test_one_day_lists_the_folders_then_one_folders_recordings_in_order():
    out = tv.day(reader({"2026-10-05": list(reversed(DAY))}), "2026-10-05")
    assert [(f["folder"], f["recordings"], f["llm_calls"]) for f in out["folders"]] == [
        ("Ben_Lin_Test", 3, 2), ("Neil", 1, 0)]
    one = tv.day(reader({"2026-10-05": list(reversed(DAY))}), "2026-10-05", "Ben_Lin_Test")
    assert [r["session"] for r in one["recordings"]] == ["sid1", "sid2", "sidR"]
    steps = [e["step"] for e in one["recordings"][0]["events"]]
    assert steps[:4] == ["llm_call", "extraction", "llm_call", "extraction"], "time order"


def test_a_range_is_bounded():
    with pytest.raises(ValueError):
        tv.funnel(reader({}), "2026-09-01", "2026-10-05")
    with pytest.raises(ValueError):
        tv.funnel(reader({}), "2026-10-05", "2026-10-01")


def test_the_s3_reader_reads_every_object_of_the_day():
    class S3:
        def __init__(self):
            self.objects = {
                "traces/2026-10-05/Ben_Lin_Test/a.jsonl": json.dumps(DAY[0]) + "\n" + json.dumps(DAY[1]) + "\n",
                "traces/2026-10-05/Neil/b.jsonl": json.dumps(DAY[-1]) + "\nnot json\n"}

        def get_paginator(self, op):
            objects = self.objects

            class P:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
            return P()

        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(self.objects[Key].encode())}
    assert len(tv.s3_day_reader(S3(), "b")("2026-10-05")) == 3
    assert len(tv.s3_day_reader(S3(), "b", "Neil")("2026-10-05")) == 1


# ---- the endpoints -------------------------------------------------------------------

ADMIN = {"id": "u", "company_id": "c-op", "global_role": "platform_admin"}
GM = {"id": "u", "company_id": "c-1", "global_role": "gm"}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org.trace_views, "s3_day_reader",
                        lambda s3, bucket, folder=None: reader({"2026-10-05": DAY}))
    monkeypatch.setattr(org, "s3", lambda: None)
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, cid: {"name": {"c-1": "Naylor Love", "c-2": "Other"}[cid]})


def get(caller, route, params):
    ev = {"queryStringParameters": params}
    return {"/trace/day": org.trace_day_endpoint,
            "/trace/funnel": org.trace_funnel_endpoint}[route](None, caller, ev)


def body(res):
    return json.loads(res["body"])


def test_only_the_platform_admin_reads_traces(wired):
    for route in ("/trace/day", "/trace/funnel"):
        assert get(GM, route, {"date": "2026-10-05"})["statusCode"] == 403


def test_the_admin_gets_the_day_and_the_funnel_with_company_names(wired):
    day = body(get(ADMIN, "/trace/day", {"date": "2026-10-05"}))
    assert [(f["folder"], f["company_name"]) for f in day["folders"]] == [
        ("Ben_Lin_Test", "Naylor Love"), ("Neil", "Other")]
    fun = body(get(ADMIN, "/trace/funnel", {"from": "2026-10-05", "to": "2026-10-05"}))
    assert fun["folders"][0]["company_name"] == "Naylor Love"
    assert get(ADMIN, "/trace/funnel", {"from": "2026-01-01", "to": "2026-10-05"})["statusCode"] == 400
    assert get(ADMIN, "/trace/day", {"date": "05-10-2026"})["statusCode"] == 400


def test_the_routes_are_dispatched():
    src = open(org.__file__, encoding="utf-8").read()
    assert 'route == "/trace/day" and method == "GET"' in src
    assert 'route == "/trace/funnel" and method == "GET"' in src


def test_org_api_may_read_and_list_the_traces():
    import os
    t = open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml"),
             encoding="utf-8").read()
    start = t.index("\n  OrgApiFunction:")
    block = t[start:t.index("\n  ExtractSessionFunction:", start)]
    grant = block[block.index("/traces/*") - 300:block.index("/traces/*")]
    assert "s3:GetObject" in grant
    assert "- traces/*" in block, "ListBucket on traces/: an empty day must not read as AccessDenied"
