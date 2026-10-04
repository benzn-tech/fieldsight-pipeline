"""Unit: the legacy gateway never reads or hands out the lake-wide summary.

`reports/{date}/summary_report.json` is built from EVERY tenant's daily reports. This
gateway has no company concept (roles come from DynamoDB, max admin/gm), so it cannot tell
a customer's admin from the operator. Owner decision 2026-10-05: no role gets it here;
org-api serves it to platform_admin only. Three doors: get_timeline, get_dates (count
fallback), get_presigned_url.
"""
import datetime
import json

import pytest

api = pytest.importorskip("lambda_fieldsight_api")

DATE = "2026-08-14"
SUMMARY_KEY = f"reports/{DATE}/summary_report.json"
SUMMARY_DOC = {"topics": [{"topic_title": "OTHER TENANT SECRET", "category": "safety"}] * 7,
               "marker": "lake-wide"}
_TS = datetime.datetime(2026, 8, 14, 12, 0, 0)


class _NoSuchKey(Exception):
    pass


class _Body:
    def __init__(self, doc):
        self._b = json.dumps(doc).encode()

    def read(self):
        return self._b


class FakeS3:
    """Serves the summary if asked (so a read is observable) and records every key."""

    class exceptions:
        NoSuchKey = _NoSuchKey

    def __init__(self):
        self.got = []
        self.presigned = []

    def get_object(self, Bucket=None, Key=None):
        self.got.append(Key)
        if Key == SUMMARY_KEY:
            return {"Body": _Body(SUMMARY_DOC)}
        raise _NoSuchKey(Key)

    def head_object(self, Bucket=None, Key=None):
        return {"ContentLength": 1, "LastModified": _TS}

    def list_objects_v2(self, Bucket=None, Prefix=""):
        return {}

    def get_paginator(self, _op):
        outer = self

        class _P:
            def paginate(self, **kw):
                if kw.get("Delimiter") == "/":
                    yield {"CommonPrefixes": [{"Prefix": f"reports/{DATE}/"}]}
                else:
                    yield {}
        return _P()

    def generate_presigned_url(self, _op, Params, ExpiresIn):
        self.presigned.append(Params["Key"])
        return "https://example.invalid/signed"


def caller(role):
    return {"role": role, "display_name": "Sam Manager", "sites": ["s-1"],
            "managed_sites": ["s-1"], "company_id": "c-1"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeS3()
    monkeypatch.setattr(api, "s3_client", f)
    monkeypatch.setattr(api, "_deleted_bases", lambda folder, date: set())
    monkeypatch.setattr(api, "_any_folder_deleted_on", lambda date: False)
    monkeypatch.setattr(api.nz_time, "nz_now", lambda: datetime.datetime(2026, 8, 20))
    monkeypatch.setattr(api, "get_accessible_users",
                        lambda c, site_filter=None: [{"folder_name": "Ben_Test"}])
    return f


@pytest.mark.parametrize("role", ["admin", "gm"])
def test_timeline_with_no_user_never_reads_the_summary(fake, role):
    res = api.get_timeline({"date": DATE}, caller(role))
    assert SUMMARY_KEY not in fake.got
    assert "OTHER TENANT SECRET" not in res["body"]
    assert "lake-wide" not in res["body"]


@pytest.mark.parametrize("role", ["admin", "site_manager"])
def test_dates_never_count_from_the_summary(fake, role):
    res = api.get_dates({"months": "2"}, caller(role))
    dates = json.loads(res["body"])["dates"]
    assert DATE in dates, "the date must still be listed (else this test is vacuous)"
    assert SUMMARY_KEY not in fake.got
    assert dates[DATE]["topics"] == 0 and dates[DATE]["safety"] == 0


@pytest.mark.parametrize("role", ["admin", "gm", "site_manager"])
def test_presign_of_the_summary_is_403_for_every_role(fake, role):
    res = api.get_presigned_url({"key": SUMMARY_KEY}, caller(role))
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_admin_can_still_presign_a_normal_report(fake):
    res = api.get_presigned_url({"key": f"reports/{DATE}/Ben/daily_report.json"},
                                caller("admin"))
    assert res["statusCode"] == 200
    assert fake.presigned == [f"reports/{DATE}/Ben/daily_report.json"]
