"""Unit: `self_introductions` rides the extraction artifact beside `speaker_turns`.

Final pass only, like `speaker_turns` -- a live artifact is provisional and re-runs
constantly; asking the same person to confirm the same intro every 90 seconds would be
worse than not asking at all. See the design doc's data-path section and the plan's
Task 2.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")
import llm_utils  # noqa: E402
import self_introduction  # noqa: E402


class FakeNoSuchKey(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeNoSuchKey()
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


def _fake_call_llm_returning(payload):
    def _fn(prompt, max_tokens=4096, force_json=False, enable_thinking=None, **kw):
        return json.dumps(payload), None
    return _fn


def make_transcribe_json(text, start=0.0, end=None, speaker=None):
    words = text.split()
    n = len(words) or 1
    if end is None:
        end = start + n
    step = (end - start) / n
    items = []
    t = start
    for w in words:
        item = {
            "type": "pronunciation",
            "start_time": f"{t:.3f}",
            "end_time": f"{t + step:.3f}",
            "alternatives": [{"content": w, "confidence": "0.9"}],
        }
        if speaker:
            item["speaker_label"] = speaker
        items.append(item)
        t += step
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


BUCKET = "test-bucket"
SEG1_KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_off0.0_to30.0_srcwav.json"
SEG2_KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_off30.0_to60.0_srcwav.json"
SESSION_BASE = "Benl1_2026-07-06_10-00-00"


@pytest.fixture(autouse=True)
def reset_site_cache():
    les._sites_cache = None
    yield
    les._sites_cache = None


def _fake_s3_with_intro():
    return FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json(
            "Hi this is Petros from Cassidy", start=0.0, speaker="spk_0")),
        SEG2_KEY: json.dumps(make_transcribe_json(
            "check the scaffold today", start=30.0, speaker="spk_0")),
    })


def test_final_pass_carries_self_introductions(monkeypatch):
    monkeypatch.setattr(les, "s3", lambda: _fake_s3_with_intro())
    monkeypatch.setattr(llm_utils, "call_llm",
                        _fake_call_llm_returning({"topics": [], "declared_site": None}))

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert extraction["self_introductions"] == [{
        "source_filename": os.path.basename(SEG1_KEY),
        "speaker_label": "spk_0",
        "start_sec": 0.0, "end_sec": 6.0,
        "heard_name": "Petros", "company_name": "Cassidy",
        "quote": "Hi this is Petros from Cassidy",
    }]


def test_live_pass_carries_no_self_introductions(monkeypatch):
    """Same reasoning as `speaker_turns`: a provisional pass must not queue a
    suggestion that a re-run five seconds later would duplicate."""
    monkeypatch.setattr(les, "s3", lambda: _fake_s3_with_intro())
    monkeypatch.setattr(llm_utils, "call_llm",
                        _fake_call_llm_returning({"topics": [], "declared_site": None}))

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=False)

    assert extraction["self_introductions"] == []


def test_self_introductions_and_speaker_turns_agree_on_file_and_label(monkeypatch):
    """The seam `test_anonymous_speaker_rebind.py:77` guards for the re-bind's
    `source_filename` spelling -- guarded here for the same reason and the same
    field: two producers of `(source_filename, speaker_label)` disagreeing is
    invisible from either side alone."""
    monkeypatch.setattr(les, "s3", lambda: _fake_s3_with_intro())
    monkeypatch.setattr(llm_utils, "call_llm",
                        _fake_call_llm_returning({"topics": [], "declared_site": None}))

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    turn_keys = {(t["source_filename"], t["speaker_label"]) for t in extraction["speaker_turns"]}
    intro_keys = {(i["source_filename"], i["speaker_label"])
                  for i in extraction["self_introductions"]}
    assert intro_keys <= turn_keys


def test_a_detector_exception_never_fails_the_extraction(monkeypatch, caplog):
    monkeypatch.setattr(les, "s3", lambda: _fake_s3_with_intro())
    monkeypatch.setattr(llm_utils, "call_llm",
                        _fake_call_llm_returning({"topics": [], "declared_site": None}))

    def _boom(turns, min_turn_sec=3.0):
        raise RuntimeError("boom")

    monkeypatch.setattr(self_introduction, "find", _boom)
    caplog.set_level("ERROR")

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert extraction["self_introductions"] == []
    assert "self_introduction" in caplog.text or "introduction" in caplog.text
