"""Task 4c: shared modules (trace, reindex, deletion mirror, match request) and the legacy
downloader go through lake_put.

Global-prefix writers (download_claims/, pending_downloads/) deliberately stay raw (C6).
"""
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

import deletion_mirror  # noqa: E402
import match_request  # noqa: E402
import pipeline_trace  # noqa: E402

BUCKET = "b"


class FakeS3:
    def __init__(self, fail_manifest=False):
        self.puts = []
        self.fail_manifest = fail_manifest

    def put_object(self, **kw):
        if kw["Key"].startswith("lake_manifest/") and self.fail_manifest:
            raise RuntimeError("manifest bucket unreachable")
        self.puts.append(kw)
        return {}

    def manifest_lines(self):
        return [json.loads(p["Body"]) for p in self.puts if p["Key"].startswith("lake_manifest/")]

    def data_puts(self):
        return [p for p in self.puts if not p["Key"].startswith("lake_manifest/")]


@pytest.fixture(autouse=True)
def _trace_on(monkeypatch):
    monkeypatch.setenv("TRACE_EVENTS", "on")
    pipeline_trace._state.set(None)
    yield
    pipeline_trace._state.set(None)


# (a) manifest line per writer --------------------------------------------------------------

def test_trace_flush_adds_exactly_one_manifest_line_per_invocation():
    s3 = FakeS3()
    pipeline_trace.begin("x", user_folder="Ben_Lin", date="2026-10-05", company_id="c-1")
    for _ in range(25):                      # many events, still one object, one manifest line
        pipeline_trace.event("step")
    key = pipeline_trace.flush(s3, BUCKET)
    assert key and key.startswith("traces/2026-10-05/Ben_Lin/")
    assert len(s3.data_puts()) == 1
    (line,) = s3.manifest_lines()
    assert line["key"] == key
    assert line["hint"] == {"folder": "Ben_Lin"}  # never the caller-set company
    assert s3.data_puts()[0]["ContentType"] == "application/x-ndjson"


def test_deletion_mirror_write_mirror_records_a_manifest_line():
    s3 = FakeS3()
    key = deletion_mirror.write_mirror(s3, BUCKET, "Ben_Lin", "2026-10-05", ["s1", "s2"])
    assert key == "redactions/Ben_Lin/2026-10-05/deleted_sessions.json"
    (line,) = s3.manifest_lines()
    assert line["key"] == key and line["hint"] == {"folder": "Ben_Lin"}
    assert s3.data_puts()[0]["ContentType"] == "application/json"


def test_match_request_emit_records_a_manifest_line():
    s3 = FakeS3()
    site = "123e4567-e89b-12d3-a456-426614174000"
    key = match_request.emit(s3, BUCKET, site, "2026-10-05", "extractions/x.json", [{"t": 1}])
    (line,) = s3.manifest_lines()
    assert line["key"] == key and line["hint"] == {"site_id": site}


def test_downloader_upload_keeps_its_content_type_and_records_a_manifest_line(monkeypatch):
    dl = pytest.importorskip("lambda_downloader", reason="requires boto3")
    s3 = FakeS3()
    monkeypatch.setattr(dl, "s3_client", s3)
    key = "users/Ben_Lin/video/2026-10-05/clip.mp4"
    assert dl.upload_to_s3(BUCKET, key, b"abc", "video/mp4") == (True, None)
    (put,) = s3.data_puts()
    assert put["ContentType"] == "video/mp4" and put["Body"] == b"abc"
    (line,) = s3.manifest_lines()
    assert line["key"] == key and line["size"] == 3
    s3.puts.clear()
    dl.upload_to_s3(BUCKET, key, b"abc")                      # no content type -> none sent
    assert "ContentType" not in s3.data_puts()[0]


# (b) no conditional put in this batch: the conditional claims stay raw (global prefixes) ------

def test_the_conditional_download_claims_stay_raw():
    orch = (SRC / "lambda_orchestrator.py").read_text(encoding="utf-8")
    assert 'IfNoneMatch="*"' in orch and "lake_put" not in orch
    dl = (SRC / "lambda_downloader.py").read_text(encoding="utf-8")
    assert re.search(r"s3_client\.put_object\(Bucket=bucket, Key=claim_key", dl)
    assert re.search(r"s3_client\.put_object\(\s*Bucket=bucket,\s*Key=pending_key|pending_downloads/", dl)


# (c) a failing manifest write never fails the writer ---------------------------------------------

def test_a_failing_manifest_write_does_not_fail_trace_flush():
    s3 = FakeS3(fail_manifest=True)
    pipeline_trace.begin("x", user_folder="Ben_Lin", date="2026-10-05")
    pipeline_trace.event("step")
    key = pipeline_trace.flush(s3, BUCKET)
    assert key and [p["Key"] for p in s3.data_puts()] == [key]


def test_a_failing_manifest_write_does_not_fail_the_deletion_mirror():
    s3 = FakeS3(fail_manifest=True)
    key = deletion_mirror.write_mirror(s3, BUCKET, "Ben_Lin", "2026-10-05", ["s1"])
    assert [p["Key"] for p in s3.data_puts()] == [key]


def test_a_failing_manifest_write_does_not_fail_match_request():
    s3 = FakeS3(fail_manifest=True)
    key = match_request.emit(s3, BUCKET, "site-1", "2026-10-05", "extractions/x.json", [{"t": 1}])
    assert [p["Key"] for p in s3.data_puts()] == [key]


# wiring pin ------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["pipeline_trace.py", "reindex.py", "deletion_mirror.py",
                                  "match_request.py"])
def test_adopted_files_have_no_raw_put(name):
    text = (SRC / name).read_text(encoding="utf-8")
    # the docstring in match_request mentions put_object without calling it
    assert not re.search(r"\.(put_object|upload_file)\(", text), name
    assert "lake_put" in text, name


def test_downloader_upload_to_s3_is_the_only_tenant_writer_and_goes_through_lake_put():
    text = (SRC / "lambda_downloader.py").read_text(encoding="utf-8")
    # the two raw puts left are the global download_claims/ and pending_downloads/ writers
    assert text.count("s3_client.put_object(") == 2
    assert "lake_put(bucket, key, data, client=s3_client" in text
