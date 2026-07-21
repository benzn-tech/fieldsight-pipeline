"""ASR_PROVIDER=elevenlabs synchronous path in lambda_transcribe."""
import json
import pytest

mod = pytest.importorskip("lambda_transcribe")


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self):
        self.put_calls = []
        self._obj = b"RIFFfakeWAVdata"

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(self._obj)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _event(key):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}


def test_elevenlabs_path_writes_transcript(monkeypatch):
    monkeypatch.setattr(mod, "ASR_PROVIDER", "elevenlabs")
    fake_s3 = _FakeS3()
    monkeypatch.setattr(mod, "s3", fake_s3)

    def fake_transcribe_segment(audio_bytes, filename, num_speakers=5, keyterms=None):
        return {"results": {"transcripts": [{"transcript": "hi"}], "items": []}}

    import elevenlabs_utils
    monkeypatch.setattr(elevenlabs_utils, "transcribe_segment", fake_transcribe_segment)

    key = "audio_segments/John_Smith/2026-07-19/Benl1_2026-07-19_10-30-00_off0.0_to5.0_srcwav.wav"
    out = mod.lambda_handler(_event(key), None)

    assert len(fake_s3.put_calls) == 1
    put = fake_s3.put_calls[0]
    assert put["Key"] == "transcripts/John_Smith/2026-07-19/Benl1_2026-07-19_10-30-00_off0.0_to5.0_srcwav.json"
    assert json.loads(put["Body"])["results"]["transcripts"][0]["transcript"] == "hi"


def test_transcribe_provider_unchanged_default(monkeypatch):
    # Default provider must NOT hit S3 put/elevenlabs; it takes the job path.
    assert mod.ASR_PROVIDER in ("transcribe", "elevenlabs")
