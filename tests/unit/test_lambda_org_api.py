import datetime as _dt
import io
import json
import re

import pytest
from botocore.exceptions import ClientError

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
    # get_me hydrates company_name via get_company_by_id; FakeConn has no
    # real cursor, so stub it (tests override the name where they assert it).
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, cid: {"id": cid, "name": "Acme Co"})
    # list_org_sites now attaches per-site user_count + company_name; default
    # these to empty so existing site-list tests don't hit FakeConn's cursor.
    monkeypatch.setattr(org.memberships, "count_by_site", lambda conn, ids: {})
    monkeypatch.setattr(org.companies, "list_companies", lambda conn: [])
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


def test_get_me_graded_sources_site_ids_from_visible_scope(wired):
    # MINOR-2: /me's site_ids come from the graded reach (visible_scope via
    # _allowed_site_ids), not the legacy binary accessible_site_ids. The
    # request-scoped visible_scope memo (MINOR-1) must NOT leak into the body.
    wired.setattr(org, "GRADED_ROLES", True)
    # caller already carries a request-scoped memo -> the strip must remove it.
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "regional_manager",
                                     "_visible_scope": {"should": "be stripped"}})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID, OTHER_SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "RM",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy scope used")))
    res = org.lambda_handler(make_event("GET", "/api/org/me"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert set(b["site_ids"]) == {SITE_ID, OTHER_SITE_ID}
    assert "_visible_scope" not in b                     # internal memo not exposed


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
    # list_org_sites now decorates every row with user_count + company_name
    # (defaults 0 / None from the wired fixture's empty stubs).
    assert body_of(res)["sites"] == [
        {"id": "s-1", "name": "Alpha", "user_count": 0, "company_name": None}]


def test_list_sites_worker_gets_membership_sites(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: ["s-2"])
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: [{"id": i, "name": "Beta"} for i in ids])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert body_of(res)["sites"] == [
        {"id": "s-2", "name": "Beta", "user_count": 0, "company_name": None}]


def test_list_sites_decorates_user_count_and_company_name(wired):
    """Every site row carries member count (#8 Users) + owning company name
    (#2 company tag), scoped to the sites already returned."""
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, include_archived=False: [
                      {"id": "s-1", "name": "Alpha", "company_id": "c-south"},
                      {"id": "s-2", "name": "Beta", "company_id": "c-briv"}])
    wired.setattr(org.memberships, "count_by_site",
                  lambda conn, ids: {"s-1": 4})
    wired.setattr(org.companies, "list_companies", lambda conn: [
        {"id": "c-south", "name": "Southbase"},
        {"id": "c-briv", "name": "Briv Construction"}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    sites = body_of(res)["sites"]
    assert sites[0]["user_count"] == 4 and sites[0]["company_name"] == "Southbase"
    assert sites[1]["user_count"] == 0 and sites[1]["company_name"] == "Briv Construction"


def test_list_sites_graded_regional_manager_full_membership_set(wired):
    # MINOR-2: under graded roles the site-selector must source its site set
    # from visible_scope (via _allowed_site_ids), matching /live-items -- a
    # regional_manager no longer under-returns vs the dashboard. Real
    # _allowed_site_ids runs; only visible_scope is stubbed.
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "regional_manager"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID, OTHER_SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "RM",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    # legacy binary accessible_site_ids must NOT be consulted under graded roles
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy scope used")))
    captured = {}
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: captured.update(ids=set(ids)) or [{"id": i} for i in sorted(ids)])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert res["statusCode"] == 200
    assert captured["ids"] == {SITE_ID, OTHER_SITE_ID}   # full graded reach via visible_scope
    assert {s["id"] for s in body_of(res)["sites"]} == {SITE_ID, OTHER_SITE_ID}


def test_list_org_sites_includes_slug(wired):
    # sites.py's _COLS already includes slug (Task 1) — list_org_sites just
    # forwards whatever the repository returns, so this proves the field
    # isn't stripped anywhere between the query and the HTTP response.
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, include_archived=False: [
                      {"id": "s-1", "name": "Alpha", "slug": "alpha"}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["sites"][0]["slug"] == "alpha"


def test_list_org_sites_includes_address(wired):
    # sites.py's _COLS already includes address — list_org_sites just
    # forwards whatever the repository returns, so this proves the field
    # isn't stripped anywhere between the query and the HTTP response.
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, include_archived=False: [
                      {"id": "s-1", "name": "Alpha", "address": "12 Queen St"}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["sites"][0]["address"] == "12 Queen St"


def test_create_site_admin_ok(wired):
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(company_id=company_id, name=name, location=location,
                       address=address)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "New Site", "location": "Chch", "address": "12 Queen St"}), None)
    assert res["statusCode"] == 201
    assert created == {"company_id": "c-uuid-1", "name": "New Site",
                       "location": "Chch", "address": "12 Queen St"}


def test_create_site_persists_coordinates(wired):
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(latitude=latitude, longitude=longitude, address=address)
        return {"id": "s-geo", "company_id": company_id, "name": name,
                "latitude": latitude, "longitude": longitude}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Geo Site", "address": "1 Colombo St",
        "latitude": -43.5321, "longitude": 172.6362}), None)
    assert res["statusCode"] == 201
    assert created == {"latitude": -43.5321, "longitude": 172.6362,
                       "address": "1 Colombo St"}
    assert body_of(res)["latitude"] == -43.5321


def test_create_site_rejects_non_numeric_latitude(wired):
    called = []
    wired.setattr(org.sites, "create_site",
                  lambda *a, **k: called.append(1) or {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Bad", "latitude": "not-a-number", "longitude": 10}), None)
    assert res["statusCode"] == 400
    assert called == []  # never reached the repo


def test_create_site_rejects_out_of_range_longitude(wired):
    wired.setattr(org.sites, "create_site", lambda *a, **k: {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Bad", "latitude": -43.5, "longitude": 999}), None)
    assert res["statusCode"] == 400


def test_patch_site_persists_coordinates(wired):
    seen = {}

    def fake_update(conn, site_id, company_id, name=None, location=None,
                    client=None, industry=None, address=None,
                    latitude=None, longitude=None):
        seen.update(latitude=latitude, longitude=longitude)
        return {"id": site_id, "latitude": latitude, "longitude": longitude}

    wired.setattr(org.sites, "update_site", fake_update)
    res = org.lambda_handler(make_event("PATCH", "/api/org/sites/s-1", body={
        "latitude": -41.2865, "longitude": 174.7762}), None)
    assert res["statusCode"] == 200
    assert seen == {"latitude": -41.2865, "longitude": 174.7762}


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


def test_list_members_platform_admin_spans_all_companies(wired):
    """platform_admin sits in an empty operator company; its Team directory
    must span every tenant (list_all_*) and carry a company_name label,
    NOT company-pin to the caller's own (empty) company."""
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": "c-platform",
                                     "global_role": "platform_admin"})
    # company-pinned reads would be wired to blow up if hit
    wired.setattr(org.users, "list_company_users",
                  lambda *a, **k: pytest.fail("must not company-pin"))
    wired.setattr(org.users, "list_all_users", lambda conn, include_archived=False: [
        {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-south"},
        {"id": "u-2", "cognito_sub": "sub-2", "company_id": "c-briv"},
    ])
    wired.setattr(org.memberships, "list_all_memberships", lambda conn: [
        {"user_id": "u-1", "cognito_sub": "sub-1", "site_id": "s-1", "role": "pm"},
    ])
    wired.setattr(org.companies, "list_companies", lambda conn: [
        {"id": "c-south", "name": "Southbase"},
        {"id": "c-briv", "name": "Briv Construction"},
    ])
    res = org.lambda_handler(make_event("GET", "/api/org/members"), None)
    assert res["statusCode"] == 200
    members = body_of(res)["members"]
    assert members[0]["company_name"] == "Southbase"
    assert members[0]["memberships"] == [{"site_id": "s-1", "role": "pm"}]
    assert members[1]["company_name"] == "Briv Construction"


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


def _by_sub_same_company(conn, sub):
    if sub == "sub-1":
        return dict(CALLER)
    if sub == "sub-2":
        return {**CALLER, "cognito_sub": "sub-2", "id": "u-2"}
    return None


def test_patch_member_folder_admin_ok(wired):
    seen = {}

    def fake_set_folder(conn, sub, folder_name):
        seen.update(sub=sub, folder_name=folder_name)

    wired.setattr(org.users, "get_user_by_sub", _by_sub_same_company)
    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    wired.setattr(org.users, "set_folder_name", fake_set_folder)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "Neil Blunden"}), None)
    assert res["statusCode"] == 200
    assert seen == {"sub": "sub-2", "folder_name": "Neil_Blunden"}
    assert body_of(res)["cognito_sub"] == "sub-2"


def test_patch_member_folder_normalizes_spaces(wired):
    seen = {}

    def fake_set_folder(conn, sub, folder_name):
        seen.update(sub=sub, folder_name=folder_name)

    wired.setattr(org.users, "get_user_by_sub", _by_sub_same_company)
    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    wired.setattr(org.users, "set_folder_name", fake_set_folder)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "  Amy Rose  "}), None)
    assert res["statusCode"] == 200
    assert seen["folder_name"] == "Amy_Rose"  # spaces -> underscore, leading/trailing stripped first


def test_patch_member_folder_worker_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "Neil Blunden"}), None)
    assert res["statusCode"] == 403


def test_patch_member_folder_missing_name_400(wired):
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "   "}), None)
    assert res["statusCode"] == 400
    res2 = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={}), None)
    assert res2["statusCode"] == 400


def test_patch_member_folder_foreign_member_404(wired):
    def by_sub(conn, sub):
        if sub == "sub-1":
            return dict(CALLER)
        if sub == "sub-2":
            return {**CALLER, "cognito_sub": "sub-2", "company_id": "OTHER-co"}
        return None

    wired.setattr(org.users, "get_user_by_sub", by_sub)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "Neil Blunden"}), None)
    assert res["statusCode"] == 404


def test_patch_member_folder_collision_409(wired):
    wired.setattr(org.users, "get_user_by_sub", _by_sub_same_company)
    wired.setattr(org.users, "get_by_folder_name_global",
                  lambda conn, folder: {**CALLER, "cognito_sub": "sub-other", "folder_name": folder})
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/folder", body={"folder_name": "Neil Blunden"}), None)
    assert res["statusCode"] == 409


def test_backfill_enrolls_unenrolled_login(wired):
    wired.setattr(org.users, "list_company_logins_unenrolled",
                  lambda conn, cid: [
                      {"id": "u-2", "cognito_sub": "sub-2", "first_name": "Neil", "last_name": "Blunden"}])
    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    seen = {}

    def fake_set_folder(conn, sub, folder_name):
        seen.update(sub=sub, folder_name=folder_name)

    wired.setattr(org.users, "set_folder_name", fake_set_folder)
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["enrolled"] == [{"sub": "sub-2", "folder_name": "Neil_Blunden"}]
    assert b["skipped"] == []
    assert seen == {"sub": "sub-2", "folder_name": "Neil_Blunden"}


def test_backfill_skips_collision(wired):
    wired.setattr(org.users, "list_company_logins_unenrolled",
                  lambda conn, cid: [
                      {"id": "u-2", "cognito_sub": "sub-2", "first_name": "Neil", "last_name": "Blunden"}])
    wired.setattr(org.users, "get_by_folder_name_global",
                  lambda conn, folder: {**CALLER, "cognito_sub": "sub-other", "folder_name": folder})
    seen = {}
    wired.setattr(org.users, "set_folder_name",
                  lambda conn, sub, folder_name: seen.update(sub=sub, folder_name=folder_name))
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill"), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["enrolled"] == []
    assert b["skipped"] == [{"sub": "sub-2", "reason": "folder taken by another user"}]
    assert seen == {}  # collision -> set_folder_name never called, no 500


def test_backfill_non_admin_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "gm"})
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill"), None)
    assert res["statusCode"] == 403


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
    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    wired.setattr(org.users, "set_folder_name",
                  lambda conn, sub, folder: {
                      "id": "u-new", "cognito_sub": sub, "folder_name": folder})
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


def test_create_member_auto_enrolls_folder_name(member_wired):
    # admin invites "Neil Blunden" -> folder_name auto-set to "Neil_Blunden" (D4)
    wired, fake = member_wired
    seen = {}

    def fake_set_folder(conn, sub, folder_name):
        seen.update(sub=sub, folder_name=folder_name)

    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    wired.setattr(org.users, "set_folder_name", fake_set_folder)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "neil@x.com", "first_name": "Neil", "last_name": "Blunden",
        "memberships": [],
    }), None)
    assert res["statusCode"] == 201
    assert seen == {"sub": "sub-new", "folder_name": "Neil_Blunden"}


def test_create_member_skips_autoenroll_on_folder_collision(member_wired):
    # another user already owns "Neil_Blunden" -> invite still succeeds, folder
    # left unset (no 500 from the global unique index)
    wired, fake = member_wired
    seen = {}

    def fake_set_folder(conn, sub, folder_name):
        seen.update(sub=sub, folder_name=folder_name)

    wired.setattr(org.users, "get_by_folder_name_global",
                  lambda conn, folder: {**CALLER, "cognito_sub": "other-sub", "folder_name": folder})
    wired.setattr(org.users, "set_folder_name", fake_set_folder)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "neil2@x.com", "first_name": "Neil", "last_name": "Blunden",
        "memberships": [],
    }), None)
    assert res["statusCode"] == 201
    assert seen == {}  # collision -> set_folder_name never called, no 500


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


class _FakeS3Paginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, Bucket=None, Prefix=None):
        yield from self.pages


class FakeS3:
    def __init__(self):
        self.copied = []
        self.deleted = []
        self.missing_source = False
        self.objects = {}  # programme.py get_object/put_object store
        self.get_object_calls = []  # keys requested via get_object, in order
        # Override to make get_object raise ClientError with this code
        # instead of NoSuchKey when Key is missing (e.g. "AccessDenied" to
        # simulate a ListBucket-less IAM role — see read_programme).
        self.get_object_error_code = "NoSuchKey"
        # /timeline admin_disambiguation's S3 folder listing
        # (_list_report_folders, now paginated -- Fix wave 1 review finding
        # 3). list_objects_pages, when set, is a list of {"Contents": [...]}
        # page dicts yielded one-per-paginate-iteration (multi-page
        # truncation regression coverage); list_objects_response is the
        # single-page default most tests use.
        self.list_objects_response = {"Contents": []}
        self.list_objects_pages = None

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        pages = self.list_objects_pages if self.list_objects_pages is not None \
            else [self.list_objects_response]
        return _FakeS3Paginator(pages)

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]

    def copy_object(self, Bucket=None, CopySource=None, Key=None):
        if self.missing_source:
            # Real S3 returns AccessDenied (not NoSuchKey) for a missing copy
            # source when the role lacks s3:ListBucket — confirmed in 3b smoke.
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "CopyObject")
        self.copied.append((CopySource["Key"], Key))

    def delete_object(self, Bucket=None, Key=None):
        self.deleted.append(Key)

    def get_object(self, Bucket=None, Key=None):
        self.get_object_calls.append(Key)
        if Key not in self.objects:
            # Matches real boto3: NoSuchKey is itself a ClientError subclass.
            raise ClientError({"Error": {"Code": self.get_object_error_code}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.objects[Key] = Body


@pytest.fixture
def presign_wired(wired):
    fake = FakeS3()
    wired.setattr(org, "_s3_client", fake)
    # Fix wave 1 review finding 1: admin_disambiguation now resolves the
    # lake-owner company before serving summary_report.json verbatim.
    # Default it to the CALLER's own company (c-uuid-1) so every existing
    # admin/gm test keeps its prior behavior unchanged; tests exercising the
    # gate itself override this per-test.
    wired.setattr(org.companies, "get_company_by_name",
                  lambda conn, name: {"id": "c-uuid-1", "name": name})
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
                                        body={"name": "Renamed", "location": "Akl",
                                              "address": "12 Queen St"}), None)
    assert res["statusCode"] == 200
    assert seen["sid"] == "s-1" and seen["cid"] == "c-uuid-1"
    assert seen["name"] == "Renamed" and seen["location"] == "Akl"
    assert seen["address"] == "12 Queen St"


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
                  site_slug=None, allowed_site_slugs=None, include_archived=False):
        seen.update(company_id=company_id, kind=kind, date_from=date_from,
                    date_to=date_to, site_slug=site_slug,
                    allowed_site_slugs=allowed_site_slugs,
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
                     "site_slug": "site-b", "allowed_site_slugs": None,
                     "include_archived": True}


def test_observations_graded_off_company_wide_unchanged(wired):
    # Regression: with GRADED_ROLES at its default (off), /observations must
    # stay company-wide -- allowed_site_slugs is never computed/passed.
    seen = {}

    def fake_list(conn, company_id, kind=None, date_from=None, date_to=None,
                  site_slug=None, allowed_site_slugs=None, include_archived=False):
        seen.update(allowed_site_slugs=allowed_site_slugs)
        return []

    wired.setattr(org.observations, "list_observations", fake_list)
    res = org.lambda_handler(make_event("GET", "/api/org/observations"), None)
    assert res["statusCode"] == 200
    assert seen["allowed_site_slugs"] is None
    assert org.GRADED_ROLES is False


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


# ----------------------------------------------------------
# /live-items
# ----------------------------------------------------------
def test_live_items_requires_date(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/live-items"), None)
    assert res["statusCode"] == 400
    assert "date" in body_of(res)["error"]


def test_live_items_rejects_invalid_date(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "07-07-2026"}), None)
    assert res["statusCode"] == 400


def test_live_items_admin_uses_company_sites(wired):
    seen = {}
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, **kw: (seen.update(cid=cid)
                                           or [{"id": "s-1"}, {"id": "s-2"}]))
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, site_ids, date, author_ids=None: (
                      seen.update(site_ids=site_ids, date=date, author_ids=author_ids)
                      or [{"id": "t-1", "is_live": True, "action_items": [],
                           "safety_observations": []}]))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "2026-07-07"}), None)
    assert res["statusCode"] == 200
    assert seen["cid"] == "c-uuid-1"
    assert sorted(seen["site_ids"]) == ["s-1", "s-2"]   # _allowed_site_ids is a set -- order not guaranteed
    assert seen["date"] == "2026-07-07"
    assert seen["author_ids"] is None                   # graded-off byte parity: no author filter
    assert body_of(res)["topics"] == [{"id": "t-1", "is_live": True, "action_items": [],
                                       "safety_observations": []}]


def test_live_items_worker_uses_accessible_site_ids(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = {}
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: (seen.update(uid=uid, role=role) or ["s-3"]))
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, site_ids, date, author_ids=None: (
                      seen.update(site_ids=site_ids, author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "2026-07-07"}), None)
    assert res["statusCode"] == 200
    assert seen["uid"] == "u-uuid-1" and seen["role"] == "worker"
    assert seen["site_ids"] == ["s-3"]
    assert seen["author_ids"] is None                   # graded-off byte parity: no author filter
    assert body_of(res)["topics"] == []


def test_live_items_graded_off_passes_no_author_filter(wired):
    # Regression: with GRADED_ROLES at its default (off), _author_filter must
    # return None regardless of caller role -- no author narrowing at all.
    seen = {}
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": "s-1"}])
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, site_ids, date, author_ids=None: (
                      seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "2026-07-07"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] is None
    assert org.GRADED_ROLES is False


def test_live_items_response_passthrough_with_children(wired):
    canned = [{
        "id": "t-1", "site_id": "s-1", "site_name": "Alpha", "user_name": "Ada L",
        "is_live": True, "source_s3_key": "extractions/Ada_L/2026-07-07/x.json",
        "action_items": [{"id": "a-1", "text": "fix ladder"}],
        "safety_observations": [{"id": "so-1", "observation": "loose rail"}],
    }]
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": "s-1"}])
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, site_ids, date, author_ids=None: canned)
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "2026-07-07"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["topics"] == canned
    assert body["topics"][0]["action_items"] == [{"id": "a-1", "text": "fix ladder"}]
    assert body["topics"][0]["safety_observations"] == [{"id": "so-1", "observation": "loose rail"}]


def test_live_items_payload_includes_findings_with_impact(wired):
    """Task 5 of docs/superpowers/plans/2026-07-13-programme-impact-link.md:
    /live-items needs ZERO route changes to expose findings -- it serializes
    whatever topics.list_topics_for_date returns generically (no child
    allowlist). This test pins that passthrough for the new `findings` key,
    incl. the programme-impact columns (entity_name/programme_task_id/
    impact_severity)."""
    canned = [{
        "id": "t-1", "site_id": "s-1", "site_name": "Alpha", "user_name": "Ada L",
        "is_live": True, "source_s3_key": "extractions/Ada_L/2026-07-07/x.json",
        "action_items": [], "safety_observations": [],
        "findings": [{
            "id": "f-1", "observation": "Missing edge protection",
            "domain": "safety", "severity": "major",
            "entity_name": "Acme Scaffolding", "entity_trade": "scaffolding",
            "programme_task_id": "task-42", "impact_severity": "major",
            "impact_task_name": "Level 3 Pour", "impact_note": "Blocks the pour",
        }],
    }]
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": "s-1"}])
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, site_ids, date, author_ids=None: canned)
    res = org.lambda_handler(make_event("GET", "/api/org/live-items",
                                        params={"date": "2026-07-07"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    findings = body["topics"][0]["findings"]
    assert findings == canned[0]["findings"]
    assert findings[0]["entity_name"] == "Acme Scaffolding"
    assert findings[0]["programme_task_id"] == "task-42"
    assert findings[0]["impact_severity"] == "major"


# ----------------------------------------------------------
# /programme (S3-backed JSON blob; `site` is the org site's UUID, not a
# slug — ACL mirrors list_live_items EXACTLY: admin/gm (ALL scope) via
# sites.list_company_sites, everyone else via memberships.accessible_site_ids.
# By default both are wired to allow SITE_ID, so individual tests only need
# to override whichever one is relevant to the scenario.)
# ----------------------------------------------------------
# Real UUID shape — required now that _resolve_site_param distinguishes a
# UUID `?site=` value from a slug by regex; a non-UUID-shaped placeholder
# would be (mis)treated as a slug and go down the get_company_site_by_slug
# path instead of the plain passthrough these fixtures exercise.
SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"


@pytest.fixture
def programme_wired(wired):
    fake = FakeS3()
    wired.setattr(org, "_s3_client", fake)
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, **kw: [{"id": SITE_ID}])
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: [SITE_ID])
    return wired, fake


# ----------------------------------------------------------
# _resolve_site_param — shared helper behind get_programme/put_programme.
# Accepts a site UUID (original contract, unchanged) OR a slug (new — this
# unblocks the report side's ?site=<slug> reaching org endpoints). Either
# way the resolved id still has to clear the same ACL as before.
# ----------------------------------------------------------
def test_resolve_site_param_accepts_uuid(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    site_id, err = org._resolve_site_param(FakeConn(), CALLER, SITE_ID)
    assert err is None
    assert site_id == SITE_ID


def test_resolve_site_param_accepts_slug(wired):
    seen = {}

    def fake_by_slug(conn, company_id, slug):
        seen.update(company_id=company_id, slug=slug)
        return {"id": SITE_ID, "slug": slug} if slug == "alpha" else None

    wired.setattr(org.sites, "get_company_site_by_slug", fake_by_slug)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    site_id, err = org._resolve_site_param(FakeConn(), CALLER, "alpha")
    assert err is None
    assert site_id == SITE_ID
    assert seen == {"company_id": "c-uuid-1", "slug": "alpha"}


def test_resolve_site_param_unknown_slug_404(wired):
    wired.setattr(org.sites, "get_company_site_by_slug", lambda conn, cid, slug: None)
    site_id, err = org._resolve_site_param(FakeConn(), CALLER, "ghost-slug")
    assert site_id is None
    assert err["statusCode"] == 404


def test_resolve_site_param_no_access_403(wired):
    # a real UUID, correctly parsed, but not in the caller's allowed set
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {OTHER_SITE_ID})
    site_id, err = org._resolve_site_param(FakeConn(), CALLER, SITE_ID)
    assert site_id is None
    assert err["statusCode"] == 403


def test_get_programme_hit(programme_wired):
    wired, fake = programme_wired
    fake.objects[f"programmes/{SITE_ID}/programme.json"] = json.dumps(
        {"tasks": [{"id": "t-1", "name": "Foundations"}]}).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["programme"] == {"tasks": [{"id": "t-1", "name": "Foundations"}]}


def test_get_programme_miss_returns_null_200(programme_wired):
    wired, fake = programme_wired
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["programme"] is None


def test_get_programme_site_required_400(programme_wired):
    wired, fake = programme_wired
    res = org.lambda_handler(make_event("GET", "/api/org/programme"), None)
    assert res["statusCode"] == 400
    assert "site" in body_of(res)["error"]


def test_get_programme_cross_company_403(programme_wired):
    wired, fake = programme_wired
    # Requested site isn't in the caller's company at all, so it never
    # appears in list_company_sites — same denial path as any other id
    # outside the allowed set (no separate cross-company lookup exists
    # anymore; list_company_sites is already company-scoped SQL).
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": OTHER_SITE_ID}), None)
    assert res["statusCode"] == 403


def test_non_all_role_non_member_site_403(programme_wired):
    wired, fake = programme_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "pm"})
    # accessible_site_ids (membership scope) does NOT include OTHER_SITE_ID
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: [SITE_ID])
    res_get = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": OTHER_SITE_ID}), None)
    assert res_get["statusCode"] == 403

    res_put = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", params={"site": OTHER_SITE_ID},
        body={"tasks": []}), None)
    assert res_put["statusCode"] == 403


def test_admin_any_company_site_ok(programme_wired):
    wired, fake = programme_wired
    # resolve_scope("admin") == "ALL" -> allowed set comes from
    # list_company_sites, NOT accessible_site_ids (which we deliberately
    # leave empty here to prove the ALL-scope path is what's used).
    wired.setattr(org.memberships, "accessible_site_ids", lambda conn, uid, role: [])
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, **kw: [{"id": SITE_ID}, {"id": OTHER_SITE_ID}])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": OTHER_SITE_ID}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["programme"] is None


def test_programme_get_by_slug_works(programme_wired):
    wired, fake = programme_wired
    wired.setattr(org.sites, "get_company_site_by_slug",
                  lambda conn, cid, slug: {"id": SITE_ID} if slug == "alpha" else None)
    fake.objects[f"programmes/{SITE_ID}/programme.json"] = json.dumps(
        {"tasks": [{"id": "t-1", "name": "Foundations"}]}).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": "alpha"}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["programme"] == {"tasks": [{"id": "t-1", "name": "Foundations"}]}


def test_programme_get_by_uuid_still_works(programme_wired):
    # Backward compat: the S3 key stays UUID-based and the original
    # ?site=<uuid> contract still resolves without touching get_company_site_by_slug.
    wired, fake = programme_wired
    fake.objects[f"programmes/{SITE_ID}/programme.json"] = json.dumps(
        {"tasks": [{"id": "t-1", "name": "Foundations"}]}).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["programme"] == {"tasks": [{"id": "t-1", "name": "Foundations"}]}


def test_put_programme_role_gate(programme_wired):
    wired, fake = programme_wired
    body = {"tasks": [{"id": "t-1", "name": "Foundations"}]}
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", params={"site": SITE_ID}, body=body), None)
    assert res["statusCode"] == 403

    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "admin"})
    res_admin = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", params={"site": SITE_ID}, body=body), None)
    assert res_admin["statusCode"] == 200

    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "pm"})
    res_pm = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", params={"site": SITE_ID}, body=body), None)
    assert res_pm["statusCode"] == 200


def test_put_programme_writes_key_and_updated_at(programme_wired):
    wired, fake = programme_wired
    body = {"tasks": [{"id": "t-1", "name": "Foundations"}]}
    res = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", params={"site": SITE_ID}, body=body), None)
    assert res["statusCode"] == 200
    saved = body_of(res)["programme"]
    assert saved["tasks"] == [{"id": "t-1", "name": "Foundations"}]
    assert saved["updated_at"]
    stored = json.loads(fake.objects[f"programmes/{SITE_ID}/programme.json"])
    assert stored == saved


def test_put_programme_site_required_400(programme_wired):
    wired, fake = programme_wired
    res = org.lambda_handler(make_event(
        "PUT", "/api/org/programme", body={"tasks": []}), None)
    assert res["statusCode"] == 400
    assert "site" in body_of(res)["error"]


def test_put_programme_malformed_body_400(programme_wired):
    wired, fake = programme_wired
    ev = make_event("PUT", "/api/org/programme", params={"site": SITE_ID})
    ev["body"] = "{not json"
    res = org.lambda_handler(ev, None)
    assert res["statusCode"] == 400


def test_read_programme_returns_none_on_nosuchkey_clienterror():
    fake = FakeS3()
    fake.get_object_error_code = "NoSuchKey"
    assert org.programme.read_programme(fake, "bucket", SITE_ID) is None


def test_read_programme_reraises_accessdenied():
    fake = FakeS3()
    fake.get_object_error_code = "AccessDenied"
    with pytest.raises(ClientError):
        org.programme.read_programme(fake, "bucket", SITE_ID)


def test_allowed_site_ids_stringifies_uuid():
    """Regression: DB returns uuid.UUID site ids; the ?site= param is a str.
    _allowed_site_ids must return string ids so the `in` check matches
    (real-Aurora 403 bug the string-id mocks missed)."""
    import uuid as _uuid
    import lambda_org_api as m
    sid = _uuid.uuid4()

    class _Conn: pass
    caller = {"global_role": "admin", "company_id": "co-1", "id": "u-1"}
    orig = m.sites.list_company_sites
    m.sites.list_company_sites = lambda conn, cid: [{"id": sid}]
    try:
        allowed = m._allowed_site_ids(_Conn(), caller)
    finally:
        m.sites.list_company_sites = orig
    assert str(sid) in allowed
    assert all(isinstance(x, str) for x in allowed)


# ----------------------------------------------------------
# /rollup/portfolio (Phase 4c leg-1 — deterministic SQL aggregation)
#
# Two test styles:
#   - repo-level (rollup.portfolio_counts): a SQL-level FakeConn/FakeCursor
#     double that feeds one canned result list per cursor().execute() call,
#     in call order — mirrors tests/unit/test_topics_repo.py's FakeConn used
#     for list_topics_for_date's multi-query pattern (that repo test lives
#     in a separate file since it captures raw SQL; here we keep it local
#     since the brief scopes all 9 new tests to this file).
#   - handler-level (list_portfolio_rollup): the existing `wired` fixture,
#     monkeypatching org.sites / org.memberships / org.rollup exactly like
#     the /live-items and /programme tests above.
# ----------------------------------------------------------
class _RollupFakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows


class _RollupFakeConn:
    """`results` is consumed in call order: one entry (a list of row dicts)
    per cursor().execute() call — one entry per GROUP BY query."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return _RollupFakeCursor(self)


def test_portfolio_counts_merges_four_queries():
    conn = _RollupFakeConn(results=[
        [{"site_id": "s-1", "open_safety": 2, "open_high_safety": 1}],
        [{"site_id": "s-1", "open_actions": 3, "total_actions": 5, "overdue_actions": 1}],
        [{"site_id": "s-1", "topics_count": 7, "participants": 4}],
        [{"site_id": "s-1", "last_activity_at": _dt.date(2026, 7, 18)}],
    ])
    counts = org.rollup.portfolio_counts(conn, ["s-1"])
    assert len(conn.calls) == 4
    assert "safety_observations" in conn.calls[0]["sql"]
    assert "action_items" in conn.calls[1]["sql"]
    assert "topics" in conn.calls[2]["sql"]
    assert "MAX(report_date)" in conn.calls[3]["sql"]
    assert "report_date >=" not in conn.calls[3]["sql"]   # all-time, NOT the 30-day window
    assert conn.calls[0]["params"] == (["s-1"],)
    # psycopg returns datetime.date for MAX(report_date) — the repo must
    # normalise it to an ISO string so the JSON layer never sees a date object.
    assert counts == {"s-1": {
        "open_safety": 2, "open_high_safety": 1,
        "open_actions": 3, "total_actions": 5, "overdue_actions": 1,
        "topics_count": 7, "participants": 4,
        "last_activity_at": "2026-07-18",
    }}


def test_zero_count_site_included():
    # no rows come back from any of the 4 GROUP BY queries for either site
    conn = _RollupFakeConn(results=[[], [], [], []])
    counts = org.rollup.portfolio_counts(conn, ["s-1", "s-2"])
    zero = {"open_safety": 0, "open_high_safety": 0, "open_actions": 0,
            "total_actions": 0, "overdue_actions": 0, "topics_count": 0,
            "participants": 0, "last_activity_at": None}
    assert counts == {"s-1": zero, "s-2": dict(zero)}


def test_site_id_keys_are_strings():
    """Regression: DB returns uuid.UUID site ids from the GROUP BY queries —
    every merged dict key must be str() (the exact bug that once 403'd
    /programme; see _allowed_site_ids above)."""
    import uuid as _uuid
    sid = _uuid.uuid4()
    conn = _RollupFakeConn(results=[
        [{"site_id": sid, "open_safety": 1, "open_high_safety": 0}],
        [], [], [],
    ])
    counts = org.rollup.portfolio_counts(conn, [sid])
    assert str(sid) in counts
    assert all(isinstance(k, str) for k in counts)


def test_portfolio_counts_last_activity_is_all_time_not_windowed():
    # A site whose only topics are OLDER than 30 days still gets a
    # last_activity_at (all-time MAX), even though the 30-day topics query
    # returned no row for it — the exact reason the MAX lives in its own
    # query instead of the windowed one.
    conn = _RollupFakeConn(results=[
        [],                                                  # safety
        [],                                                  # actions
        [],                                                  # 30-day topics: nothing
        [{"site_id": "s-1", "last_activity_at": _dt.date(2026, 1, 3)}],
    ])
    counts = org.rollup.portfolio_counts(conn, ["s-1"])
    assert counts["s-1"]["topics_count"] == 0
    assert counts["s-1"]["last_activity_at"] == "2026-01-03"


def test_portfolio_counts_last_activity_none_without_topics():
    conn = _RollupFakeConn(results=[[], [], [], []])
    counts = org.rollup.portfolio_counts(conn, ["s-1"])
    assert counts["s-1"]["last_activity_at"] is None


def test_status_red_on_high_safety():
    assert org._status({"open_high_safety": 1, "open_safety": 0, "open_actions": 0}) == "red"


def test_status_yellow_on_open():
    assert org._status({"open_high_safety": 0, "open_safety": 1, "open_actions": 0}) == "yellow"
    assert org._status({"open_high_safety": 0, "open_safety": 0, "open_actions": 2}) == "yellow"


def test_status_green_when_zero():
    assert org._status({"open_high_safety": 0, "open_safety": 0, "open_actions": 0}) == "green"


def test_portfolio_rollup_admin_all_sites(wired):
    wired.setattr(org.sites, "list_company_sites",
                  lambda conn, cid, **kw: [{"id": "s-1"}, {"id": "s-2"}])
    wired.setattr(org.rollup, "portfolio_counts",
                  lambda conn, site_ids: {
                      "s-1": {"open_safety": 0, "open_high_safety": 0, "open_actions": 0,
                              "total_actions": 0, "overdue_actions": 0, "topics_count": 0,
                              "participants": 0},
                      "s-2": {"open_safety": 1, "open_high_safety": 0, "open_actions": 0,
                              "total_actions": 0, "overdue_actions": 0, "topics_count": 0,
                              "participants": 0},
                  })
    res = org.lambda_handler(make_event("GET", "/api/org/rollup/portfolio"), None)
    assert res["statusCode"] == 200
    sites_by_id = {s["site_id"]: s for s in body_of(res)["sites"]}
    assert set(sites_by_id) == {"s-1", "s-2"}
    assert sites_by_id["s-1"]["status"] == "green"
    assert sites_by_id["s-2"]["status"] == "yellow"


def test_portfolio_rollup_worker_memberships_only(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = {}
    wired.setattr(org.memberships, "accessible_site_ids",
                  lambda conn, uid, role: (seen.update(uid=uid, role=role) or ["s-3"]))
    wired.setattr(org.rollup, "portfolio_counts",
                  lambda conn, site_ids: (seen.update(site_ids=site_ids) or {
                      "s-3": {"open_safety": 0, "open_high_safety": 0, "open_actions": 0,
                              "total_actions": 0, "overdue_actions": 0, "topics_count": 0,
                              "participants": 0},
                  }))
    res = org.lambda_handler(make_event("GET", "/api/org/rollup/portfolio"), None)
    assert res["statusCode"] == 200
    assert seen["uid"] == "u-uuid-1" and seen["role"] == "worker"
    assert seen["site_ids"] == {"s-3"}  # _allowed_site_ids returns a set
    assert body_of(res)["sites"] == [{
        "site_id": "s-3", "open_safety": 0, "open_high_safety": 0, "open_actions": 0,
        "total_actions": 0, "overdue_actions": 0, "topics_count": 0, "participants": 0,
        "status": "green",
    }]


def test_portfolio_rollup_empty_site_ids_empty(wired):
    # worker with no memberships -> _allowed_site_ids returns an empty set;
    # the real rollup.portfolio_counts short-circuits on it without a query.
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "accessible_site_ids", lambda conn, uid, role: [])
    res = org.lambda_handler(make_event("GET", "/api/org/rollup/portfolio"), None)
    assert res["statusCode"] == 200
    assert body_of(res)["sites"] == []


# ----------------------------------------------------------
# /programme/suggestions (Task 5 — manager review queue for the matcher's
# `pending` rows; ACL reuses _resolve_site_param / _allowed_site_ids exactly
# like /programme above. `programme_wired` (defined above) wires FakeS3 +
# sites/memberships to allow SITE_ID only.)
# ----------------------------------------------------------
def _suggestion_row(**over):
    base = {
        "id": "sugg-1", "site_id": SITE_ID, "task_id": "t-1", "topic_id": "topic-1",
        "topic_title": "Poured slab", "topic_summary": "Crew finished the pour.",
        "topic_user_id": "u-1", "report_date": "2026-07-10",
        "source_s3_key": "reports/2026-07-10/foo/daily_report.json",
        "task_name": "Foundations", "task_status_before": "in_progress",
        "task_progress_before": 40, "suggested_status": "completed",
        "suggested_progress": 100, "confidence": 0.9,
        "match_evidence": {"programme_updated_at": "2026-07-01T00:00:00+00:00"},
        "dedupe_key": "abc123", "state": "pending", "decided_by": None,
        "decided_at": None, "applied_status": None, "applied_progress": None,
        "created_at": "2026-07-10T00:00:00+00:00", "updated_at": "2026-07-10T00:00:00+00:00",
    }
    base.update(over)
    return base


def test_list_suggestions_admin_ok(programme_wired):
    wired, fake = programme_wired
    canned = [_suggestion_row()]
    seen = {}
    wired.setattr(org.programme_suggestions, "list_for_site",
                  lambda conn, site_id, state: (seen.update(site_id=site_id, state=state) or canned))
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme/suggestions", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["suggestions"] == canned
    assert seen == {"site_id": SITE_ID, "state": "pending"}


def test_list_suggestions_state_all_passes_none(programme_wired):
    wired, fake = programme_wired
    seen = {}
    wired.setattr(org.programme_suggestions, "list_for_site",
                  lambda conn, site_id, state: (seen.update(state=state) or []))
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme/suggestions",
        params={"site": SITE_ID, "state": "all"}), None)
    assert res["statusCode"] == 200
    assert seen["state"] is None


def test_list_suggestions_inaccessible_site_403(programme_wired):
    wired, fake = programme_wired
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme/suggestions", params={"site": OTHER_SITE_ID}), None)
    assert res["statusCode"] == 403


def test_list_suggestions_worker_403(programme_wired):
    wired, fake = programme_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event(
        "GET", "/api/org/programme/suggestions", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 403


def test_confirm_applies_status_and_writes(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get",
                  lambda conn, sid: row if sid == "sugg-1" else None)
    doc = {"leaves": [{"task_id": "t-1", "parent_id": "p-1", "name": "Foundations",
                       "start": "2026-07-01", "end": "2026-07-15",
                       "status": "in_progress", "progress_pct": 40}],
           "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    written = {}

    def fake_write(s3c, bucket, site_id, doc_, updated_at):
        written.update(site_id=site_id, doc=doc_, updated_at=updated_at)
        doc_["updated_at"] = updated_at
        return doc_

    wired.setattr(org.programme, "write_programme", fake_write)
    decided = {}
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None:
                      (decided.update(sid=sid, state=state, decided_by=decided_by,
                                      applied_status=applied_status,
                                      applied_progress=applied_progress) or {**row, "state": state}))
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"confirmed": True, "task_id": "t-1",
                            "applied_status": "completed", "applied_progress": 100}
    assert written["site_id"] == SITE_ID
    assert written["doc"]["leaves"][0]["status"] == "completed"
    assert written["doc"]["leaves"][0]["progress_pct"] == 100
    assert decided == {"sid": "sugg-1", "state": "confirmed", "decided_by": "u-uuid-1",
                       "applied_status": "completed", "applied_progress": 100}


def test_confirm_reviewer_override_status_and_progress(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 40}],
           "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    written = {}
    wired.setattr(org.programme, "write_programme",
                  lambda s3c, bucket, site_id, doc_, updated_at: (written.update(doc=doc_) or doc_))
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None: {**row})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"status": "in_progress", "progress_pct": 75}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["applied_status"] == "in_progress"
    assert body_of(res)["applied_progress"] == 75
    assert written["doc"]["leaves"][0]["status"] == "in_progress"
    assert written["doc"]["leaves"][0]["progress_pct"] == 75


def test_confirm_task_missing_marks_stale_409(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row(task_id="ghost-task")
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 40}],
           "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    staled = {}
    wired.setattr(org.programme_suggestions, "mark_stale",
                  lambda conn, sid: (staled.update(sid=sid) or {**row, "state": "stale"}))
    write_calls = {"n": 0}
    wired.setattr(org.programme, "write_programme",
                  lambda *a, **k: write_calls.update(n=write_calls["n"] + 1))
    decide_calls = {"n": 0}
    wired.setattr(org.programme_suggestions, "decide",
                  lambda *a, **k: decide_calls.update(n=decide_calls["n"] + 1))
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 409
    assert staled == {"sid": "sugg-1"}
    assert write_calls["n"] == 0
    assert decide_calls["n"] == 0


def test_confirm_task_changed_since_match_409(programme_wired):
    # Fable #1 fix: staleness is now PER-TASK (task_status_before/
    # task_progress_before snapshot vs the live task), not a whole-doc
    # updated_at comparison. Here task t-1's live progress_pct (75) has
    # moved on from what the matcher saw (task_progress_before=40) --
    # someone else changed it since the suggestion was made.
    wired, fake = programme_wired
    row = _suggestion_row(task_status_before="in_progress", task_progress_before=40)
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 75}],
           "parents": [], "updated_at": "2026-07-05T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    write_calls = {"n": 0}
    wired.setattr(org.programme, "write_programme",
                  lambda *a, **k: write_calls.update(n=write_calls["n"] + 1))
    decide_calls = {"n": 0}
    wired.setattr(org.programme_suggestions, "decide",
                  lambda *a, **k: decide_calls.update(n=decide_calls["n"] + 1))
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 409
    assert write_calls["n"] == 0
    assert decide_calls["n"] == 0


def test_confirm_second_pending_suggestion_for_other_task_not_blocked(programme_wired):
    # Regression for the CRITICAL bug: confirming ANY suggestion used to
    # re-stamp programme.json's whole-doc updated_at, and the old check
    # compared THAT against match_evidence.programme_updated_at -- so
    # confirming suggestion A for task t-1 permanently 409'd every OTHER
    # pending suggestion (e.g. B for task t-2) on the SAME site, forever
    # (upsert never refreshes match_evidence). The fix scopes staleness to
    # the one task each suggestion is about, so confirming A must not
    # affect B's confirmability at all.
    wired, fake = programme_wired
    rows = {
        "sugg-A": _suggestion_row(
            id="sugg-A", task_id="t-1", topic_id="topic-a",
            task_status_before="in_progress", task_progress_before=40,
            suggested_status="completed", suggested_progress=100),
        "sugg-B": _suggestion_row(
            id="sugg-B", task_id="t-2", topic_id="topic-b",
            task_status_before="not_started", task_progress_before=0,
            suggested_status="in_progress", suggested_progress=10),
    }
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: rows.get(sid))
    doc = {"leaves": [
        {"task_id": "t-1", "status": "in_progress", "progress_pct": 40},
        {"task_id": "t-2", "status": "not_started", "progress_pct": 0},
    ], "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)

    def fake_write(s3c, bucket, site_id, doc_, updated_at):
        doc_["updated_at"] = updated_at  # mirrors the real write_programme
        return doc_

    wired.setattr(org.programme, "write_programme", fake_write)

    def fake_decide(conn, sid, state, decided_by, applied_status=None, applied_progress=None):
        rows[sid] = {**rows[sid], "state": state}
        return rows[sid]

    wired.setattr(org.programme_suggestions, "decide", fake_decide)

    res_a = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-A/confirm", body={}), None)
    assert res_a["statusCode"] == 200
    # doc.updated_at has now moved on -- under the OLD whole-doc check this
    # alone would 409 every other pending suggestion for this site.

    res_b = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-B/confirm", body={}), None)
    assert res_b["statusCode"] == 200  # THE key regression assertion -- not 409


def test_confirm_already_decided_409(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row(state="confirmed")
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 409


def test_confirm_unknown_id_404(programme_wired):
    wired, fake = programme_wired
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: None)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-ghost/confirm", body={}), None)
    assert res["statusCode"] == 404


def test_confirm_cross_company_site_403(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row(site_id=OTHER_SITE_ID)
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 403


def test_confirm_worker_403(programme_wired):
    wired, fake = programme_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 403


def test_reject_marks_rejected(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    decided = {}
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None:
                      (decided.update(sid=sid, state=state, decided_by=decided_by) or
                       {**row, "state": state}))
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/reject"), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"rejected": True}
    assert decided == {"sid": "sugg-1", "state": "rejected", "decided_by": "u-uuid-1"}


def test_reject_already_decided_409(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row(state="rejected")
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/reject"), None)
    assert res["statusCode"] == 409


def test_confirm_never_lowers_progress_on_auto_value(programme_wired):
    # suggested_progress (60) is below the task's current progress_pct (80)
    # and the reviewer did NOT explicitly send progress_pct -> keep 80.
    # task_progress_before=80 matches the live doc -- this is testing the
    # never-lower-progress rule, not the (separate) per-task staleness gate.
    wired, fake = programme_wired
    row = _suggestion_row(suggested_progress=60, task_progress_before=80)
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 80}],
           "parents": [], "updated_at": row["match_evidence"]["programme_updated_at"]}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    written = {}
    wired.setattr(org.programme, "write_programme",
                  lambda s3c, bucket, site_id, doc_, updated_at: (written.update(doc=doc_) or doc_))
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None: {**row})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 200
    assert written["doc"]["leaves"][0]["progress_pct"] == 80  # not lowered to 60
    assert body_of(res)["applied_progress"] == 80


def test_confirm_explicit_lower_progress_allowed(programme_wired):
    # An explicit reviewer-typed lower value IS allowed (only the
    # auto-suggested value is protected from silently lowering progress).
    wired, fake = programme_wired
    row = _suggestion_row(suggested_progress=60, task_progress_before=80)
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 80}],
           "parents": [], "updated_at": row["match_evidence"]["programme_updated_at"]}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    written = {}
    wired.setattr(org.programme, "write_programme",
                  lambda s3c, bucket, site_id, doc_, updated_at: (written.update(doc=doc_) or doc_))
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None: {**row})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"progress_pct": 50}), None)
    assert res["statusCode"] == 200
    assert written["doc"]["leaves"][0]["progress_pct"] == 50
    assert body_of(res)["applied_progress"] == 50


# ----------------------------------------------------------
# Fable #2 — decide() is the compare-and-swap gate: it must be called BEFORE
# write_programme, and a None return (another request already decided this
# suggestion) must short-circuit with 409 WITHOUT writing to S3.
# ----------------------------------------------------------
def test_confirm_decide_cas_second_call_gets_409_no_write(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 40}],
           "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    write_calls = {"n": 0}
    wired.setattr(org.programme, "write_programme",
                  lambda *a, **k: (write_calls.update(n=write_calls["n"] + 1) or doc))
    # 1st decide() call "wins" the race (returns the confirmed row); 2nd
    # call simulates another request having already decided it (returns
    # None, mirroring decide()'s real `WHERE state='pending'` guard).
    decide_results = iter([{**row, "state": "confirmed"}, None])
    wired.setattr(
        org.programme_suggestions, "decide",
        lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None:
            next(decide_results))

    res1 = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res1["statusCode"] == 200
    assert write_calls["n"] == 1

    res2 = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res2["statusCode"] == 409
    assert write_calls["n"] == 1  # NOT incremented -- the loser never writes S3


# ----------------------------------------------------------
# Fable #5 — a suggestion whose source topic was retracted (topic_id NULL
# via ON DELETE SET NULL on topics deletion/supersession) must be caught at
# confirm time: mark stale + 409, rather than staying silently confirmable.
# ----------------------------------------------------------
def test_confirm_retracted_topic_marks_stale_409(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row(topic_id=None)
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    staled = {}
    wired.setattr(org.programme_suggestions, "mark_stale",
                  lambda conn, sid: (staled.update(sid=sid) or {**row, "state": "stale"}))
    write_calls = {"n": 0}
    wired.setattr(org.programme, "write_programme",
                  lambda *a, **k: write_calls.update(n=write_calls["n"] + 1))
    decide_calls = {"n": 0}
    wired.setattr(org.programme_suggestions, "decide",
                  lambda *a, **k: decide_calls.update(n=decide_calls["n"] + 1))
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm", body={}), None)
    assert res["statusCode"] == 409
    assert staled == {"sid": "sugg-1"}
    assert write_calls["n"] == 0
    assert decide_calls["n"] == 0


# ----------------------------------------------------------
# Fable #9 — reviewer overrides in the confirm body must be validated
# before they can reach programme.json (a bad status/progress used to
# either 500 (TypeError comparing str < int) or write out-of-range data).
# ----------------------------------------------------------
def test_confirm_rejects_out_of_range_progress_400(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"progress_pct": 150}), None)
    assert res["statusCode"] == 400


def test_confirm_rejects_non_integer_progress_400(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"progress_pct": "50"}), None)
    assert res["statusCode"] == 400


def test_confirm_rejects_invalid_status_400(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"status": "bogus"}), None)
    assert res["statusCode"] == 400


def test_confirm_valid_progress_override_applied(programme_wired):
    wired, fake = programme_wired
    row = _suggestion_row()
    wired.setattr(org.programme_suggestions, "get", lambda conn, sid: row)
    doc = {"leaves": [{"task_id": "t-1", "status": "in_progress", "progress_pct": 40}],
           "parents": [], "updated_at": "2026-07-01T00:00:00+00:00"}
    wired.setattr(org.programme, "read_programme", lambda s3c, bucket, site_id: doc)
    written = {}
    wired.setattr(org.programme, "write_programme",
                  lambda s3c, bucket, site_id, doc_, updated_at: (written.update(doc=doc_) or doc_))
    wired.setattr(org.programme_suggestions, "decide",
                  lambda conn, sid, state, decided_by, applied_status=None, applied_progress=None: {**row})
    res = org.lambda_handler(make_event(
        "POST", "/api/org/programme/suggestions/sugg-1/confirm",
        body={"progress_pct": 60}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["applied_progress"] == 60
    assert written["doc"]["leaves"][0]["progress_pct"] == 60


# ----------------------------------------------------------
# /timeline (authority-flip Task 4 — org-api compatibility shim). D1
# contract: byte-identical S3 verbatim for days without extraction topics,
# Aurora-rendered daily_report.json shape for days that have them.
# `presign_wired` (defined above) wires FakeS3 as org._s3_client — the same
# module-level client the shim reads LAKE_BUCKET through (s3() is bucket-
# agnostic; Bucket is a per-call param, no second client needed).
# ----------------------------------------------------------
def _topic_row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
        "category": "safety", "title": "Morning walk", "summary": "Walked the site.",
        "time_range": "08:00 – 08:15", "participants": ["Ada L"],
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def test_timeline_requires_date(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/timeline"), None)
    assert res["statusCode"] == 400
    assert "date" in body_of(res)["error"]
    res2 = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "14-07-2026"}), None)
    assert res2["statusCode"] == 400


def test_timeline_shim_serves_s3_verbatim_when_no_extraction_topics(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    verbatim_doc = {
        "report_date": "2026-07-07", "site": "Alpha", "user_name": "Ada L",
        "executive_summary": "All quiet.", "topics": [{"topic_id": 0, "topic_title": "Legacy"}],
        "_report_metadata": {"source": "nightly_report", "version": "v3.5",
                             "recordings_processed": 4, "total_words": 812},
        "extra_legacy_field": "must survive unchanged",
    }
    fake.objects["reports/2026-07-07/Ada_L/daily_report.json"] = json.dumps(verbatim_doc).encode()
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-07", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == verbatim_doc  # EXACT passthrough -- nothing added/dropped/renamed


def test_timeline_shim_renders_override_when_extraction_topics_exist(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix: [_topic_row()])
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": SITE_ID}])
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["_report_metadata"] == {"source": "live_extraction", "version": "flip-v1"}
    assert body["site"] == "Alpha"
    assert body["user_name"] == "Ada L"
    assert len(body["topics"]) == 1
    assert body["topics"][0]["topic_title"] == "Morning walk"


def test_render_shape_topic_ids_positional_and_ordered():
    rows = [_topic_row(id="t-1", title="First"), _topic_row(id="t-2", title="Second"),
            _topic_row(id="t-3", title="Third")]
    shape = org.render_report_shape(rows, None, "2026-07-14", "Ada_L")
    assert [t["topic_id"] for t in shape["topics"]] == [0, 1, 2]
    assert [t["topic_title"] for t in shape["topics"]] == ["First", "Second", "Third"]


def test_render_shape_safety_flags_from_findings_with_legacy_fallback():
    with_findings = _topic_row(findings=[
        {"observation": "Loose scaffold", "domain": "safety", "severity": "major",
         "recommended_action": "Tag out"},
        {"observation": "Wrong paint batch", "domain": "quality", "severity": "minor",
         "recommended_action": "Reorder"},  # non-safety domain -- excluded from safety_flags
    ])
    legacy = _topic_row(findings=[], safety_observations=[
        {"observation": "Missing handrail", "risk_level": "high"},
    ])
    shape = org.render_report_shape([with_findings, legacy], None, "2026-07-14", "Ada_L")
    assert shape["topics"][0]["safety_flags"] == [
        {"observation": "Loose scaffold", "risk_level": "high", "recommended_action": "Tag out"},
    ]
    assert shape["topics"][1]["safety_flags"] == [
        {"observation": "Missing handrail", "risk_level": "high", "recommended_action": None},
    ]


def test_render_shape_deadline_prefers_deadline_text():
    row = _topic_row(action_items=[
        {"id": "a-1", "text": "Order tape", "responsible": "Ada", "priority": "high",
         "deadline_text": "Tomorrow 8am", "deadline": "2026-07-15", "status": "open"},
        {"id": "a-2", "text": "Fix rail", "responsible": "Sam", "priority": "medium",
         "deadline_text": None, "deadline": "2026-07-16", "status": "open"},
        {"id": "a-3", "text": "Sweep site", "responsible": None, "priority": "low",
         "deadline_text": None, "deadline": None, "status": "open"},
    ])
    shape = org.render_report_shape([row], None, "2026-07-14", "Ada_L")
    items = shape["topics"][0]["action_items"]
    assert items[0]["deadline"] == "Tomorrow 8am"   # deadline_text wins over deadline
    assert items[1]["deadline"] == "2026-07-16"      # falls back to str(deadline)
    assert items[2]["deadline"] is None              # neither present


def test_render_report_shape_exposes_action_item_id_and_status():
    # editable-tasks-reassignment Task 1: the durable id + authoritative
    # status must be on each rendered action item so the card can PATCH it.
    row = _topic_row(action_items=[
        {"id": "a-1", "text": "do X", "responsible": "Neo Tan",
         "deadline": None, "deadline_text": "Tomorrow", "priority": "high", "status": "done"},
    ])
    shape = org.render_report_shape([row], None, "2026-07-18", "Neo_Tan")
    item = shape["topics"][0]["action_items"][0]
    assert item["id"] == "a-1" and item["status"] == "done"
    assert item["action"] == "do X" and item["responsible"] == "Neo Tan"


def test_render_shape_merges_doc_prose_fields():
    row = _topic_row()
    doc = {
        "executive_summary": "Productive day.",
        "safety_observations": [{"observation": "x", "risk_level": "low"}],
        "quality_and_compliance": [{"item": "y"}],
        "critical_dates_and_deadlines": [{"date": "2026-07-20", "item": "Pour"}],
        "topics": [{"topic_title": "SHOULD NOT LEAK THROUGH"}],  # doc's own topics ignored
    }
    shape = org.render_report_shape([row], doc, "2026-07-14", "Ada_L")
    assert shape["executive_summary"] == "Productive day."
    assert shape["safety_observations"] == [{"observation": "x", "risk_level": "low"}]
    assert shape["quality_and_compliance"] == [{"item": "y"}]
    assert shape["critical_dates_and_deadlines"] == [{"date": "2026-07-20", "item": "Pour"}]
    assert shape["topics"][0]["topic_title"] == "Morning walk"  # rendered from rows, not doc

    # doc=None (no same-day S3 doc exists at all) -- prose fields degrade to
    # defaults, not a KeyError/AttributeError.
    shape_no_doc = org.render_report_shape([row], None, "2026-07-14", "Ada_L")
    assert shape_no_doc["executive_summary"] is None
    assert shape_no_doc["safety_observations"] == []
    assert shape_no_doc["quality_and_compliance"] == []
    assert shape_no_doc["critical_dates_and_deadlines"] == []


def test_404_body_matches_prod_shape(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Ghost_User"}), None)
    assert res["statusCode"] == 404
    assert body_of(res) == {"message": "No report for Ghost_User on 2026-07-14",
                            "date": "2026-07-14"}


def test_non_all_scope_user_mismatch_403(presign_wired):
    # Fix wave 1 review finding 2: an explicit ?user= for a DIFFERENT folder
    # than the caller's own used to be silently overridden to self (D10),
    # returning the caller's own report under the other user's URL -- a
    # mislabeled-data bug. Must now be rejected with 403 before any read.
    wired, fake = presign_wired
    seen_has_topics = {"called": False}
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: seen_has_topics.update(called=True))
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Someone_Else"}), None)
    assert res["statusCode"] == 403
    assert "own timeline" in body_of(res)["error"]
    assert seen_has_topics["called"] is False  # no Aurora read attempted for the other folder
    assert fake.get_object_calls == []          # no S3 read attempted either


def test_non_all_scope_user_equals_own_folder_200(presign_wired):
    # ?user= present but equal to the caller's own folder -- self-serve as before.
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    doc = {"report_date": "2026-07-14", "topics": []}
    fake.objects["reports/2026-07-14/Ada_L/daily_report.json"] = json.dumps(doc).encode()
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == doc


def test_non_all_scope_absent_user_self_serves_200(presign_wired):
    # No ?user= at all -- forced to caller's own folder (D10), self-serve.
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    seen = {}
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: (seen.update(prefix=prefix) or False))
    doc = {"report_date": "2026-07-14", "topics": []}
    fake.objects["reports/2026-07-14/Ada_L/daily_report.json"] = json.dumps(doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert seen["prefix"] == "extractions/Ada_L/2026-07-14/"
    assert res["statusCode"] == 200
    assert body_of(res) == doc


def test_non_all_scope_without_folder_name_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": None})
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 403


def test_admin_no_user_unions_extraction_folders(presign_wired):
    wired, fake = presign_wired
    fake.list_objects_response = {"Contents": [
        {"Key": "reports/2026-07-14/Ada_L/daily_report.json"},
        {"Key": "reports/2026-07-14/Ada_L_debug/daily_report.json"},   # _debug -- filtered out
        {"Key": "reports/2026-07-14/Outsider_Co/daily_report.json"},  # not in this company
    ]}

    def fake_by_folder(conn, cid, folder):
        return {"id": "u-2", "folder_name": folder} if folder == "Ada_L" else None

    wired.setattr(org.users, "get_by_folder_name", fake_by_folder)
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: ["Sam_Trainor"])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"date": "2026-07-14", "available_users": ["Ada_L", "Sam_Trainor"]}


def test_admin_summary_report_verbatim(presign_wired):
    # presign_wired's default companies.get_company_by_name pins the "internal"
    # company to c-uuid-1 -- the same id as CALLER -- so this caller IS the
    # lake owner and still gets the summary doc verbatim (Fix wave 1 finding 1).
    wired, fake = presign_wired
    summary_doc = {"date": "2026-07-14", "company_summary": "All sites green.", "extra": 1}
    fake.objects["reports/2026-07-14/summary_report.json"] = json.dumps(summary_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == summary_doc


def test_admin_summary_report_gated_for_non_owner_company(presign_wired):
    # Fix wave 1 review finding 1 (CRITICAL cross-tenant leak): the summary
    # doc is a lake-wide aggregate across EVERY company's folders. A caller
    # whose company isn't the lake owner must never see it verbatim, even
    # though the doc exists in S3 -- must fall through to the (company-
    # scoped) disambiguation union instead, and must never even GetObject it.
    wired, fake = presign_wired
    wired.setattr(org.companies, "get_company_by_name",
                  lambda conn, name: {"id": "OTHER-owner-co", "name": name})
    summary_doc = {"date": "2026-07-14", "company_summary": "All sites green.", "extra": 1}
    fake.objects["reports/2026-07-14/summary_report.json"] = json.dumps(summary_doc).encode()
    # Two candidates -- forces the union envelope response (not the
    # one-candidate recursion path) so this test isolates the gate itself.
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: ["Sam_Trainor", "Ada_L"])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"date": "2026-07-14", "available_users": ["Ada_L", "Sam_Trainor"]}
    assert "reports/2026-07-14/summary_report.json" not in fake.get_object_calls


def test_admin_summary_report_skipped_when_owner_unresolved(presign_wired):
    # Fail-closed proof: if the lake-owner company can't be resolved at all
    # (e.g. COMPANY_NAME points at a company row that doesn't exist), the
    # summary branch is skipped for EVERYONE, not just non-owners -- it
    # never falls back to "no gate" behavior.
    wired, fake = presign_wired
    wired.setattr(org.companies, "get_company_by_name", lambda conn, name: None)
    summary_doc = {"date": "2026-07-14", "company_summary": "All sites green.", "extra": 1}
    fake.objects["reports/2026-07-14/summary_report.json"] = json.dumps(summary_doc).encode()
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 404  # no candidates either -- proves it never touched the summary doc
    assert "reports/2026-07-14/summary_report.json" not in fake.get_object_calls


def test_list_report_folders_paginates_across_pages(presign_wired):
    # Fix wave 1 review finding 3: a single date prefix holds every
    # company's folders in the multi-tenant lake, so an unpaginated
    # list_objects_v2 silently truncates at 1000 keys. Simulate a
    # truncated response spanning two pages and prove both contribute
    # candidates to the union.
    wired, fake = presign_wired
    fake.list_objects_pages = [
        {"Contents": [{"Key": "reports/2026-07-14/Ada_L/daily_report.json"}]},
        {"Contents": [{"Key": "reports/2026-07-14/Sam_T/daily_report.json"}]},
    ]
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-x", "folder_name": folder})
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"date": "2026-07-14", "available_users": ["Ada_L", "Sam_T"]}


def test_admin_no_candidates_404(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 404
    assert body_of(res) == {"message": "No reports for 2026-07-14", "date": "2026-07-14"}


def test_admin_one_candidate_recurses_to_single_user(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: ["Ada_L"])
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    verbatim_doc = {"report_date": "2026-07-14", "topics": []}
    fake.objects["reports/2026-07-14/Ada_L/daily_report.json"] = json.dumps(verbatim_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == verbatim_doc


def test_site_acl_filters_override_rows(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    allowed_row = _topic_row(id="t-1", site_id=SITE_ID, title="Allowed site topic")
    denied_row = _topic_row(id="t-2", site_id=OTHER_SITE_ID, title="Denied site topic")
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [allowed_row, denied_row])
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": SITE_ID}])
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert len(body["topics"]) == 1
    assert body["topics"][0]["topic_title"] == "Allowed site topic"


def test_site_acl_filters_all_rows_falls_back_to_s3(presign_wired):
    # Every override row sits outside the caller's site ACL -- must fall
    # through to the S3 verbatim/404 branch, never render an empty override.
    wired, fake = presign_wired
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [_topic_row(site_id=OTHER_SITE_ID)])
    wired.setattr(org.sites, "list_company_sites", lambda conn, cid, **kw: [{"id": SITE_ID}])
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Ada_L"}), None)
    assert res["statusCode"] == 404


def test_explicit_user_not_in_company_404(presign_wired):
    # RETARGET override 5: an ALL-scope caller's explicit ?user= must resolve
    # to a folder in THEIR OWN company before any Aurora/S3 read is attempted.
    wired, fake = presign_wired
    seen_has_topics = {"called": False}
    wired.setattr(org.users, "get_by_folder_name", lambda conn, cid, folder: None)
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: seen_has_topics.update(called=True))
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Outsider"}), None)
    assert res["statusCode"] == 404
    assert seen_has_topics["called"] is False  # no Aurora read attempted for an unverified folder


# ----------------------------------------------------------
# /dates (Phase 2 read consolidation) — Timeline dots, membership-scoped.
# ACL mirrors /live-items and /programme EXACTLY via _allowed_site_ids /
# _resolve_site_param; an out-of-scope ?site 403s before any date read,
# which is the fix for the legacy get_dates dots leak (visibility spec §1.1).
# ----------------------------------------------------------
def test_dates_admin_scopes_to_allowed_ids(wired):
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1", "s-2"})
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(site_ids=set(site_ids), since=since, author_ids=author_ids)
                      or [_dt.date(2026, 7, 16)]))
    res = org.lambda_handler(make_event("GET", "/api/org/dates", params={"months": "2"}), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == {"s-1", "s-2"}          # no ?site -> full accessible set
    assert isinstance(seen["since"], _dt.date)          # NZ window is a date (BUG-37, not a bare str)
    assert seen["author_ids"] is None                   # graded-off byte parity: no author filter
    assert body_of(res)["dates"] == {"2026-07-16": {"hasReport": True}}


def test_dates_graded_off_passes_no_author_filter(wired):
    # Regression: with GRADED_ROLES at its default (off), /dates must not
    # narrow by author regardless of caller role.
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates"), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] is None
    assert org.GRADED_ROLES is False


def test_dates_worker_scope_via_allowed_ids(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-3"})
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(site_ids=set(site_ids)) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates"), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == {"s-3"}                  # membership scope, not all-company


def test_dates_rejects_site_outside_accessible_set_403(wired):
    # the dots-leak fix: an out-of-scope ?site must 403 BEFORE any date read,
    # not fall through to a lake-wide scan (legacy get_dates bug).
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    called = []
    wired.setattr(org.topics, "list_report_dates",
                  lambda *a, **k: called.append(1) or [])
    res = org.lambda_handler(make_event("GET", "/api/org/dates",
                                        params={"site": OTHER_SITE_ID}), None)
    assert res["statusCode"] == 403
    assert called == []                                 # never reached the date query


def test_dates_with_accessible_site_scopes_to_it(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID, OTHER_SITE_ID})
    seen = {}
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(site_ids=list(site_ids)) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == [SITE_ID]                # scoped to the one accessible ?site


def test_site_members_returns_members_for_accessible_site(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    seen = {}
    wired.setattr(org.memberships, "members_for_site",
                  lambda conn, cid, sid: (seen.update(cid=cid, sid=sid)
                                          or [{"id": "u-1", "first_name": "Ada", "site_role": "worker"}]))
    res = org.lambda_handler(make_event("GET", "/api/org/sites/" + SITE_ID + "/members"), None)
    assert res["statusCode"] == 200
    assert seen == {"cid": "c-uuid-1", "sid": SITE_ID}   # company from caller, site from the URL
    body = body_of(res)
    assert body["site"] == SITE_ID
    assert body["members"][0]["first_name"] == "Ada"


def test_site_members_denies_site_outside_accessible_set_403(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    called = []
    wired.setattr(org.memberships, "members_for_site", lambda *a, **k: called.append(1) or [])
    res = org.lambda_handler(make_event("GET", "/api/org/sites/" + OTHER_SITE_ID + "/members"), None)
    assert res["statusCode"] == 403
    assert called == []                                  # ACL rejects before the members read


# ----------------------------------------------------------
# Phase 3 Task 2 -- per-path x per-role graded ACL tests (visibility spec
# §3.1/§3.2). GRADED_ROLES=True is set explicitly per test via
# wired.setattr(org, "GRADED_ROLES", True); org.scope.visible_scope is
# stubbed to pin one role's exact envelope (no DB needed). The graded-off
# regression tests for /live-items, /dates, /observations already live next
# to their siblings above (test_live_items_graded_off_passes_no_author_filter,
# test_dates_graded_off_passes_no_author_filter,
# test_observations_graded_off_company_wide_unchanged); /timeline's graded-off
# regression is test_timeline_graded_off_forces_self_403_on_other below.
# ----------------------------------------------------------

# ---- /live-items per-user filter (the R1/R4 gap) ----
def test_live_items_worker_filters_to_own_author(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "W",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, sids, date, author_ids=None: (seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] == {"u-self"}               # worker: own author only


def test_live_items_site_manager_self_plus_workers(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self", "u-w1", "u-w2"},
                                        "user_scope": "SELF+WORKERS", "self_folder": "SM",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, sids, date, author_ids=None: (seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] == {"u-self", "u-w1", "u-w2"}   # own + site workers, never other managers


def test_live_items_pm_membership_no_author_filter(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, sids, date, author_ids=None: (seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] is None                     # SITE scope: every author on in-scope sites


def test_live_items_admin_unfiltered(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID, OTHER_SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID, OTHER_SITE_ID}, "author_ids": None,
                                        "user_scope": "ALL", "self_folder": None,
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_topics_for_date",
                  lambda conn, sids, date, author_ids=None: (seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/live-items", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] is None


# ---- /dates author filter ----
def test_dates_worker_author_filtered(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "W",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates"), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] == {"u-self"}


def test_dates_admin_no_author_filter(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "ALL", "self_folder": None,
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since, author_ids=None: (
                      seen.update(author_ids=author_ids) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates"), None)
    assert res["statusCode"] == 200
    assert seen["author_ids"] is None


# ---- /timeline graded authority ----
def test_timeline_worker_denied_other_user_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "Ada_L",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-other", "folder_name": folder})
    seen_has_topics = {"called": False}
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: seen_has_topics.update(called=True))
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Someone_Else"}), None)
    assert res["statusCode"] == 403
    assert seen_has_topics["called"] is False   # denied before any Aurora/S3 read


def test_timeline_worker_defaults_to_self(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "Ada_L",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    doc = {"report_date": "2026-07-14", "topics": []}
    fake.objects["reports/2026-07-14/Ada_L/daily_report.json"] = json.dumps(doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == doc


def test_timeline_site_manager_may_view_worker_on_site(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "SM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self", "u-w1"},
                                        "user_scope": "SELF+WORKERS", "self_folder": "SM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-w1", "folder_name": folder})
    # CRITICAL-1: an authorized cross-user view is served from the worker's
    # in-scope Aurora topics (site-clipped), never the verbatim daily_report.json
    # and never the target's whole-day prose.
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [_topic_row(site_id=SITE_ID, title="Worker in-scope walk")])
    leak_doc = {"report_date": "2026-07-14", "executive_summary": "WHOLE-DAY PROSE",
                "topics": [{"topic_title": "SHOULD NOT APPEAR"}]}
    fake.objects["reports/2026-07-14/Worker1_Folder/daily_report.json"] = json.dumps(leak_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "Worker1_Folder"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert [t["topic_title"] for t in body["topics"]] == ["Worker in-scope walk"]
    assert body["executive_summary"] is None                 # target's whole-day prose not merged
    assert "reports/2026-07-14/Worker1_Folder/daily_report.json" not in fake.get_object_calls


def test_timeline_site_manager_denied_other_site_manager_403(presign_wired):
    # BUG-25 class: a site_manager must never see another site_manager's
    # timeline, only own + workers (D3).
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "SM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self", "u-w1"},
                                        "user_scope": "SELF+WORKERS", "self_folder": "SM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-sm2", "folder_name": folder})
    seen_has_topics = {"called": False}
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: seen_has_topics.update(called=True))
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "Other_SM_Folder"}), None)
    assert res["statusCode"] == 403
    assert seen_has_topics["called"] is False


def test_timeline_pm_may_view_any_in_scope_user(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "PM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-x", "folder_name": folder})
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {SITE_ID: "worker"})    # target's site IS in the pm's site_ids
    # CRITICAL-1: authorized cross-user view -> site-clipped Aurora topics only,
    # never the verbatim daily_report.json / whole-day prose.
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [_topic_row(site_id=SITE_ID, title="In-scope walk")])
    leak_doc = {"report_date": "2026-07-14", "executive_summary": "WHOLE-DAY PROSE",
                "topics": [{"topic_title": "SHOULD NOT APPEAR"}]}
    fake.objects["reports/2026-07-14/Other_User/daily_report.json"] = json.dumps(leak_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "Other_User"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert [t["topic_title"] for t in body["topics"]] == ["In-scope walk"]
    assert body["executive_summary"] is None                 # whole-day prose not merged (clipped)


def test_timeline_pm_denied_out_of_scope_user_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "PM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-y", "folder_name": folder})
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {OTHER_SITE_ID: "worker"})   # target's site is NOT in pm's site_ids
    seen_has_topics = {"called": False}
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: seen_has_topics.update(called=True))
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "Out_Of_Scope_User"}), None)
    assert res["statusCode"] == 403
    assert seen_has_topics["called"] is False


def test_timeline_admin_unchanged_disambiguation(presign_wired):
    # ALL-scope callers still hit admin_disambiguation for a bare (no ?user)
    # request when graded -- unchanged from the legacy behavior.
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "ALL", "self_folder": None,
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.topics, "list_extraction_folder_names_for_date",
                  lambda conn, cid, date: [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14"}), None)
    assert res["statusCode"] == 404
    assert body_of(res) == {"message": "No reports for 2026-07-14", "date": "2026-07-14"}


def test_timeline_graded_off_forces_self_403_on_other(presign_wired):
    # Regression: with GRADED_ROLES at its default (off), the legacy
    # hard-force-self branch runs verbatim and never touches scope.visible_
    # scope at all.
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})

    def _boom(conn, caller):
        raise AssertionError("visible_scope must not be called when GRADED_ROLES is off")

    wired.setattr(org.scope, "visible_scope", _boom)
    res = org.lambda_handler(make_event("GET", "/api/org/timeline",
                                        params={"date": "2026-07-14", "user": "Someone_Else"}), None)
    assert res["statusCode"] == 403
    assert "own timeline" in body_of(res)["error"]
    assert org.GRADED_ROLES is False


# ---- /timeline CRITICAL-1: cross-user views must be site-clipped ----
# Leak scenario: worker W is a member of Site A (pm P's) and Site B (NOT P's).
# On a date W worked only at Site B, P requests W's timeline: _can_view_folder
# is True (they share Site A) but the CONTENT served must contain none of W's
# out-of-scope Site-B content -- no prose, no out-of-scope topics.
def test_timeline_pm_cross_user_out_of_scope_site_no_leak(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "PM_Folder"})
    # pm P: SITE scope, reach = Site A (SITE_ID) only.
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-w", "folder_name": folder})
    # W is a member of BOTH Site A (shared -> _can_view_folder True) and Site B.
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {SITE_ID: "worker", OTHER_SITE_ID: "worker"})
    # ...but on THIS date W only worked at Site B (OTHER_SITE_ID), out of P's scope.
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [_topic_row(site_id=OTHER_SITE_ID, title="SITE-B walk")])
    leak_doc = {"report_date": "2026-07-14", "site": "Site B", "user_name": "W",
                "executive_summary": "SECRET SITE-B SUMMARY",
                "safety_observations": [{"observation": "SITE-B HAZARD"}],
                "quality_and_compliance": [{"item": "SITE-B QA"}],
                "critical_dates_and_deadlines": [{"item": "SITE-B POUR"}],
                "topics": [{"topic_title": "SITE-B TOPIC"}]}
    fake.objects["reports/2026-07-14/W_Folder/daily_report.json"] = json.dumps(leak_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "W_Folder"}), None)
    assert res["statusCode"] == 404                     # no in-scope content -> NOT the verbatim doc
    blob = res["body"]
    for secret in ("SECRET SITE-B SUMMARY", "SITE-B HAZARD", "SITE-B QA",
                   "SITE-B POUR", "SITE-B TOPIC", "SITE-B walk"):
        assert secret not in blob                        # no prose, no out-of-scope topic leaked
    # the un-clipped daily_report.json is never even fetched for a cross-user view
    assert "reports/2026-07-14/W_Folder/daily_report.json" not in fake.get_object_calls


def test_timeline_pm_cross_user_in_scope_returns_clipped_content(presign_wired):
    # Same multi-site target, but on THIS date W worked BOTH sites. Only the
    # in-scope topic (Site A) may surface; the out-of-scope topic (Site B) and
    # the whole-day prose must be clipped out.
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "PM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-w", "folder_name": folder})
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {SITE_ID: "worker", OTHER_SITE_ID: "worker"})
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [
                      _topic_row(site_id=SITE_ID, title="In-scope walk"),
                      _topic_row(site_id=OTHER_SITE_ID, title="SITE-B walk"),
                  ])
    leak_doc = {"executive_summary": "SECRET SITE-B SUMMARY",
                "safety_observations": [{"observation": "SITE-B HAZARD"}],
                "quality_and_compliance": [{"item": "SITE-B QA"}],
                "critical_dates_and_deadlines": [{"item": "SITE-B POUR"}],
                "topics": [{"topic_title": "SITE-B TOPIC"}]}
    fake.objects["reports/2026-07-14/W_Folder/daily_report.json"] = json.dumps(leak_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "W_Folder"}), None)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert [t["topic_title"] for t in body["topics"]] == ["In-scope walk"]   # out-of-scope topic clipped
    assert body["executive_summary"] is None                 # whole-day prose omitted
    assert body["safety_observations"] == []
    assert body["quality_and_compliance"] == []
    assert body["critical_dates_and_deadlines"] == []
    blob = res["body"]
    for secret in ("SECRET SITE-B SUMMARY", "SITE-B HAZARD", "SITE-B QA",
                   "SITE-B POUR", "SITE-B TOPIC", "SITE-B walk"):
        assert secret not in blob
    assert "reports/2026-07-14/W_Folder/daily_report.json" not in fake.get_object_calls


def test_timeline_pm_own_timeline_unaffected_by_clip(presign_wired):
    # Own timeline (user == self_folder): full verbatim/prose served, UNCHANGED.
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "PM_Folder"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": None,
                                        "user_scope": "SITE", "self_folder": "PM_Folder",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    own_doc = {"report_date": "2026-07-14", "executive_summary": "MY OWN SUMMARY", "topics": []}
    fake.objects["reports/2026-07-14/PM_Folder/daily_report.json"] = json.dumps(own_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "PM_Folder"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == own_doc                       # own timeline served verbatim, prose intact


def test_timeline_admin_all_scope_sees_full_target_content(presign_wired):
    # user_scope == ALL (admin/gm/platform_admin): still sees the target's
    # whole-day verbatim report, UNCHANGED -- the clip only applies to graded
    # non-ALL callers viewing someone else.
    wired, fake = presign_wired
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID, OTHER_SITE_ID}, "author_ids": None,
                                        "user_scope": "ALL", "self_folder": None,
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-w", "folder_name": folder})
    wired.setattr(org.topics, "has_topics_for_source_prefix", lambda conn, prefix: False)
    full_doc = {"report_date": "2026-07-14", "executive_summary": "FULL SUMMARY",
                "safety_observations": [{"observation": "HAZARD"}],
                "topics": [{"topic_title": "X"}]}
    fake.objects["reports/2026-07-14/W_Folder/daily_report.json"] = json.dumps(full_doc).encode()
    res = org.lambda_handler(make_event(
        "GET", "/api/org/timeline", params={"date": "2026-07-14", "user": "W_Folder"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == full_doc                      # admin (ALL scope) still sees everything


# ----------------------------------------------------------
# /transcripts (Aurora-identity read — fixes the legacy /transcripts
# gateway's DynamoDB-identity 403 for Aurora-only accounts, e.g.
# site_manager Ben_UCPK). ACL mirrors /timeline's GRADED_ROLES-off path
# verbatim; `presign_wired` wires FakeS3 as org._s3_client (same client
# _read_org_transcripts reads S3_BUCKET through).
# ----------------------------------------------------------

def _transcript_doc():
    return {
        "results": {
            "transcripts": [{"transcript": "Hello there. All good on site."}],
            "audio_segments": [
                {"speaker_label": "spk_0", "transcript": "Hello there.",
                 "start_time": "0.0", "end_time": "2.0"},
                {"speaker_label": "spk_1", "transcript": "All good on site.",
                 "start_time": "2.5", "end_time": "5.0"},
            ],
            "items": [
                {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
                 "alternatives": [{"content": "Hello"}]},
                {"type": "pronunciation", "start_time": "0.6", "end_time": "1.0",
                 "alternatives": [{"content": "there"}]},
            ],
        }
    }


def _wire_one_transcript_file(fake, folder, date, filename="_2026-07-18_08-00-00.json"):
    key = f"transcripts/{folder}/{date}/{folder}{filename}"
    fake.list_objects_response = {"Contents": [{"Key": key}]}
    fake.objects[key] = json.dumps(_transcript_doc()).encode()
    return key


def test_transcripts_requires_date(wired):
    res = org.lambda_handler(make_event("GET", "/api/org/transcripts"), None)
    assert res["statusCode"] == 400
    assert "date" in body_of(res)["error"]
    res2 = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "18-07-2026"}), None)
    assert res2["statusCode"] == 400


def test_transcripts_non_all_caller_reads_own_folder(presign_wired):
    # site_manager Ben_UCPK's actual bug: an Aurora-provisioned, non-ALL
    # caller reading their OWN transcripts must get the transcript shape,
    # not a 403 -- this is the regression the Aurora route exists to fix.
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "site_manager", "folder_name": "Ben_UCPK"})
    _wire_one_transcript_file(fake, "Ben_UCPK", "2026-07-18")
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["count"] == 1
    assert b["speaker_count"] == 2
    assert b["total_speaker_segments"] == 2
    assert b["speakers"] == ["spk_0", "spk_1"]
    assert b["speaker_segments"][0] == {
        "speaker": "spk_0", "text": "Hello there.",
        "start": 28800.0, "end": 28802.0, "time_label": "08:00:00", "duration": 2.0,
    }
    assert b["speaker_segments"][1]["time_label"] == "08:00:02"
    assert b["segments"][0]["filename"] == "Ben_UCPK_2026-07-18_08-00-00.json"


def test_transcripts_non_all_caller_explicit_own_folder_ok(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    _wire_one_transcript_file(fake, "Ada_L", "2026-07-18")
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200


def test_transcripts_non_all_caller_other_user_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18", "user": "Someone_Else"}), None)
    assert res["statusCode"] == 403


def test_transcripts_non_all_caller_without_folder_name_403(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": None})
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 403


def test_transcripts_admin_with_user_ok(presign_wired):
    wired, fake = presign_wired  # CALLER default global_role is "admin"
    wired.setattr(org.users, "get_by_folder_name",
                  lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    _wire_one_transcript_file(fake, "Ada_L", "2026-07-18")
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18", "user": "Ada_L"}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["count"] == 1


def test_transcripts_admin_user_not_in_company_404(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_by_folder_name", lambda conn, cid, folder: None)
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18", "user": "Ghost_User"}), None)
    assert res["statusCode"] == 404


def test_transcripts_no_files_returns_no_transcripts_message(presign_wired):
    wired, fake = presign_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker", "folder_name": "Ada_L"})
    fake.list_objects_response = {"Contents": []}
    res = org.lambda_handler(make_event(
        "GET", "/api/org/transcripts", params={"date": "2026-07-18"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {
        "text": "", "segments": [], "speaker_segments": [], "message": "No transcripts found",
    }


# ---- /observations site scoping ----
def test_observations_worker_scoped_to_member_site_slugs(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID}, "author_ids": {"u-self"},
                                        "user_scope": "SELF", "self_folder": "W",
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    wired.setattr(org.sites, "list_sites_by_ids",
                  lambda conn, ids: [{"id": SITE_ID, "slug": "site-a"}])
    seen = {}
    wired.setattr(org.observations, "list_observations",
                  lambda conn, company_id, kind=None, date_from=None, date_to=None,
                         site_slug=None, allowed_site_slugs=None, include_archived=False: (
                      seen.update(allowed_site_slugs=allowed_site_slugs) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/observations"), None)
    assert res["statusCode"] == 200
    assert seen["allowed_site_slugs"] == {"site-a"}


def test_observations_admin_company_wide(wired):
    wired.setattr(org, "GRADED_ROLES", True)
    wired.setattr(org.scope, "visible_scope",
                  lambda conn, caller: {"site_ids": {SITE_ID, OTHER_SITE_ID}, "author_ids": None,
                                        "user_scope": "ALL", "self_folder": None,
                                        "self_user_id": "u-self", "company_id": "c-uuid-1",
                                        "cross_company": False})
    seen = {}
    wired.setattr(org.observations, "list_observations",
                  lambda conn, company_id, kind=None, date_from=None, date_to=None,
                         site_slug=None, allowed_site_slugs=None, include_archived=False: (
                      seen.update(allowed_site_slugs=allowed_site_slugs) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/observations"), None)
    assert res["statusCode"] == 200
    assert seen["allowed_site_slugs"] is None


# ---- Phase 3 Task 3: platform_admin cross-company writes (D6) ----

def test_create_site_platform_admin_targets_other_company(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "platform_admin"})
    wired.setattr(org.companies, "get_company_by_id",
                  lambda conn, cid: {"id": cid, "name": "Other Co"})
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(company_id=company_id, name=name)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "Cross Co Site", "target_company_id": "c-uuid-B"}), None)
    assert res["statusCode"] == 201
    assert created["company_id"] == "c-uuid-B"          # pinned to target, not caller's company
    assert body_of(res)["company_id"] == "c-uuid-B"


def test_create_site_non_platform_cannot_target_other_company_403(wired):
    # caller stays the default admin (CALLER) -- not platform_admin
    called = []
    wired.setattr(org.sites, "create_site",
                  lambda *a, **k: called.append(1) or {"id": "s-new"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "X", "target_company_id": "c-uuid-B"}), None)
    assert res["statusCode"] == 403
    assert called == []                                  # create_site never reached


def test_create_site_admin_own_company_unaffected(wired):
    # no target_company_id -> caller.company_id, unchanged
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(company_id=company_id, name=name)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "New Site"}), None)
    assert res["statusCode"] == 201
    assert created["company_id"] == "c-uuid-1"


def test_create_site_platform_admin_unknown_target_company_404(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "platform_admin"})
    wired.setattr(org.companies, "get_company_by_id", lambda conn, cid: None)
    called = []
    wired.setattr(org.sites, "create_site",
                  lambda *a, **k: called.append(1) or {"id": "s-new"})
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "X", "target_company_id": "c-ghost"}), None)
    assert res["statusCode"] == 404
    assert called == []


def test_create_site_default_path_unchanged_regardless_of_graded_roles_flag(wired):
    # Task 3's write-path guards are independent of the Task 2 GRADED_ROLES
    # read flag: on or off, a caller with no target_company_id creates in
    # their own company, byte-for-byte identical to pre-Task-3 behavior
    # (mirrors test_create_site_admin_ok exactly, flag toggled on).
    wired.setattr(org, "GRADED_ROLES", True)
    created = {}

    def fake_create(conn, company_id, name, location=None, client=None,
                    industry=None, icon_s3_key=None, address=None,
                    latitude=None, longitude=None):
        created.update(company_id=company_id, name=name, location=location,
                       address=address)
        return {"id": "s-new", "company_id": company_id, "name": name}

    wired.setattr(org.sites, "create_site", fake_create)
    res = org.lambda_handler(make_event("POST", "/api/org/sites", body={
        "name": "New Site", "location": "Chch", "address": "12 Queen St"}), None)
    assert res["statusCode"] == 201
    assert created == {"company_id": "c-uuid-1", "name": "New Site",
                       "location": "Chch", "address": "12 Queen St"}


def test_create_member_platform_admin_targets_other_company(member_wired):
    # upsert_user company_id == target; membership site checks use target company
    wired, fake = member_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: ({**CALLER, "global_role": "platform_admin"}
                                     if sub == "sub-1" else None))
    wired.setattr(org.companies, "get_company_by_id",
                  lambda conn, cid: {"id": cid, "name": "Other Co"})
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-B"})
    seen = {}

    def fake_upsert(conn, sub, email, **kw):
        seen.update(kw)
        return {"id": "u-new", "cognito_sub": sub, "email": email, **kw}

    wired.setattr(org.users, "upsert_user", fake_upsert)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new@x.nz", "target_company_id": "c-uuid-B",
        "memberships": [{"site_id": "s-1", "role": "worker"}],
    }), None)
    assert res["statusCode"] == 201
    assert seen["company_id"] == "c-uuid-B"


def test_create_member_non_platform_cannot_target_other_company_403(member_wired):
    wired, fake = member_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "target_company_id": "c-uuid-B"}), None)
    assert res["statusCode"] == 403
    assert fake.created == []                            # Cognito never reached


def test_create_member_target_company_id_absent_unaffected(member_wired):
    wired, fake = member_wired
    seen = {}

    def fake_upsert(conn, sub, email, **kw):
        seen.update(kw)
        return {"id": "u-new", "cognito_sub": sub, "email": email, **kw}

    wired.setattr(org.users, "upsert_user", fake_upsert)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new@x.nz"}), None)
    assert res["statusCode"] == 201
    assert seen["company_id"] == "c-uuid-1"               # caller's own company, unchanged


def test_create_member_only_platform_admin_may_grant_platform_admin_403(member_wired):
    # caller admin, body global_role='platform_admin' -> 403
    wired, fake = member_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "global_role": "platform_admin"}), None)
    assert res["statusCode"] == 403
    assert fake.created == []


def test_create_member_platform_admin_may_grant_platform_admin(member_wired):
    wired, fake = member_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: ({**CALLER, "global_role": "platform_admin"}
                                     if sub == "sub-1" else None))
    seen = {}

    def fake_upsert(conn, sub, email, **kw):
        seen.update(kw)
        return {"id": "u-new", "cognito_sub": sub, "email": email, **kw}

    wired.setattr(org.users, "upsert_user", fake_upsert)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new-admin@x.nz", "global_role": "platform_admin"}), None)
    assert res["statusCode"] == 201
    assert seen["global_role"] == "platform_admin"


def test_create_member_platform_admin_unknown_target_company_404(member_wired):
    wired, fake = member_wired
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: ({**CALLER, "global_role": "platform_admin"}
                                     if sub == "sub-1" else None))
    wired.setattr(org.companies, "get_company_by_id", lambda conn, cid: None)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "x@x.nz", "target_company_id": "c-ghost"}), None)
    assert res["statusCode"] == 404
    assert fake.created == []


def test_create_member_default_path_unchanged_regardless_of_graded_roles_flag(member_wired):
    # mirrors test_create_member_creates_and_enrolls exactly, flag toggled on
    wired, fake = member_wired
    wired.setattr(org, "GRADED_ROLES", True)
    res = org.lambda_handler(make_event("POST", "/api/org/members", body={
        "email": "new@x.nz", "first_name": "New", "global_role": "site_manager",
        "memberships": [{"site_id": "s-1", "role": "site_manager"}],
    }), None)
    assert res["statusCode"] == 201
    b = body_of(res)
    assert b["user"]["cognito_sub"] == "sub-new"
    assert b["memberships"] == [{"user_id": "u-new", "site_id": "s-1",
                                 "role": "site_manager"}]


def test_patch_member_role_only_platform_admin_may_grant_platform_admin_403(wired):
    # caller stays the default admin (CALLER) -- not platform_admin
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "platform_admin"}), None)
    assert res["statusCode"] == 403


def test_patch_member_role_platform_admin_may_grant_platform_admin(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "platform_admin"})
    seen = {}

    def fake_set(conn, sub, company_id, role):
        seen.update(sub=sub, company_id=company_id, role=role)
        return {**CALLER, "cognito_sub": sub, "global_role": role}

    wired.setattr(org.users, "set_global_role", fake_set)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/members/sub-2/role", body={"global_role": "platform_admin"}), None)
    assert res["statusCode"] == 200
    assert seen["role"] == "platform_admin"


# ----------------------------------------------------------
# PATCH /api/org/action-items/{id} (editable-tasks-reassignment spec Task 1)
# ACL: admin/gm (resolve_scope ALL), this site's pm/site_manager (membership
# authority via memberships.caller_site_roles), or the current assignee
# (responsible == caller's display name) may edit; the task's site must
# also be in the caller's reach (_allowed_site_ids). Reassignment target
# must be a member of the task's site (memberships.members_for_site).
# ----------------------------------------------------------
AITEM = {"id": "a-1", "site_id": SITE_ID, "company_id": "c-uuid-1",
         "responsible": "Ada Owner", "status": "open", "priority": "low"}


def _wire_item(wired, item=AITEM, roles=None, members=None):
    wired.setattr(org.action_items, "get_action_item", lambda conn, i: dict(item))
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {item["site_id"]})
    wired.setattr(org.memberships, "caller_site_roles", lambda conn, uid: roles or {})
    wired.setattr(org.memberships, "members_for_site",
                  lambda conn, cid, sid: members or [{"first_name": "Neo", "last_name": "Tan"}])
    seen = {}
    wired.setattr(org.action_items, "update_action_item_fields",
                  lambda conn, i, fields, by: (seen.update(fields=fields, by=by) or {**item, **fields}))
    return seen


def test_patch_action_item_admin_updates_priority(wired):
    seen = _wire_item(wired)                                   # CALLER is admin (resolve_scope ALL)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "medium"}), None)
    assert res["statusCode"] == 200
    assert seen["fields"] == {"priority": "medium"} and seen["by"] == CALLER["cognito_sub"]


def test_patch_action_item_site_manager_of_site_may_edit(wired):
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = _wire_item(wired, roles={SITE_ID: "site_manager"})  # membership authority, not admin
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "blocked"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"status": "blocked"}


def test_patch_action_item_current_assignee_may_edit_own(wired):
    caller = {**CALLER, "global_role": "worker", "first_name": "Ada", "last_name": "Owner"}
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: caller)
    seen = _wire_item(wired, roles={})                         # no site role, but IS the assignee
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "done"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"status": "done"}


def test_patch_action_item_outsider_worker_denied_403(wired):
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: {**CALLER, "global_role": "worker",
                                                                   "first_name": "X", "last_name": "Y"})
    _wire_item(wired, roles={SITE_ID: "worker"})               # worker on the site, not the assignee
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "done"}), None)
    assert res["statusCode"] == 403


def test_patch_action_item_site_out_of_reach_403(wired):
    wired.setattr(org.action_items, "get_action_item",
                  lambda conn, i: {**AITEM, "site_id": OTHER_SITE_ID})
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})   # not OTHER_SITE_ID
    called = []
    wired.setattr(org.action_items, "update_action_item_fields", lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "low"}), None)
    assert res["statusCode"] == 403 and called == []          # never written


def test_patch_action_item_cross_company_row_404(wired):
    wired.setattr(org.action_items, "get_action_item",
                  lambda conn, i: {**AITEM, "company_id": "OTHER-CO"})
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "low"}), None)
    assert res["statusCode"] == 404


def test_patch_action_item_platform_admin_edits_cross_company(wired):
    """platform_admin (is_cross_company) edits a task in ANOTHER company: both
    the company-pin 404 and the resolve_scope==ALL authority gate yield to
    is_cross_company (mirrors the Team/sites fix in #96). A company admin/gm
    stays pinned — see test_patch_action_item_cross_company_row_404 above."""
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": "c-platform",
                                     "global_role": "platform_admin"})
    seen = _wire_item(wired, item={**AITEM, "company_id": "c-south"})  # task in another company
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"priority": "high"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"priority": "high"}


def test_patch_action_item_reassign_to_site_member_ok(wired):
    seen = _wire_item(wired, members=[{"first_name": "Neo", "last_name": "Tan"}])
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"responsible": "Neo Tan"}), None)
    assert res["statusCode"] == 200 and seen["fields"] == {"responsible": "Neo Tan"}


def test_patch_action_item_reassign_to_non_member_400(wired):
    _wire_item(wired, members=[{"first_name": "Neo", "last_name": "Tan"}])
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"responsible": "Someone Else"}), None)
    assert res["statusCode"] == 400


def test_patch_action_item_bad_status_400(wired):
    _wire_item(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1",
                                        body={"status": "finished"}), None)
    assert res["statusCode"] == 400


def test_patch_action_item_empty_body_400(wired):
    _wire_item(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/action-items/a-1", body={}), None)
    assert res["statusCode"] == 400
