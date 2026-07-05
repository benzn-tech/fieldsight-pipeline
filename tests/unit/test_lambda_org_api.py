"""Handler-level tests for lambda_org_api: fake conn, patched repositories,
fake boto3 clients. Repository SQL itself is covered by the integration
suite (tests/integration/test_org_repositories.py)."""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


# ----------------------------------------------------------------------
# Fixtures & fakes
# ----------------------------------------------------------------------

class FakeConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeS3:
    def __init__(self):
        self.presigned = []

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        self.presigned.append((op, Params["Key"]))
        return f"https://s3.test/{op}/{Params['Key']}"

    def get_object(self, Bucket, Key):
        raise RuntimeError("S3 unavailable in unit tests")


class FakeCognito:
    class exceptions:
        class UsernameExistsException(Exception):
            pass

    def __init__(self, existing=None, pool_users=None):
        self.created = []
        self.existing = existing or {}          # email -> sub
        self.pool_users = pool_users or []      # list of attr dicts

    def admin_create_user(self, UserPoolId, Username, UserAttributes, DesiredDeliveryMediums):
        if Username in self.existing:
            raise self.exceptions.UsernameExistsException()
        self.created.append(Username)
        return {"User": {"Attributes": [{"Name": "sub", "Value": f"new-{Username}"}]}}

    def admin_get_user(self, UserPoolId, Username):
        return {"UserAttributes": [{"Name": "sub", "Value": self.existing[Username]}]}

    def get_paginator(self, op):
        assert op == "list_users"
        pages = [{"Users": [{"Attributes": [{"Name": k, "Value": v} for k, v in u.items()]}
                            for u in self.pool_users]}]

        class P:
            def paginate(self, UserPoolId):
                return iter(pages)
        return P()


def user_row(sub="sub-1", email="a@x.com", role="worker", company="co-1",
             uid="u-1", first=None, last=None, avatar=None):
    return {"id": uid, "cognito_sub": sub, "company_id": company, "email": email,
            "first_name": first, "last_name": last, "avatar_s3_key": avatar,
            "global_role": role, "created_at": "2026-07-04"}


def make_event(method="GET", path="/api/org/me", sub="sub-1", email="a@x.com",
               body=None, params=None):
    ev = {"httpMethod": method, "path": path,
          "requestContext": {"authorizer": {"claims": {}}}}
    if sub:
        ev["requestContext"]["authorizer"]["claims"] = {"sub": sub, "email": email}
    if body is not None:
        ev["body"] = json.dumps(body)
    if params:
        ev["queryStringParameters"] = params
    return ev


def call(event):
    return org.lambda_handler(event, None)


def parsed(resp):
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def conn(monkeypatch):
    c = FakeConn()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: c)
    return c


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3", lambda: fake)
    return fake


@pytest.fixture
def as_user(monkeypatch):
    """Pin the caller row returned by users.get_user_by_sub."""
    def _pin(row):
        monkeypatch.setattr(org.users, "get_user_by_sub",
                            lambda conn, sub: row if row and sub == row["cognito_sub"] else None)
        return row
    return _pin


# ----------------------------------------------------------------------
# Plumbing: auth, routing, transactions
# ----------------------------------------------------------------------

def test_missing_claims_401(conn):
    status, body = parsed(call(make_event(sub=None)))
    assert status == 401
    assert conn.rolled_back and conn.closed


def test_unknown_route_404(conn, as_user):
    as_user(user_row())
    status, body = parsed(call(make_event(path="/api/org/nope")))
    assert status == 404


def test_db_unavailable_503(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no route to host")
    monkeypatch.setattr(org, "get_connection", boom)
    status, body = parsed(call(make_event()))
    assert status == 503


def test_commit_on_success_rollback_on_error(conn, as_user, s3, monkeypatch):
    as_user(user_row())
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda c, u, r: [])
    status, _ = parsed(call(make_event()))
    assert status == 200 and conn.committed and not conn.rolled_back

    conn2 = FakeConn()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: conn2)
    status, _ = parsed(call(make_event(path="/api/org/nope")))
    assert status == 404 and conn2.rolled_back and not conn2.committed


def test_cors_headers_on_responses(conn, as_user, s3, monkeypatch):
    as_user(user_row())
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda c, u, r: [])
    resp = call(make_event())
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


# ----------------------------------------------------------------------
# /me
# ----------------------------------------------------------------------

def test_me_bootstraps_profile_on_first_call(conn, s3, monkeypatch):
    calls = {}
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda c, sub: None)

    def fake_upsert(c, sub, email, **kw):
        calls["args"] = (sub, email, kw)
        return user_row(sub=sub, email=email, company=None)
    monkeypatch.setattr(org.users, "upsert_user", fake_upsert)
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda c, u, r: [])

    status, body = parsed(call(make_event(sub="new-sub", email="n@x.com")))
    assert status == 200
    assert calls["args"][0] == "new-sub" and calls["args"][1] == "n@x.com"
    # login-sync bootstrap must NOT set role/company
    assert calls["args"][2] == {}
    assert body["me"]["cognito_sub"] == "new-sub"


def test_me_includes_presigned_avatar(conn, s3, as_user, monkeypatch):
    as_user(user_row(avatar="org-assets/avatars/sub-1"))
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda c, u, r: ["s-1"])
    status, body = parsed(call(make_event()))
    assert status == 200
    assert body["me"]["avatar_url"].endswith("org-assets/avatars/sub-1")
    assert body["site_ids"] == ["s-1"]


def test_patch_me_updates_names_only(conn, s3, as_user, monkeypatch):
    as_user(user_row())
    recorded = {}

    def fake_update(c, sub, first_name=None, last_name=None, avatar_s3_key=None):
        recorded.update(sub=sub, first=first_name, last=last_name, avatar=avatar_s3_key)
        return user_row(first=first_name, last=last_name)
    monkeypatch.setattr(org.users, "update_profile", fake_update)

    status, body = parsed(call(make_event("PATCH", body={"first_name": "Ann", "last_name": "Lee"})))
    assert status == 200
    assert recorded == {"sub": "sub-1", "first": "Ann", "last": "Lee", "avatar": None}


@pytest.mark.parametrize("locked", ["global_role", "company_id", "email", "cognito_sub"])
def test_patch_me_rejects_privileged_fields(conn, as_user, locked):
    as_user(user_row())
    status, body = parsed(call(make_event("PATCH", body={locked: "x", "first_name": "A"})))
    assert status == 400
    assert locked in body["error"]


def test_patch_me_empty_body_400(conn, as_user):
    as_user(user_row())
    status, _ = parsed(call(make_event("PATCH", body={})))
    assert status == 400


# ----------------------------------------------------------------------
# /sites
# ----------------------------------------------------------------------

def test_get_sites_worker_membership_scoped(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="worker"))
    monkeypatch.setattr(org.memberships, "accessible_site_ids", lambda c, u, r: ["s-1"])
    seen = {}

    def fake_by_ids(c, ids):
        seen["ids"] = ids
        return [{"id": "s-1", "company_id": "co-1", "name": "North Wharf",
                 "location": None, "client": None, "industry": None,
                 "icon_s3_key": None, "created_at": "x"}]
    monkeypatch.setattr(org.sites, "list_sites_by_ids", fake_by_ids)

    status, body = parsed(call(make_event(path="/api/org/sites")))
    assert status == 200
    assert seen["ids"] == ["s-1"]
    assert body["sites"][0]["name"] == "North Wharf"
    assert body["sites"][0]["icon_url"] is None


def test_get_sites_admin_company_scoped(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    monkeypatch.setattr(org.sites, "list_company_sites",
                        lambda c, cid: [{"id": "s-9", "company_id": cid, "name": "HQ",
                                         "location": None, "client": None, "industry": None,
                                         "icon_s3_key": "org-assets/site-icons/s-9", "created_at": "x"}])
    status, body = parsed(call(make_event(path="/api/org/sites")))
    assert status == 200
    assert body["sites"][0]["icon_url"].endswith("site-icons/s-9")


def test_post_sites_worker_403(conn, as_user):
    as_user(user_row(role="worker"))
    status, _ = parsed(call(make_event("POST", "/api/org/sites", body={"name": "X"})))
    assert status == 403


def test_post_sites_no_company_403(conn, as_user):
    as_user(user_row(role="admin", company=None))
    status, _ = parsed(call(make_event("POST", "/api/org/sites", body={"name": "X"})))
    assert status == 403


def test_post_sites_missing_name_400(conn, as_user):
    as_user(user_row(role="admin"))
    status, _ = parsed(call(make_event("POST", "/api/org/sites", body={})))
    assert status == 400


def test_post_sites_created_201(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="gm"))
    recorded = {}

    def fake_create(c, cid, name, **kw):
        recorded.update(cid=cid, name=name, **kw)
        return {"id": "s-new", "company_id": cid, "name": name, "location": kw.get("location"),
                "client": None, "industry": None, "icon_s3_key": None, "created_at": "x"}
    monkeypatch.setattr(org.sites, "create_site", fake_create)

    status, body = parsed(call(make_event(
        "POST", "/api/org/sites", body={"name": "New Site", "location": "Chch"})))
    assert status == 201
    assert recorded["cid"] == "co-1" and recorded["name"] == "New Site"
    assert body["site"]["id"] == "s-new"


# ----------------------------------------------------------------------
# /members
# ----------------------------------------------------------------------

def test_get_members_requires_manager(conn, as_user):
    as_user(user_row(role="site_manager"))
    status, _ = parsed(call(make_event(path="/api/org/members")))
    assert status == 403


def test_get_members_joins_memberships(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    monkeypatch.setattr(org.users, "list_company_users",
                        lambda c, cid: [user_row(uid="u-1"), user_row(sub="sub-2", uid="u-2")])
    monkeypatch.setattr(org.memberships, "list_company_memberships",
                        lambda c, cid: [{"id": "m1", "user_id": "u-2", "site_id": "s-1",
                                         "role": "worker", "cognito_sub": "sub-2"}])
    status, body = parsed(call(make_event(path="/api/org/members")))
    assert status == 200
    by_sub = {m["cognito_sub"]: m for m in body["members"]}
    assert by_sub["sub-1"]["memberships"] == []
    assert by_sub["sub-2"]["memberships"] == [{"site_id": "s-1", "role": "worker"}]


def test_post_members_worker_403(conn, as_user):
    as_user(user_row(role="worker"))
    status, _ = parsed(call(make_event("POST", "/api/org/members", body={"email": "x@y.co"})))
    assert status == 403


def test_post_members_invalid_email_400(conn, as_user):
    as_user(user_row(role="admin"))
    status, _ = parsed(call(make_event("POST", "/api/org/members", body={"email": "not-an-email"})))
    assert status == 400


def test_post_members_gm_cannot_create_admin(conn, as_user):
    as_user(user_row(role="gm"))
    status, body = parsed(call(make_event(
        "POST", "/api/org/members", body={"email": "x@y.co", "global_role": "admin"})))
    assert status == 403


def test_post_members_invalid_role_403(conn, as_user):
    as_user(user_row(role="admin"))
    status, _ = parsed(call(make_event(
        "POST", "/api/org/members", body={"email": "x@y.co", "global_role": "superuser"})))
    assert status == 403


def test_post_members_site_not_in_company_400(conn, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    status, body = parsed(call(make_event("POST", "/api/org/members", body={
        "email": "x@y.co",
        "memberships": [{"site_id": "11111111-1111-4111-8111-111111111111", "role": "worker"}]})))
    assert status == 400


def test_post_members_malformed_site_uuid_400(conn, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    status, _ = parsed(call(make_event("POST", "/api/org/members", body={
        "email": "x@y.co", "memberships": [{"site_id": "robert'); DROP TABLE"}]})))
    assert status == 400


def test_post_members_happy_path(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    site_id = "11111111-1111-4111-8111-111111111111"
    cognito = FakeCognito()
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    monkeypatch.setattr(org.sites, "list_company_sites",
                        lambda c, cid: [{"id": site_id, "name": "S"}])
    upserts, mships = [], []

    def fake_upsert(c, sub, email, **kw):
        upserts.append((sub, email, kw))
        return user_row(sub=sub, email=email, uid="u-new",
                        role=kw.get("global_role"), first=kw.get("first_name"))
    monkeypatch.setattr(org.users, "upsert_user", fake_upsert)

    def fake_ensure(c, uid, sid, role):
        mships.append((uid, sid, role))
        return {"id": "m-1", "user_id": uid, "site_id": sid, "role": role, "created_at": "x"}
    monkeypatch.setattr(org.memberships, "ensure_membership", fake_ensure)

    status, body = parsed(call(make_event("POST", "/api/org/members", body={
        "email": "New@Y.co", "first_name": "New", "global_role": "site_manager",
        "memberships": [{"site_id": site_id, "role": "site_manager"}]})))
    assert status == 201
    assert cognito.created == ["new@y.co"], "email must be lowercased"
    assert upserts[0][0] == "new-new@y.co"
    assert upserts[0][2]["company_id"] == "co-1"
    assert upserts[0][2]["global_role"] == "site_manager"
    assert mships == [("u-new", site_id, "site_manager")]
    assert body["member"]["memberships"] == [{"site_id": site_id, "role": "site_manager"}]


def test_post_members_existing_cognito_user_reuses_sub(conn, s3, as_user, monkeypatch):
    """Cognito account exists but has NO app profile yet → adopted."""
    as_user(user_row(role="admin"))
    cognito = FakeCognito(existing={"old@y.co": "existing-sub"})
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    upserts = []

    def fake_upsert(c, sub, email, **kw):
        upserts.append(sub)
        return user_row(sub=sub, email=email, uid="u-x")
    monkeypatch.setattr(org.users, "upsert_user", fake_upsert)

    status, _ = parsed(call(make_event("POST", "/api/org/members", body={"email": "old@y.co"})))
    assert status == 201
    assert upserts == ["existing-sub"]
    assert cognito.created == []


def _pin_lookup(monkeypatch, rows_by_sub):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda c, sub: rows_by_sub.get(sub))


def test_post_members_reinvite_same_company_409(conn, monkeypatch):
    """Re-adding an already-provisioned member must NOT rewrite their role."""
    caller = user_row(role="admin")
    target = user_row(sub="existing-sub", uid="u-2", email="old@y.co", role="gm")
    _pin_lookup(monkeypatch, {"sub-1": caller, "existing-sub": target})
    cognito = FakeCognito(existing={"old@y.co": "existing-sub"})
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    upserts = []
    monkeypatch.setattr(org.users, "upsert_user",
                        lambda c, sub, email, **kw: upserts.append(sub))

    status, body = parsed(call(make_event(
        "POST", "/api/org/members", body={"email": "old@y.co", "global_role": "worker"})))
    assert status == 409
    assert upserts == [], "existing profile must not be touched"
    assert conn.rolled_back


def test_post_members_cross_company_grab_409(conn, monkeypatch):
    """Adding another company's member must not move them across tenants."""
    caller = user_row(role="admin", company="co-1")
    target = user_row(sub="existing-sub", uid="u-2", email="theirs@y.co", company="co-OTHER")
    _pin_lookup(monkeypatch, {"sub-1": caller, "existing-sub": target})
    cognito = FakeCognito(existing={"theirs@y.co": "existing-sub"})
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    upserts = []
    monkeypatch.setattr(org.users, "upsert_user",
                        lambda c, sub, email, **kw: upserts.append(sub))

    status, _ = parsed(call(make_event(
        "POST", "/api/org/members", body={"email": "theirs@y.co"})))
    assert status == 409
    assert upserts == []


def test_post_members_adopts_companyless_profile(conn, s3, monkeypatch):
    """A profile auto-created at login (company NULL) is legitimately adopted."""
    caller = user_row(role="admin")
    floating = user_row(sub="existing-sub", uid="u-2", email="new@y.co", company=None)
    _pin_lookup(monkeypatch, {"sub-1": caller, "existing-sub": floating})
    cognito = FakeCognito(existing={"new@y.co": "existing-sub"})
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    monkeypatch.setattr(org.sites, "list_company_sites", lambda c, cid: [])
    upserts = []

    def fake_upsert(c, sub, email, **kw):
        upserts.append((sub, kw.get("company_id")))
        return user_row(sub=sub, email=email, uid="u-2")
    monkeypatch.setattr(org.users, "upsert_user", fake_upsert)

    status, _ = parsed(call(make_event("POST", "/api/org/members", body={"email": "new@y.co"})))
    assert status == 201
    assert upserts == [("existing-sub", "co-1")]


# ----------------------------------------------------------------------
# /members/{sub}/role
# ----------------------------------------------------------------------

def _pin_two_users(monkeypatch, caller, target):
    def lookup(c, sub):
        if sub == caller["cognito_sub"]:
            return caller
        if sub == target["cognito_sub"]:
            return target
        return None
    monkeypatch.setattr(org.users, "get_user_by_sub", lookup)


def test_patch_role_self_change_400(conn, monkeypatch, as_user):
    as_user(user_row(role="admin"))
    status, _ = parsed(call(make_event(
        "PATCH", "/api/org/members/sub-1/role", body={"role": "worker"})))
    assert status == 400


def test_patch_role_target_other_company_404(conn, monkeypatch):
    caller = user_row(role="admin")
    target = user_row(sub="sub-2", uid="u-2", company="other-co")
    _pin_two_users(monkeypatch, caller, target)
    status, _ = parsed(call(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"role": "pm"})))
    assert status == 404


def test_patch_role_gm_cannot_touch_admin_403(conn, monkeypatch):
    caller = user_row(role="gm")
    target = user_row(sub="sub-2", uid="u-2", role="admin")
    _pin_two_users(monkeypatch, caller, target)
    status, _ = parsed(call(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"role": "worker"})))
    assert status == 403


def test_patch_role_gm_cannot_promote_to_admin_403(conn, monkeypatch):
    caller = user_row(role="gm")
    target = user_row(sub="sub-2", uid="u-2", role="worker")
    _pin_two_users(monkeypatch, caller, target)
    status, _ = parsed(call(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"role": "admin"})))
    assert status == 403


def test_patch_role_happy(conn, s3, monkeypatch):
    caller = user_row(role="admin")
    target = user_row(sub="sub-2", uid="u-2", role="worker")
    _pin_two_users(monkeypatch, caller, target)
    recorded = {}

    def fake_set(c, sub, role):
        recorded.update(sub=sub, role=role)
        return user_row(sub=sub, uid="u-2", role=role)
    monkeypatch.setattr(org.users, "set_global_role", fake_set)

    status, body = parsed(call(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"role": "pm"})))
    assert status == 200
    assert recorded == {"sub": "sub-2", "role": "pm"}
    assert body["member"]["global_role"] == "pm"


# ----------------------------------------------------------------------
# upload-url / asset-url
# ----------------------------------------------------------------------

def test_upload_url_bad_content_type_400(conn, as_user):
    as_user(user_row())
    status, _ = parsed(call(make_event("POST", "/api/org/upload-url",
                                       body={"kind": "avatar", "content_type": "text/html"})))
    assert status == 400


def test_upload_url_bad_kind_400(conn, as_user):
    as_user(user_row())
    status, _ = parsed(call(make_event("POST", "/api/org/upload-url",
                                       body={"kind": "malware", "content_type": "image/png"})))
    assert status == 400


def test_upload_url_avatar_persists_key(conn, s3, as_user, monkeypatch):
    as_user(user_row())
    recorded = {}

    def fake_update(c, sub, first_name=None, last_name=None, avatar_s3_key=None):
        recorded["key"] = avatar_s3_key
        return user_row(avatar=avatar_s3_key)
    monkeypatch.setattr(org.users, "update_profile", fake_update)

    status, body = parsed(call(make_event("POST", "/api/org/upload-url",
                                          body={"kind": "avatar", "content_type": "image/png"})))
    assert status == 200
    assert recorded["key"] == "org-assets/avatars/sub-1"
    assert body["key"] == "org-assets/avatars/sub-1"
    assert ("put_object", "org-assets/avatars/sub-1") in s3.presigned


def test_upload_url_site_icon_worker_403(conn, as_user):
    as_user(user_row(role="worker"))
    status, _ = parsed(call(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/png",
        "site_id": "11111111-1111-4111-8111-111111111111"})))
    assert status == 403


def test_upload_url_site_icon_wrong_company_404(conn, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    sid = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(org.sites, "get_site",
                        lambda c, s: {"id": sid, "company_id": "other-co"})
    status, _ = parsed(call(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/png", "site_id": sid})))
    assert status == 404


def test_upload_url_site_icon_happy(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    sid = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(org.sites, "get_site",
                        lambda c, s: {"id": sid, "company_id": "co-1"})
    recorded = {}
    monkeypatch.setattr(org.sites, "set_icon_key",
                        lambda c, s, k: recorded.update(site=s, key=k) or {"id": s})

    status, body = parsed(call(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/webp", "site_id": sid})))
    assert status == 200
    assert recorded == {"site": sid, "key": f"org-assets/site-icons/{sid}"}


@pytest.mark.parametrize("bad_key", ["reports/2026/x.png", "org-assets/../users/secret",
                                     "", "config/user_mapping.json"])
def test_asset_url_rejects_bad_keys(conn, as_user, bad_key):
    as_user(user_row())
    status, _ = parsed(call(make_event(path="/api/org/asset-url",
                                       params={"key": bad_key})))
    assert status == 400


def test_asset_url_happy(conn, s3, as_user):
    as_user(user_row())
    status, body = parsed(call(make_event(path="/api/org/asset-url",
                                          params={"key": "org-assets/avatars/sub-9"})))
    assert status == 200
    assert body["url"].endswith("org-assets/avatars/sub-9")


# ----------------------------------------------------------------------
# /seed
# ----------------------------------------------------------------------

POOL = [
    {"sub": "sub-ben", "email": "ben@x.com", "name": "Ben Lin"},
    {"sub": "sub-jt", "email": "jt@x.com", "name": "Jarley Trainor"},
]


def _seed_env(monkeypatch, provisioned=0, mapping_doc=None):
    monkeypatch.setattr(org.users, "count_provisioned_users", lambda c: provisioned)
    monkeypatch.setattr(org.companies, "get_company_by_name", lambda c, n: None)
    monkeypatch.setattr(org.companies, "create_company",
                        lambda c, n, industry=None: {"id": "co-new", "name": n})
    monkeypatch.setattr(org, "_load_user_mapping_from_s3", lambda: mapping_doc)
    upserts = []

    def fake_upsert(c, sub, email, **kw):
        row = user_row(sub=sub, email=email, uid=f"u-{sub}",
                       role=kw.get("global_role") or "worker",
                       first=kw.get("first_name"), last=kw.get("last_name"),
                       company=kw.get("company_id"))
        upserts.append((sub, email, kw))
        return row
    monkeypatch.setattr(org.users, "upsert_user", fake_upsert)

    created_sites = []

    def fake_create_site(c, cid, name, **kw):
        s = {"id": f"s-{len(created_sites)}", "company_id": cid, "name": name,
             "location": kw.get("location"), "client": kw.get("client"),
             "industry": kw.get("industry"), "icon_s3_key": None, "created_at": "x"}
        created_sites.append(s)
        return s
    monkeypatch.setattr(org.sites, "get_site_by_name", lambda c, cid, n: None)
    monkeypatch.setattr(org.sites, "create_site", fake_create_site)

    added = []
    monkeypatch.setattr(org.memberships, "ensure_membership",
                        lambda c, uid, sid, role: added.append((uid, sid, role)) or
                        {"id": "m", "user_id": uid, "site_id": sid, "role": role, "created_at": "x"})
    return upserts, created_sites, added


def test_seed_bootstrap_on_pristine_db(conn, s3, as_user, monkeypatch):
    as_user(user_row(sub="sub-ben", email="ben@x.com", company=None, role="worker"))
    cognito = FakeCognito(pool_users=POOL)
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    mapping = {
        "sites": {"sb1108": {"name": "Ellesmere", "location": "Chch", "client": "MoE"}},
        "mapping": {"Benl1": {"name": "Jarley Trainor", "role": "site_manager",
                              "sites": ["sb1108"]}},
    }
    upserts, created_sites, added = _seed_env(monkeypatch, provisioned=0, mapping_doc=mapping)

    status, body = parsed(call(make_event("POST", "/api/org/seed", sub="sub-ben",
                                          email="ben@x.com", body={"company_name": "Southbase"})))
    assert status == 200
    # the seeding caller became admin, the other pool user stayed default
    roles = {email: kw.get("global_role") for _, email, kw in upserts}
    assert roles["ben@x.com"] == "admin"
    assert roles["jt@x.com"] is None
    assert [s["name"] for s in created_sites] == ["Ellesmere"]
    # Jarley (matched by Cognito name attr) got the site_manager membership
    assert added == [("u-sub-jt", "s-0", "site_manager")]
    assert body["users"] == 2 and body["sites"] == 1 and body["memberships"] == 1
    assert body["user_mapping_loaded"] is True


def test_seed_rerun_requires_admin(conn, as_user, monkeypatch):
    as_user(user_row(role="worker"))
    monkeypatch.setattr(org.users, "count_provisioned_users", lambda c: 4)
    status, _ = parsed(call(make_event("POST", "/api/org/seed", body={})))
    assert status == 403


def test_seed_rerun_allowed_for_admin(conn, s3, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    cognito = FakeCognito(pool_users=POOL)
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    _seed_env(monkeypatch, provisioned=4, mapping_doc=None)
    status, body = parsed(call(make_event("POST", "/api/org/seed", body={})))
    assert status == 200
    assert body["user_mapping_loaded"] is False


def test_seed_invalid_role_in_map_400(conn, as_user, monkeypatch):
    as_user(user_row(role="admin"))
    monkeypatch.setattr(org.users, "count_provisioned_users", lambda c: 0)
    status, _ = parsed(call(make_event("POST", "/api/org/seed",
                                       body={"roles": {"x@y.co": "root"}})))
    assert status == 400


def test_seed_sites_from_body_win_over_mapping(conn, s3, as_user, monkeypatch):
    as_user(user_row(sub="sub-ben", email="ben@x.com", role="admin"))
    cognito = FakeCognito(pool_users=POOL)
    monkeypatch.setattr(org, "_cognito", lambda: cognito)
    mapping = {"sites": {"a": {"name": "MappingSite"}}, "mapping": {}}
    upserts, created_sites, _ = _seed_env(monkeypatch, provisioned=0, mapping_doc=mapping)
    status, body = parsed(call(make_event(
        "POST", "/api/org/seed", sub="sub-ben", email="ben@x.com",
        body={"sites": [{"name": "BodySite", "location": "Wanaka"}]})))
    assert status == 200
    assert [s["name"] for s in created_sites] == ["BodySite"]
