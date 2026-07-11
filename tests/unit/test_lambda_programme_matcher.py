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
                        progress_before=40, status_before="in_progress"):
    """Wires a full clean-match handler scenario: one topic, one candidate
    task, embeddings that put them at cosine distance 0, and a Claude
    verdict picking that task. Returns (req_key, fake_lambda) so a test can
    tweak `confidence`/etc. via the args and inspect the writer invoke."""
    req_key = "match_requests/site-1/2026-07-12/abc123.json"
    req = {
        "site_id": "site-1", "report_date": "2026-07-12",
        "source_s3_key": "extractions/Benl1/2026-07-12/sess.json",
        "topics": [{
            "topic_id": "topic-1", "title": "Steel frame progress",
            "summary": "Crew finished the steel frame today.",
            "user_id": "user-1", "action_items": [{"text": "close out steel frame"}],
        }],
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
    assert sent == {"suggestions": result["suggestions"]}


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
