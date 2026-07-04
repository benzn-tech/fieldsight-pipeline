import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


def make_event(method, path, sub="sub-1", body=None, params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}


@pytest.fixture
def wired(monkeypatch):
    """Wire a FakeConn and a default admin caller; tests override as needed."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    return monkeypatch


def body_of(res):
    return json.loads(res["body"])


def test_unknown_caller_403(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/me", sub="sub-ghost"), None)
    assert res["statusCode"] == 403


def test_caller_without_company_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": None})
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert res["statusCode"] == 403


def test_get_me_returns_profile_and_sites(wired):
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: ["s-uuid-1", "s-uuid-2"])
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["cognito_sub"] == "sub-1"
    assert b["global_role"] == "admin"
    assert b["site_ids"] == ["s-uuid-1", "s-uuid-2"]
    assert res["headers"]["Access-Control-Allow-Origin"] == "*"


def test_patch_me_updates_profile_fields_only(wired):
    seen = {}

    def fake_update(conn, sub, first_name=None, last_name=None, avatar_s3_key=None):
        seen.update(sub=sub, first=first_name, last=last_name, avatar=avatar_s3_key)
        return {**CALLER, "first_name": first_name or CALLER["first_name"]}

    wired.setattr(org.users, "update_profile", fake_update)
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "first_name": "Grace", "global_role": "admin"}), None)
    assert res["statusCode"] == 200
    assert seen["first"] == "Grace"
    assert seen["avatar"] is None  # role key ignored, not smuggled anywhere


def test_patch_me_rejects_foreign_avatar_key(wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "avatar_s3_key": "reports/2026-03-02/evil.json"}), None)
    assert res["statusCode"] == 400


def test_unknown_route_404(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/nope"), None)
    assert res["statusCode"] == 404


def test_malformed_json_400(wired):
    ev = make_event("PATCH", "/api/org/me")
    ev["body"] = "{not json"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400
