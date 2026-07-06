import json
import re

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


def test_list_sites_admin_gets_company_sites(wired):
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, include_archived=False: [{"id": "s-1", "name": "Alpha"}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["sites"] == [{"id": "s-1", "name": "Alpha"}]


def test_list_sites_worker_gets_membership_sites(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: ["s-2"])
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: [{"id": i, "name": "Beta"} for i in ids])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert body_of(res)["sites"] == [{"id": "s-2", "name": "Beta"}]


def test_create_site_admin_ok(wired):
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None):
        created.update(company_id=company_id, name=name, location=location)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "New Site", "location": "Chch"}), None)
    assert res["statusCode"] == 201
    assert created == {"company_id": "c-uuid-1", "name": "New Site", "location": "Chch"}


def test_create_site_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites",
                                        body={"name": "X"}), None)
    assert res["statusCode"] == 403


def test_create_site_requires_name(wired):
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={}), None)
    assert res["statusCode"] == 400


def test_list_members_joins_memberships(wired):
    wired.setattr(org.users, "list_company_users", lambda conn, cid, include_archived=False: [
        {"id": "u-1", "cognito_sub": "sub-1", "email": "a@x.nz"},
        {"id": "u-2", "cognito_sub": "sub-2", "email": "b@x.nz"},
    ])
    wired.setattr(org.memberships, "list_company_memberships", lambda conn, cid: [
        {"user_id": "u-1", "cognito_sub": "sub-1", "site_id": "s-1", "role": "worker"},
    ])
    res = org.lambda_handler(make_event("GET", "/api/org/members"), None)
    assert res["statusCode"] == 200
    members = body_of(res)["members"]
    assert members[0]["memberships"] == [{"site_id": "s-1", "role": "worker"}]
    assert members[1]["memberships"] == []


def test_list_members_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("GET", "/api/org/members"), None)
    assert res["statusCode"] == 403


def test_patch_role_admin_ok(wired):
    seen = {}

    def fake_set(conn, sub, company_id, role):
        seen.update(sub=sub, company_id=company_id, role=role)
        return {**CALLER, "cognito_sub": sub, "global_role": role}

    wired.setattr(org.users, "set_global_role", fake_set)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 200
    assert seen == {"sub": "sub-2", "company_id": "c-uuid-1", "role": "pm"}


def test_patch_role_rejects_unknown_role(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "root"}), None)
    assert res["statusCode"] == 400


def test_patch_role_gm_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 403


def test_patch_role_unknown_target_404(wired):
    wired.setattr(org.users, "set_global_role", lambda *a: None)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-ghost/role", body={"global_role": "pm"}), None)
    assert res["statusCode"] == 404


class FakeCognito:
    def __init__(self, exists=False):
        self.exists = exists
        self.created = []

    def admin_create_user(self, **kw):
        if self.exists:
            raise self.exceptions.UsernameExistsException(
                {"Error": {"Code": "UsernameExistsException", "Message": "exists"}},
                "AdminCreateUser")
        self.created.append(kw)
        return {"User": {"Attributes": [
            {"Name": "sub", "Value": "sub-new"},
            {"Name": "email", "Value": kw["Username"]},
        ]}}

    def admin_get_user(self, **kw):
        return {"UserAttributes": [{"Name": "sub", "Value": "sub-existing"}]}

    class exceptions:
        class UsernameExistsException(Exception):
            def __init__(self, *a, **k):
                super().__init__("exists")


@pytest.fixture
def member_wired(wired):
    fake = FakeCognito()
    wired.setattr(org, "_cognito_client", fake)
    wired.setattr(org.users, "upsert_user",
                  lambda conn, sub, email, **kw: {
                      "id": "u-new", "cognito_sub": sub, "email": email, **kw})
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1"})
    wired.setattr(org.memberships, "ensure_membership",
                  lambda conn, uid, sid, role: {
                      "user_id": uid, "site_id": sid, "role": role})
    return wired, fake


def test_create_member_creates_and_enrolls(member_wired):
    wired, fake = member_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new@x.nz", "first_name": "New", "global_role": "site_manager",
        "memberships": [{"site_id": "s-1", "role": "site_manager"}],
    }), None)
    assert res["statusCode"] == 201
    b = body_of(res)
    assert b["user"]["cognito_sub"] == "sub-new"
    assert b["memberships"] == [{"user_id": "u-new", "site_id": "s-1",
                                 "role": "site_manager"}]
    assert fake.created[0]["Username"] == "new@x.nz"
    assert fake.created[0]["UserPoolId"] == org.COGNITO_USER_POOL_ID


def test_create_member_existing_cognito_user_is_idempotent(member_wired):
    wired, fake = member_wired
    fake.exists = True
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "old@x.nz"}), None)
    assert res["statusCode"] == 201
    assert body_of(res)["user"]["cognito_sub"] == "sub-existing"


def test_create_member_rejects_bad_global_role(member_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "global_role": "superuser"}), None)
    assert res["statusCode"] == 400


def test_create_member_rejects_foreign_site(member_wired):
    wired, fake = member_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "OTHER-company"})
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "memberships": [{"site_id": "s-9", "role": "worker"}],
    }), None)
    assert res["statusCode"] == 403


def test_create_member_rejects_bad_membership_role(member_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "memberships": [{"site_id": "s-1", "role": "admin"}],
    }), None)
    assert res["statusCode"] == 400


def test_create_member_rejects_cross_company_existing_user(member_wired):
    wired, fake = member_wired
    fake.exists = True

    def by_sub(conn, sub):
        if sub == "sub-1":
            return dict(CALLER)
        if sub == "sub-existing":
            return {**CALLER, "cognito_sub": "sub-existing", "company_id": "OTHER-co"}
        return None

    wired.setattr(org.users, "get_user_by_sub", by_sub)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "taken@x.nz"}), None)
    assert res["statusCode"] == 409


def test_create_member_same_company_reinvite_preserves_role(member_wired):
    wired, fake = member_wired
    fake.exists = True
    seen = {}

    def by_sub(conn, sub):
        if sub == "sub-1":
            return dict(CALLER)
        if sub == "sub-existing":
            return {**CALLER, "cognito_sub": "sub-existing", "global_role": "pm"}
        return None

    def fake_upsert(conn, sub, email, **kw):
        seen.update(kw)
        return {"id": "u-x", "cognito_sub": sub, "email": email}

    wired.setattr(org.users, "get_user_by_sub", by_sub)
    wired.setattr(org.users, "upsert_user", fake_upsert)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "taken@x.nz"}), None)
    assert res["statusCode"] == 201
    assert seen["global_role"] is None  # not demoted to "worker"


def test_create_member_non_admin_403(member_wired):
    wired, fake = member_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz"}), None)
    assert res["statusCode"] == 403


def test_create_member_rejects_archived_site(member_wired):
    wired, fake = member_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1", "archived_at": "2026-07-01"})
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "memberships": [{"site_id": "s-arch", "role": "worker"}],
    }), None)
    assert res["statusCode"] == 409


def test_archived_caller_blocked_except_get_me(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "archived_at": "2026-07-04"})
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    assert org.lambda_handler(make_event("GET", "/api/org/me"), None)["statusCode"] == 200
    assert org.lambda_handler(make_event("GET", "/api/org/sites"), None)["statusCode"] == 403
    assert org.lambda_handler(make_event("POST", "/api/org/sites", body={"name": "X"}), None)["statusCode"] == 403


def test_archive_site_admin_ok_and_404(wired):
    seen = {}
    wired.setattr(org.sites, "archive_site",
                  lambda conn, sid, cid: (seen.update(sid=sid, cid=cid)
                                          or {"id": sid, "archived_at": "2026-07-04"}))
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)
    assert res["statusCode"] == 200
    assert seen == {"sid": "s-1", "cid": "c-uuid-1"}
    wired.setattr(org.sites, "archive_site", lambda conn, sid, cid: None)
    assert org.lambda_handler(make_event("POST", "/api/org/sites/s-9/archive"), None)["statusCode"] == 404


def test_unarchive_site_routes(wired):
    wired.setattr(org.sites, "unarchive_site",
                  lambda conn, sid, cid: {"id": sid, "archived_at": None})
    res = org.lambda_handler(make_event("POST", "/api/org/sites/s-1/unarchive"), None)
    assert res["statusCode"] == 200 and body_of(res)["archived_at"] is None


def test_archive_site_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    assert org.lambda_handler(make_event("POST", "/api/org/sites/s-1/archive"), None)["statusCode"] == 403


def test_archive_member_ok_but_never_self(wired):
    wired.setattr(org.users, "archive_user",
                  lambda conn, sub, cid: {"cognito_sub": sub, "archived_at": "x"})
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-2/archive"), None)["statusCode"] == 200
    # self-archive is always blocked (you can't lock yourself out)
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-1/archive"), None)["statusCode"] == 400
    # unarchive self is fine (row can't be reached anyway while archived, but no self-guard needed)
    wired.setattr(org.users, "unarchive_user", lambda conn, sub, cid: {"cognito_sub": sub, "archived_at": None})
    assert org.lambda_handler(make_event("POST", "/api/org/members/sub-2/unarchive"), None)["statusCode"] == 200


def test_include_archived_param_admin_only(wired):
    seen = {}

    def fake_list(conn, cid, include_archived=False):
        seen["inc"] = include_archived
        return []

    wired.setattr(org.sites, "list_company_sites", fake_list)
    ev = make_event("GET", "/api/org/sites")
    ev["queryStringParameters"] = {"include_archived": "1"}
    org.lambda_handler(ev, None)
    assert seen["inc"] is True
    # workers never get archived rows (membership path has no include flag)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids", lambda *a: [])
    wired.setattr(org.sites, "list_sites_by_ids", lambda conn, ids: [])
    ev2 = make_event("GET", "/api/org/sites")
    ev2["queryStringParameters"] = {"include_archived": "1"}
    assert org.lambda_handler(ev2, None)["statusCode"] == 200  # ignored, not honored


def test_create_member_archived_same_company_409(member_wired):
    wired, fake = member_wired
    fake.exists = True

    def by_sub(conn, sub):
        if sub == "sub-1":
            return dict(CALLER)
        if sub == "sub-existing":
            return {**CALLER, "cognito_sub": "sub-existing", "archived_at": "2026-07-01"}
        return None

    wired.setattr(org.users, "get_user_by_sub", by_sub)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "back@x.nz"}), None)
    assert res["statusCode"] == 409


class FakeS3:
    def __init__(self):
        self.copied = []
        self.deleted = []
        self.missing_source = False

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]

    def copy_object(self, Bucket=None, CopySource=None, Key=None):
        if self.missing_source:
            from botocore.exceptions import ClientError
            # Real S3 returns AccessDenied (not NoSuchKey) for a missing copy
            # source when the role lacks s3:ListBucket — confirmed in 3b smoke.
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "CopyObject")
        self.copied.append((CopySource["Key"], Key))

    def delete_object(self, Bucket=None, Key=None):
        self.deleted.append(Key)


@pytest.fixture
def presign_wired(wired):
    fake = FakeS3()
    wired.setattr(org, "_s3_client", fake)
    return wired, fake


def test_upload_url_avatar(presign_wired):
    wired, fake = presign_wired
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "avatar", "content_type": "image/png"}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["key"].startswith("org-assets/pending/sub-1/")
    assert b["key"].endswith(".png")
    assert fake.last["op"] == "put_object"
    assert fake.last["params"]["ContentType"] == "image/png"


def test_upload_url_site_icon_admin_gets_pending_key(presign_wired):
    wired, fake = presign_wired
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/webp"}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["key"].startswith("org-assets/pending/sub-1/")


def test_upload_url_site_icon_worker_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "site_icon", "content_type": "image/png"}), None)
    assert res["statusCode"] == 403


def test_upload_url_rejects_content_type(presign_wired):
    res = org.lambda_handler(make_event("POST", "/api/org/upload-url", body={
        "kind": "avatar", "content_type": "application/x-sh"}), None)
    assert res["statusCode"] == 400


def test_asset_url_prefix_guard(presign_wired):
    res = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url", params={"key": "reports/2026/secret.json"}), None)
    assert res["statusCode"] == 400
    res2 = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url",
        params={"key": "org-assets/avatars/sub-1/a.png"}), None)
    assert res2["statusCode"] == 200
    assert body_of(res2)["url"].endswith("a.png")


def test_asset_url_rejects_pending_reads(presign_wired):
    res = org.lambda_handler(make_event(
        "GET", "/api/org/asset-url",
        params={"key": "org-assets/pending/sub-1/x.png"}), None)
    assert res["statusCode"] == 400


def test_patch_me_avatar_must_be_caller_scoped(wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/me", body={
        "avatar_s3_key": "org-assets/avatars/sub-OTHER/x.png"}), None)
    assert res["statusCode"] == 400


def test_non_string_inputs_get_400_not_500(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": ["admin"]}), None)
    assert res["statusCode"] == 400
    res2 = org.lambda_handler(make_event(
        "POST", "/api/org/sites", body={"name": 123}), None)
    assert res2["statusCode"] == 400


def test_patch_me_relocates_pending_avatar_and_deletes_old(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": "org-assets/avatars/sub-1/old.png"})
    captured = {}
    wired.setattr(org.users, "update_profile",
                  lambda conn, sub, **kw: (captured.update(kw) or {**CALLER, **kw}))
    pending = "org-assets/pending/sub-1/newhex.png"
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
                                        body={"avatar_s3_key": pending}), None)
    assert res["statusCode"] == 200
    assert captured["avatar_s3_key"] == "org-assets/avatars/sub-1/newhex.png"
    assert fake.copied == [(pending, "org-assets/avatars/sub-1/newhex.png")]
    assert pending in fake.deleted and "org-assets/avatars/sub-1/old.png" in fake.deleted


def test_patch_me_expired_pending_400(presign_wired):
    wired, fake = presign_wired
    fake.missing_source = True
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
        body={"avatar_s3_key": "org-assets/pending/sub-1/gone.png"}), None)
    assert res["statusCode"] == 400
    assert "expired" in body_of(res)["error"]


def test_patch_me_explicit_null_clears_avatar(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": "org-assets/avatars/sub-1/old.png"})
    wired.setattr(org.users, "update_profile",
                  lambda conn, sub, **kw: {**CALLER, **kw})
    wired.setattr(org.users, "clear_avatar",
                  lambda conn, sub: {**CALLER, "avatar_s3_key": None})
    res = org.lambda_handler(make_event("PATCH", "/api/org/me",
                                        body={"avatar_s3_key": None}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["avatar_s3_key"] is None
    assert "org-assets/avatars/sub-1/old.png" in fake.deleted
    assert fake.copied == []


def test_create_site_relocates_pending_icon(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.sites, "create_site",
                  lambda conn, cid, name, **kw: {"id": "s-new", "name": name})
    seticon = {}
    wired.setattr(org.sites, "set_site_icon",
                  lambda conn, sid, key: (seticon.update(sid=sid, key=key)
                                          or {"id": sid, "icon_s3_key": key}))
    pending = "org-assets/pending/sub-1/ic.png"
    res = org.lambda_handler(make_event("POST", "/api/org/sites",
        body={"name": "New", "icon_s3_key": pending}), None)
    assert res["statusCode"] == 201
    assert fake.copied == [(pending, "org-assets/site-icons/s-new/ic.png")]
    assert seticon == {"sid": "s-new", "key": "org-assets/site-icons/s-new/ic.png"}
    assert pending in fake.deleted


def test_patch_site_updates_fields(wired):
    seen = {}
    wired.setattr(org.sites, "update_site",
                  lambda conn, sid, cid, **kw: (seen.update(sid=sid, cid=cid, **kw)
                                                or {"id": sid, "name": kw.get("name") or "Old",
                                                    "icon_s3_key": None}))
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                        body={"name": "Renamed", "location": "Akl"}), None)
    assert res["statusCode"] == 200
    assert seen["sid"] == "s-1" and seen["cid"] == "c-uuid-1"
    assert seen["name"] == "Renamed" and seen["location"] == "Akl"


def test_patch_site_worker_403_and_missing_404(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    assert org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                         body={"name": "X"}), None)["statusCode"] == 403
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    wired.setattr(org.sites, "update_site", lambda conn, sid, cid, **kw: None)
    assert org.lambda_handler(make_event("PATCH", "/api/org/sites/s-9",
                                         body={"name": "X"}), None)["statusCode"] == 404


def test_patch_site_swaps_icon_and_deletes_old(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.sites, "update_site",
                  lambda conn, sid, cid, **kw: {"id": sid, "name": "S",
                                                "icon_s3_key": "org-assets/site-icons/s-1/old.png"})
    wired.setattr(org.sites, "set_site_icon",
                  lambda conn, sid, key: {"id": sid, "icon_s3_key": key})
    pending = "org-assets/pending/sub-1/new.png"
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1",
                                        body={"icon_s3_key": pending}), None)
    assert res["statusCode"] == 200
    assert fake.copied == [(pending, "org-assets/site-icons/s-1/new.png")]
    assert pending in fake.deleted and "org-assets/site-icons/s-1/old.png" in fake.deleted


# ----------------------------------------------------------
# /observations
# ----------------------------------------------------------
def test_create_observation_ok(wired):
    # worker-role caller proves there is no role gate on create
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    created = {}

    def fake_create(conn, company_id, kind, site_slug, author_sub, author_name,
                    observation, risk_level=None, recommended_action=None,
                    report_date=None):
        created.update(company_id=company_id, kind=kind, site_slug=site_slug,
                       author_sub=author_sub, author_name=author_name,
                       observation=observation, risk_level=risk_level,
                       recommended_action=recommended_action, report_date=report_date)
        return {"id": "o-1", "company_id": company_id, "kind": kind,
                "site_slug": site_slug, "observation": observation}

    wired.setattr(org.observations, "create_observation", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/observations", body={
        "kind": "safety", "site_slug": "site-a", "observation": "Loose scaffold",
    }), None)
    assert res["statusCode"] == 201
    assert created["company_id"] == "c-uuid-1"
    assert created["kind"] == "safety"
    assert created["site_slug"] == "site-a"
    assert created["author_sub"] == "sub-1"
    assert created["author_name"] == "Ada L"
    assert created["observation"] == "Loose scaffold"
    # report_date has no SQL default — the endpoint must ALWAYS supply it
    assert created["report_date"] is not None
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", created["report_date"])


def test_create_observation_bad_kind_400(wired):
    res = org.lambda_handler(make_event("POST", "/api/org/observations", body={
        "kind": "danger", "site_slug": "site-a", "observation": "text",
    }), None)
    assert res["statusCode"] == 400


def test_create_observation_missing_text_400(wired):
    res = org.lambda_handler(make_event("POST", "/api/org/observations", body={
        "kind": "safety", "site_slug": "site-a", "observation": "",
    }), None)
    assert res["statusCode"] == 400


def test_list_observations_filters(wired):
    seen = {}

    def fake_list(conn, company_id, kind=None, date_from=None, date_to=None,
                  site_slug=None, include_archived=False):
        seen.update(company_id=company_id, kind=kind, date_from=date_from,
                    date_to=date_to, site_slug=site_slug,
                    include_archived=include_archived)
        return [{"id": "o-1"}]

    wired.setattr(org.observations, "list_observations", fake_list)
    res = org.lambda_handler(make_event("GET", "/api/org/observations", params={
        "kind": "quality", "from": "2026-07-01", "to": "2026-07-04",
        "site_slug": "site-b", "include_archived": "1",
    }), None)
    assert res["statusCode"] == 200
    assert body_of(res)["observations"] == [{"id": "o-1"}]
    assert seen == {"company_id": "c-uuid-1", "kind": "quality",
                     "date_from": "2026-07-01", "date_to": "2026-07-04",
                     "site_slug": "site-b", "include_archived": True}


def test_patch_status_author_ok(wired):
    # non-admin caller who IS the author of the observation
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.observations, "get_observation",
                  lambda conn, cid, oid: {"id": oid, "author_sub": "sub-1",
                                          "company_id": "c-uuid-1"})
    seen = {}
    wired.setattr(org.observations, "set_status",
                  lambda conn, cid, oid, status: (seen.update(cid=cid, oid=oid, status=status)
                                                  or {"id": oid, "status": status}))
    res = org.lambda_handler(make_event("PATCH", "/api/org/observations/o-1",
                                        body={"status": "closed"}), None)
    assert res["statusCode"] == 200
    assert seen == {"cid": "c-uuid-1", "oid": "o-1", "status": "closed"}


def test_patch_status_other_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.observations, "get_observation",
                  lambda conn, cid, oid: {"id": oid, "author_sub": "sub-OTHER",
                                          "company_id": "c-uuid-1"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/observations/o-1",
                                        body={"status": "closed"}), None)
    assert res["statusCode"] == 403


def test_patch_status_admin_ok(wired):
    # default CALLER is admin and NOT the author — role should still allow it
    wired.setattr(org.observations, "get_observation",
                  lambda conn, cid, oid: {"id": oid, "author_sub": "sub-OTHER",
                                          "company_id": "c-uuid-1"})
    seen = {}
    wired.setattr(org.observations, "set_status",
                  lambda conn, cid, oid, status: (seen.update(status=status)
                                                  or {"id": oid, "status": status}))
    res = org.lambda_handler(make_event("PATCH", "/api/org/observations/o-1",
                                        body={"status": "open"}), None)
    assert res["statusCode"] == 200
    assert seen["status"] == "open"


def test_patch_status_bad_value_400(wired):
    res = org.lambda_handler(make_event("PATCH", "/api/org/observations/o-1",
                                        body={"status": "cancelled"}), None)
    assert res["statusCode"] == 400


def test_archive_requires_admin_or_gm(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event("POST", "/api/org/observations/o-1/archive"), None)
    assert res["statusCode"] == 403

    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    wired.setattr(org.observations, "set_archived",
                  lambda conn, cid, oid, archived: {"id": oid, "archived_at": "2026-07-06"})
    res2 = org.lambda_handler(make_event("POST", "/api/org/observations/o-1/archive"), None)
    assert res2["statusCode"] == 200


def test_observation_cross_company_404(wired):
    wired.setattr(org.observations, "get_observation", lambda conn, cid, oid: None)
    res = org.lambda_handler(make_event("PATCH", "/api/org/observations/o-9",
                                        body={"status": "open"}), None)
    assert res["statusCode"] == 404
