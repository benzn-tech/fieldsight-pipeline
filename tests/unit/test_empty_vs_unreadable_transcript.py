"""Unit: "nothing was said" and "we could not read this" are different events.

`normalize_transcript` returns None for both — `not parsed['full_text']` covers
a transcript with no words and a file that isn't a transcript at all — and
`assemble_deduped_turns` reported both as "Skipping unnormalizable transcript
segment".

That cost a real investigation. Nine segments of prod session sid61be49d5...
(Ben_UCPK2, 2026-08-07) were logged that way; the word "unnormalizable" reads
as corruption, so it went into the roadmap as "a different silent loss, not yet
diagnosed". They were 355-byte AWS Transcribe results, `status: COMPLETED`,
`transcript: ""`, `items: []` — and every one of their VAD sidecars says
`vad_result: fallback_full_audio, speech_duration_sec: 0`. Nothing was lost.
The audio was silent, Transcribe agreed, and the log said the wrong thing about
why.

The distinction matters more from here on. With `DROP_SILENT_CHUNKS` on (the
default since the silence work), a silent chunk is never transcribed at all —
so an empty transcript now means **the transcriber found nothing in audio that
VAD did judge to be speech**. That is exactly the too-quiet signal the loudness
normalisation targets, and the before/after metric for whether it helped. Filed
under the same message as a corrupt file, it cannot be counted.
"""
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session")


# The real 355-byte artifact, trimmed to the fields that matter.
REAL_EMPTY = {
    "jobName": "fieldsight_Ben_UCPK2_..._c0004_off0_0_to30_0_srcwav",
    "accountId": "509194952652",
    "status": "COMPLETED",
    "results": {
        "language_code": "en-US",
        "transcripts": [{"transcript": ""}],
        "speaker_labels": None,
        "items": [],
        "audio_segments": [],
    },
}


def test_the_real_prod_artifact_is_recognised_as_empty():
    assert les._is_empty_transcript(REAL_EMPTY)


@pytest.mark.parametrize("payload", [
    {"results": {"transcripts": [{"transcript": ""}], "items": []}},
    {"results": {"transcripts": [{"transcript": "   "}], "items": []}},
    {"results": {"transcripts": [], "items": []}},
])
def test_transcripts_with_no_words_are_empty_not_unreadable(payload):
    assert les._is_empty_transcript(payload)


@pytest.mark.parametrize("payload", [
    None,
    "not a dict",
    {},                                        # no results at all
    {"results": None},
    {"results": {}},                           # no transcripts key
    {"results": {"transcripts": "not a list"}},
    {"error": "AccessDenied"},                 # something else entirely
])
def test_anything_not_recognisably_a_transcript_is_left_as_unreadable(payload):
    """Conservative on purpose: a genuinely broken file must never be quietly
    downgraded to "nothing was said", which would hide a real failure behind a
    benign message — the same mistake in the opposite direction."""
    assert not les._is_empty_transcript(payload)


def test_a_transcript_with_words_is_not_empty():
    assert not les._is_empty_transcript(
        {"results": {"transcripts": [{"transcript": "check the scaffold tags"}]}})


# ------------------------------------------------------------------
# What the log actually says
# ------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        return _FakePaginator(self.objects)


KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_off0.0_to30.0_srcwav.json"


def test_an_empty_transcript_is_not_reported_as_a_failure(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(les, "s3", lambda: _FakeS3({KEY: json.dumps(REAL_EMPTY)}))

    turns, sources = les.assemble_deduped_turns("bucket", [KEY])

    assert turns == [] and sources == []
    text = caplog.text
    assert "unnormalizable" not in text, "the word that sent the last reader hunting"
    assert "empty result" in text
    assert "NOT a parse failure" in text
    assert not [r for r in caplog.records if r.levelname == "WARNING"], (
        "a silent chunk is expected, not a warning")


def test_a_genuinely_unreadable_transcript_still_warns(monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(les, "s3", lambda: _FakeS3(
        {KEY: json.dumps({"something": "that is not a transcript"})}))

    turns, sources = les.assemble_deduped_turns("bucket", [KEY])

    assert turns == [] and sources == []
    assert any(r.levelname == "WARNING" and "unreadable" in r.getMessage()
               for r in caplog.records)


def test_the_two_cases_use_different_messages(monkeypatch, caplog):
    """The whole point: grep has to be able to tell them apart, because one is
    routine and the other is a fault."""
    caplog.set_level("INFO")
    empty_key = KEY
    bad_key = KEY.replace("10-00-00", "10-01-00")
    monkeypatch.setattr(les, "s3", lambda: _FakeS3({
        empty_key: json.dumps(REAL_EMPTY),
        bad_key: json.dumps({"not": "a transcript"}),
    }))

    les.assemble_deduped_turns("bucket", [empty_key, bad_key])

    messages = [r.getMessage() for r in caplog.records]
    assert len({m.split(" ")[0] for m in messages}) > 1
    assert any("empty result" in m for m in messages)
    assert any("unreadable" in m for m in messages)
