"""One model call, inside the worker, because org-api is in-VPC and cannot reach one.
Everything the model is given is already filtered: the actions come from extraction and
the excluded spans are gone from the transcript before the prompt is built."""
import datetime as dt
import json

import pytest

import lambda_session_report as sr


ARTIFACT = {
    "requestId": "r1", "folder": "Ben_UCPK2", "date": "2026-09-10",
    "sessionId": "sid" + "a" * 32, "resultKey": "session_report_results/x.json",
    "title": "Meeting Notes", "deliver": "download",
    "generate": {"templateId": "personal-meeting", "templateVersion": 3},
    "window": {"from": "09:00", "to": "11:30"},
    "excludedTopics": [],
    "content": {"date": "2026-09-10", "participants": ["Ben"], "topics": [
        {"topic_title": "Roofing", "summary": "Xtreme withdrew.", "time_range": "09:10 - 09:20",
         "action_items": [{"action": "Send roofing prices", "responsible": "Alex", "deadline": None}]},
    ]},
}

PROSE = ("### What this was\nBen's site meeting.\n\n"
         "### Actions\n- **Alex** - send roofing prices - *no date*\n")


@pytest.fixture
def wired(monkeypatch):
    calls = {}
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 9, 10, 9, 5), "transcripts/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble",
                        lambda *a, **k: [{"at": dt.datetime(2026, 9, 10, 9, 5),
                                          "line": "[09:05:00] Ben: roofing"}])

    def fake_call(prompt, **kw):
        calls["prompt"] = prompt
        calls["kw"] = kw
        return PROSE, None
    monkeypatch.setattr(sr.llm_utils, "call_llm", fake_call)
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "muse-spark-1.3")
    monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document",
                        lambda *a, **k: __import__("io").BytesIO(b"PK-docx"))
    written = []
    monkeypatch.setattr(sr, "_write_result", lambda key, payload: written.append(payload))
    monkeypatch.setattr(sr, "_put_document", lambda *a, **k: "session_reports/x.docx")
    monkeypatch.setattr(sr, "_session_was_deleted", lambda artifact: False)
    return calls, written


def test_a_request_that_names_a_template_is_generated_not_assembled(wired):
    calls, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "done"
    assert written[0]["generated"] is True
    assert written[0]["templateId"] == "personal-meeting" and written[0]["templateVersion"] == 3
    assert written[0]["model"] == "muse-spark-1.3"
    assert "### What this was" in calls["prompt"] or "What this was" in calls["prompt"]
    assert "Send roofing prices" in calls["prompt"] and "Alex" in calls["prompt"]


def test_the_model_call_is_bounded_so_it_cannot_outlive_the_function(wired):
    calls, _ = wired
    sr.process_request(dict(ARTIFACT))
    assert calls["kw"].get("deadline"), "an unbounded retry ladder outlives Timeout: 300"


def test_an_empty_answer_is_an_error_not_an_empty_report(wired, monkeypatch):
    _, written = wired
    monkeypatch.setattr(sr.llm_utils, "call_llm",
                        lambda prompt, **kw: (None, "empty answer from model (finish_reason=length)"))
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "error"
    assert "empty answer" in written[0]["error"]


def test_an_unplaceable_exclusion_stops_the_report(wired):
    _, written = wired
    art = dict(ARTIFACT, excludedTopics=[{"id": "t9", "time_range": "all morning"}])
    sr.process_request(art)
    assert written[0]["status"] == "error"
    assert "cannot be placed" in written[0]["error"]


def test_an_unplaceable_exclusion_outside_the_window_still_stops_the_report(wired):
    """The window-scoping fix only excuses a topic whose time_range PARSES and
    lies wholly outside the request's window. One that cannot be placed at all
    cannot be proven to lie outside it either, so it must still fail closed --
    window narrowing must never become a way to smuggle past this guard."""
    _, written = wired
    art = dict(ARTIFACT, excludedTopics=[{"id": "t9", "time_range": "all afternoon"}])
    sr.process_request(art)
    assert written[0]["status"] == "error"
    assert "cannot be placed" in written[0]["error"]


def test_an_excluded_topic_wholly_outside_the_window_does_not_abort_and_clips_nothing(
        wired, monkeypatch):
    """A day-scoped excludedTopics list can legitimately name a topic far outside
    THIS request's narrow window (e.g. a redacted 16:00 topic on a 09:00-11:30
    request). Its time_range parses fine, it just does not overlap the window, so
    it must be dropped before the fail-closed check rather than aborting the
    request -- and it must not clip anything either, since it never applied here.

    The 16:05 turn stands in for the edge case that makes this an observable
    behaviour, not just a request that happens to succeed either way: a picked
    recording can straddle the window and still carry turns past it (assemble
    does not itself clip to the window), so without the window-scoping fix the
    16:00-16:20 exclusion would still reach `drop_spans` and cut it, even though
    it has nothing to do with this request's window."""
    calls, written = wired
    turns = [{"at": dt.datetime(2026, 9, 10, 9, 5), "until": dt.datetime(2026, 9, 10, 9, 5),
             "line": "[09:05:00] Ben: roofing"},
             {"at": dt.datetime(2026, 9, 10, 16, 5), "until": dt.datetime(2026, 9, 10, 16, 5),
             "line": "[16:05:00] Ben: unrelated afternoon remark"}]
    monkeypatch.setattr(sr.transcript_window, "assemble", lambda *a, **k: list(turns))
    art = dict(ARTIFACT, excludedTopics=[{"id": "t9", "time_range": "16:00 - 16:20"}])
    sr.process_request(art)
    assert written[0]["status"] == "done"
    assert "roofing" in calls["prompt"]
    assert "unrelated afternoon remark" in calls["prompt"]


# ----------------------------------------------------------
# CRITICAL 2 — only the model call was time-bounded; select_keys/assemble ran
# unbounded before it. A fake Lambda context whose remaining time is nearly
# exhausted must fail closed with a clear reason and must never reach the model.
# ----------------------------------------------------------

class _FakeContext:
    def __init__(self, remaining_ms):
        self._ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._ms


def test_a_nearly_exhausted_context_errors_out_before_calling_the_model(wired):
    calls, written = wired
    # 5s left on the invocation, and the reserve for render+write alone is 30s --
    # there is nothing left for either the read phase or the model call.
    sr.process_request(dict(ARTIFACT), _FakeContext(5000))
    assert written[0]["status"] == "error"
    assert "budget" in written[0]["error"]
    assert "prompt" not in calls, "the model must never be called with an exhausted budget"


def test_a_context_with_ample_time_still_generates(wired):
    calls, written = wired
    sr.process_request(dict(ARTIFACT), _FakeContext(120000))
    assert written[0]["status"] == "done"
    assert calls["kw"].get("deadline"), "the model call must still carry a bounded deadline"


def test_without_a_context_the_fallback_budget_still_bounds_the_model_call(wired):
    """No context (e.g. a direct unit-test call, or process_request's own default) --
    falls back to the fixed GENERATION_BUDGET_SECONDS exactly as before."""
    calls, written = wired
    sr.process_request(dict(ARTIFACT))
    assert written[0]["status"] == "done"
    assert calls["kw"]["deadline"] == sr.GENERATION_BUDGET_SECONDS


def test_a_request_without_generate_still_assembles_exactly_as_before(wired, monkeypatch):
    _, written = wired
    seen = {}
    # NOTE: the file imports this the old way -- `from lambda_meeting_minutes import
    # generate_word_document` -- so every pre-existing worker test patches the bare
    # name `sr.generate_word_document`, not `sr.lambda_meeting_minutes.*`. Patching
    # the module attribute here would silently miss and call the real renderer.
    monkeypatch.setattr(sr, "generate_word_document",
                        lambda data, title: seen.setdefault("data", data) and __import__("io").BytesIO(b"PK"))
    # This path (unlike the generate path above, whose S3 write is mocked via
    # `_put_document`) still does its own real s3().put_object -- fake it the same
    # way test_session_report_worker.py's fake_s3 fixture does.
    class _S3:
        def put_object(self, Bucket, Key, Body, ContentType=None):
            return {}
    monkeypatch.setattr(sr, "s3", lambda: _S3())
    art = dict(ARTIFACT)
    art.pop("generate")
    sr.process_request(art)
    assert written[0]["status"] == "done"
    assert "generated" not in written[0]
    assert seen["data"]["topics"], "the old path still renders from topics"
