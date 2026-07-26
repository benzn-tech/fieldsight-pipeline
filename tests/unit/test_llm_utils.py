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
