"""POST /api/org/voice/session -- mints a voice-llm token for the authenticated caller.

The token's sub must be the authorizer-verified Cognito sub, never anything from the body.
"""
import json
import time

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import voice_session as vs  # noqa: E402

KEY = "test-signing-key"
CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "member", "created_at": "2026-08-04",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def call(monkeypatch, caller=CALLER, body=None):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller) if caller else None)
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    resp = org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/voice/session", "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None, "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_mints_a_token_for_the_authorizer_sub(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_SIGNING_KEY", KEY)
    status, body = call(monkeypatch, body={"sub": "someone-else"})
    assert status == 200
    assert vs.verify(body["token"], KEY, now=int(time.time())) == "sub-1"
    assert body["expires_at"] > int(time.time())


def test_unconfigured_key_is_503(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_SIGNING_KEY", "")
    status, body = call(monkeypatch)
    assert status == 503
    assert "token" not in body


def test_archived_caller_is_refused(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_SIGNING_KEY", KEY)
    status, _ = call(monkeypatch, caller=dict(CALLER, archived_at="2026-09-01"))
    assert status == 403


def test_unprovisioned_caller_is_refused(monkeypatch):
    monkeypatch.setenv("VOICE_SESSION_SIGNING_KEY", KEY)
    status, _ = call(monkeypatch, caller=None)
    assert status == 403
