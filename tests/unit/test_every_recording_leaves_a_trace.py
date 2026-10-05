"""What the pipeline did with a recording is written down, step by step.

Owner, 2026-10-05: every recording should be traceable the way an agent's
tool calls are -- which steps ran, how many model calls each made, whether a
template or keyword fired, and when something did NOT fire, that it was
looked for. Kept as JSONL under traces/<date>/<folder>/ for 180 days; an
agent reads them.

THE test is `an extraction leaves its pass, its markers and its model calls`,
run through the real extract_session with the trace switched on.
"""
import io
import json
import os

import pytest

import llm_usage
import photo_binding as pb
import photo_rebind
import pipeline_trace

es = pytest.importorskip("lambda_extract_session")


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("TRACE_EVENTS", "on")
    pipeline_trace._state.set(None)
    yield
    pipeline_trace._state.set(None)


class Puts:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


def events_of(put):
    return [json.loads(line) for line in put["Body"].decode("utf-8").splitlines()]


# ---- the recorder ------------------------------------------------------------------

def test_one_object_per_invocation_under_the_recordings_day():
    pipeline_trace.begin("extract-session", user_folder="Ben_Lin_Test", date="2026-10-05",
                         session="sid1")
    pipeline_trace.event("extraction", "ok", detail={"topics": 4})
    pipeline_trace.event("checklist", "no_match", detail={"checklist": "Steel"})
    s3 = Puts()
    key = pipeline_trace.flush(s3, "bucket")
    assert key.startswith("traces/2026-10-05/Ben_Lin_Test/") and key.endswith(".jsonl")
    assert "-extract-session-" in key
    lines = events_of(s3.puts[0])
    assert [(e["step"], e["result"]) for e in lines] == [("extraction", "ok"),
                                                          ("checklist", "no_match")]
    first = lines[0]
    assert first["v"] == 1 and first["trace_id"] == "Ben_Lin_Test/2026-10-05/sid1"
    assert first["lambda"] == "extract-session" and first["user_folder"] == "Ben_Lin_Test"
    assert set(first) >= {"at", "invocation", "company_id", "detail", "evidence", "model",
                          "prompt_tokens", "completion_tokens", "seconds"}


def test_evidence_is_cut_at_200_characters():
    pipeline_trace.begin("x", user_folder="f", date="2026-10-05")
    pipeline_trace.event("location_marker", "timed", evidence="w" * 500)
    s3 = Puts()
    pipeline_trace.flush(s3, "b")
    assert len(events_of(s3.puts[0])[0]["evidence"]) == 200


def test_nothing_recorded_writes_nothing_and_never_makes_a_client():
    pipeline_trace.begin("x", user_folder="f", date="2026-10-05")

    def factory():
        raise AssertionError("no client for an empty trace")
    assert pipeline_trace.flush(factory, "b") is None


def test_switched_off_writes_nothing(monkeypatch):
    monkeypatch.setenv("TRACE_EVENTS", "off")
    pipeline_trace.begin("x", user_folder="f", date="2026-10-05")
    pipeline_trace.event("extraction")
    s3 = Puts()
    assert pipeline_trace.flush(s3, "b") is None and s3.puts == []


def test_a_failed_write_never_fails_the_work():
    class Broken:
        def put_object(self, **kw):
            raise RuntimeError("AccessDenied")
    pipeline_trace.begin("x", user_folder="f", date="2026-10-05")
    pipeline_trace.event("extraction")
    assert pipeline_trace.flush(Broken(), "b") is None


def test_outside_an_invocation_an_event_is_a_no_op():
    pipeline_trace.event("extraction")          # no begin(): nothing to raise about


def test_every_model_call_is_counted_from_the_usage_line():
    pipeline_trace.begin("x", user_folder="f", date="2026-10-05")
    llm_usage.log_usage("openrouter", "muse-spark", "extract_session", 41.2,
                        prompt_tokens=18000, completion_tokens=900)
    s3 = Puts()
    pipeline_trace.flush(s3, "b")
    e = events_of(s3.puts[0])[0]
    assert (e["step"], e["model"], e["prompt_tokens"], e["completion_tokens"], e["seconds"]) == \
        ("llm_call", "muse-spark", 18000, 900, 41.2)
    assert e["detail"]["caller"] == "extract_session"


# ---- extraction --------------------------------------------------------------------

FILE = ("ben_lin_test_2026-10-05_11-02-01_sid37168e5632af4360b3784c752823f46c_c0007"
        "_off0.0_to109.0_srcwav.json")
KEY = "transcripts/Ben_Lin_Test/2026-10-05/" + FILE
WORDS = [("Okay,", 4.7), ("back", 4.9), ("to", 5.2), ("the", 5.4), ("level", 5.5),
         ("one", 5.8), ("inspections.", 6.4), ("Moving", 14.0), ("up", 14.3), ("to", 14.5),
         ("level", 14.8), ("two.", 15.1)]


def _transcript():
    return {"results": {"transcripts": [{"transcript": " ".join(w for w, _ in WORDS)}],
                        "items": [{"type": "pronunciation", "start_time": str(t),
                                   "end_time": str(t + 0.2), "speaker_label": "spk_0",
                                   "alternatives": [{"content": w, "confidence": "1.0"}]}
                                  for w, t in WORDS]}}


class S3(Puts):
    def get_object(self, Bucket, Key):
        if Key != KEY:
            raise KeyError(Key)
        return {"Body": io.BytesIO(json.dumps(_transcript()).encode())}

    def get_paginator(self, op):
        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": KEY}] if KEY.startswith(Prefix) else []}
        return P()


def test_THE_an_extraction_leaves_its_pass_its_markers_and_its_model_calls(monkeypatch):
    import llm_utils
    s3 = S3()
    monkeypatch.setattr(es, "s3", lambda: s3)
    monkeypatch.setattr(es, "_sites_cache", None)
    monkeypatch.setattr(es.time, "sleep", lambda s: None)

    def model(prompt, **kw):
        llm_usage.log_usage("openrouter", "muse-spark", "extract_session", 3.0,
                            prompt_tokens=1000, completion_tokens=100)
        return json.dumps({"topics": [], "declared_site": None, "location_markers": [
            {"at": "11:02", "location": "level one", "quote": "back to the level one"},
            {"at": "11:02", "location": "level nine", "quote": "somewhere never said"}]}), None
    monkeypatch.setattr(llm_utils, "call_llm", model)

    es.extract_session("b", "Ben_Lin_Test", "2026-10-05",
                       "sid37168e5632af4360b3784c752823f46c", final=True)

    traces = [p for p in s3.puts if p["Key"].startswith("traces/2026-10-05/Ben_Lin_Test/")]
    assert len(traces) == 1, "one trace object for the invocation"
    steps = [(e["step"], e["result"]) for e in events_of(traces[0])]
    assert ("llm_call", "ok") in steps
    assert ("extraction", "ok") in steps
    assert ("location_marker", "timed") in steps and ("location_marker", "minute_only") in steps
    ext = next(e for e in events_of(traces[0]) if e["step"] == "extraction")
    assert ext["detail"]["pass"] == "final" and ext["detail"]["markers_timed"] == 1


# ---- photo placement ---------------------------------------------------------------

def test_each_photo_says_where_it_went_and_why():
    topics = [{"time_range": "10:58 – 11:02"}, {"time_range": "11:02 – 11:03"},
              {"time_range": "13:00 – 13:05"}]
    photos = [{"key": k, "filename": k, "hhmm": t[:5], "hhmmss": t} for k, t in
              (("a", "11:02:08"), ("b", "13:02:00"), ("c", "13:20:00"), ("d", "16:00:00"))]
    markers = [{"at": "10:58", "location": "level one"},
               {"at": "11:02", "at_s": "11:02:14", "location": "level two"}]
    why = {}
    pb.photos_for_topics(photos, topics, markers=markers, explain=why)
    assert why == {"a": "location", "b": "clock", "c": "carried", "d": "unbound"}


def test_the_rebind_puts_every_photo_on_the_trace():
    pipeline_trace.begin("item-writer", user_folder="f", date="2026-10-05")
    day_topics = [{"id": "t1", "title": "Level 1", "time_range": "10:58 – 11:02"}]
    photos = [{"key": "a", "filename": "a.jpg", "hhmm": "11:00", "hhmmss": "11:00:05"},
              {"key": "z", "filename": "z.jpg", "hhmm": "16:00", "hhmmss": "16:00:00"}]
    photo_rebind._trace(photos, day_topics, {0: [photos[0]]}, {"a": "clock"}, [])
    s3 = Puts()
    pipeline_trace.flush(s3, "b")
    ev = events_of(s3.puts[0])
    assert [(e["step"], e["result"]) for e in ev] == [
        ("photo_placed", "clock"), ("photo_placed", "unbound"), ("photo_binding", "ok")]
    assert ev[0]["detail"]["topic"] == {"id": "t1", "title": "Level 1"}
    assert ev[2]["detail"]["clock"] == 1 and ev[2]["detail"]["unbound"] == 1


# ---- template reports and the glossary -----------------------------------------------

def test_a_report_records_its_template_and_each_checklist():
    import lambda_session_report as sr
    pipeline_trace.begin("session-report", user_folder="f", date="2026-10-05")
    sr._trace_report({"templateName": "Daily", "templateVersion": 3, "topicsOffered": 4,
                      "codeFilled": {"Weather": {}}, "photosPlaced": 9,
                      "checklists": {"Pre-pour": {"items": 6, "answered": 4,
                                                  "dropped": [{"item": "5", "reason": "no quote"}]},
                                     "Steel": {"items": 5, "answered": 0, "dropped": []}}})
    sr._trace_glossary({"topics": [{"title": "TIKAHA room"}]},
                       [{"wrong_term": "Tikaha", "right_term": "Te Kaha"},
                        {"wrong_term": "Jon", "right_term": "John"}])
    s3 = Puts()
    pipeline_trace.flush(s3, "b")
    got = [(e["step"], e["result"]) for e in events_of(s3.puts[0])]
    assert got == [("template_report", "ok"), ("checklist", "answered"),
                   ("checklist", "no_match"), ("glossary", "applied"), ("glossary", "no_match")]


def test_the_four_steps_may_write_their_traces():
    t = open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml"),
             encoding="utf-8").read()
    names = ["SessionReportFunction:", "OrgApiFunction:", "ExtractSessionFunction:",
             "ItemWriterFunction:"]
    for name in names:
        start = t.index("\n  " + name)
        end = t.find("\n  ", start + 3)
        nxt = min(i for i in (t.find("\n  " + n, start + 3) for n in
                              ["SessionReportFunction:", "AskAgentFunction:", "OrgApiFunction:",
                               "ExtractSessionFunction:", "RollingSummaryFunction:",
                               "ItemWriterFunction:", "KeyframeFunction:"]) if i > start)
        assert "/traces/*" in t[start:nxt], name


# ---- the handlers open and write the trace ----------------------------------------

def test_the_item_writer_writes_a_trace_per_extraction(monkeypatch):
    import lambda_item_writer as iw
    s3 = Puts()
    monkeypatch.setattr(iw, "s3", lambda: s3)
    monkeypatch.setattr(iw, "S3_BUCKET", "b")
    monkeypatch.setattr(iw, "write_extraction_items",
                        lambda date, folder, key: pipeline_trace.event("photo_binding") or {})
    key = "extractions/Ben_Lin_Test/2026-10-05/sid37168e5632af4360b3784c752823f46c.json"
    iw.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)
    assert [p["Key"].split("/")[:3] for p in s3.puts] == [["traces", "2026-10-05", "Ben_Lin_Test"]]
    assert events_of(s3.puts[0])[0]["session"] == "sid37168e5632af4360b3784c752823f46c"


def test_the_report_worker_writes_a_trace_per_request(monkeypatch):
    import lambda_session_report as sr
    artifact = {"folder": "Ben_Lin_Test", "date": "2026-10-05", "sessionId": "sidX",
                "companyId": "c-1", "resultKey": "r"}

    class Store(Puts):
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(json.dumps(artifact).encode())}
    s3 = Store()
    monkeypatch.setattr(sr, "s3", lambda: s3)
    monkeypatch.setattr(sr, "S3_BUCKET", "b")
    monkeypatch.setattr(sr, "process_request",
                        lambda a, c=None: pipeline_trace.event("template_report"))
    sr.lambda_handler({"Records": [{"s3": {"object": {"key": "session_reports/x.json"}}}]}, None)
    e = events_of(s3.puts[0])[0]
    assert s3.puts[0]["Key"].startswith("traces/2026-10-05/Ben_Lin_Test/")
    assert (e["company_id"], e["session"]) == ("c-1", "sidX")
