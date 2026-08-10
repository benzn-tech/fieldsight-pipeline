"""Pins the DashScope file-transcription contract.

Every assertion here corresponds to a mistake that has actually been made, four
times across separate sessions. The point is not coverage — it is that the next
person to guess at this call gets a red test instead of a misleading API error.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
os.environ.setdefault("DASHSCOPE_API_KEY", "testing")

import dashscope_utils as ds  # noqa: E402


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakeHttp:
    """Records every request so the body can be asserted, and replays a script."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body) if body else None})
        return self.script.pop(0)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(ds, "DASHSCOPE_API_KEY", "testing")


def _install(monkeypatch, script):
    fake = FakeHttp(script)
    monkeypatch.setattr(ds.urllib3, "PoolManager", lambda *a, **k: fake)
    return fake


SUBMIT = FakeResp(200, {"output": {"task_id": "t1"}})
def done(url="https://x/result.json"):
    return FakeResp(200, {"output": {"task_status": "SUCCEEDED",
                                     "result": {"transcription_url": url}}})
RESULT = FakeResp(200, {"transcripts": [{"text": "hi", "sentences":
                                         [{"speaker_id": 0, "text": "hi"}]}]})


def test_input_field_is_file_urls_plural():
    """The singular name is reported as InvalidParameter.MalformedURL — an error
    that points at the URL, not at the field. That misread cost two rounds."""
    import inspect
    src = inspect.getsource(ds.transcribe_file)
    assert '"file_urls": [file_url]' in src
    assert '"file_url":' not in src


def test_submit_body_shape(monkeypatch):
    fake = _install(monkeypatch, [SUBMIT, done(), RESULT])
    ds.transcribe_file("https://audio", speaker_count=2, _sleep=lambda s: None)
    body = fake.calls[0]["body"]
    assert body["model"] == "qwen-audio-3.0-asr-flash-filetrans"
    assert body["input"] == {"file_urls": ["https://audio"]}
    assert body["parameters"]["diarization_enabled"] is True
    assert body["parameters"]["speaker_count"] == 2


def test_diarization_is_on_by_default(monkeypatch):
    """A call made without diarization proves nothing about whether the model
    supports speakers. That exact non-result was recorded as 'qwen does not
    diarize' and repeated for weeks."""
    fake = _install(monkeypatch, [SUBMIT, done(), RESULT])
    ds.transcribe_file("https://audio", _sleep=lambda s: None)
    assert fake.calls[0]["body"]["parameters"]["diarization_enabled"] is True


def test_hotwords_go_inline_with_weights(monkeypatch):
    fake = _install(monkeypatch, [SUBMIT, done(), RESULT])
    ds.transcribe_file("https://audio", hotwords=["Bunnings", "Halswell"],
                       _sleep=lambda s: None)
    assert fake.calls[0]["body"]["parameters"]["vocabulary"] == {
        "Bunnings": 5, "Halswell": 5}

    fake = _install(monkeypatch, [SUBMIT, done(), RESULT])
    ds.transcribe_file("https://audio", hotwords={"Mitre 10": 4},
                       _sleep=lambda s: None)
    assert fake.calls[0]["body"]["parameters"]["vocabulary"] == {"Mitre 10": 4}


def test_transcript_is_fetched_from_a_second_url(monkeypatch):
    """The task payload never contains the text. Returning it directly would
    hand callers a URL and an empty transcript."""
    fake = _install(monkeypatch, [SUBMIT, done("https://x/r.json"), RESULT])
    out = ds.transcribe_file("https://audio", _sleep=lambda s: None)
    assert out["transcripts"][0]["text"] == "hi"
    assert fake.calls[-1]["url"] == "https://x/r.json"


def test_fun_asr_result_shape_is_also_handled(monkeypatch):
    """qwen puts it at output.result, fun-asr at output.results[0]. Handling one
    shape only would make a model swap return nothing, silently."""
    alt = FakeResp(200, {"output": {"task_status": "SUCCEEDED",
                                    "results": [{"transcription_url": "https://x/r.json"}]}})
    _install(monkeypatch, [SUBMIT, alt, RESULT])
    out = ds.transcribe_file("https://audio", _sleep=lambda s: None)
    assert out["transcripts"][0]["sentences"][0]["speaker_id"] == 0


def test_instance_pool_exhausted_is_retried(monkeypatch):
    """Vendor capacity, not a bad request: the identical body succeeds minutes
    later. Failing hard here invites 'this model does not work' conclusions."""
    busy = FakeResp(200, {"output": {"task_status": "FAILED",
                                     "code": "INSTANCE_POOL_EXHAUSTED"}})
    fake = _install(monkeypatch, [SUBMIT, busy, SUBMIT, done(), RESULT])
    out = ds.transcribe_file("https://audio", _sleep=lambda s: None)
    assert out["transcripts"][0]["text"] == "hi"
    assert sum(1 for c in fake.calls if c["method"] == "POST") == 2


def test_file_download_failed_is_not_retried(monkeypatch):
    """An unreachable URL will not become reachable by asking again — and it is
    usually an expired presigned URL, which retrying only hides."""
    bad = FakeResp(200, {"output": {"task_status": "FAILED",
                                    "code": "FILE_DOWNLOAD_FAILED"}})
    fake = _install(monkeypatch, [SUBMIT, bad])
    with pytest.raises(RuntimeError, match="FILE_DOWNLOAD_FAILED"):
        ds.transcribe_file("https://audio", _sleep=lambda s: None)
    assert sum(1 for c in fake.calls if c["method"] == "POST") == 1


def test_transcription_endpoint_is_not_the_single_shot_endpoint():
    """Two different APIs with different body shapes. Pointing one at the other
    is the root of the whole family of mistakes."""
    assert ds.DASHSCOPE_TRANSCRIPTION_URL.endswith("/audio/asr/transcription")
    assert "multimodal-generation" in ds.DASHSCOPE_AIGC_URL
    assert ds.DASHSCOPE_TRANSCRIPTION_URL != ds.DASHSCOPE_AIGC_URL


def test_model_id_carries_filetrans():
    """An id without it is rejected on this endpoint as 'Model not exist.',
    which reads like the model was withdrawn."""
    assert "filetrans" in ds.DASHSCOPE_FILETRANS_MODEL
    assert ds.DASHSCOPE_FILETRANS_MODEL != ds.DASHSCOPE_ASR_MODEL
