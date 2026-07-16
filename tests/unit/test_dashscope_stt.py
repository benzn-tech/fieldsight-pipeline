"""
Tests for dashscope_utils.stt — SP-Ask Task 2 (TDD).

Mirrors test_dashscope_utils.py exactly: monkeypatch DASHSCOPE_API_KEY and
time.sleep on the module, and monkeypatch urllib3.PoolManager.request at the
CLASS level (stt() constructs a fresh PoolManager() internally, so patching an
instance wouldn't reach it) so no test makes a real network call.
"""
import base64
import json

import pytest

du = pytest.importorskip("dashscope_utils", reason="requires urllib3 (installed in CI)")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _asr_payload(text):
    return {"output": {"choices": [{"message": {"content": [{"text": text}]}}]}}


@pytest.fixture(autouse=True)
def dashscope_key(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(du.time, "sleep", lambda seconds: None)


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "")
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY not set"):
        du.stt(b"\x00\x01", "m4a")


def test_empty_audio_returns_empty(monkeypatch):
    def fail(self, *a, **k):
        raise AssertionError("no HTTP call for empty audio")
    monkeypatch.setattr(du.urllib3.PoolManager, "request", fail)
    assert du.stt(b"", "m4a") == ""


def test_posts_base64_audio_and_model(monkeypatch):
    captured = {}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return _FakeResponse(200, _asr_payload("hello site"))

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    text = du.stt(b"RIFFdata", "m4a")

    assert text == "hello site"
    assert captured["method"] == "POST"
    assert captured["url"] == du.DASHSCOPE_AIGC_URL
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == du.DASHSCOPE_ASR_MODEL
    audio_part = captured["body"]["input"]["messages"][0]["content"][0]["audio"]
    assert audio_part == "data:audio/m4a;base64," + base64.b64encode(b"RIFFdata").decode("ascii")


def test_tolerates_string_content(monkeypatch):
    payload = {"output": {"choices": [{"message": {"content": "plain text"}}]}}
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(200, payload))
    assert du.stt(b"x", "m4a") == "plain text"


def test_missing_content_returns_empty(monkeypatch):
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(200, {"output": {}}))
    assert du.stt(b"x", "m4a") == ""


def test_missing_content_logs_warning(monkeypatch, caplog):
    """Structure-missing (as opposed to a genuinely silent clip) must leave a
    WARNING in CloudWatch -- the only way to tell "broken integration" apart
    from "user said nothing" while the DashScope response shape is still an
    unverified guess. Return value must still be "" (fail-soft unchanged)."""
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(200, {"output": {}}))
    with caplog.at_level("WARNING"):
        result = du.stt(b"x", "m4a")
    assert result == ""
    assert any(
        "DashScope ASR response missing expected structure" in r.message
        for r in caplog.records
    )


def test_unexpected_content_type_logs_warning(monkeypatch, caplog):
    """content present but neither str nor list (e.g. None) -- also a
    structure mismatch, not a silent clip -- must warn and still return ""."""
    payload = {"output": {"choices": [{"message": {"content": None}}]}}
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(200, payload))
    with caplog.at_level("WARNING"):
        result = du.stt(b"x", "m4a")
    assert result == ""
    assert any(
        "DashScope ASR response missing expected structure" in r.message
        for r in caplog.records
    )


def test_retries_on_503_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(503, {"message": "busy"})
        return _FakeResponse(200, _asr_payload("second try"))

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)
    assert du.stt(b"x", "m4a") == "second try"
    assert calls["n"] == 2


def test_permanent_400_raises(monkeypatch):
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(400, {"message": "bad audio"}))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        du.stt(b"x", "m4a")
