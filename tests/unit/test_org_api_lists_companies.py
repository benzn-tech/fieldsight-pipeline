"""GET /api/org/companies -- who may list every tenant, and what they get back.

A list of every tenant's name is a cross-tenant disclosure. The gate is
is_cross_company (platform_admin only), the same predicate that alone lets a caller
send target_company_id to create_org_site / patch_org_site. Every other role is
refused, and the body carries exactly {id, name}.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

# What the repo function returns today PLUS a column it might grow tomorrow.
ROWS = [
    {"id": "c-1", "name": "Alpha Build", "industry": "construction",
     "created_at": "2026-01-01", "voiceprint_consent_basis": "attestation"},
    {"id": "c-2", "name": "Beta Civil", "industry": None,
     "created_at": "2026-02-01", "voiceprint_consent_basis": None},
]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def call(monkeypatch, role):
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": role, "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "list_companies", lambda conn: [dict(r) for r in ROWS])
    resp = org.lambda_handler({
        "httpMethod": "GET", "path": "/api/org/companies", "queryStringParameters": None,
        "body": None, "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_platform_admin_gets_every_company(monkeypatch):
    status, body = call(monkeypatch, "platform_admin")
    assert status == 200
    assert body == {"companies": [{"id": "c-1", "name": "Alpha Build"},
                                  {"id": "c-2", "name": "Beta Civil"}]}


def test_only_id_and_name_leave_the_handler(monkeypatch):
    _, body = call(monkeypatch, "platform_admin")
    for c in body["companies"]:
        assert set(c) == {"id", "name"}


@pytest.mark.parametrize("role", ["admin", "gm", "pm", "site_manager", "worker",
                                  "regional_manager", "member", "", "nonsense"])
def test_every_other_role_is_refused_and_sees_no_company(monkeypatch, role):
    status, body = call(monkeypatch, role)
    assert status == 403
    assert "companies" not in body
    assert "Alpha Build" not in json.dumps(body)
