"""Unit: which voice speaks the answer, and what it may not share.

Unlike the STT swap, this one is NOT a latency change. The incumbent measures
2.07s total for a two-sentence answer -- 1.0s of connection, 0.75s of model --
and pre-opening its socket during STT+LLM was measured and saved NOTHING
(2.07 -> 2.08), because `connect()` is synchronous. It is here because one voice
vendor was asked for, and because the incumbent's model carries a vendor
retirement note.

Two things must hold and neither is about speed.

THE CONTAINER. Both providers hand the device the same WAV: ElevenLabs returns
raw PCM and is wrapped with the incumbent's own `_pcm_to_wav`. The mobile client
writes the bytes to a file and gives it to MediaPlayer, so a container change is
a silent playback failure, not an error.

THE KEY. Three consumers now want ElevenLabs -- the recording pipeline's
transcription, Ask's STT, and this. EL credit is a shared per-key pool whose
exhaustion presents as the feature simply STOPPING, so one pool across three
consumers would let any of them silently stop the other two.
"""
import pytest

aa = pytest.importorskip(
    "lambda_ask_agent",
    reason="requires the ask agent's dependencies (installed in CI)")
el = pytest.importorskip("elevenlabs_utils")


def test_the_default_is_the_incumbent(monkeypatch):
    monkeypatch.delenv("ASK_TTS_PROVIDER", raising=False)
    import types
    ds = types.ModuleType("dashscope_utils")
    seen = {}

    def _tts(text):
        seen["dashscope"] = text
        return b"RIFF-incumbent"

    ds.tts = _tts
    ds._pcm_to_wav = lambda pcm, **k: b"RIFF" + pcm
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", ds)
    assert aa._tts("hello") == b"RIFF-incumbent"
    assert seen["dashscope"] == "hello"


def test_an_unknown_provider_is_the_incumbent_not_an_error(monkeypatch):
    """A typo in a stack parameter must not leave the device with no voice."""
    monkeypatch.setenv("ASK_TTS_PROVIDER", "eleven_labs")   # sic
    import types
    ds = types.ModuleType("dashscope_utils")
    ds.tts = lambda text: b"RIFF-incumbent"
    ds._pcm_to_wav = lambda pcm, **k: b"RIFF" + pcm
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", ds)
    assert aa._tts("hello") == b"RIFF-incumbent"


def test_elevenlabs_audio_reaches_the_device_as_wav(monkeypatch):
    """THE container test. EL returns raw PCM; the device only ever decodes WAV,
    and MediaPlayer failing on an unexpected container is silence, not an
    error."""
    monkeypatch.setenv("ASK_TTS_PROVIDER", "elevenlabs")
    import types
    ds = types.ModuleType("dashscope_utils")
    ds.tts = lambda text: b"SHOULD-NOT-BE-CALLED"
    ds._pcm_to_wav = lambda pcm, **k: b"RIFF" + pcm
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", ds)
    monkeypatch.setattr(el, "tts", lambda text: b"\x00\x01rawpcm")
    out = aa._tts("hello")
    assert out.startswith(b"RIFF"), "EL's PCM must be wrapped, never sent bare"
    assert b"rawpcm" in out


def test_elevenlabs_without_its_own_key_refuses(monkeypatch):
    """No fallback to either of the other two keys. A shared pool is how one
    consumer silently stops another, and the repo has already paid for that once
    with the TEST/prod transcription keys."""
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "")
    monkeypatch.setattr(el, "ELEVENLABS_API_KEY", "the-pipelines-key")
    monkeypatch.setattr(el, "ELEVENLABS_ASK_API_KEY", "the-stt-key")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY_TTS"):
        el.tts("hello")


def test_empty_text_makes_no_request(monkeypatch):
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")
    assert el.tts("") == b""
    assert el.tts("   ") == b""


def test_the_request_shape(monkeypatch):
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")
    monkeypatch.setattr(el, "ELEVENLABS_TTS_VOICE", "voice-1")
    monkeypatch.setattr(el, "ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")
    seen = {}

    class _Resp:
        status = 200
        data = b"\x00\x01pcm"

    class _Pool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            seen.update(url=url, headers=headers, timeout=timeout, body=body)
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    assert el.tts("hello") == b"\x00\x01pcm"
    assert "voice-1" in seen["url"]
    assert "pcm_24000" in seen["url"], "the container the caller wraps must be asked for"
    assert seen["headers"]["xi-api-key"] == "k"
    assert seen["timeout"] == 10, "10s, not the batch path's 280"
    assert b"eleven_flash_v2_5" in seen["body"]


def test_a_permanent_error_is_not_retried(monkeypatch):
    """Against a 29s ceiling, spending the user's remaining seconds proving a
    bad key is still bad turns a slow answer into no answer."""
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")
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
        el.tts("hello")
    assert calls["n"] == 1


def test_a_200_with_no_audio_is_a_failure(monkeypatch):
    """Silence returned as success is the worst outcome: the device would play
    nothing and report nothing, which reads to the user as the product ignoring
    them."""
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")

    class _Resp:
        status = 200
        data = b""

    class _Pool:
        def request(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    with pytest.raises(RuntimeError, match="no audio"):
        el.tts("hello")


def test_the_speed_actually_goes_on_the_wire(monkeypatch):
    """A vendor that ignores an unknown field returns 200 either way, so
    "we asked for 1.2" is not the same claim as "it was sent". Measured against
    the real endpoint the audio DID shorten (5.36s -> 5.20s on
    v3-conversational, 4.64 -> 3.81 on flash), which is the only proof the
    parameter is real -- this test pins that we keep sending it."""
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")
    monkeypatch.setattr(el, "ELEVENLABS_TTS_SPEED", 1.2)
    seen = {}

    class _Resp:
        status = 200
        data = b"pcm"

    class _Pool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            seen["body"] = body
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    el.tts("hello")
    import json as _j
    sent = _j.loads(seen["body"])
    assert sent["voice_settings"]["speed"] == 1.2


def test_speed_one_sends_nothing(monkeypatch):
    """1.0 is "as written", and sending it would be a settings object that says
    nothing -- which is how a future reader concludes the field is required."""
    monkeypatch.setattr(el, "ELEVENLABS_TTS_API_KEY", "k")
    monkeypatch.setattr(el, "ELEVENLABS_TTS_SPEED", 1.0)
    seen = {}

    class _Resp:
        status = 200
        data = b"pcm"

    class _Pool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            seen["body"] = body
            return _Resp()

    monkeypatch.setattr(el.urllib3, "PoolManager", lambda *a, **k: _Pool())
    el.tts("hello")
    import json as _j
    assert "voice_settings" not in _j.loads(seen["body"])


def test_the_conversational_model_is_the_default():
    """Chosen on measurement, not on the docs: the vendor documents
    v3-conversational on the Text-to-Dialogue WebSocket, and the plain HTTP
    /stream endpoint was verified to accept it. If someone later "fixes" this
    back to flash for speed, the trade being made is 0.51s against the
    expressiveness of a voice a person on a site listens to."""
    assert el.ELEVENLABS_TTS_MODEL == "eleven_v3_conversational"
    assert el.ELEVENLABS_TTS_VOICE == "bPkjmCb0W1xUBvyH2Afs"
