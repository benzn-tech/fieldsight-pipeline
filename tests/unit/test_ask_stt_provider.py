"""Unit: which transcriber the spoken question goes to, and what it may not share.

STT sits FIRST in the voice chain and nothing else can start until it returns.
Measured 2026-09-09 on three real site clips, three runs each:

    clip     DashScope    ElevenLabs
     3s        3.73s        0.88s
     8s        6.39s        1.01s
    14s        7.72s        1.36s

Four to six times, and the spread collapses with it (DashScope 3.15-6.85 on the
8s clip; EL 0.98-1.16). It is the only place on the whole path where whole
seconds are available -- TTS totals 2.07s and pre-opening its socket was measured
to save nothing.

The load-bearing test in this file is not the speed one -- speed cannot be
tested in CI. It is `test_elevenlabs_without_its_own_key_refuses`. EL credit is a
shared per-key pool whose exhaustion presents as transcription silently
STOPPING, and production's recording pipeline already runs on
ELEVENLABS_API_KEY. A fallback to that key would let an afternoon of voice
questions stop the recording pipeline, with no error anywhere. The refusal is
the feature.
"""
import pytest

aa = pytest.importorskip(
    "lambda_ask_agent",
    reason="requires the ask agent's dependencies (installed in CI)")
el = pytest.importorskip("elevenlabs_utils")


def test_the_default_is_the_incumbent(monkeypatch):
    """Unset must mean unchanged. A provider seam that switches on its own is a
    deploy that changes behaviour nobody asked it to change."""
    monkeypatch.delenv("ASK_STT_PROVIDER", raising=False)
    seen = {}
    import types
    ds = types.ModuleType("dashscope_utils")
    def _ds(audio, fmt):
        seen["dashscope"] = (audio, fmt)
        return "heard"
    ds.stt = _ds
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", ds)
    assert aa._stt(b"audio", "m4a") == "heard"
    assert "dashscope" in seen


def test_an_unknown_provider_is_the_incumbent_not_an_error(monkeypatch):
    """A typo in a stack parameter must not take voice Ask offline."""
    monkeypatch.setenv("ASK_STT_PROVIDER", "elevnlabs")   # sic
    import types
    ds = types.ModuleType("dashscope_utils")
    ds.stt = lambda audio, fmt: "heard"
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", ds)
    assert aa._stt(b"audio", "m4a") == "heard"


def test_elevenlabs_is_used_when_named(monkeypatch):
    monkeypatch.setenv("ASK_STT_PROVIDER", "elevenlabs")
    seen = {}
    def _el(audio, filename="clip.wav"):
        seen["el"] = filename
        return "heard"
    monkeypatch.setattr(el, "stt_short", _el)
    assert aa._stt(b"audio", "m4a") == "heard"
    assert seen["el"] == "clip.m4a", "the vendor must see the real container"


def test_elevenlabs_without_its_own_key_refuses(monkeypatch):
    """THE test. EL credit is one shared pool per key and running out looks like
    transcription simply stopping -- no error, no alarm. Production's recording
    pipeline already runs on ELEVENLABS_API_KEY.

    So a missing ASK key must RAISE, never quietly borrow the other one. If this
    test is ever 'fixed' by adding a fallback, an afternoon of voice questions
    can stop the recording pipeline and nothing will say so."""
    monkeypatch.setattr(el, "ELEVENLABS_ASK_API_KEY", "")
    monkeypatch.setattr(el, "ELEVENLABS_API_KEY", "the-pipelines-key")
    with pytest.raises(RuntimeError, match="ELEVENLABS_ASK_API_KEY"):
        el.stt_short(b"audio")


def test_a_silent_clip_is_empty_not_an_error(monkeypatch):
    """The caller distinguishes "heard nothing" from "failed": an empty
    transcript plays the device's error cue with a transcript key, a raise plays
    it without one. Both reach the user, but only one is honest about why."""
    monkeypatch.setattr(el, "ELEVENLABS_ASK_API_KEY", "k")
    assert el.stt_short(b"") == ""


def test_diarisation_is_not_requested(monkeypatch):
    """Measured to cost nothing (0.78 vs 0.88, 1.02 vs 1.01, 1.53 vs 1.36), so
    asking for it would buy a multi-tenancy exposure for free -- speaker features
    are the part of this vendor whose scoping has never been verified. A question
    asked into a push-to-talk key has one speaker."""
    monkeypatch.setattr(el, "ELEVENLABS_ASK_API_KEY", "k")
    captured = {}

    class _Resp:
        status = 200
        data = b'{"text": "how is level three going"}'

    class _Pool:
        def request(self, method, url, fields=None, headers=None, timeout=None):
            captured["fields"] = fields
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    assert el.stt_short(b"audio") == "how is level three going"
    names = [f[0] for f in captured["fields"]]
    assert "diarize" not in names
    assert "num_speakers" not in names
    assert captured["headers"]["xi-api-key"] == "k"
    assert captured["timeout"] == 10, "10s, not the batch path's 280"


def test_a_permanent_error_is_not_retried(monkeypatch):
    """Seven seconds of backoff against a 29s ceiling turns a slow answer into
    no answer. A bad key will not become valid on a second try."""
    monkeypatch.setattr(el, "ELEVENLABS_ASK_API_KEY", "k")
    calls = {"n": 0}

    class _Resp:
        status = 401
        data = b"unauthorised"

    class _Pool:
        def request(self, *a, **k):
            calls["n"] += 1
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    with pytest.raises(RuntimeError, match="401"):
        el.stt_short(b"audio")
    assert calls["n"] == 1, "a permanent error must cost one attempt, not two"
