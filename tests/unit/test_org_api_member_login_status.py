"""GET /api/org/members -- per-row `login_status` from Cognito, and the /me last-seen stamp.

login_status is the Cognito UserStatus for the row's cognito_sub (CONFIRMED,
FORCE_CHANGE_PASSWORD, ...), from one paginated ListUsers sweep per request, or null when
unknown. A Cognito failure must never fail the list: everyone gets null.

A user whose Cognito account is disabled (Enabled false; offboarding disables a company's
users) reads login_status "DISABLED" whatever its UserStatus, because a disabled user keeps
CONFIRMED and must not show as "Signed in". A user record without Enabled counts as enabled,
so an all-enabled pool answers exactly what it answered before.
"""
import json

import pytest
from botocore.exceptions import ClientError

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "a@x.nz", "global_role": "admin", "archived_at": None}


def make_event(method, path, sub="sub-1"):
    return {"httpMethod": method, "path": path, "queryStringParameters": None, "body": None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self

    def rollback(self):
        pass


class FakeCognito:
    def __init__(self, pages=None, fail=None):
        self.pages = pages or []
        self.fail = fail
        self.calls = []

    def list_users(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise self.fail
        i = len(self.calls) - 1
        return self.pages[i]


def _user(sub, status):
    return {"Username": sub, "UserStatus": status, "Attributes": [{"Name": "sub", "Value": sub}]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "list_company_users", lambda conn, cid, include_archived=False: [
        {"id": "u-1", "cognito_sub": "sub-1", "last_seen_at": None},
        {"id": "u-2", "cognito_sub": "sub-2", "last_seen_at": None},
        {"id": "u-3", "cognito_sub": "sub-not-in-pool", "last_seen_at": None},
        {"id": "u-4", "cognito_sub": None, "last_seen_at": None},   # field-only person
    ])
    monkeypatch.setattr(org.memberships, "list_company_memberships", lambda conn, cid: [])
    monkeypatch.setattr(org, "COGNITO_USER_POOL_ID", "ap-southeast-2_TESTPOOL")
    return monkeypatch


def _members(res):
    assert res["statusCode"] == 200
    return {m["id"]: m for m in json.loads(res["body"])["members"]}


def test_login_status_maps_sub_to_user_status_across_pages(env):
    fake = FakeCognito(pages=[
        {"Users": [_user("sub-1", "CONFIRMED")], "PaginationToken": "p2"},
        {"Users": [_user("sub-2", "FORCE_CHANGE_PASSWORD"), _user("sub-x", "RESET_REQUIRED")]},
    ])
    env.setattr(org, "_cognito_client", fake)
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert m["u-1"]["login_status"] == "CONFIRMED"
    assert m["u-2"]["login_status"] == "FORCE_CHANGE_PASSWORD"
    assert m["u-3"]["login_status"] is None
    assert m["u-4"]["login_status"] is None
    # one sweep, sub attribute only, the handler's pool, second page by token
    assert fake.calls == [
        {"UserPoolId": "ap-southeast-2_TESTPOOL", "AttributesToGet": ["sub"], "Limit": 60},
        {"UserPoolId": "ap-southeast-2_TESTPOOL", "AttributesToGet": ["sub"], "Limit": 60,
         "PaginationToken": "p2"},
    ]


def test_cognito_access_denied_gives_null_for_everyone_and_the_list_still_answers(env):
    fake = FakeCognito(fail=ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "ListUsers"))
    env.setattr(org, "_cognito_client", fake)
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert len(m) == 4
    assert all(row["login_status"] is None for row in m.values())
    assert all("last_seen_at" in row for row in m.values())


def test_failure_on_a_later_page_drops_the_partial_map(env):
    class Flaky(FakeCognito):
        def list_users(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return {"Users": [_user("sub-1", "CONFIRMED")], "PaginationToken": "p2"}
            raise RuntimeError("throttled")
    env.setattr(org, "_cognito_client", Flaky())
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert all(row["login_status"] is None for row in m.values())


def test_no_pool_configured_makes_no_cognito_call(env):
    env.setattr(org, "COGNITO_USER_POOL_ID", "")
    fake = FakeCognito(fail=AssertionError("must not be called"))
    env.setattr(org, "_cognito_client", fake)
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert fake.calls == []
    assert all(row["login_status"] is None for row in m.values())


def test_me_stamps_the_caller_and_a_stamp_failure_does_not_fail_me(env):
    seen = []

    def boom(conn, sub):
        seen.append(sub)
        raise RuntimeError("column last_seen_at does not exist")
    env.setattr(org.users, "touch_last_seen", boom)
    env.setattr(org, "GRADED_ROLES", False)
    env.setattr(org.memberships, "accessible_site_ids", lambda conn, uid, role: [])
    env.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {})
    env.setattr(org.companies, "get_company_by_id", lambda conn, cid: {"name": "Acme"})
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert seen == ["sub-1"]
    assert res["statusCode"] == 200, res


def _u(sub, status, enabled):
    u = _user(sub, status)
    u["Enabled"] = enabled
    return u


def test_disabled_user_reads_disabled_not_confirmed_across_pages(env):
    fake = FakeCognito(pages=[
        {"Users": [_u("sub-1", "CONFIRMED", True)], "PaginationToken": "p2"},
        {"Users": [_u("sub-x", "CONFIRMED", False)], "PaginationToken": "p3"},
        {"Users": [_u("sub-2", "CONFIRMED", False)]},
    ])
    env.setattr(org, "_cognito_client", fake)
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert m["u-1"]["login_status"] == "CONFIRMED"
    assert m["u-2"]["login_status"] == "DISABLED"   # Cognito still says CONFIRMED
    assert m["u-3"]["login_status"] is None          # not in the pool
    assert m["u-4"]["login_status"] is None          # no login at all
    assert len(fake.calls) == 3
    assert fake.calls[2]["PaginationToken"] == "p3"
    assert all(c["AttributesToGet"] == ["sub"] for c in fake.calls)


def test_disabled_dominates_an_invited_status(env):
    fake = FakeCognito(pages=[{"Users": [_u("sub-2", "FORCE_CHANGE_PASSWORD", False)]}])
    env.setattr(org, "_cognito_client", fake)
    m = _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    assert m["u-2"]["login_status"] == "DISABLED"


def test_enabled_pool_answers_exactly_as_before(env):
    """Explicit Enabled true and an absent Enabled give the same rows, with no new key."""
    def rows(pages):
        env.setattr(org, "_cognito_client", FakeCognito(pages=pages))
        return _members(org.lambda_handler(make_event("GET", "/api/org/members"), None))
    explicit = rows([{"Users": [_u("sub-1", "CONFIRMED", True),
                                _u("sub-2", "FORCE_CHANGE_PASSWORD", True)]}])
    absent = rows([{"Users": [_user("sub-1", "CONFIRMED"),
                              _user("sub-2", "FORCE_CHANGE_PASSWORD")]}])
    assert explicit == absent
    assert explicit["u-1"]["login_status"] == "CONFIRMED"
    assert explicit["u-2"]["login_status"] == "FORCE_CHANGE_PASSWORD"
    assert set(explicit["u-1"]) == {"id", "cognito_sub", "last_seen_at", "memberships",
                                    "login_status"}


def test_helper_returns_status_and_enabled(env):
    env.setattr(org, "_cognito_client", FakeCognito(pages=[
        {"Users": [_u("sub-1", "CONFIRMED", False), _user("sub-2", "CONFIRMED")]}]))
    assert org._cognito_login_status_by_sub() == {
        "sub-1": {"UserStatus": "CONFIRMED", "Enabled": False},
        "sub-2": {"UserStatus": "CONFIRMED", "Enabled": True},
    }
