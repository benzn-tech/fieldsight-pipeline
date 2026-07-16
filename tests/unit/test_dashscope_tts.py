"""
Tests for dashscope_utils.tts — rewritten for the Qwen-TTS-Realtime SDK
(WebSocket streaming), replacing the old multimodal-generation HTTP call
(DashScope rejected model "qwen-tts" there with HTTP 400 InvalidParameter:
Model not exist).

The SDK (`dashscope.audio.qwen_tts_realtime`) is NOT installed in this test
environment (it ships only via DashScopeLayer in the deployed Lambda) --
tts() imports it lazily on first real call. So instead of installing the
SDK, these tests monkeypatch the three module-level placeholder globals
dashscope_utils exposes for it (QwenTtsRealtime, QwenTtsRealtimeCallback,
AudioFormat) with fakes BEFORE calling tts(); tts() sees they're already
non-None and skips the real `import dashscope`.
"""
import base64
import struct
import wave
import io

import pytest

du = pytest.importorskip("dashscope_utils", reason="requires urllib3 (installed in CI)")


class _FakeAudioFormat:
    PCM_24000HZ_MONO_16BIT = "pcm24000mono16bit"


class _FakeCallbackBase:
    """Stand-in for the real QwenTtsRealtimeCallback base class. The real
    one presumably defines the on_open/on_close/on_event/... interface;
    dashscope_utils._TtsCallback overrides everything it needs, so an empty
    base is sufficient here."""
    pass


def _make_fake_client(events, raise_on_connect=None, never_finish=False):
    """Build a Fake QwenTtsRealtime class. `events` is a list of dicts fed to
    callback.on_event(...) when finish() is called (simulating the server
    streaming response.audio.delta chunks then session.finished)."""

    class _FakeClient:
        def __init__(self, model, callback, url=None):
            self.model = model
            self.callback = callback
            self.url = url
            self.closed = False

        def connect(self):
            if raise_on_connect:
                raise raise_on_connect

        def update_session(self, voice=None, response_format=None, mode=None):
            self.voice = voice
            self.response_format = response_format
            self.mode = mode

        def append_text(self, text):
            pass

        def finish(self):
            if never_finish:
                return
            for ev in events:
                self.callback.on_event(ev)

        def close(self):
            self.closed = True

    return _FakeClient


@pytest.fixture(autouse=True)
def dashscope_key(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def fast_timeout(monkeypatch):
    # Never let a "hangs forever" test actually hang.
    monkeypatch.setattr(du, "DASHSCOPE_TTS_TIMEOUT_SECONDS", 0.2)


@pytest.fixture(autouse=True)
def reset_sdk_globals(monkeypatch):
    # tts() treats QwenTtsRealtime is None as "do the real lazy import" --
    # tests must always monkeypatch it (via the fixtures/helpers below), and
    # must NOT leak a patched value into unrelated tests.
    monkeypatch.setattr(du, "QwenTtsRealtimeCallback", _FakeCallbackBase)
    monkeypatch.setattr(du, "AudioFormat", _FakeAudioFormat)


def _delta_event(pcm_chunk):
    return {"type": "response.audio.delta", "delta": base64.b64encode(pcm_chunk).decode("ascii")}


_FINISHED_EVENT = {"type": "session.finished"}


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(du, "DASHSCOPE_API_KEY", "")
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY not set"):
        du.tts("hello")


def test_empty_text_returns_empty_bytes(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("no QwenTtsRealtime constructed for empty text")
    monkeypatch.setattr(du, "QwenTtsRealtime", fail)
    assert du.tts("   ") == b""
    assert du.tts("") == b""


def test_tts_returns_wav_wrapping_concatenated_pcm(monkeypatch):
    pcm1 = b"\x01\x00\x02\x00"
    pcm2 = b"\x03\x00\x04\x00"
    events = [_delta_event(pcm1), _delta_event(pcm2), _FINISHED_EVENT]
    fake_client_cls = _make_fake_client(events)
    monkeypatch.setattr(du, "QwenTtsRealtime", fake_client_cls)

    out = du.tts("the slab pour finished")

    assert out.startswith(b"RIFF")
    assert b"WAVE" in out

    # Parse the 44-byte PCM WAV header directly.
    riff, riff_size, wave_tag = struct.unpack("<4sI4s", out[0:12])
    fmt_tag, fmt_size, audio_fmt, channels, sample_rate, byte_rate, block_align, bits = \
        struct.unpack("<4sIHHIIHH", out[12:36])
    data_tag, data_size = struct.unpack("<4sI", out[36:44])

    assert riff == b"RIFF"
    assert wave_tag == b"WAVE"
    assert fmt_tag == b"fmt "
    assert audio_fmt == 1  # PCM
    assert channels == 1
    assert sample_rate == 24000
    assert bits == 16
    assert byte_rate == 24000 * 1 * 16 // 8
    assert block_align == 1 * 16 // 8
    assert data_tag == b"data"

    pcm_expected = pcm1 + pcm2
    assert data_size == len(pcm_expected)
    assert out[44:] == pcm_expected

    # Also readable via the stdlib `wave` module, as a sanity cross-check.
    with wave.open(io.BytesIO(out), "rb") as wf:
        assert wf.getframerate() == 24000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.readframes(wf.getnframes()) == pcm_expected


def test_no_audio_delta_raises(monkeypatch):
    fake_client_cls = _make_fake_client([_FINISHED_EVENT])  # finishes, no audio
    monkeypatch.setattr(du, "QwenTtsRealtime", fake_client_cls)

    with pytest.raises(RuntimeError, match="no audio"):
        du.tts("hi")


def test_connect_error_raises_runtime_error(monkeypatch):
    fake_client_cls = _make_fake_client([], raise_on_connect=ConnectionError("boom"))
    monkeypatch.setattr(du, "QwenTtsRealtime", fake_client_cls)

    with pytest.raises(RuntimeError, match="DashScope TTS failed"):
        du.tts("hi")


def test_timeout_waiting_for_finished_raises_runtime_error(monkeypatch):
    fake_client_cls = _make_fake_client([], never_finish=True)  # never calls on_event
    monkeypatch.setattr(du, "QwenTtsRealtime", fake_client_cls)

    with pytest.raises(RuntimeError, match="timed out"):
        du.tts("hi")


def test_abnormal_close_before_finished_raises_runtime_error(monkeypatch):
    class _AbnormalCloseClient(_make_fake_client([])):
        def finish(self):
            self.callback.on_close(1011, "internal error")

    monkeypatch.setattr(du, "QwenTtsRealtime", _AbnormalCloseClient)

    with pytest.raises(RuntimeError, match="closed abnormally"):
        du.tts("hi")


def test_client_closed_even_on_success(monkeypatch):
    events = [_delta_event(b"\x00\x01"), _FINISHED_EVENT]
    fake_client_cls = _make_fake_client(events)
    instances = []

    class _TrackedClient(fake_client_cls):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            instances.append(self)

    monkeypatch.setattr(du, "QwenTtsRealtime", _TrackedClient)
    du.tts("hi")

    assert len(instances) == 1
    assert instances[0].closed is True


def test_session_config_uses_model_voice_and_pcm_format(monkeypatch):
    events = [_delta_event(b"\x00\x01"), _FINISHED_EVENT]
    fake_client_cls = _make_fake_client(events)
    instances = []

    class _TrackedClient(fake_client_cls):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            instances.append(self)

    monkeypatch.setattr(du, "QwenTtsRealtime", _TrackedClient)
    du.tts("hi")

    inst = instances[0]
    assert inst.model == du.DASHSCOPE_TTS_MODEL
    assert inst.url == du.DASHSCOPE_TTS_WS_URL
    assert inst.voice == du.DASHSCOPE_TTS_VOICE
    assert inst.response_format == _FakeAudioFormat.PCM_24000HZ_MONO_16BIT
    assert inst.mode == "server_commit"


# ============================================================
# Pure helper: _pcm_to_wav
# ============================================================

def test_pcm_to_wav_header_default_params():
    pcm = b"\x11\x22\x33\x44\x55\x66"
    out = du._pcm_to_wav(pcm)

    assert len(out) == 44 + len(pcm)
    riff, riff_size, wave_tag = struct.unpack("<4sI4s", out[0:12])
    fmt_tag, fmt_size, audio_fmt, channels, sample_rate, byte_rate, block_align, bits = \
        struct.unpack("<4sIHHIIHH", out[12:36])
    data_tag, data_size = struct.unpack("<4sI", out[36:44])

    assert riff == b"RIFF"
    assert riff_size == 36 + len(pcm)
    assert wave_tag == b"WAVE"
    assert fmt_tag == b"fmt "
    assert fmt_size == 16
    assert audio_fmt == 1
    assert channels == 1
    assert sample_rate == 24000
    assert bits == 16
    assert byte_rate == 24000 * 2
    assert block_align == 2
    assert data_tag == b"data"
    assert data_size == len(pcm)
    assert out[44:] == pcm


def test_pcm_to_wav_header_custom_params():
    pcm = b"\x00" * 100
    out = du._pcm_to_wav(pcm, sample_rate=16000, channels=2, bits=8)

    fmt_tag, fmt_size, audio_fmt, channels, sample_rate, byte_rate, block_align, bits = \
        struct.unpack("<4sIHHIIHH", out[12:36])
    assert sample_rate == 16000
    assert channels == 2
    assert bits == 8
    assert block_align == 2 * 8 // 8
    assert byte_rate == 16000 * 2 * 8 // 8


def test_pcm_to_wav_empty_pcm():
    out = du._pcm_to_wav(b"")
    assert len(out) == 44
    assert out[:4] == b"RIFF"
    data_tag, data_size = struct.unpack("<4sI", out[36:44])
    assert data_size == 0
