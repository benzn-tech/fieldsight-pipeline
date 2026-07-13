"""
Tests for src/lambda_programme_matcher.py — Task 3 of the programme<->item
feedback plan (TDD):

  docs/superpowers/specs/2026-07-12-programme-item-feedback-design.md (S5)
  docs/superpowers/plans/2026-07-12-programme-item-feedback.md (Task 3)

Pure-function tests (candidate_tasks / rank_by_embedding / parse_verdict)
need zero I/O. Handler tests monkeypatch S3, dashscope_utils.embed,
claude_utils.call_claude, repositories.programme.read_programme, and the
lambda-invoke client -- style of tests/unit/test_lambda_extract_session.py
(FakeS3 double, dummy AWS env vars so an eager boto3.client('s3') at import
time never blows up) and tests/unit/test_lambda_suggestion_writer.py
(monkeypatching a module attribute, e.g. `sw.programme_suggestions.x`).
"""
import io
import json
import os
from datetime import date

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("DASHSCOPE_API_KEY", "dashscope-test-dummy-key")

lpm = pytest.importorskip("lambda_programme_matcher", reason="requires boto3/psycopg (installed in CI)")
import claude_utils  # noqa: E402  (import after importorskip, same module the handler calls)

BUCKET = "test-bucket"


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

def _task(**overrides):
    t = dict(task_id="T-001", parent_id="P-1", name="Steel frame",
              start="2026-04-01", end="2026-04-10")
    t.update(overrides)
    return t


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}


class FakeLambdaClient:
    def __init__(self, response_payload=None, function_error=None):
        self.invoke_calls = []
        self.response_payload = response_payload if response_payload is not None else {"written": 0}
        self.function_error = function_error

    def invoke(self, **kwargs):
        self.invoke_calls.append(kwargs)
        resp = {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def _match_request_event(key):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


def _clean_match_setup(monkeypatch, confidence=0.9, suggested_progress=100,
                        progress_before=40, status_before="in_progress",
                        findings=None):
    """Wires a full clean-match handler scenario: one topic, one candidate
    task, embeddings that put them at cosine distance 0, and a Claude
    verdict picking that task. Returns (req_key, fake_lambda) so a test can
    tweak `confidence`/etc. via the args and inspect the writer invoke.

    `findings` (2026-07-13 plan, Task 4): when given, added as the topic's
    "findings" list (mirrors the item-writer artifact contract); when None
    (default), the topic has NO "findings" key at all, same as every
    pre-Task-4 artifact and every report-path artifact -- exercises the
    `.get(..., [])` backward-compat no-op."""
    req_key = "match_requests/site-1/2026-07-12/abc123.json"
    topic = {
        "topic_id": "topic-1", "title": "Steel frame progress",
        "summary": "Crew finished the steel frame today.",
        "user_id": "user-1", "action_items": [{"text": "close out steel frame"}],
    }
    if findings is not None:
        topic["findings"] = findings
    req = {
        "site_id": "site-1", "report_date": "2026-07-12",
        "source_s3_key": "extractions/Benl1/2026-07-12/sess.json",
        "topics": [topic],
    }
    fake_s3 = FakeS3({req_key: json.dumps(req)})
    monkeypatch.setattr(lpm, "s3", lambda: fake_s3)
    monkeypatch.setattr(lpm, "S3_BUCKET", BUCKET)

    programme_doc = {
        "leaves": [
            _task(task_id="T-1", name="Steel frame", start="2026-07-01", end="2026-07-15",
                  status=status_before, progress_pct=progress_before),
        ],
        "updated_at": "2026-07-10T00:00:00Z",
    }
    monkeypatch.setattr(lpm.programme, "read_programme", lambda *a, **k: programme_doc)
    monkeypatch.setattr(lpm.dashscope_utils, "embed", lambda texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.setattr(
        claude_utils, "call_claude",
        lambda prompt, max_tokens=512: (
            json.dumps({
                "task_id": "T-1", "confidence": confidence,
                "suggested_status": "completed", "suggested_progress": suggested_progress,
                "evidence": "finished the steel frame",
            }),
            None,
        ),
    )
    fake_lambda = FakeLambdaClient()
    monkeypatch.setattr(lpm, "lambda_client", lambda: fake_lambda)
    return req_key, fake_lambda


# ---------------------------------------------------------------------------
# candidate_tasks — deterministic hard gate (spec S5 step 2)
# ---------------------------------------------------------------------------

def test_candidate_window_includes_slipped_task():
    doc = {"leaves": [_task(task_id="T-1", start="2026-04-01", end="2026-04-10")]}
    # 12 days after end -- still within the default LAG_DAYS=14 (task ran
    # late, still discussed after its planned end).
    result = lpm.candidate_tasks(doc, date(2026, 4, 22), lead_days=7, lag_days=14)
    assert [t["task_id"] for t in result] == ["T-1"]

    # 20 days after end -- outside the lag window.
    result2 = lpm.candidate_tasks(doc, date(2026, 4, 30), lead_days=7, lag_days=14)
    assert result2 == []

    # 5 days BEFORE start -- within the default LEAD_DAYS=7 (work started early).
    result3 = lpm.candidate_tasks(doc, date(2026, 3, 27), lead_days=7, lag_days=14)
    assert [t["task_id"] for t in result3] == ["T-1"]


def test_candidate_missing_status_is_candidate():
    task = _task(task_id="T-2", start="2026-04-01", end="2026-04-10")
    assert "status" not in task
    doc = {"leaves": [task]}
    result = lpm.candidate_tasks(doc, date(2026, 4, 5))
    assert [t["task_id"] for t in result] == ["T-2"]


def test_candidate_missing_end_is_ongoing():
    task = _task(task_id="T-3", start="2026-01-01")
    del task["end"]
    doc = {"leaves": [task]}
    # far-future report_date -- still a candidate because a missing `end`
    # opens the window to +infinity (task is ongoing/open-ended).
    result = lpm.candidate_tasks(doc, date(2027, 1, 1))
    assert [t["task_id"] for t in result] == ["T-3"]


def test_candidate_excludes_completed():
    doc = {"leaves": [
        _task(task_id="T-4", status="completed", start="2026-04-01", end="2026-04-10"),
        _task(task_id="T-5", status="group", start="2026-04-01", end="2026-04-10"),
        _task(task_id="T-6", status="in_progress", start="2026-04-01", end="2026-04-10"),
    ]}
    result = lpm.candidate_tasks(doc, date(2026, 4, 5))
    assert [t["task_id"] for t in result] == ["T-6"]


def test_candidate_report_date_as_string_is_coerced():
    doc = {"leaves": [_task(task_id="T-7", start="2026-04-01", end="2026-04-10")]}
    result = lpm.candidate_tasks(doc, "2026-04-05")
    assert [t["task_id"] for t in result] == ["T-7"]


def test_candidate_skips_malformed_leaf_date_keeps_good_leaf():
    # Fable review MINOR #7: one leaf with an unparseable start/end must
    # not abort the whole loop (date.fromisoformat raising ValueError used
    # to crash candidate_tasks entirely -- failing every future artifact
    # for that site, not just the one bad leaf).
    doc = {"leaves": [
        _task(task_id="T-good", start="2026-04-01", end="2026-04-10"),
        _task(task_id="T-bad", start="TBC", end="2026-04-10"),
    ]}
    result = lpm.candidate_tasks(doc, date(2026, 4, 5))
    assert [t["task_id"] for t in result] == ["T-good"]


def test_candidate_skips_malformed_end_date_too():
    doc = {"leaves": [
        _task(task_id="T-good", start="2026-04-01", end="2026-04-10"),
        _task(task_id="T-bad", start="2026-04-01", end="unknown"),
    ]}
    result = lpm.candidate_tasks(doc, date(2026, 4, 5))
    assert [t["task_id"] for t in result] == ["T-good"]


# ---------------------------------------------------------------------------
# rank_by_embedding — cosine distance floor + top_k trim
# ---------------------------------------------------------------------------

def test_rank_drops_beyond_max_dist_and_top_k():
    topic_vec = [1.0, 0.0, 0.0]
    tasks = [_task(task_id="T-A"), _task(task_id="T-B"), _task(task_id="T-C"),
             _task(task_id="T-D"), _task(task_id="T-E")]
    vecs = [
        [1.0, 0.0, 0.0],   # T-A: identical direction -> dist 0
        [1.0, 1.0, 0.0],   # T-B: dist ~0.293 (within 0.55)
        [0.0, 1.0, 0.0],   # T-C: orthogonal -> dist 1.0 (dropped, > 0.55)
        [1.0, 3.0, 0.0],   # T-D: dist ~0.684 (dropped, > 0.55)
        [2.0, 0.0, 0.0],   # T-E: same direction, different magnitude -> dist 0
    ]
    result = lpm.rank_by_embedding(topic_vec, tasks, vecs, max_dist=0.55, top_k=2)
    # T-A and T-E tie at distance 0 (cosine ignores magnitude); T-B survives
    # the distance filter (0.293 <= 0.55) but is trimmed by top_k=2.
    assert [t["task_id"] for t in result] == ["T-A", "T-E"]


def test_rank_empty_when_all_beyond_max_dist():
    topic_vec = [1.0, 0.0]
    tasks = [_task(task_id="T-1")]
    vecs = [[0.0, 1.0]]  # orthogonal -> dist 1.0
    result = lpm.rank_by_embedding(topic_vec, tasks, vecs, max_dist=0.55, top_k=5)
    assert result == []


# ---------------------------------------------------------------------------
# parse_verdict — double-gate accept (survivor set AND confidence floor)
# ---------------------------------------------------------------------------

def test_parse_verdict_accepts_valid():
    raw = json.dumps({"task_id": "T-1", "confidence": 0.9, "suggested_status": "completed",
                       "suggested_progress": 100, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1", "T-2"}, conf_min=0.70)
    assert verdict is not None
    assert verdict["task_id"] == "T-1"
    assert verdict["confidence"] == 0.9


def test_parse_verdict_none_when_pick_not_survivor():
    raw = json.dumps({"task_id": "T-99", "confidence": 0.95, "suggested_status": "completed",
                       "suggested_progress": None, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1", "T-2"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_none_below_confidence():
    raw = json.dumps({"task_id": "T-1", "confidence": 0.5, "suggested_status": "in_progress",
                       "suggested_progress": None, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_none_when_task_id_null():
    # The correct, fail-closed answer: null task_id is NOT an error, just no match.
    raw = json.dumps({"task_id": None, "confidence": 0.95, "suggested_status": None,
                       "suggested_progress": None, "evidence": "no clear match"})
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_none_when_unparseable():
    verdict = lpm.parse_verdict("not json at all {{{", {"T-1"}, conf_min=0.70)
    assert verdict is None


# ---------------------------------------------------------------------------
# parse_verdict — confidence gate must reject NaN/Infinity/out-of-range
# (Fable review MINOR #6: `float('nan') < CONF_MIN` is False, so the old
# one-sided `confidence < conf_min` check let NaN straight through, and let
# any confidence > 1.0 through too since only the lower bound was checked.)
# ---------------------------------------------------------------------------
def test_parse_verdict_none_when_confidence_nan():
    raw = ('{"task_id": "T-1", "confidence": NaN, "suggested_status": "completed", '
           '"suggested_progress": null, "evidence": "e"}')
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_none_when_confidence_above_one():
    raw = json.dumps({"task_id": "T-1", "confidence": 5.0, "suggested_status": "completed",
                       "suggested_progress": None, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_none_when_confidence_non_numeric_string():
    raw = json.dumps({"task_id": "T-1", "confidence": "high", "suggested_status": "completed",
                       "suggested_progress": None, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is None


def test_parse_verdict_accepts_in_range_confidence():
    raw = json.dumps({"task_id": "T-1", "confidence": 0.8, "suggested_status": "completed",
                       "suggested_progress": None, "evidence": "e"})
    verdict = lpm.parse_verdict(raw, {"T-1"}, conf_min=0.70)
    assert verdict is not None
    assert verdict["confidence"] == 0.8


# ---------------------------------------------------------------------------
# _coerce_suggested_progress — same whitelist treatment as suggested_status
# (Fable review IMPORTANT #3: an unchecked suggested_progress reaching the
# writer's single-transaction batch insert would abort the WHOLE batch on
# the migration's CHECK (suggested_progress BETWEEN 0 AND 100) constraint.)
# ---------------------------------------------------------------------------
def test_coerce_suggested_progress_out_of_range_to_none():
    assert lpm._coerce_suggested_progress(105) is None
    assert lpm._coerce_suggested_progress(-1) is None


def test_coerce_suggested_progress_non_numeric_to_none():
    assert lpm._coerce_suggested_progress("about half") is None


def test_coerce_suggested_progress_valid_int_passthrough():
    assert lpm._coerce_suggested_progress(60) == 60


def test_coerce_suggested_progress_whole_float_coerced():
    assert lpm._coerce_suggested_progress(60.0) == 60


def test_coerce_suggested_progress_fractional_float_to_none():
    assert lpm._coerce_suggested_progress(60.5) is None


def test_coerce_suggested_progress_none_passthrough():
    assert lpm._coerce_suggested_progress(None) is None


def test_coerce_suggested_progress_bool_to_none():
    # bool is a subclass of int in Python -- must not sneak through as 0/1.
    assert lpm._coerce_suggested_progress(True) is None


# ---------------------------------------------------------------------------
# Handler — an invalid suggested_progress is coerced to None; if
# suggested_status ends up None too, the existing "real change" gate still
# drops the whole suggestion (verifies #3 composes correctly with the
# pre-existing real-change logic, not just the pure coercion helper).
# ---------------------------------------------------------------------------
def test_handler_invalid_progress_coerced_and_no_status_drops_suggestion(monkeypatch):
    req_key, fake_lambda = _clean_match_setup(monkeypatch)
    monkeypatch.setattr(
        claude_utils, "call_claude",
        lambda prompt, max_tokens=512: (
            json.dumps({
                "task_id": "T-1", "confidence": 0.9,
                "suggested_status": None, "suggested_progress": 105,
                "evidence": "e",
            }),
            None,
        ),
    )
    result = lpm.lambda_handler(_match_request_event(req_key), None)
    assert result["suggestions"] == []
    assert fake_lambda.invoke_calls == []


def test_handler_invalid_progress_coerced_valid_status_still_suggests(monkeypatch):
    # suggested_progress=105 is invalid -> coerced to None, but
    # suggested_status="completed" differs from status_before -> still a
    # real change, so a suggestion IS produced, just without a progress value.
    req_key, fake_lambda = _clean_match_setup(monkeypatch)
    monkeypatch.setattr(
        claude_utils, "call_claude",
        lambda prompt, max_tokens=512: (
            json.dumps({
                "task_id": "T-1", "confidence": 0.9,
                "suggested_status": "completed", "suggested_progress": 105,
                "evidence": "e",
            }),
            None,
        ),
    )
    result = lpm.lambda_handler(_match_request_event(req_key), None)
    assert len(result["suggestions"]) == 1
    assert result["suggestions"][0]["suggested_status"] == "completed"
    assert result["suggestions"][0]["suggested_progress"] is None


# ---------------------------------------------------------------------------
# build_prompt — Fable review MINOR #8: the Date line must reflect the
# request's report_date (topics never carry their own date/report_date key
# in the match_requests contract, so the old `topic.get('date') or
# topic.get('report_date')` was always empty), and candidate lines should
# surface progress_pct/assignees when present (defensive .get -- both are
# frequently absent on a real programme leaf).
# ---------------------------------------------------------------------------
def test_build_prompt_includes_report_date():
    topic = {"title": "t", "summary": "s", "action_items": [], "report_date": "2026-07-12"}
    prompt = lpm.build_prompt(topic, [_task(task_id="T-1")])
    assert "Date: 2026-07-12" in prompt


def test_build_prompt_candidate_line_includes_progress_and_assignees():
    candidate = _task(task_id="T-1", name="Steel frame", status="in_progress",
                      progress_pct=40, assignees=["Ben", "Sam"])
    prompt = lpm.build_prompt({"title": "t", "summary": "s"}, [candidate])
    assert "40" in prompt
    assert "Ben" in prompt and "Sam" in prompt


def test_build_prompt_candidate_line_defensive_when_absent():
    # progress_pct/assignees are OPTIONAL on a real programme leaf -- must
    # not KeyError when absent.
    candidate = _task(task_id="T-1", name="Steel frame")
    assert "progress_pct" not in candidate and "assignees" not in candidate
    prompt = lpm.build_prompt({"title": "t", "summary": "s"}, [candidate])
    assert "T-1" in prompt


def test_process_topic_injects_report_date_into_prompt(monkeypatch):
    # Handler-level: _process_topic must pass the request's report_date
    # through to build_prompt even though the topic dict itself never
    # carries one.
    req_key, fake_lambda = _clean_match_setup(monkeypatch)
    captured = {}
    real_build_prompt = lpm.build_prompt

    def spying_build_prompt(topic, candidates):
        captured["topic"] = topic
        return real_build_prompt(topic, candidates)

    monkeypatch.setattr(lpm, "build_prompt", spying_build_prompt)
    lpm.lambda_handler(_match_request_event(req_key), None)
    assert captured["topic"].get("report_date") == "2026-07-12"


# ---------------------------------------------------------------------------
# Handler — clean match produces exactly one writer suggestion
# ---------------------------------------------------------------------------

def test_handler_clean_match_produces_one_suggestion(monkeypatch):
    req_key, fake_lambda = _clean_match_setup(monkeypatch)

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert len(result["suggestions"]) == 1
    s = result["suggestions"][0]
    assert s["site_id"] == "site-1"
    assert s["task_id"] == "T-1"
    assert s["topic_id"] == "topic-1"
    assert s["topic_title"] == "Steel frame progress"
    assert s["report_date"] == "2026-07-12"
    assert s["source_s3_key"] == "extractions/Benl1/2026-07-12/sess.json"
    assert s["task_name"] == "Steel frame"
    assert s["task_status_before"] == "in_progress"
    assert s["task_progress_before"] == 40
    assert s["suggested_status"] == "completed"
    assert s["suggested_progress"] == 100
    assert s["confidence"] == 0.9
    assert s["match_evidence"]["programme_updated_at"] == "2026-07-10T00:00:00Z"
    assert s["match_evidence"]["assignee_overlap"] is None

    assert len(fake_lambda.invoke_calls) == 1
    sent = json.loads(fake_lambda.invoke_calls[0]["Payload"])
    assert sent == {"suggestions": result["suggestions"], "impacts": result["impacts"]}


def test_handler_below_threshold_produces_zero(monkeypatch):
    req_key, fake_lambda = _clean_match_setup(monkeypatch, confidence=0.5)

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert result["suggestions"] == []
    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# Handler — dry_run returns would-be suggestions without invoking the writer
# ---------------------------------------------------------------------------

def test_handler_dry_run_skips_writer_invoke(monkeypatch):
    req_key, fake_lambda = _clean_match_setup(monkeypatch)

    event = _match_request_event(req_key)
    event["dry_run"] = True
    result = lpm.lambda_handler(event, None)

    assert len(result["suggestions"]) == 1
    assert result["dry_run"] is True
    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# Handler — no programme / no leaves -> skip topic, no exception
# ---------------------------------------------------------------------------

def test_handler_missing_programme_skips_topic(monkeypatch):
    req_key = "match_requests/site-1/2026-07-12/abc123.json"
    req = {
        "site_id": "site-1", "report_date": "2026-07-12",
        "source_s3_key": "extractions/Benl1/2026-07-12/sess.json",
        "topics": [{"topic_id": "topic-1", "title": "t", "summary": "s",
                     "user_id": None, "action_items": []}],
    }
    fake_s3 = FakeS3({req_key: json.dumps(req)})
    monkeypatch.setattr(lpm, "s3", lambda: fake_s3)
    monkeypatch.setattr(lpm, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(lpm.programme, "read_programme", lambda *a, **k: None)

    def fail_if_called(*a, **k):
        raise AssertionError("must not embed/call Claude when there's no programme")

    monkeypatch.setattr(lpm.dashscope_utils, "embed", fail_if_called)
    monkeypatch.setattr(claude_utils, "call_claude", fail_if_called)
    fake_lambda = FakeLambdaClient()
    monkeypatch.setattr(lpm, "lambda_client", lambda: fake_lambda)

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert result["suggestions"] == []
    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# Handler — fail-closed: an embed/Claude failure propagates, writes nothing
# ---------------------------------------------------------------------------

def test_handler_embed_failure_raises_and_writes_nothing(monkeypatch):
    req_key = "match_requests/site-1/2026-07-12/abc123.json"
    req = {
        "site_id": "site-1", "report_date": "2026-07-12",
        "source_s3_key": "extractions/Benl1/2026-07-12/sess.json",
        "topics": [{"topic_id": "topic-1", "title": "Steel frame progress",
                     "summary": "s", "user_id": None, "action_items": []}],
    }
    fake_s3 = FakeS3({req_key: json.dumps(req)})
    monkeypatch.setattr(lpm, "s3", lambda: fake_s3)
    monkeypatch.setattr(lpm, "S3_BUCKET", BUCKET)
    programme_doc = {"leaves": [_task(task_id="T-1", start="2026-07-01", end="2026-07-15")]}
    monkeypatch.setattr(lpm.programme, "read_programme", lambda *a, **k: programme_doc)

    def boom(texts):
        raise RuntimeError("DashScope embed request failed after 4 attempts")

    monkeypatch.setattr(lpm.dashscope_utils, "embed", boom)
    fake_lambda = FakeLambdaClient()
    monkeypatch.setattr(lpm, "lambda_client", lambda: fake_lambda)

    with pytest.raises(RuntimeError, match="DashScope"):
        lpm.lambda_handler(_match_request_event(req_key), None)

    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# Handler — a progress decrease is never a "real change"; a verdict that is
# neither a status change nor a genuine progress increase yields no suggestion.
# ---------------------------------------------------------------------------

def test_handler_progress_decrease_not_real_change(monkeypatch):
    # suggested_status == current status ("in_progress"); suggested_progress
    # (30) is LOWER than task_progress_before (80) -- not a real change either.
    req_key, fake_lambda = _clean_match_setup(
        monkeypatch, suggested_progress=30, progress_before=80,
    )
    # Override the Claude stub to also propose the SAME status as current.
    monkeypatch.setattr(
        claude_utils, "call_claude",
        lambda prompt, max_tokens=512: (
            json.dumps({
                "task_id": "T-1", "confidence": 0.9,
                "suggested_status": "in_progress", "suggested_progress": 30,
                "evidence": "e",
            }),
            None,
        ),
    )

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert result["suggestions"] == []
    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# build_impact_prompt / parse_impact_verdicts -- 2026-07-13 plan, Task 4
# (programme-impact-link: finding -> task via a per-finding double-gate).
# ---------------------------------------------------------------------------

def test_impact_prompt_lists_findings_with_severity_prior():
    topic = {"title": "Steel frame progress", "summary": "Crew finished the steel frame today."}
    findings = [{
        "finding_id": "F-1", "observation": "Delayed delivery of steel beams",
        "domain": "progress", "severity": "major",
        "entity_name": "SteelCo", "entity_trade": "Steel",
    }]
    candidates = [_task(task_id="T-1", name="Steel frame")]

    prompt = lpm.build_impact_prompt(topic, findings, candidates)

    assert "F-1" in prompt
    assert "Delayed delivery of steel beams" in prompt
    assert "major" in prompt
    assert "your prior" in prompt  # severity labelled as extraction-stage prior
    assert "SteelCo" in prompt
    assert "Steel" in prompt
    assert "T-1" in prompt


def test_impact_verdict_rejects_nonsurvivor_for_that_finding():
    # Finding A's own survivor set is {T-1}; finding B's is {T-2}. Claude
    # picks T-2 for BOTH -- A's pick must be rejected (T-2 isn't in A's
    # survivor set, even though it IS a valid pick for B).
    raw = json.dumps({"impacts": [
        {"finding_id": "F-A", "task_id": "T-2", "impact_severity": "major",
         "note": "n", "confidence": 0.9},
        {"finding_id": "F-B", "task_id": "T-2", "impact_severity": "minor",
         "note": "n", "confidence": 0.9},
    ]})
    survivor_ids_by_finding = {"F-A": {"T-1"}, "F-B": {"T-2"}}
    finding_severity_by_id = {"F-A": "minor", "F-B": "none"}

    result = lpm.parse_impact_verdicts(raw, survivor_ids_by_finding, finding_severity_by_id, conf_min=0.70)

    assert [r["finding_id"] for r in result] == ["F-B"]
    assert result[0]["task_id"] == "T-2"


def test_impact_verdict_bad_severity_falls_back_to_finding_severity():
    raw = json.dumps({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "catastrophic",
         "note": "n", "confidence": 0.9},
    ]})
    result = lpm.parse_impact_verdicts(raw, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70)
    assert result[0]["impact_severity"] == "minor"

    # Missing impact_severity entirely also falls back.
    raw2 = json.dumps({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "note": "n", "confidence": 0.9},
    ]})
    result2 = lpm.parse_impact_verdicts(raw2, {"F-1": {"T-1"}}, {"F-1": "major"}, conf_min=0.70)
    assert result2[0]["impact_severity"] == "major"


def test_impact_verdict_confidence_gate_nan_and_range():
    # NaN -- every comparison with NaN is False, so a one-sided
    # `confidence < conf_min` check would let it through; the two-sided
    # range guard (copied from parse_verdict) must not.
    raw_nan = ('{"impacts": [{"finding_id": "F-1", "task_id": "T-1", '
               '"impact_severity": "minor", "note": "n", "confidence": NaN}]}')
    assert lpm.parse_impact_verdicts(raw_nan, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70) == []

    raw_high = json.dumps({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "minor",
         "note": "n", "confidence": 5.0},
    ]})
    assert lpm.parse_impact_verdicts(raw_high, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70) == []

    raw_low = json.dumps({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "minor",
         "note": "n", "confidence": 0.1},
    ]})
    assert lpm.parse_impact_verdicts(raw_low, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70) == []

    raw_ok = json.dumps({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "minor",
         "note": "n", "confidence": 0.85},
    ]})
    result = lpm.parse_impact_verdicts(raw_ok, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70)
    assert len(result) == 1
    assert result[0]["confidence"] == 0.85


def test_impact_verdict_unknown_finding_id_dropped():
    # A finding_id Claude invented (or one that had zero embedding
    # survivors and so was never in the prompt) drops just that element --
    # the OTHER, valid element in the same batch must still be accepted.
    raw = json.dumps({"impacts": [
        {"finding_id": "F-UNKNOWN", "task_id": "T-1", "impact_severity": "minor",
         "note": "n", "confidence": 0.9},
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "minor",
         "note": "n", "confidence": 0.9},
    ]})
    result = lpm.parse_impact_verdicts(raw, {"F-1": {"T-1"}}, {"F-1": "minor"}, conf_min=0.70)
    assert [r["finding_id"] for r in result] == ["F-1"]


# ---------------------------------------------------------------------------
# Handler -- impact phase adapter (2026-07-13 plan, Task 4).
# ---------------------------------------------------------------------------

def _dispatch_claude(impact_response):
    """A call_claude stub that distinguishes the suggestion-phase prompt
    from the impact-phase prompt by content: build_impact_prompt's finding
    lines always contain "finding_id=", never present in build_prompt."""
    def _dispatch(prompt, max_tokens=512):
        if "finding_id=" in prompt:
            return json.dumps(impact_response), None
        return json.dumps({
            "task_id": "T-1", "confidence": 0.9,
            "suggested_status": "completed", "suggested_progress": 100,
            "evidence": "finished the steel frame",
        }), None
    return _dispatch


def test_handler_collects_impacts_and_invokes_writer_once(monkeypatch):
    findings = [{
        "finding_id": "F-1", "observation": "Steel delivery delayed",
        "domain": "progress", "severity": "major",
        "entity_name": "SteelCo", "entity_trade": "Steel",
    }]
    req_key, fake_lambda = _clean_match_setup(monkeypatch, findings=findings)
    monkeypatch.setattr(claude_utils, "call_claude", _dispatch_claude({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "major",
         "note": "steel delayed", "confidence": 0.9},
    ]}))

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert len(result["suggestions"]) == 1
    assert len(result["impacts"]) == 1
    impact = result["impacts"][0]
    assert impact["finding_id"] == "F-1"
    assert impact["task_id"] == "T-1"
    assert impact["impact_severity"] == "major"
    assert impact["impact_note"] == "steel delayed"
    assert impact["impact_task_name"] == "Steel frame"
    assert impact["impact_evidence"]["finding_severity"] == "major"
    assert impact["impact_evidence"]["llm_confidence"] == 0.9
    assert impact["impact_evidence"]["programme_updated_at"] == "2026-07-10T00:00:00Z"

    # ONE writer invoke carrying BOTH keys, after everything is processed.
    assert len(fake_lambda.invoke_calls) == 1
    sent = json.loads(fake_lambda.invoke_calls[0]["Payload"])
    assert sent == {"suggestions": result["suggestions"], "impacts": result["impacts"]}


def test_report_artifact_without_findings_skips_impact_phase(monkeypatch):
    # No `findings` kwarg -> the topic has no "findings" key at all, same
    # as a report-path artifact or a pre-Task-4 legacy artifact.
    req_key, fake_lambda = _clean_match_setup(monkeypatch)
    call_count = {"n": 0}

    def counting_claude(prompt, max_tokens=512):
        call_count["n"] += 1
        return json.dumps({
            "task_id": "T-1", "confidence": 0.9,
            "suggested_status": "completed", "suggested_progress": 100,
            "evidence": "finished the steel frame",
        }), None

    monkeypatch.setattr(claude_utils, "call_claude", counting_claude)

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    assert result["impacts"] == []
    assert len(result["suggestions"]) == 1
    # Only the suggestion-phase call happened -- the impact phase no-op'd
    # BEFORE ever calling Claude a second time.
    assert call_count["n"] == 1


def test_zero_survivor_finding_excluded_from_claude_call(monkeypatch):
    findings = [
        {"finding_id": "F-close", "observation": "close obs", "domain": "progress",
         "severity": "minor", "entity_name": "A", "entity_trade": "B"},
        {"finding_id": "F-far", "observation": "far obs", "domain": "progress",
         "severity": "minor", "entity_name": "A", "entity_trade": "B"},
    ]
    req_key, fake_lambda = _clean_match_setup(monkeypatch, findings=findings)

    # T-1's candidate name is "Steel frame"; give the far finding an
    # orthogonal vector (cosine distance 1.0, > SIM_MAX_DIST=0.55) so it has
    # ZERO survivors, while everything else (topic, candidate, close
    # finding) stays at distance 0 via a shared vector.
    def fake_embed(texts):
        return [[0.0, 1.0] if t == "far obs" else [1.0, 0.0] for t in texts]

    monkeypatch.setattr(lpm.dashscope_utils, "embed", fake_embed)

    captured_prompts = []

    def dispatch_and_capture(prompt, max_tokens=512):
        captured_prompts.append(prompt)
        if "finding_id=" in prompt:
            return json.dumps({"impacts": [
                {"finding_id": "F-close", "task_id": "T-1", "impact_severity": "minor",
                 "note": "n", "confidence": 0.9},
            ]}), None
        return json.dumps({
            "task_id": "T-1", "confidence": 0.9,
            "suggested_status": "completed", "suggested_progress": 100,
            "evidence": "finished the steel frame",
        }), None

    monkeypatch.setattr(claude_utils, "call_claude", dispatch_and_capture)

    result = lpm.lambda_handler(_match_request_event(req_key), None)

    impact_prompts = [p for p in captured_prompts if "finding_id=" in p]
    assert len(impact_prompts) == 1
    assert "F-close" in impact_prompts[0]
    assert "F-far" not in impact_prompts[0]
    assert [i["finding_id"] for i in result["impacts"]] == ["F-close"]


def test_dry_run_returns_impacts_without_invoke(monkeypatch):
    findings = [{
        "finding_id": "F-1", "observation": "Steel delivery delayed",
        "domain": "progress", "severity": "major",
        "entity_name": "SteelCo", "entity_trade": "Steel",
    }]
    req_key, fake_lambda = _clean_match_setup(monkeypatch, findings=findings)
    monkeypatch.setattr(claude_utils, "call_claude", _dispatch_claude({"impacts": [
        {"finding_id": "F-1", "task_id": "T-1", "impact_severity": "major",
         "note": "n", "confidence": 0.9},
    ]}))

    event = _match_request_event(req_key)
    event["dry_run"] = True
    result = lpm.lambda_handler(event, None)

    assert result["dry_run"] is True
    assert len(result["suggestions"]) == 1
    assert len(result["impacts"]) == 1
    assert fake_lambda.invoke_calls == []
