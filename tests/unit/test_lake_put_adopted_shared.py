"""Task 4c: shared modules (trace, reindex, deletion mirror, match request) go through
lake_put. (The RealPTT downloader that was also adopted here was removed 2026-10-29.)
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

