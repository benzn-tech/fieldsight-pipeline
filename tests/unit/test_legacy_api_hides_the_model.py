"""The legacy gateway (`fieldsight-prod-api`, still live -- see
test_legacy_api_honours_deletions.py) serves `daily_report.json` /
`summary_report.json` to the customer-facing site byte-for-byte in three
places (get_timeline x2, find_any_report). `_report_metadata.model` is an
internal provenance field -- the report generator's own debug record and the
stored S3 JSON both carry it deliberately, so a bad report can still be
traced to the model that wrote it -- but this gateway is the door the
customer's browser walks through, and it must not carry the vendor/model out
with it.
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

api = pytest.importorskip("lambda_fieldsight_api")


# ------------------------------------------------------- the pure helper

def test_without_vendor_metadata_strips_only_model():
    doc = {"executive_summary": ["x"],
           "_report_metadata": {"generated_at": "t", "model": "claude-sonnet-5",
                                 "recordings_processed": 4}}
    out = api._without_vendor_metadata(doc)
    assert "model" not in out["_report_metadata"]
    assert out["_report_metadata"]["recordings_processed"] == 4
    assert out["executive_summary"] == ["x"]
    # The input is untouched -- this redacts a copy, not the internal record.
    assert doc["_report_metadata"]["model"] == "claude-sonnet-5"


def test_without_vendor_metadata_is_a_no_op_when_nothing_to_strip():
    no_meta = {"topics": []}
    assert api._without_vendor_metadata(no_meta) is no_meta

    no_model = {"_report_metadata": {"source": "nightly"}}
    assert api._without_vendor_metadata(no_model) is no_model

    assert api._without_vendor_metadata(None) is None
    assert api._without_vendor_metadata("not a dict") == "not a dict"


# ------------------------------------------------- wired through get_timeline

class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class _S3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise api.s3_client.exceptions.NoSuchKey({}, "get_object")
        return {"Body": _Body(self.objects[Key])}


@pytest.fixture(autouse=True)
def _no_deletions(monkeypatch):
    monkeypatch.setattr(api, "_deleted_bases", lambda folder, date: set())
    monkeypatch.setattr(api, "_any_folder_deleted_on", lambda date: False)


def test_get_timeline_strips_the_model_for_a_named_user(monkeypatch):
    stored = {"executive_summary": ["A day happened."],
              "_report_metadata": {"model": "meta/muse-spark-1.3-contributor",
                                    "recordings_processed": 2}}
    monkeypatch.setattr(api, "s3_client",
                        _S3({f"{api.REPORT_PREFIX}2026-09-20/Ben_UCPK2/daily_report.json": stored}))
    monkeypatch.setattr(api, "can_access_user_data", lambda caller, user: True)
    caller = {"role": "pm"}
    res = api.get_timeline({"date": "2026-09-20", "user": "Ben_UCPK2"}, caller)
    body = json.loads(res["body"])
    assert "model" not in body["_report_metadata"]
    assert body["_report_metadata"]["recordings_processed"] == 2
    assert body["executive_summary"] == ["A day happened."]


def test_get_timeline_strips_the_model_for_the_admin_summary(monkeypatch):
    stored = {"topics": [{"topic_title": "x"}],
              "_report_metadata": {"model": "gemini-3.8-flash"}}
    monkeypatch.setattr(api, "s3_client",
                        _S3({f"{api.REPORT_PREFIX}2026-09-20/summary_report.json": stored}))
    caller = {"role": "admin"}
    res = api.get_timeline({"date": "2026-09-20"}, caller)
    body = json.loads(res["body"])
    assert "model" not in body["_report_metadata"]
    assert body["topics"] == [{"topic_title": "x"}]


def test_find_any_report_strips_the_model_on_the_single_candidate_path(monkeypatch):
    stored = {"topics": [], "_report_metadata": {"model": "qwen3.6-flash"}}
    key = f"{api.REPORT_PREFIX}2026-09-20/Solo_Folder/daily_report.json"

    class _Listable(_S3):
        def list_objects_v2(self, Bucket, Prefix):
            return {"Contents": [{"Key": key}]}

    monkeypatch.setattr(api, "s3_client", _Listable({key: stored}))
    res = api.find_any_report("2026-09-20", caller=None)
    body = json.loads(res["body"])
    assert "model" not in body["_report_metadata"]
