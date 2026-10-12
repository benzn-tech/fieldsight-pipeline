"""Task 10 (R10b): admin_disambiguation serves a company's own summary to that company's admin/gm.

platform_admin keeps the lake-wide document. Everyone else who reaches the branch gets ONLY
reports/{date}/companies/{caller company}/summary_report.json -- the key is built from the
caller row, never from the request -- behind the unchanged _day_has_deleted_sources guard. With no
company document the code falls through to the folder-union path exactly as before.

Serving is behind COMPANY_SUMMARY_SERVE (default off): it replaces the person picker an
admin/gm sees today, which is the owner's product decision.
"""
import io
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CO_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OPERATOR = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
DATE = "2026-10-12"
LAKE_KEY = f"reports/{DATE}/summary_report.json"


def company_key(cid, date=DATE):
    return f"reports/{date}/companies/{cid}/summary_report.json"


DOCS = {
    LAKE_KEY: {"who": "lake-wide"},
    company_key(CO_A): {"who": "A"},
    company_key(CO_B): {"who": "B"},
}


class FakeConn:
    def execute(self, *a, **k):
        class _C:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _C()


class FakeS3:
    def __init__(self, objects):
        self.objects, self.get_object_calls = objects, []

    def get_object(self, Bucket, Key):
        self.get_object_calls.append(Key)
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def get_paginator(self, name):
        class _P:
            def paginate(self_inner, **kw):
                return [{"Contents": []}]
        return _P()


@pytest.fixture
def lake(monkeypatch):
    monkeypatch.setenv("COMPANY_SUMMARY_SERVE", "on")

    def _make(objects=None, deleted=False):
        s3 = FakeS3(DOCS if objects is None else objects)
        monkeypatch.setattr(org, "_s3_client", s3)
        monkeypatch.setattr(org, "_day_has_deleted_sources", lambda conn, folder, date: deleted)
        monkeypatch.setattr(org.topics, "list_extraction_folder_names_for_date",
                            lambda conn, cid, date: [])
        monkeypatch.setattr(org.recordings, "folders_with_uploads_for_date",
                            lambda conn, cid, date: set())
        return s3
    return _make


def _caller(role, company):
    return {"id": "u-1", "cognito_sub": "sub-1", "company_id": company,
            "global_role": role, "archived_at": None}


@pytest.mark.parametrize("role", ["gm", "admin"])
def test_a_companys_officer_gets_that_companys_summary(lake, role):
    s3 = lake()
    res = org.admin_disambiguation(FakeConn(), _caller(role, CO_A), DATE)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"who": "A"}
    assert s3.get_object_calls == [company_key(CO_A)]


def test_platform_admin_still_gets_the_lake_wide_document(lake):
    s3 = lake()
    res = org.admin_disambiguation(FakeConn(), _caller("platform_admin", OPERATOR), DATE)
    assert json.loads(res["body"]) == {"who": "lake-wide"}
    assert s3.get_object_calls == [LAKE_KEY]


def test_no_company_document_falls_through_to_the_folder_union(lake):
    s3 = lake({LAKE_KEY: {"who": "lake-wide"}})            # A has no document of its own
    res = org.admin_disambiguation(FakeConn(), _caller("gm", CO_A), DATE)
    assert res["statusCode"] == 404                         # nothing else to offer: as before
    assert json.loads(res["body"])["message"] == f"No reports for {DATE}"
    assert s3.get_object_calls == [company_key(CO_A)]       # and never the lake-wide one


def test_the_deleted_source_guard_still_withholds_the_company_document(lake):
    s3 = lake(deleted=True)
    res = org.admin_disambiguation(FakeConn(), _caller("gm", CO_A), DATE)
    assert res["statusCode"] == 404
    assert s3.get_object_calls == []


@pytest.mark.parametrize("date", ["2026-10-12", "2026-01-01", "2020-02-29", "../x", f"{DATE}/../.."])
@pytest.mark.parametrize("role", ["gm", "admin"])
def test_b_never_receives_a_key(lake, role, date):
    """Whatever the date or the request, B's officer only ever asks S3 for B's key."""
    s3 = lake({company_key(CO_A, date): {"who": "A"}, company_key(CO_B, date): {"who": "B"}})
    org.admin_disambiguation(FakeConn(), _caller(role, CO_B), date)
    assert s3.get_object_calls
    assert all(k.startswith("reports/") and f"/companies/{CO_B}/" in k for k in s3.get_object_calls)
    assert not any(CO_A in k for k in s3.get_object_calls)


def test_a_caller_without_a_company_gets_no_company_document(lake):
    s3 = lake()
    org.admin_disambiguation(FakeConn(), _caller("gm", None), DATE)
    assert s3.get_object_calls == []


def test_by_default_a_company_officer_still_gets_the_picker_not_a_summary(lake, monkeypatch):
    s3 = lake()
    monkeypatch.delenv("COMPANY_SUMMARY_SERVE", raising=False)
    res = org.admin_disambiguation(FakeConn(), _caller("gm", CO_A), DATE)
    assert company_key(CO_A) not in s3.get_object_calls
    assert json.loads(res["body"]) != {"who": "A"}
