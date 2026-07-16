"""
Tests for dashscope_utils.tts — SP-Ask Task 3 (TDD). Same monkeypatch style as
test_dashscope_stt.py (class-level urllib3.PoolManager.request patch).
"""
import base64
import json

import pytest

du = pytest.importorskip("dashscope_utils", reason="requires urllib3 (installed in CI)")


class _FakeResponse:
    def __init__(self, status, payload=None, raw=None):
        self.status = status
        self.data = raw if raw is not None else json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def dashscope_key(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(du.time, "sleep", lambda seconds: None)


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "")
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY not set"):
        du.tts("hello")


def test_empty_text_returns_empty_bytes(monkeypatch):
    def fail(self, *a, **k):
        raise AssertionError("no HTTP call for empty text")
    monkeypatch.setattr(du.urllib3.PoolManager, "request", fail)
    assert du.tts("   ") == b""


def test_request_includes_text_voice_and_wav(monkeypatch):
    captured = {}
    wav = b"RIFF....WAVE"

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json.loads(body)
        return _FakeResponse(200, {"output": {"audio": {
            "data": base64.b64encode(wav).decode("ascii")}}})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    out = du.tts("the slab pour finished")

    assert out == wav
    assert captured["url"] == du.DASHSCOPE_AIGC_URL
    assert captured["body"]["model"] == du.DASHSCOPE_TTS_MODEL
    assert captured["body"]["input"]["text"] == "the slab pour finished"
    assert captured["body"]["input"]["voice"] == du.DASHSCOPE_TTS_VOICE
    assert captured["body"]["parameters"]["format"] == "wav"


def test_fetches_url_when_no_inline(monkeypatch):
    wav = b"RIFFfromurl"
    seq = []

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        seq.append((method, url))
        if method == "POST":
            return _FakeResponse(200, {"output": {"audio": {"url": "https://cdn/x.wav"}}})
        return _FakeResponse(200, raw=wav)  # GET the audio URL

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    assert du.tts("hi") == wav
    assert seq[0][0] == "POST"
    assert seq[1] == ("GET", "https://cdn/x.wav")


def test_missing_audio_raises(monkeypatch):
    monkeypatch.setattr(du.urllib3.PoolManager, "request",
                        lambda self, *a, **k: _FakeResponse(200, {"output": {}}))
    with pytest.raises(RuntimeError, match="missing audio"):
        du.tts("hi")


def test_url_fetch_retries_on_503_then_succeeds(monkeypatch):
    wav = b"RIFFretried"
    calls = {"get": 0}

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        if method == "POST":
            return _FakeResponse(200, {"output": {"audio": {"url": "https://cdn/x.wav"}}})
        calls["get"] += 1
        if calls["get"] == 1:
            return _FakeResponse(503, {"error": "gateway"})
        return _FakeResponse(200, raw=wav)

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    assert du.tts("hi") == wav
    assert calls["get"] == 2  # retried once after the 503


def test_url_fetch_permanent_404_raises(monkeypatch):
    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        if method == "POST":
            return _FakeResponse(200, {"output": {"audio": {"url": "https://cdn/x.wav"}}})
        return _FakeResponse(404, {"error": "not found"})

    monkeypatch.setattr(du.urllib3.PoolManager, "request", fake_request)

    with pytest.raises(RuntimeError, match="HTTP 404"):
        du.tts("hi")
