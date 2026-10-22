"""POST /api/org/members/{sub}/resend-invite -- re-send the Cognito invitation.

Some invitees never receive the first email (the pool sends from Cognito's
default address, which corporate mail filters quarantine). The endpoint re-sends
it while the login is still FORCE_CHANGE_PASSWORD, and refuses with a clear
message once the person has set a password. Authorization mirrors POST /members
(admin, gm, platform_admin).
"""
import json

import pytest
from botocore.exceptions import ClientError

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


def make_event(method, path, sub="sub-1"):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": None,
        "body": None,
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self

    def rollback(self):
        pass


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "admin@x.nz", "global_role": "admin", "archived_at": None}
TARGET = {"id": "u-2", "cognito_sub": "sub-2", "company_id": "c-1",
          "email": "invitee@x.nz", "global_role": "pm", "archived_at": None}


class FakeCognito:
    def __init__(self, status="FORCE_CHANGE_PASSWORD", get_error=None, create_error=None,
                 email="invitee@x.nz"):
        self.status = status
        self.email = email
        self.get_error = get_error
        self.create_error = create_error
        self.got = []
        self.created = []

    def admin_get_user(self, **kw):
        self.got.append(kw)
        if self.get_error:
            raise ClientError({"Error": {"Code": self.get_error, "Message": "x"}}, "AdminGetUser")
        attrs = [{"Name": "sub", "Value": kw["Username"]}]
        if self.email:
            attrs.append({"Name": "email", "Value": self.email})
        return {"Username": kw["Username"], "UserStatus": self.status, "UserAttributes": attrs}

    def admin_create_user(self, **kw):
        # The real pool signs in by email: a sub here is InvalidParameterException
        # "Username should be an email" (TEST, 2026-10-22) -- the first deploy did that.
        if "@" not in kw["Username"]:
            raise ClientError({"Error": {"Code": "InvalidParameterException",
                                         "Message": "Username should be an email."}},
                              "AdminCreateUser")
        self.created.append(kw)
        if self.create_error:
            raise ClientError({"Error": {"Code": self.create_error, "Message": "x"}},
                              "AdminCreateUser")
        return {"User": {"Username": kw["Username"]}}


@pytest.fixture
def env(monkeypatch):
    people = {"sub-1": dict(CALLER), "sub-2": dict(TARGET)}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: people.get(sub))
    fake = FakeCognito()
    monkeypatch.setattr(org, "_cognito_client", fake)
    return people, fake


def call(sub_path="sub-2", caller="sub-1"):
    res = org.lambda_handler(
        make_event("POST", f"/api/org/members/{sub_path}/resend-invite", sub=caller), None)
    return res["statusCode"], json.loads(res["body"])


def test_pending_invitee_gets_a_resend_addressed_by_cognito_email(env):
    _, fake = env
    status, body = call()
    assert status == 200
    assert body == {"resent": True, "email": "invitee@x.nz",
                    "user_status": "FORCE_CHANGE_PASSWORD"}
    assert fake.created == [{"UserPoolId": org.COGNITO_USER_POOL_ID, "Username": "invitee@x.nz",
                             "MessageAction": "RESEND", "DesiredDeliveryMediums": ["EMAIL"]}]
    assert fake.got[0]["Username"] == "sub-2"


def test_resend_uses_the_email_cognito_holds_not_the_aurora_column(env):
    people, fake = env
    people["sub-2"]["email"] = "drifted@elsewhere.nz"
    fake.email = "real-login@x.nz"
    status, body = call()
    assert status == 200
    assert fake.created[0]["Username"] == "real-login@x.nz"
    assert body["email"] == "real-login@x.nz"


def test_login_without_email_is_409_and_no_call(env):
    _, fake = env
    fake.email = None
    status, _ = call()
    assert status == 409
    assert fake.created == []


def test_confirmed_member_gets_409_and_no_email(env):
    _, fake = env
    fake.status = "CONFIRMED"
    status, body = call()
    assert status == 409
    assert body["user_status"] == "CONFIRMED"
    assert "Forgot password" in body["error"]
    assert fake.created == []


def test_race_unsupported_state_is_409(env):
    _, fake = env
    fake.create_error = "UnsupportedUserStateException"
    status, body = call()
    assert status == 409
    assert body["user_status"] == "CONFIRMED"


@pytest.mark.parametrize("role", ["regional_manager", "pm", "site_manager", "worker"])
def test_non_inviter_is_403(env, role):
    people, fake = env
    people["sub-1"]["global_role"] = role
    status, _ = call()
    assert status == 403
    assert fake.got == [] and fake.created == []


def test_gm_resends_for_own_company_member(env):
    # Owner decision 2026-10-22: a gm may resend, with the admin's company rule.
    people, fake = env
    people["sub-1"]["global_role"] = "gm"
    status, body = call()
    assert status == 200
    assert body["resent"] is True
    assert len(fake.created) == 1


def test_other_company_target_is_404_for_gm(env):
    people, fake = env
    people["sub-1"]["global_role"] = "gm"
    people["sub-2"]["company_id"] = "c-other"
    status, _ = call()
    assert status == 404
    assert fake.got == [] and fake.created == []


def test_other_company_target_is_404_for_admin(env):
    people, fake = env
    people["sub-2"]["company_id"] = "c-other"
    status, _ = call()
    assert status == 404
    assert fake.got == [] and fake.created == []


def test_platform_admin_reaches_other_company(env):
    people, fake = env
    people["sub-1"]["global_role"] = "platform_admin"
    people["sub-2"]["company_id"] = "c-other"
    status, _ = call()
    assert status == 200
    assert len(fake.created) == 1


def test_unknown_target_is_404(env):
    _, fake = env
    status, _ = call(sub_path="sub-ghost")
    assert status == 404
    assert fake.created == []


def test_archived_target_is_409(env):
    people, fake = env
    people["sub-2"]["archived_at"] = "2026-10-01"
    status, _ = call()
    assert status == 409
    assert fake.created == []


def test_member_without_cognito_login_is_404(env):
    _, fake = env
    fake.get_error = "UserNotFoundException"
    status, _ = call()
    assert status == 404
    assert fake.created == []


def test_get_is_not_routed(env):
    _, fake = env
    res = org.lambda_handler(make_event("GET", "/api/org/members/sub-2/resend-invite"), None)
    assert res["statusCode"] in (404, 405)
    assert fake.created == []
