"""Tests for src/llm_utils.py — provider dispatch + retry + JSON extraction.

Mirrors tests/unit/test_dashscope_utils.py: module-level env-derived constants
are monkeypatched on the module object, and urllib3.PoolManager.request is
patched at the class level (each call builds a fresh PoolManager()).
"""
import json
import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _patch_request(monkeypatch, responses):
    """responses: list of _FakeResponse (or Exception) returned in order."""
    calls = {"bodies": [], "urls": [], "headers": []}
    seq = list(responses)

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        calls["bodies"].append(body)
        calls["urls"].append(url)
        calls["headers"].append(headers)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake_request)
    return calls


# --- anthropic path ---

def test_anthropic_success(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(lu, "CLAUDE_MODEL", "claude-sonnet-4-6")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"content": [{"type": "text", "text": "hello"}]}),
    ])
    text, err = lu.call_llm("hi", max_tokens=100)
    assert (text, err) == ("hello", None)
    assert "api.anthropic.com" in calls["urls"][0]
    assert json.loads(calls["bodies"][0])["max_tokens"] == 100


def test_anthropic_missing_key(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "")
    text, err = lu.call_llm("hi")
    assert text is None and "ANTHROPIC_API_KEY" in err


# --- qwen path ---

def test_qwen_success_prose(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_MODEL", "qwen-flash")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "answer"}}]}),
    ])
    text, err = lu.call_llm("hi", max_tokens=200, force_json=False)
    assert (text, err) == ("answer", None)
    body = json.loads(calls["bodies"][0])
    assert body["model"] == "qwen-flash"
    assert body["max_tokens"] == 200
    assert "response_format" not in body
    assert body["enable_thinking"] is False   # explicit — see regression test below
    assert calls["headers"][0]["Authorization"] == "Bearer sk-w"


def test_qwen_force_json_omits_max_tokens(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    lu.call_llm("give JSON", max_tokens=999, force_json=True)
    body = json.loads(calls["bodies"][0])
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body
    assert body["enable_thinking"] is False   # explicit — see regression test below


def test_qwen_non_thinking_sends_enable_thinking_false_explicitly(monkeypatch):
    # Regression: DashScope's Qwen3 models DEFAULT to thinking when the
    # enable_thinking flag is OMITTED. The code used to omit it on the
    # non-thinking path, so QWEN_ENABLE_THINKING=false was inert and every
    # "non-thinking" call still reasoned — ~10x latency (qwen3.7-max summary
    # 38s vs 4s). The flag MUST be sent explicitly False.
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_ENABLE_THINKING", False)
    for force_json in (True, False):
        calls = _patch_request(monkeypatch, [
            _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
        ])
        lu.call_llm("hi", max_tokens=200, force_json=force_json)
        body = json.loads(calls["bodies"][0])
        assert body["enable_thinking"] is False   # present AND False, never omitted


def test_qwen_thinking_sets_flag_and_skips_response_format(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_ENABLE_THINKING", True)
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    # Even with force_json=True, thinking mode must NOT force response_format
    # (DashScope: thinking + json_object risks non-strict JSON) and must not cap
    # max_tokens; it relies on the prompt's JSON instruction + extract_json().
    lu.call_llm("give JSON", max_tokens=999, force_json=True)
    body = json.loads(calls["bodies"][0])
    assert body["enable_thinking"] is True
    assert "response_format" not in body
    assert "max_tokens" not in body


def test_qwen_per_call_enable_thinking_overrides_the_env_default(monkeypatch):
    """One Lambda can need both modes: lambda_extract_session runs a fast live
    pass while recording and a thinking-mode final pass at session close. The
    override must beat the env default in BOTH directions, and must also switch
    the response_format/max_tokens branch that rides along with it."""
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")

    # env says thinking ON, this call forces it OFF
    monkeypatch.setattr(lu, "QWEN_ENABLE_THINKING", True)
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    lu.call_llm("p", max_tokens=999, force_json=True, enable_thinking=False)
    body = json.loads(calls["bodies"][0])
    assert body["enable_thinking"] is False
    assert body["response_format"] == {"type": "json_object"}

    # env says thinking OFF, this call forces it ON
    monkeypatch.setattr(lu, "QWEN_ENABLE_THINKING", False)
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    lu.call_llm("p", max_tokens=999, force_json=True, enable_thinking=True)
    body = json.loads(calls["bodies"][0])
    assert body["enable_thinking"] is True
    assert "response_format" not in body


def test_qwen_enable_thinking_none_keeps_the_env_default(monkeypatch):
    """Backward compatibility: every pre-existing caller omits the argument and
    must keep its exact previous behaviour."""
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_ENABLE_THINKING", True)
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    lu.call_llm("p", max_tokens=999, force_json=True)      # no enable_thinking arg
    assert json.loads(calls["bodies"][0])["enable_thinking"] is True


def test_qwen_retries_on_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(503, {"error": {"message": "busy"}}),
        _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ])
    text, err = lu.call_llm("hi")
    assert (text, err) == ("ok", None)
    assert len(calls["urls"]) == 2  # retried once


def test_qwen_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    _patch_request(monkeypatch, [_FakeResponse(500, {}) for _ in range(lu.MAX_ATTEMPTS)])
    text, err = lu.call_llm("hi")
    assert text is None and err is not None


# --- api_key_configured + extract_json ---

def test_api_key_configured(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "")
    assert lu.api_key_configured() is False
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    assert lu.api_key_configured() is True


def test_extract_json_fenced():
    assert lu.extract_json('prefix ```json\n{"a": 1}\n``` suffix') == {"a": 1}


def test_extract_json_braces_fallback():
    assert lu.extract_json('noise {"b": 2} trailing') == {"b": 2}


def test_extract_json_failure_returns_none():
    assert lu.extract_json("no json here") is None


# ---- sampling temperature ----------------------------------------------
#
# Nothing in this repo has ever set `temperature`, so every call has taken the
# provider default (DashScope documents 0.7 for the non-thinking Qwen path).
#
# Measured 2026-08-12 over a preregistered 2x2, 10 calls per cell on one fixed
# session: temperature=0 did NOT make the extraction reproducible -- the action
# count still ranged 1-9 against the default's 1-10, and the coverage
# difference was inside the noise (p = 0.29). That hypothesis is dead.
#
# What it DID do, and the effect is far too large to be noise: the share of
# action items carrying a `responsible` went from 79% to 92%. That field is the
# one that has already cost something -- a misheard name put the wrong
# responsible party into a customer email -- so the knob is worth having for
# that reason alone, and for no other.
#
# Shipped as a knob rather than a new default: one experiment, one session, one
# task, and `call_llm` is shared by the rolling summary, finalize, the matcher
# and the ask agent, none of which were measured.

def test_temperature_is_sent_when_configured(monkeypatch):
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    sent = {}
    monkeypatch.setattr(lu, "_post_with_retry",
                        lambda url, body, headers: (sent.update(body=json.loads(body)), (None, "stop"))[1])
    lu._call_qwen("p", 100, True, enable_thinking=False)
    assert sent["body"]["temperature"] == 0.0


def test_temperature_is_absent_when_not_configured(monkeypatch):
    """Unset must mean UNSENT, not zero. Sending 0 by default would change
    every caller in the repo on the strength of one experiment."""
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", None)
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    sent = {}
    monkeypatch.setattr(lu, "_post_with_retry",
                        lambda url, body, headers: (sent.update(body=json.loads(body)), (None, "stop"))[1])
    lu._call_qwen("p", 100, True, enable_thinking=False)
    assert "temperature" not in sent["body"]


def test_the_anthropic_path_honours_it_too(monkeypatch):
    """A knob that silently does nothing on one provider is the unwired-toggle
    shape: flipping LLM_PROVIDER would quietly drop it."""
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "k")
    sent = {}
    monkeypatch.setattr(lu, "_post_with_retry",
                        lambda url, body, headers: (sent.update(body=json.loads(body)), (None, "stop"))[1])
    lu._call_anthropic("p", 100)
    assert sent["body"]["temperature"] == 0.0


def test_a_thinking_call_also_carries_it(monkeypatch):
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    sent = {}
    monkeypatch.setattr(lu, "_post_with_retry",
                        lambda url, body, headers: (sent.update(body=json.loads(body)), (None, "stop"))[1])
    lu._call_qwen("p", 100, True, enable_thinking=True)
    assert sent["body"]["temperature"] == 0.0


# ---- usage telemetry -----------------------------------------------------
#
# We have exactly one cost data point in this whole repo -- a hand-run bench
# in a code comment above `call_llm` -- and no production evidence for the
# model/caching decisions we are about to make. This log line is the fix, and
# it has to (a) actually appear at the level the Lambda runtime runs at
# (proven separately, see test_llm_usage_line_survives_the_lambda_runtime_
# default below), (b) never log prompt/completion text, and (c) never turn a
# vendor usage-shape surprise into a failed call.

import logging


def _usage_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("LLM_USAGE")]


def test_anthropic_usage_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(lu, "CLAUDE_MODEL", "claude-sonnet-4-6")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "hello"}],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 5,
            },
        }),
    ])
    with caplog.at_level(logging.INFO):
        text, err = lu.call_llm("hi", max_tokens=100, caller="extraction")
    assert (text, err) == ("hello", None)
    lines = _usage_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "provider=anthropic" in line
    assert "model=claude-sonnet-4-6" in line
    assert "caller=extraction" in line
    assert "prompt_tokens=120" in line
    assert "completion_tokens=30" in line
    assert "cache_read_tokens=100" in line
    assert "cache_write_tokens=5" in line
    # Never the prompt or the completion text.
    assert "hello" not in line
    assert "hi" not in line


def test_anthropic_missing_usage_fails_open(monkeypatch, caplog):
    """The vendor omitting `usage` entirely must not cost the caller its
    already-successful answer -- a telemetry bug must never become an outage."""
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(lu, "CLAUDE_MODEL", "claude-sonnet-4-6")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {"content": [{"type": "text", "text": "hello"}]}),  # no "usage" key
    ])
    with caplog.at_level(logging.INFO):
        text, err = lu.call_llm("hi", max_tokens=100)
    assert (text, err) == ("hello", None)
    lines = _usage_lines(caplog)
    assert len(lines) == 1
    assert "prompt_tokens=-" in lines[0]
    assert "completion_tokens=-" in lines[0]


def test_qwen_usage_is_logged_with_reasoning_and_caller(monkeypatch, caplog):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_MODEL", "qwen-flash")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {
            "model": "qwen-flash",
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 80,
                "completion_tokens_details": {"reasoning_tokens": 40},
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        }),
    ])
    with caplog.at_level(logging.INFO):
        text, err = lu.call_llm("hi", max_tokens=200, caller="verdict")
    assert (text, err) == ("answer", None)
    lines = _usage_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "provider=qwen" in line
    assert "model=qwen-flash" in line
    assert "caller=verdict" in line
    assert "prompt_tokens=500" in line
    assert "completion_tokens=80" in line
    assert "reasoning_tokens=40" in line
    assert "cache_read_tokens=200" in line
    assert "answer" not in line


def test_qwen_usage_without_reasoning_field_logs_a_dash(monkeypatch, caplog):
    """A vendor that does not report reasoning tokens (or a call that never
    reasoned) must not raise, and must not fabricate a number."""
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {
            "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }),
    ])
    with caplog.at_level(logging.INFO):
        lu.call_llm("hi", max_tokens=200)
    lines = _usage_lines(caplog)
    assert len(lines) == 1
    assert "reasoning_tokens=-" in lines[0]
    assert "cache_read_tokens=-" in lines[0]


def test_qwen_missing_usage_key_fails_open(monkeypatch, caplog):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "answer"},
                                        "finish_reason": "stop"}]}),  # no "usage" key
    ])
    with caplog.at_level(logging.INFO):
        text, err = lu.call_llm("hi", max_tokens=200)
    assert (text, err) == ("answer", None)
    lines = _usage_lines(caplog)
    assert len(lines) == 1
    assert "prompt_tokens=-" in lines[0]


def test_caller_defaults_to_unknown_when_not_passed(monkeypatch, caplog):
    """Every pre-existing call site is unmodified and must keep working, now
    tagged 'unknown' rather than silently missing from cost attribution."""
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "answer"},
                                        "finish_reason": "stop"}]}),
    ])
    with caplog.at_level(logging.INFO):
        lu.call_llm("hi", max_tokens=200)
    lines = _usage_lines(caplog)
    assert "caller=unknown" in lines[0]
