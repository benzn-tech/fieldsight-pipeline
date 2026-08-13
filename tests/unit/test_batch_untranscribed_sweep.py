"""Unit: the pass that notices audio nothing else came back for.

Plan: docs/superpowers/plans/2026-08-13-burst-arrival-defects.md phase 6.

Two failures reach this layer and nothing else catches either:

* **a bucket that came due while nobody was listening.** Readiness is elapsed quiet, and
  `seal_ready_runs` only runs when a chunk arrives. Live capture is fine — the next chunk
  re-checks — but a device that uploads its backlog in one burst and then goes silent leaves
  every bucket open forever. The acceptance replay produced exactly this: 153 chunks
  registered, zero batches, until something poked the session by hand.
* **a batch that was sealed and never transcribed.** 27 of these on the first replay, all
  HTTP 429 from the provider's concurrency ceiling, each caught by a per-record `except`,
  reported as `status: error`, and never revisited. About 54 minutes of audio.

Both are "somebody has to come back", which is what a sweep is.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import batch_redrive  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
PREFIX = f"audio_segments/Ben_UCPK/2026-08-13"
BATCH = f"{PREFIX}/Benl1_2026-08-13_09-00-00_sid{SID}_c0004_bn4_off0.0_to114.0_srcwav.wav"


class FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.copies = []

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, Bucket=None, Prefix=None):
                yield {"Contents": [{"Key": k, "LastModified": v}
                                    for k, v in outer.objects.items()
                                    if k.startswith(Prefix)]}
        return P()

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": 1}

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.copies.append(Key)


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item=None, ConditionExpression=None):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None):
        pk = ExpressionAttributeValues[":pk"]
        sk = ExpressionAttributeValues.get(":sk", "")
        return {"Items": [v for (p, s), v in sorted(self.items.items())
                          if p == pk and s.startswith(sk)]}


def _aged(seconds_old):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_old)


def test_a_batch_wav_with_no_transcript_past_the_grace_is_redriven():
    """The 429 loss. The WAV is on S3, the transcript never landed, and the ledger says
    `sealed` so no planner will ever look at it again."""
    s3 = FakeS3({BATCH: _aged(1000)})
    table = FakeTable()
    out = batch_redrive.redrive_untranscribed(s3, "b", SID, table, redrive_after_sec=900)
    assert s3.copies == [BATCH]
    assert out["redriven"] == 1


def test_a_batch_with_a_transcript_is_left_alone():
    """The negative, through the same key derivation the reader uses -- never a hand-built
    key, which is how the transcripts/ vs audio_segments/ mismatch survived a green suite."""
    import batch_stitch as bs
    s3 = FakeS3({BATCH: _aged(1000),
                 bs.map_key_for_transcript(BATCH) or "": _aged(1000)})
    s3.objects[BATCH.replace("audio_segments/", "transcripts/").replace(".wav", ".json")] = \
        _aged(1000)
    out = batch_redrive.redrive_untranscribed(s3, "b", SID, FakeTable(),
                                              redrive_after_sec=900)
    assert s3.copies == [] and out["redriven"] == 0


def test_a_young_batch_is_not_redriven():
    """Its transcript may simply not have landed yet."""
    s3 = FakeS3({BATCH: _aged(10)})
    batch_redrive.redrive_untranscribed(s3, "b", SID, FakeTable(), redrive_after_sec=900)
    assert s3.copies == []


def test_the_redrive_is_bounded_and_the_exhaustion_is_loud(caplog):
    """A sweep that retries forever is a bill that grows forever. At the cap it stops and
    says which batch it gave up on."""
    import logging
    s3 = FakeS3({BATCH: _aged(1000)})
    table = FakeTable()
    for _ in range(3):
        batch_redrive.redrive_untranscribed(s3, "b", SID, table, redrive_after_sec=900,
                                            max_attempts=3)
    assert len(s3.copies) == 3
    with caplog.at_level(logging.ERROR):
        batch_redrive.redrive_untranscribed(s3, "b", SID, table, redrive_after_sec=900,
                                            max_attempts=3)
    assert len(s3.copies) == 3, "past the cap it must stop"
    assert any(BATCH.rsplit("/", 1)[-1] in r.getMessage() for r in caplog.records), \
        "and name the batch it gave up on"


def test_the_sweep_reports_a_zero_count(caplog):
    """Positive evidence. A guard that speaks only on failure cannot be told apart from one
    that never ran -- three IAM omissions in this feature looked exactly like nothing."""
    import logging
    with caplog.at_level(logging.INFO):
        batch_redrive.redrive_untranscribed(FakeS3({}), "b", SID, FakeTable())
    assert any("candidates=0" in r.getMessage() for r in caplog.records)
