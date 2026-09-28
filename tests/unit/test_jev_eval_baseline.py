"""Tests for the Jev shadow-eval baseline arm (Track A, Task 5).

No network, no aws CLI: `load_deployed_llm_env` is driven with a fake `run`
callable, and `baseline_row`'s programme_match path is driven with a fake
`call` callable. The real `lambda_programme_matcher.build_prompt` /
`parse_verdict` and `thread_match.MIN_SCORE` are exercised for real (spied,
not stubbed) -- this arm's whole point is "today's gate, imported, not
re-derived".
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os

import pytest

import lambda_programme_matcher
import thread_match
from scripts.jev_eval import baseline


def _programme_row(task_id="task-1", confidence=None, suggested_status=None,
                    suggested_progress=None, label="yes"):
    return {
        "id": "pm-1",
        "label": label,
        "features": {
            "observation": {
                "title": "Ground floor walls",
                "summary": "Sub A finished the ground floor walls today.",
                "date": "2026-09-20",
            },
            "task": {
                "name": "Ground floor walls",
                "status": "in_progress",
                "progress_pct": 40,
            },
        },
        "baseline": {
            "task_id": task_id,
            "confidence": confidence,
            "suggested_status": suggested_status,
            "suggested_progress": suggested_progress,
        },
    }


def _fake_call(raw_payload, error=None):
    def call(prompt, max_tokens=512, force_json=True, caller=None):
        if error is not None:
            return None, error
        return json.dumps(raw_payload), None
    return call


# ---------------------------------------------------------------------------
# question_hash: sha256 of build_prompt's own source, so a results file
# proves which prompt version produced it.
# ---------------------------------------------------------------------------

def test_programme_match_question_hash_is_build_prompt_source_hash():
    row = _programme_row()
    expected = hashlib.sha256(
        inspect.getsource(lambda_programme_matcher.build_prompt).encode("utf-8")
    ).hexdigest()
    call = _fake_call({"task_id": None, "confidence": 0.0})
    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["question_hash"] == expected


# ---------------------------------------------------------------------------
# programme_match: the REAL build_prompt/parse_verdict are called.
# ---------------------------------------------------------------------------

def test_programme_match_calls_real_build_prompt_and_parse_verdict(monkeypatch):
    calls = {}
    real_build_prompt = lambda_programme_matcher.build_prompt
    real_parse_verdict = lambda_programme_matcher.parse_verdict

    def spy_build_prompt(topic, candidates):
        calls["build_prompt"] = (topic, candidates)
        return real_build_prompt(topic, candidates)

    def spy_parse_verdict(raw, survivor_ids, conf_min):
        calls["parse_verdict"] = (raw, survivor_ids, conf_min)
        return real_parse_verdict(raw, survivor_ids, conf_min)

    monkeypatch.setattr(lambda_programme_matcher, "build_prompt", spy_build_prompt)
    monkeypatch.setattr(lambda_programme_matcher, "parse_verdict", spy_parse_verdict)

    row = _programme_row(task_id="task-1")
    call = _fake_call({"task_id": "task-1", "confidence": 0.9,
                        "suggested_status": None, "suggested_progress": None,
                        "evidence": "finished today"})

    result = baseline.baseline_row("programme_match", row, 1, call=call)

    assert "build_prompt" in calls
    topic, candidates = calls["build_prompt"]
    assert topic["title"] == "Ground floor walls"
    assert candidates == [{
        "task_id": "task-1", "name": "Ground floor walls",
        "status": "in_progress", "progress_pct": 40,
    }]

    assert "parse_verdict" in calls
    raw, survivor_ids, conf_min = calls["parse_verdict"]
    assert survivor_ids == {"task-1"}
    assert conf_min == lambda_programme_matcher.CONF_MIN

    assert result["score"] == pytest.approx(0.9)
    assert result["accepted"] is True  # 0.9 >= default CONF_MIN 0.70


def test_programme_match_matching_task_id_below_conf_min_scores_confidence_not_accepted():
    row = _programme_row(task_id="task-1")
    call = _fake_call({"task_id": "task-1", "confidence": 0.55,
                        "suggested_status": None, "suggested_progress": None,
                        "evidence": "maybe"})
    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["score"] == pytest.approx(0.55)
    assert result["accepted"] is False
    assert result["error"] is None


def test_programme_match_null_task_id_scores_zero():
    row = _programme_row(task_id="task-1")
    call = _fake_call({"task_id": None, "confidence": 0.9,
                        "suggested_status": None, "suggested_progress": None,
                        "evidence": "no match"})
    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["score"] == 0.0
    assert result["accepted"] is False
    assert result["error"] is None


def test_programme_match_wrong_task_id_scores_zero():
    row = _programme_row(task_id="task-1")
    call = _fake_call({"task_id": "some-other-task", "confidence": 0.95,
                        "suggested_status": None, "suggested_progress": None,
                        "evidence": "wrong pick"})
    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["score"] == 0.0


def test_programme_match_malformed_json_is_error_row_with_none_score():
    row = _programme_row(task_id="task-1")

    def call(prompt, max_tokens=512, force_json=True, caller=None):
        return "not json at all {{{", None

    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["score"] is None
    assert result["error"] is not None
    assert result["accepted"] is None


def test_programme_match_llm_failure_is_error_row_with_none_score():
    row = _programme_row(task_id="task-1")
    call = _fake_call(None, error="HTTP 500")
    result = baseline.baseline_row("programme_match", row, 1, call=call)
    assert result["score"] is None
    assert result["error"] == "HTTP 500"
    assert result["accepted"] is None


def test_programme_match_default_call_is_llm_utils_call_llm(monkeypatch):
    """`call` defaults to the real `llm_utils.call_llm` when not injected."""
    import llm_utils

    seen = {}

    def fake_call_llm(prompt, max_tokens=4096, force_json=False, **kwargs):
        seen["max_tokens"] = max_tokens
        seen["force_json"] = force_json
        seen["caller"] = kwargs.get("caller")
        return json.dumps({"task_id": None, "confidence": 0.0}), None

    monkeypatch.setattr(llm_utils, "call_llm", fake_call_llm)
    row = _programme_row(task_id="task-1")
    baseline.baseline_row("programme_match", row, 1)
    assert seen["max_tokens"] == 512
    assert seen["force_json"] is True
    assert seen["caller"] == "jev_eval_baseline_programme"


# ---------------------------------------------------------------------------
# threads: stored score, real thread_match.MIN_SCORE, deterministic.
# ---------------------------------------------------------------------------

def _threads_row(score):
    return {"id": "th-1", "label": "yes", "features": {}, "baseline": {"score": score}}


def test_threads_score_is_stored_score_and_accepted_uses_real_min_score():
    row = _threads_row(thread_match.MIN_SCORE + 0.01)
    result = baseline.baseline_row("threads", row, 1)
    assert result["score"] == pytest.approx(thread_match.MIN_SCORE + 0.01)
    assert result["accepted"] is True
    assert result["deterministic"] is True
    assert result["question_hash"] == "stored:thread_match"


def test_threads_below_min_score_is_not_accepted():
    row = _threads_row(thread_match.MIN_SCORE - 0.01)
    result = baseline.baseline_row("threads", row, 1)
    assert result["accepted"] is False


def test_threads_run_2_returns_same_score_deterministic():
    row = _threads_row(0.4)
    r1 = baseline.baseline_row("threads", row, 1)
    r2 = baseline.baseline_row("threads", row, 2)
    assert r1["score"] == r2["score"]
    assert r1["deterministic"] is True and r2["deterministic"] is True


def test_threads_missing_score_is_error_row():
    row = {"id": "th-2", "label": "no", "features": {}, "baseline": {}}
    result = baseline.baseline_row("threads", row, 1)
    assert result["score"] is None
    assert result["error"] is not None
    assert result["accepted"] is None


# ---------------------------------------------------------------------------
# work_class: P(non_work), deterministic.
# ---------------------------------------------------------------------------

def _work_class_row(verdict, confidence):
    return {
        "id": "wc-1", "label": "yes", "features": {},
        "baseline": {"classifier_verdict": verdict, "classifier_confidence": confidence},
    }


def test_work_class_non_work_verdict_scores_confidence_directly():
    row = _work_class_row("non_work", 0.8)
    result = baseline.baseline_row("work_class", row, 1)
    assert result["score"] == pytest.approx(0.8)
    assert result["accepted"] is True
    assert result["deterministic"] is True
    assert result["question_hash"] == "stored:classifier"


def test_work_class_work_verdict_scores_complement():
    row = _work_class_row("work", 0.8)
    result = baseline.baseline_row("work_class", row, 1)
    assert result["score"] == pytest.approx(0.2)
    assert result["accepted"] is False


def test_work_class_missing_confidence_is_error_row_not_a_guess():
    row = {"id": "wc-2", "label": "no",
           "baseline": {"classifier_verdict": "non_work", "classifier_confidence": None}}
    result = baseline.baseline_row("work_class", row, 1)
    assert result["score"] is None
    assert result["error"] is not None
    assert result["accepted"] is None


def test_work_class_missing_verdict_is_error_row():
    row = {"id": "wc-3", "label": "no",
           "baseline": {"classifier_verdict": None, "classifier_confidence": 0.9}}
    result = baseline.baseline_row("work_class", row, 1)
    assert result["score"] is None
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# baseline_row dispatch
# ---------------------------------------------------------------------------

def test_baseline_row_raises_on_unknown_set():
    with pytest.raises(baseline.JevBaselineError):
        baseline.baseline_row("not_a_real_set", {"id": "x", "label": "yes"}, 1)


# ---------------------------------------------------------------------------
# load_deployed_llm_env: no aws, no network -- fake `run`.
# ---------------------------------------------------------------------------

def _fake_run(env_vars, returncode=0, stderr=""):
    class _Result:
        pass
    def run(args, **kwargs):
        r = _Result()
        r.returncode = returncode
        r.stderr = stderr
        r.stdout = json.dumps({"Environment": {"Variables": env_vars}})
        return r
    return run


def test_load_deployed_llm_env_copies_only_allowed_keys(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("QWEN_MODEL", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("SUGGESTION_WRITER_FUNCTION", raising=False)

    env_vars = {
        "LLM_PROVIDER": "qwen",
        "QWEN_MODEL": "qwen3.8-flash",
        "QWEN_API_KEY": "secret-value-should-not-leak",
        "S3_BUCKET": "fieldsight-data-lake",
        "SUGGESTION_WRITER_FUNCTION": "fieldsight-test-suggestion-writer",
    }
    fake_run = _fake_run(env_vars)

    copied = baseline.load_deployed_llm_env(
        baseline.PROGRAMME_MATCHER_FUNCTION, run=fake_run)

    assert "LLM_PROVIDER" in copied
    assert "QWEN_MODEL" in copied
    assert "QWEN_API_KEY" in copied
    assert "S3_BUCKET" not in copied
    assert "SUGGESTION_WRITER_FUNCTION" not in copied

    import os
    assert os.environ["LLM_PROVIDER"] == "qwen"
    assert os.environ.get("S3_BUCKET") != "fieldsight-data-lake"


def test_load_deployed_llm_env_never_prints_secret_values(monkeypatch, capsys):
    env_vars = {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-super-secret-value"}
    fake_run = _fake_run(env_vars)
    copied = baseline.load_deployed_llm_env(
        baseline.PROGRAMME_MATCHER_FUNCTION, run=fake_run)
    assert copied == ["ANTHROPIC_API_KEY", "LLM_PROVIDER"]
    for item in copied:
        assert "sk-super-secret-value" != item
    captured = capsys.readouterr()
    assert "sk-super-secret-value" not in captured.out
    assert "sk-super-secret-value" not in captured.err


def test_load_deployed_llm_env_raises_on_aws_failure():
    fake_run = _fake_run({}, returncode=254, stderr="ResourceNotFoundException")
    with pytest.raises(RuntimeError):
        baseline.load_deployed_llm_env(baseline.PROGRAMME_MATCHER_FUNCTION, run=fake_run)


def test_baseline_config_reads_from_llm_utils_module_not_os_environ(monkeypatch):
    """`baseline_config()` must report what `llm_utils.call_llm` will
    actually use -- its own module constants -- never `os.environ` directly.
    Proven by setting `os.environ` to one thing and `llm_utils`'s constants
    to something else: the environ value must be ignored."""
    import llm_utils

    monkeypatch.setenv("LLM_PROVIDER", "qwen")  # os.environ says qwen...
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "anthropic")  # ...llm_utils says anthropic
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-sonnet-4-6")
    monkeypatch.setattr(llm_utils, "LLM_TEMPERATURE", 0.2)

    config = baseline.baseline_config()
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-sonnet-4-6"
    assert config["temperature"] == 0.2


# ---------------------------------------------------------------------------
# Fix round 1, finding #1: load_deployed_llm_env's os.environ write is
# invisible to llm_utils (module-level constants, read once at import) unless
# llm_utils is reloaded. These tests drive the REAL llm_utils.call_llm end to
# end, with urllib3 faked out, to prove the copied model actually reaches the
# outgoing request -- not just that os.environ was set.
# ---------------------------------------------------------------------------

class _FakeQwenResponse:
    status = 200

    def __init__(self, model_sent):
        self.data = json.dumps({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }).encode("utf-8")


class _FakeUnreachablePoolManager:
    """Fails loudly if constructed -- guards against a test silently making
    a real network call because the patch didn't take."""
    def __init__(self, *a, **kw):
        raise AssertionError("real urllib3.PoolManager() constructed in a test")


def test_load_deployed_llm_env_reload_makes_call_llm_use_the_copied_model(monkeypatch):
    import llm_utils

    # Snapshot every constant this test or the reload touches, to restore
    # afterwards so no other test in the file/session sees a mutated
    # llm_utils (or a reload triggered by an unrelated os.environ leftover).
    snapshot_env = {
        k: os.environ.get(k) for k in (
            "LLM_PROVIDER", "QWEN_MODEL", "QWEN_API_KEY", "QWEN_BASE_URL",
            "ANTHROPIC_API_KEY", "CLAUDE_MODEL", "LLM_TEMPERATURE",
        )
    }
    try:
        env_vars = {
            "LLM_PROVIDER": "qwen",
            "QWEN_MODEL": "fake-jev-eval-qwen-model",
            "QWEN_API_KEY": "fake-qwen-key-for-this-test-only",
        }
        fake_run = _fake_run(env_vars)

        copied = baseline.load_deployed_llm_env(
            baseline.PROGRAMME_MATCHER_FUNCTION, run=fake_run)
        assert "LLM_PROVIDER" in copied and "QWEN_MODEL" in copied

        # (a) llm_utils's OWN constants must equal the copied values -- not
        # just os.environ.
        assert llm_utils.LLM_PROVIDER == "qwen"
        assert llm_utils.QWEN_MODEL == "fake-jev-eval-qwen-model"

        # (b) drive the REAL llm_utils.call_llm, capturing the outgoing
        # request body via a fake urllib3.PoolManager (this module's own
        # transport -- not urllib.request).
        captured = {}

        class _FakePoolManager:
            def request(self, method, url, body=None, headers=None, timeout=None):
                captured["url"] = url
                captured["body"] = json.loads(body)
                return _FakeQwenResponse(captured["body"].get("model"))

        monkeypatch.setattr(llm_utils.urllib3, "PoolManager", _FakePoolManager)

        raw, error = llm_utils.call_llm("hello", max_tokens=64, caller="jev_test")
        assert error is None
        assert raw == "ok"
        assert captured["body"]["model"] == "fake-jev-eval-qwen-model"

        # (c) baseline_config() must agree with what the real call just used.
        config = baseline.baseline_config()
        assert config["provider"] == "qwen"
        assert config["model"] == captured["body"]["model"] == "fake-jev-eval-qwen-model"
    finally:
        for key, value in snapshot_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(llm_utils)


def test_load_deployed_llm_env_reload_does_not_touch_urllib3_when_unused(monkeypatch):
    """Sanity check on the fake above: if the reload/env copy were a no-op,
    call_llm would still dispatch to whatever provider this test PROCESS
    started with, not qwen -- guard against the fix silently not mattering
    by making a real PoolManager() construction fail the test loudly."""
    import llm_utils

    snapshot_env = {k: os.environ.get(k) for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY")}
    try:
        monkeypatch.setattr(llm_utils.urllib3, "PoolManager", _FakeUnreachablePoolManager)
        # Force a provider with no configured key so call_llm fails BEFORE
        # ever touching the (intentionally broken) PoolManager -- this just
        # proves the fake is wired to the same attribute call_llm reads.
        monkeypatch.setattr(llm_utils, "ANTHROPIC_API_KEY", "")
        monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "anthropic")
        raw, error = llm_utils.call_llm("hello", max_tokens=64)
        assert raw is None
        assert "ANTHROPIC_API_KEY" in error
    finally:
        for key, value in snapshot_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(llm_utils)
