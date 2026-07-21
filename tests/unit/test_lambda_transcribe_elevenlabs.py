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


class _FakeBadRequestException(Exception):
    pass


class _FakeExceptions:
    BadRequestException = _FakeBadRequestException


class _FakeTranscribeClient:
    """Fake AWS Transcribe client for the default ASR_PROVIDER='transcribe' path.

    get_transcription_job raises BadRequestException (job not found) so the
    handler falls through to start_transcription_job — proving the async
    default path still reaches job creation after the idempotency-check
    guard was reordered behind `if ASR_PROVIDER == 'transcribe':`.
    """

    def __init__(self):
        self.exceptions = _FakeExceptions()
        self.start_calls = []

    def get_transcription_job(self, TranscriptionJobName):
        raise self.exceptions.BadRequestException("job not found")

    def start_transcription_job(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}}


def test_transcribe_default_path_starts_job(monkeypatch):
    # Default provider must reach start_transcription_job (async path), and
    # must NOT take the elevenlabs synchronous S3 put path.
    monkeypatch.setattr(mod, "ASR_PROVIDER", "transcribe")

    fake_transcribe = _FakeTranscribeClient()
    monkeypatch.setattr(mod, "transcribe", fake_transcribe)

    fake_s3 = _FakeS3()
    monkeypatch.setattr(mod, "s3", fake_s3)

    key = "audio_segments/John_Smith/2026-07-19/Benl1_2026-07-19_10-30-00_off0.0_to5.0_srcwav.wav"
    out = mod.lambda_handler(_event(key), None)

    assert len(fake_transcribe.start_calls) == 1
    assert fake_transcribe.start_calls[0]["TranscriptionJobName"]

    # elevenlabs sync path must not have written any transcript to S3
    assert fake_s3.put_calls == []

    body = json.loads(out["body"])
    assert body["summary"]["started"] == 1
    assert body["results"][0]["status"] == "started"
