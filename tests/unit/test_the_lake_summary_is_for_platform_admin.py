"""summary_report.json is every tenant's daily reports in one document.

Only a caller allowed across tenants may have it -- is_cross_company, i.e.
platform_admin. It used to be gated on "the caller's company is the operator
company", which handed it to that company's gm; the lookup behind that gate
had been returning None, so the leak was closed only by accident. These tests
call admin_disambiguation directly: that is where the decision lives.
(Spec 2026-10-03, decision 3.)
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-07-14"
SUMMARY_KEY = f"reports/{DATE}/summary_report.json"
OPERATOR = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
SUMMARY = {"date": DATE, "company_summary": "every tenant's day"}


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
        import io
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def get_paginator(self, name):
        # _list_report_folders paginates list_objects_v2; the lake holds no
        # per-user daily_report.json here, so every page is empty.
        class _P:
            def paginate(self_inner, **kw):
                return [{"Contents": []}]
        return _P()


@pytest.fixture
def lake(monkeypatch):
    s3 = FakeS3({SUMMARY_KEY: SUMMARY})
    monkeypatch.setattr(org, "_s3_client", s3)
    monkeypatch.setattr(org, "_day_has_deleted_sources", lambda conn, folder, date: False)
    monkeypatch.setattr(org.topics, "list_extraction_folder_names_for_date",
                        lambda conn, cid, date: [])
    monkeypatch.setattr(org.recordings, "folders_with_uploads_for_date",
                        lambda conn, cid, date: set())
    return s3


def _caller(role, company):
    return {"id": "u-1", "cognito_sub": "sub-1", "company_id": company,
            "global_role": role, "archived_at": None}


def test_platform_admin_gets_the_summary(lake):
    res = org.admin_disambiguation(FakeConn(), _caller("platform_admin", OPERATOR), DATE)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == SUMMARY


@pytest.mark.parametrize("role", ["gm", "admin"])
def test_the_operator_companys_own_officers_do_not(lake, role):
    """The people the old gate would have served: ALL-scope members of the
    operator company. This is the assertion that fails against the leak."""
    org.admin_disambiguation(FakeConn(), _caller(role, OPERATOR), DATE)
    assert SUMMARY_KEY not in lake.get_object_calls


@pytest.mark.parametrize("role", ["gm", "admin"])
def test_a_customer_companys_officers_do_not(lake, role):
    org.admin_disambiguation(FakeConn(), _caller(role, "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92"), DATE)
    assert SUMMARY_KEY not in lake.get_object_calls


def test_the_gate_no_longer_looks_a_company_up_by_name():
    assert not hasattr(org, "COMPANY_NAME")
