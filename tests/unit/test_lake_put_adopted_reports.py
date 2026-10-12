"""Task 4b: report / session / embedding writers go through lake_put.

Behavioural pins plus a wiring scan over the adopted files.
"""
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

import transcript_utils  # noqa: E402

BUCKET = "test-bucket"


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
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        body = self.objects[Key]
        raw = body.encode() if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw), "ETag": '"etag-7"'}

    def get_paginator(self, op):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objects if k.startswith(Prefix)]}
        return _P()

    def manifest_lines(self):
        return [json.loads(p["Body"]) for p in self.puts if p["Key"].startswith("lake_manifest/")]

    def data_puts(self):
        return [p for p in self.puts if not p["Key"].startswith("lake_manifest/")]


# (a) a representative writer in each module produces a manifest line -------------------

def test_write_meeting_manifest_records_a_manifest_line():
    s3 = FakeS3()
    key = transcript_utils.write_meeting_manifest(s3, BUCKET, "reports/", "2026-10-01",
                                                  "Ben_Lin", ["transcripts/a.json"], "Standup")
    assert key == "reports/2026-10-01/Ben_Lin/.meeting_manifest.json"
    (line,) = s3.manifest_lines()
    assert line["key"] == key and line["bucket"] == BUCKET
    assert line["hint"] == {"folder": "Ben_Lin"}
    assert s3.data_puts()[0]["ContentType"] == "application/json"


def test_embed_report_vectors_writer_records_a_manifest_line(monkeypatch):
    ler = pytest.importorskip("lambda_embed_report", reason="requires psycopg")
    ing = pytest.importorskip("lambda_ingest", reason="requires psycopg")
    report_key = "reports/2026-03-02/Jarley_Trainor/daily_report.json"
    report = {"report_date": "2026-03-02", "user_name": "Jarley Trainor", "site": "S",
              "topics": [{"topic_id": 0, "time_range": "09:00 – 09:05", "topic_title": "T",
                          "category": "safety", "participants": ["Jarley Trainor"],
                          "summary": "Summary text.", "key_decisions": [], "action_items": [],
                          "safety_flags": []}]}
    s3 = FakeS3(objects={report_key: json.dumps(report)})
    for mod in (ler, ing):
        monkeypatch.setattr(mod, "_s3_client", s3)
        monkeypatch.setattr(mod, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(ler.dashscope_utils, "embed",
                        lambda texts, dim=None: [[0.1] * 1024 for _ in texts])
    ler.embed_report("2026-03-02", "Jarley_Trainor", report_key)
    keys = [l["key"] for l in s3.manifest_lines()]
    assert keys == ["embeddings/2026-03-02/Jarley_Trainor/vectors.json"]
    assert s3.manifest_lines()[0]["hint"] == {"folder": "Jarley_Trainor"}


def test_voice_audit_sidecar_records_a_manifest_line(monkeypatch):
    va = pytest.importorskip("lambda_voice_audit", reason="requires psycopg")
    import boto3
    s3 = FakeS3()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(va, "S3_BUCKET", BUCKET)
    key = va._write_sidecar("Ben_Lin", "the answer")
    assert [l["key"] for l in s3.manifest_lines()] == [key]
    assert s3.manifest_lines()[0]["hint"] == {"folder": "Ben_Lin"}


# (b) a conditional put still sends its condition ---------------------------------------

def test_session_finalize_recheck_still_sends_if_match(monkeypatch):
    fin = pytest.importorskip("lambda_session_finalize", reason="requires psycopg")
    import boto3
    sid, folder, date = "a" * 32, "Ben_Lin", "2026-10-01"
    key = f"extractions/{folder}/{date}/sid{sid}.json"
    s3 = FakeS3(objects={key: json.dumps({"tier": "final", "topics": []})})
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(fin, "S3_BUCKET", BUCKET)
    assert fin._recheck_final_extraction({"sessionId": sid, "folder": folder, "date": date}) is True
    (put,) = s3.data_puts()
    assert put["Key"] == key and put["IfMatch"] == '"etag-7"'
    assert [l["key"] for l in s3.manifest_lines()] == [key]


# (c) a failing manifest write never fails the writer -----------------------------------

def test_a_failing_manifest_write_does_not_fail_write_meeting_manifest():
    s3 = FakeS3(fail_manifest=True)
    key = transcript_utils.write_meeting_manifest(s3, BUCKET, "reports/", "2026-10-01",
                                                  "Ben_Lin", [], "Standup")
    assert [p["Key"] for p in s3.data_puts()] == [key]


def test_a_failing_manifest_write_does_not_fail_the_embed_writer(monkeypatch):
    ler = pytest.importorskip("lambda_embed_report", reason="requires psycopg")
    ing = pytest.importorskip("lambda_ingest", reason="requires psycopg")
    report_key = "reports/2026-03-02/Jarley_Trainor/daily_report.json"
    report = {"report_date": "2026-03-02", "user_name": "J", "site": "S",
              "topics": [{"topic_id": 0, "time_range": "09:00 – 09:05", "topic_title": "T",
                          "category": "safety", "participants": [], "summary": "Text.",
                          "key_decisions": [], "action_items": [], "safety_flags": []}]}
    s3 = FakeS3(objects={report_key: json.dumps(report)}, fail_manifest=True)
    for mod in (ler, ing):
        monkeypatch.setattr(mod, "_s3_client", s3)
        monkeypatch.setattr(mod, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(ler.dashscope_utils, "embed",
                        lambda texts, dim=None: [[0.1] * 1024 for _ in texts])
    ler.embed_report("2026-03-02", "Jarley_Trainor", report_key)
    assert [p["Key"] for p in s3.data_puts()] == ["embeddings/2026-03-02/Jarley_Trainor/vectors.json"]


# wiring pin ----------------------------------------------------------------------------------

ADOPTED = ["lambda_report_generator.py", "lambda_meeting_minutes.py", "lambda_embed_report.py",
           "lambda_rolling_summary.py", "lambda_session_finalize.py", "lambda_session_report.py",
           "transcript_utils.py", "lambda_voice_audit.py"]


@pytest.mark.parametrize("name", ADOPTED)
def test_adopted_files_have_no_raw_put_or_upload(name):
    text = (SRC / name).read_text(encoding="utf-8")
    assert not re.search(r"\.(put_object|upload_file)\(", text), name
    assert "lake_put" in text, name
