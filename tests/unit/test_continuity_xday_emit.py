"""Unit: the cross-day request artifact's key and its put (spec X3, hop 1).

Pure: a recording fake client, no boto3. All text synthetic.
"""
import json

import pytest

import continuity_xday_request
import lake_keys

SITE = "22222222-2222-2222-2222-222222222222"
DATE = "2026-11-09"


class _RecS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


def test_key_for_format():
    assert continuity_xday_request.key_for(SITE, DATE, "0123456789abcdef") == (
        f"continuity_xday_requests/{SITE}/{DATE}/0123456789abcdef.json")


def test_key_for_takes_a_date_object():
    import datetime as dt
    assert continuity_xday_request.key_for(SITE, dt.date(2026, 11, 9), "ab") == (
        f"continuity_xday_requests/{SITE}/{DATE}/ab.json")


def test_lake_keys_classifies_the_key_as_site_scoped():
    got = lake_keys.parse(continuity_xday_request.key_for(SITE, DATE, "0123456789abcdef"))
    assert got is not None
    assert got["scope"] == "tenant"
    assert got["kind"] == "xday_request"
    assert got["site_id"] == SITE
    assert got["day"] == DATE
    assert got["folder"] is None and got["company_id"] is None


def test_emit_puts_the_request_at_its_key_and_returns_it(monkeypatch):
    monkeypatch.setenv("LAKE_MANIFEST", "off")
    s3 = _RecS3()
    request = {"site_id": SITE, "date": DATE, "run_hash": "0123456789abcdef",
               "new_items": [{"ref": "N1", "text": "synthetic"}], "candidates": []}
    key = continuity_xday_request.emit(s3, "bucket-x", request)
    assert key == continuity_xday_request.key_for(SITE, DATE, "0123456789abcdef")
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "bucket-x" and put["Key"] == key
    assert put["ContentType"] == "application/json"
    assert json.loads(put["Body"]) == request


@pytest.mark.parametrize("missing", ["site_id", "date", "run_hash"])
def test_emit_refuses_a_request_without_its_key_fields(monkeypatch, missing):
    monkeypatch.setenv("LAKE_MANIFEST", "off")
    s3 = _RecS3()
    request = {"site_id": SITE, "date": DATE, "run_hash": "ab"}
    request[missing] = None
    with pytest.raises(ValueError):
        continuity_xday_request.emit(s3, "bucket-x", request)
    assert s3.puts == []
