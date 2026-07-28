"""Tests for src/elevenlabs_utils.py — scribe_v2 client + Transcript adapter.

The adapter's contract is that transcript_utils can parse its output exactly
like real AWS Transcribe JSON, so the key test round-trips through
transcript_utils.parse_transcribe_json.
"""
import json
import pytest

eu = pytest.importorskip("elevenlabs_utils", reason="requires urllib3 (installed in CI)")
tu = pytest.importorskip("transcript_utils")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(eu.time, "sleep", lambda s: None)


SCRIBE_RESPONSE = {
    "language_code": "eng",
    "language_probability": 0.98,
    "text": "pour the slab today",
    "words": [
        {"text": "pour", "start": 0.10, "end": 0.40, "speaker_id": "speaker_0", "type": "word"},
        {"text": " ", "start": 0.40, "end": 0.41, "speaker_id": "speaker_0", "type": "spacing"},
        {"text": "the", "start": 0.41, "end": 0.55, "speaker_id": "speaker_0", "type": "word"},
        {"text": "slab", "start": 0.55, "end": 0.90, "speaker_id": "speaker_1", "type": "word"},
        {"text": "today", "start": 0.90, "end": 1.30, "speaker_id": "speaker_1", "type": "word"},
    ],
}


def test_adapter_produces_transcribe_shape():
    out = eu.adapt_to_transcribe_json(SCRIBE_RESPONSE)
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    items = out["results"]["items"]
    # spacing dropped; only the 4 words become pronunciation items
    assert len(items) == 4
    assert all(it["type"] == "pronunciation" for it in items)
    assert items[0]["start_time"] == "0.1" and items[0]["end_time"] == "0.4"
    assert items[0]["alternatives"][0]["content"] == "pour"
    # two distinct speakers mapped to spk_0 / spk_1 in first-seen order
    labels = {it["speaker_label"] for it in items}
    assert labels == {"spk_0", "spk_1"}


def test_adapter_round_trips_through_transcript_utils():
    out = eu.adapt_to_transcribe_json(SCRIBE_RESPONSE)
    parsed = tu.parse_transcribe_json(out)
    # transcript_utils must accept the adapted shape without error and recover text
    assert "slab" in parsed["full_text"]


def test_adapter_no_speaker_ids_omits_labels():
    resp = {"text": "hello", "words": [{"text": "hello", "start": 0.0, "end": 0.5, "type": "word"}]}
    out = eu.adapt_to_transcribe_json(resp)
    assert "speaker_label" not in out["results"]["items"][0]


def test_load_keyterms_parses_phrase_column(tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text("# comment line\nGIB\tgib\t\tGIB\ndwang\tdwong\nBRANZ\n", encoding="utf-8")
    terms = eu.load_keyterms(str(f))
    assert terms == ["GIB", "dwang", "BRANZ"]


def test_load_keyterms_missing_file_returns_empty():
    assert eu.load_keyterms("/no/such/file.txt") == []


def test_transcribe_segment_missing_key_raises(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        eu.transcribe_segment(b"\x00", "seg.wav")


def _fields_dict(fields):
    """Collapse a list of (name, value) multipart tuples into name -> [values]."""
    out = {}
    for name, value in fields:
        out.setdefault(name, []).append(value)
    return out


def test_transcribe_segment_success(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    captured = {}

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        captured["url"] = url
        captured["fields"] = fields
        captured["headers"] = headers
        return _FakeResponse(200, SCRIBE_RESPONSE)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    out = eu.transcribe_segment(b"\x00\x01", "seg.wav", num_speakers=3, keyterms=["GIB"])
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    assert captured["headers"]["xi-api-key"] == "xi-key"
    fd = _fields_dict(captured["fields"])
    assert fd["model_id"] == [eu.ELEVENLABS_STT_MODEL]
    assert fd["num_speakers"] == ["3"]


def test_transcribe_segment_sends_keyterms_as_repeated_fields(monkeypatch):
    """Each keyterm is a separate repeated `keyterms` multipart entry (the
    scribe_v2 keyword-list contract), not one JSON-array string value."""
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    captured = {}

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        captured["fields"] = fields
        return _FakeResponse(200, SCRIBE_RESPONSE)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    eu.transcribe_segment(b"\x00", "seg.wav", keyterms=["GIB", "dwang", "BRANZ"])
    # one repeated tuple per term, carrying the raw term (no JSON serialization)
    keyterm_vals = [v for (n, v) in captured["fields"] if n == "keyterms"]
    assert keyterm_vals == ["GIB", "dwang", "BRANZ"]


def test_transcribe_segment_caps_keyterm_length_below_50(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    captured = {}

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        captured["fields"] = fields
        return _FakeResponse(200, SCRIBE_RESPONSE)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    long_term = "x" * 60
    eu.transcribe_segment(b"\x00", "seg.wav", keyterms=[long_term])
    keyterm_vals = [v for (n, v) in captured["fields"] if n == "keyterms"]
    assert len(keyterm_vals[0]) <= 49 and len(keyterm_vals[0]) < 50


KEYTERMS_400_BODY = {
    "detail": {
        "type": "validation_error",
        "code": "invalid_parameters",
        "message": "All keywords must be less than 50 characters.",
        "status": "invalid_keyword_length",
        "param": "keywords",
    }
}


def test_transcribe_segment_keyterms_400_retries_once_without_keyterms(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    seq = [_FakeResponse(400, KEYTERMS_400_BODY), _FakeResponse(200, SCRIBE_RESPONSE)]
    calls = []

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        calls.append(fields)
        return seq.pop(0)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    out = eu.transcribe_segment(b"\x00", "seg.wav", keyterms=["GIB", "dwang"])
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    assert seq == []  # exactly two requests: original + one retry
    assert len(calls) == 2
    # first attempt carried keyterms; the retry carried none
    assert any(n == "keyterms" for (n, v) in calls[0])
    assert not any(n == "keyterms" for (n, v) in calls[1])


def test_transcribe_segment_non_keyterms_400_does_not_retry(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    bad_model_body = {
        "detail": {
            "type": "validation_error",
            "code": "invalid_parameters",
            "message": "Invalid model_id.",
            "status": "invalid_model",
            "param": "model_id",
        }
    }
    calls = []

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        calls.append(fields)
        return _FakeResponse(400, bad_model_body)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        eu.transcribe_segment(b"\x00", "seg.wav", keyterms=["GIB"])
    assert len(calls) == 1  # surfaced immediately, no retry


def test_transcribe_segment_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    seq = [_FakeResponse(503, {}), _FakeResponse(200, SCRIBE_RESPONSE)]

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        return seq.pop(0)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    out = eu.transcribe_segment(b"\x00", "seg.wav")
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    assert seq == []  # both responses consumed (one retry)
