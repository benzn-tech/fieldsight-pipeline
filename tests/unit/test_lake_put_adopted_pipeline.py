"""Task 4a: the pipeline-stage writers go through lake_put.

Behavioural pins (one writer per kind) plus a wiring scan over the adopted files.
"""
import io
import json
import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

import extraction_pending as ep  # noqa: E402
import keyframe_request  # noqa: E402

BUCKET = "b"
BASE = "sidaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _Precondition(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class FakeS3:
    def __init__(self, fail_manifest=False, objects=None):
        self.puts = []
        self.fail_manifest = fail_manifest
        self.objects = dict(objects or {})

    def put_object(self, **kw):
        if kw["Key"].startswith("lake_manifest/") and self.fail_manifest:
            raise RuntimeError("manifest bucket unreachable")
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]
        return {"ETag": '"new"'}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _Missing()
        body = self.objects[Key]
        raw = body.encode() if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw), "ETag": '"etag-1"'}

    def manifest_lines(self):
        return [json.loads(p["Body"]) for p in self.puts if p["Key"].startswith("lake_manifest/")]

    def data_puts(self):
        return [p for p in self.puts if not p["Key"].startswith("lake_manifest/")]


MARKER = {"userFolder": "Ben_Lin", "date": "2026-10-01", "sessionBase": BASE,
          "request_key": "extraction_requests/aaaa.json"}


# (a) a representative writer in each module produces a manifest line -------------------

def test_extraction_pending_write_records_a_manifest_line():
    s3 = FakeS3()
    ep.write(s3, BUCKET, dict(MARKER))
    lines = s3.manifest_lines()
    assert [l["key"] for l in lines] == [ep.marker_key(BASE)]
    assert lines[0]["hint"] == {"folder": "Ben_Lin", "session_base": BASE}


def test_keyframe_request_emit_records_a_manifest_line():
    s3 = FakeS3()
    key = keyframe_request.emit(s3, BUCKET, "Ben_Lin", "2026-10-01", BASE, "extractions/x.json",
                                [{"topic_id": "t", "time_range": "00:00-00:03"}])
    assert [l["key"] for l in s3.manifest_lines()] == [key]
    assert s3.manifest_lines()[0]["hint"]["folder"] == "Ben_Lin"


# (b) a conditional put still sends its condition ---------------------------------------

def test_extraction_pending_update_still_sends_if_match():
    s3 = FakeS3(objects={ep.marker_key(BASE): json.dumps(MARKER)})
    ep.update(s3, BUCKET, BASE, lambda m: m.update(attempts=1))
    (put,) = s3.data_puts()
    assert put["IfMatch"] == '"etag-1"'
    assert put["ContentType"] == "application/json"
    assert [l["key"] for l in s3.manifest_lines()] == [ep.marker_key(BASE)]


def test_finalize_claim_enqueue_still_sends_if_none_match(monkeypatch):
    import boto3
    import lambda_finalize_claim as fc
    s3 = FakeS3()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    assert fc._enqueue({"sessionId": "f" * 32}) is True
    (put,) = s3.data_puts()
    assert put["IfNoneMatch"] == "*"
    assert [l["key"] for l in s3.manifest_lines()] == [put["Key"]]


def test_finalize_claim_enqueue_412_is_still_a_clean_false(monkeypatch):
    import boto3
    from botocore.exceptions import ClientError
    import lambda_finalize_claim as fc

    class Lose(FakeS3):
        def put_object(self, **kw):
            raise ClientError({"Error": {"Code": "PreconditionFailed"},
                               "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")

    s3 = Lose()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    assert fc._enqueue({"sessionId": "f" * 32}) is False
    assert s3.puts == []        # the loser writes no manifest line either


# (c) a failing manifest write never fails the writer -----------------------------------

def test_a_failing_manifest_write_does_not_fail_extraction_pending():
    s3 = FakeS3(fail_manifest=True)
    ep.write(s3, BUCKET, dict(MARKER))
    assert [p["Key"] for p in s3.data_puts()] == [ep.marker_key(BASE)]


def test_a_failing_manifest_write_does_not_fail_keyframe_emit():
    s3 = FakeS3(fail_manifest=True)
    key = keyframe_request.emit(s3, BUCKET, "Ben_Lin", "2026-10-01", BASE, "extractions/x.json",
                                [{"topic_id": "t", "time_range": "00:00-00:03"}])
    assert key and [p["Key"] for p in s3.data_puts()] == [key]


# wiring pin: no raw put on the adopted files ----------------------------------------------

ADOPTED = ["lambda_vad.py", "lambda_transcribe.py", "batch_seal.py", "lambda_extract_session.py",
           "lambda_finalize_claim.py", "lambda_item_writer.py", "keyframe_request.py",
           "lambda_keyframe.py", "lambda_extraction_backlog.py", "extraction_pending.py"]


@pytest.mark.parametrize("name", ADOPTED)
def test_adopted_files_have_no_raw_put_or_upload(name):
    text = (SRC / name).read_text(encoding="utf-8")
    assert not re.search(r"\.(put_object|upload_file)\(", text), name
    assert "lake_put" in text or "lake_upload_file" in text, name
