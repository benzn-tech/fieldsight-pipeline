"""PUT/DELETE /api/org/members/{sub}/memberships/{site_id}.

Until these routes existed, the ONLY production write path into `memberships`
was create_member (POST /members) -- so moving an existing person onto a second
project meant re-inviting their email and relying on create_member's idempotent
fallback as a side effect, and taking somebody OFF one project was impossible
without archiving the whole person. `memberships.archived_at` was only ever set
as collateral by sites.archive_site / users.archive_user.

The per-site role these routes write is load-bearing now that GRADED_ROLES is
true on prod: visible_scope grades a caller by membership.role per site, so a
person can be pm on Project A and worker on Project B -- a distinction nothing
could express or correct after the invite.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


def make_event(method, path, sub="sub-1", body=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


class FakeConn:
    """Records nothing: every repository call these routes make is stubbed, so
    a query reaching the connection means the handler took a path the test did
    not intend."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def transaction(self):
        return self

    def execute(self, *a, **k):
        class _Cur:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _Cur()


CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}


def body_of(res):
    return json.loads(res["body"])


TARGET = {"id": "u-2", "cognito_sub": "sub-2", "company_id": "c-uuid-1",
          "email": "b@x.nz", "first_name": "Bo", "last_name": "R",
          "global_role": "worker", "avatar_s3_key": None, "created_at": "2026-07-04"}


@pytest.fixture
def wired(monkeypatch):
    """Minimal org-api wiring: a FakeConn and nothing else stubbed, so any
    unexpected repository call shows up rather than being papered over."""
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    return monkeypatch


@pytest.fixture
def mem_wired(wired):
    """Admin caller + one in-company target + one in-company site, with both
    membership writers recorded so tests assert the CALL, not the fake."""
    calls = {"ensure": [], "archive": []}
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        dict(CALLER) if sub == "sub-1" else dict(TARGET) if sub == "sub-2" else None))
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1", "archived_at": None})

    def _ensure(conn, uid, sid, role):
        calls["ensure"].append((uid, sid, role))
        return {"user_id": uid, "site_id": sid, "role": role}

    def _archive(conn, uid, sid):
        calls["archive"].append((uid, sid))
        return {"user_id": uid, "site_id": sid, "archived_at": "2026-09-08"}

    wired.setattr(org.memberships, "ensure_membership", _ensure)
    wired.setattr(org.memberships, "archive_membership", _archive, raising=False)
    return wired, calls


def put(site="s-9", sub="sub-2", body=None, caller="sub-1"):
    return make_event("PUT", "/api/org/members/" + sub + "/memberships/" + site,
                      sub=caller, body=body)


def delete(site="s-9", sub="sub-2", caller="sub-1"):
    return make_event("DELETE", "/api/org/members/" + sub + "/memberships/" + site,
                      sub=caller)


# ---------------------------------------------------------------- PUT (add/update)
def test_put_adds_target_to_another_project(mem_wired):
    _, calls = mem_wired
    res = org.lambda_handler(put(body={"role": "pm"}), None)
    assert res["statusCode"] == 200
    assert calls["ensure"] == [("u-2", "s-9", "pm")]
    assert body_of(res)["membership"]["role"] == "pm"


def test_put_updates_the_per_site_role_of_an_existing_membership(mem_wired):
    """ensure_membership upserts on (user_id, site_id), so the same route is
    how a person's role ON ONE project is corrected -- the gap PATCH
    /members/{sub}/role never covered (that one only writes global_role)."""
    _, calls = mem_wired
    res = org.lambda_handler(put(body={"role": "site_manager"}), None)
    assert res["statusCode"] == 200
    assert calls["ensure"] == [("u-2", "s-9", "site_manager")]


def test_put_rejects_a_role_outside_the_membership_vocabulary(mem_wired):
    _, calls = mem_wired
    res = org.lambda_handler(put(body={"role": "admin"}), None)
    assert res["statusCode"] == 400
    assert calls["ensure"] == []


def test_put_rejects_missing_role(mem_wired):
    res = org.lambda_handler(put(body={}), None)
    assert res["statusCode"] == 400


def test_put_rejects_malformed_body(mem_wired):
    ev = put(body={"role": "pm"})
    ev["body"] = "{not json"
    assert org.lambda_handler(ev, None)["statusCode"] == 400


def test_put_refuses_a_site_in_another_company(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-other", "archived_at": None})
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 403
    assert calls["ensure"] == []


def test_put_refuses_a_target_in_another_company(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        dict(CALLER) if sub == "sub-1" else {**TARGET, "company_id": "c-other"}))
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 404
    assert body_of(res)["error"] == "member not found in your company"
    assert calls["ensure"] == []


def test_put_refuses_an_unknown_target(mem_wired):
    _, calls = mem_wired
    res = org.lambda_handler(put(sub="sub-ghost", body={"role": "worker"}), None)
    assert res["statusCode"] == 404
    assert body_of(res)["error"] == "member not found in your company"
    assert calls["ensure"] == []


def test_put_refuses_an_unknown_site(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.sites, "get_site", lambda conn, sid: None)
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 403
    assert calls["ensure"] == []


def test_put_refuses_an_archived_site(mem_wired):
    """Mirrors create_member: you cannot staff a project that is closed."""
    wired, calls = mem_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1",
                                     "archived_at": "2026-07-01"})
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 409
    assert calls["ensure"] == []


def test_put_refuses_an_archived_target(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        dict(CALLER) if sub == "sub-1" else {**TARGET, "archived_at": "2026-07-01"}))
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 409
    assert calls["ensure"] == []


def test_put_refuses_a_non_admin_caller(mem_wired):
    """gm resolves to ALL scope for READS, but staffing is admin-only --
    the same line create_member draws."""
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "global_role": "gm"} if sub == "sub-1" else dict(TARGET)))
    res = org.lambda_handler(put(body={"role": "worker"}), None)
    assert res["statusCode"] == 403
    assert calls["ensure"] == []


def test_put_refuses_a_worker_caller(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "global_role": "worker"} if sub == "sub-1" else dict(TARGET)))
    assert org.lambda_handler(put(body={"role": "pm"}), None)["statusCode"] == 403
    assert calls["ensure"] == []


# ---------------------------------------------------------------- platform_admin
def test_put_platform_admin_adopts_the_sites_company(mem_wired):
    """A platform_admin sits in an empty operator company; pinning to its own
    company_id would 403 every real staffing call (same fix as create_member
    and patch_org_site)."""
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "company_id": "c-platform", "global_role": "platform_admin"}
        if sub == "sub-1" else dict(TARGET)))
    res = org.lambda_handler(put(body={"role": "pm"}), None)
    assert res["statusCode"] == 200
    assert calls["ensure"] == [("u-2", "s-9", "pm")]


def test_put_platform_admin_cannot_staff_across_the_tenant_boundary(mem_wired):
    """Cross-company reach is NOT permission to mix tenants: the target must
    belong to the company that owns the site."""
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "company_id": "c-platform", "global_role": "platform_admin"}
        if sub == "sub-1" else {**TARGET, "company_id": "c-other"}))
    res = org.lambda_handler(put(body={"role": "pm"}), None)
    assert res["statusCode"] == 404
    assert body_of(res)["error"] == "member not found in your company"
    assert calls["ensure"] == []


# ---------------------------------------------------------------- DELETE (unstaff)
def test_delete_removes_the_person_from_that_project_only(mem_wired):
    _, calls = mem_wired
    res = org.lambda_handler(delete(), None)
    assert res["statusCode"] == 200
    assert calls["archive"] == [("u-2", "s-9")]


def test_delete_is_404_when_there_is_no_live_membership(mem_wired):
    """Soft-remove is idempotent at the DB level but must not report success
    for a membership that was never there -- an admin reading 200 would believe
    somebody had been taken off a project they still sit on."""
    wired, _ = mem_wired
    wired.setattr(org.memberships, "archive_membership",
                  lambda conn, uid, sid: None, raising=False)
    res = org.lambda_handler(delete(), None)
    assert res["statusCode"] == 404
    assert body_of(res)["error"] == "no active membership on that site"


def test_delete_refuses_a_site_in_another_company(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-other", "archived_at": None})
    res = org.lambda_handler(delete(), None)
    assert res["statusCode"] == 403
    assert calls["archive"] == []


def test_delete_refuses_a_target_in_another_company(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        dict(CALLER) if sub == "sub-1" else {**TARGET, "company_id": "c-other"}))
    res = org.lambda_handler(delete(), None)
    assert res["statusCode"] == 404
    assert body_of(res)["error"] == "member not found in your company"
    assert calls["archive"] == []


def test_delete_refuses_a_non_admin_caller(mem_wired):
    wired, calls = mem_wired
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "global_role": "pm"} if sub == "sub-1" else dict(TARGET)))
    assert org.lambda_handler(delete(), None)["statusCode"] == 403
    assert calls["archive"] == []


def test_delete_allows_removing_a_membership_on_an_archived_site(mem_wired):
    """Unlike PUT, unstaffing a closed project must stay possible -- otherwise
    archiving a site would freeze its roster forever."""
    wired, calls = mem_wired
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-uuid-1",
                                     "archived_at": "2026-07-01"})
    res = org.lambda_handler(delete(), None)
    assert res["statusCode"] == 200
    assert calls["archive"] == [("u-2", "s-9")]


# ---------------------------------------------------------------- route wiring
def test_the_membership_route_is_not_swallowed_by_the_member_folder_route(mem_wired):
    """/members/{sub}/folder and /members/{sub}/memberships/{site} share a
    prefix; a loose regex on the first would eat the second."""
    _, calls = mem_wired
    res = org.lambda_handler(put(site="folder", body={"role": "worker"}), None)
    assert res["statusCode"] == 200
    assert calls["ensure"] == [("u-2", "folder", "worker")]


# ------------------------------------------------- folder enrolment: same door
# patch_member_folder and backfill_member_folders were left on a literal
# `!= "admin"` while every other member route grew an ("admin", "platform_admin")
# check. A platform_admin therefore could not enrol a customer's folder_name at
# all -- and folder_name is what links a login to the recording folder its
# reports are written under, so the operator account could see a tenant's people
# but not fix the one field that makes their clips reachable.
@pytest.fixture
def folder_wired(wired):
    """Caller + a target sitting in a DIFFERENT company (the platform_admin
    case), with the folder writers recorded."""
    calls = {"set": [], "listed": []}
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        dict(CALLER) if sub == "sub-1"
        else {**TARGET, "company_id": "c-other"} if sub == "sub-2" else None))
    wired.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)

    def _set(conn, sub, folder):
        calls["set"].append((sub, folder))
        return {"cognito_sub": sub, "folder_name": folder}

    def _list(conn, cid):
        calls["listed"].append(cid)
        return [{"cognito_sub": "sub-2", "first_name": "Bo", "last_name": "R"}]

    wired.setattr(org.users, "set_folder_name", _set)
    wired.setattr(org.users, "list_company_logins_unenrolled", _list)
    wired.setattr(org.companies, "get_company_by_id",
                  lambda conn, cid: {"id": cid, "name": "Other Co"})
    return wired, calls


def folder_event(sub="sub-2", body=None, caller="sub-1"):
    return make_event("PATCH", "/api/org/members/" + sub + "/folder",
                      sub=caller, body=body if body is not None else {"folder_name": "Bo R"})


def as_platform_admin(wired, target_company="c-other"):
    wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: (
        {**CALLER, "company_id": "c-platform", "global_role": "platform_admin"}
        if sub == "sub-1" else {**TARGET, "company_id": target_company}))


def test_platform_admin_may_enrol_a_folder_in_a_customer_company(folder_wired):
    wired, calls = folder_wired
    as_platform_admin(wired)
    res = org.lambda_handler(folder_event(), None)
    assert res["statusCode"] == 200
    assert calls["set"] == [("sub-2", "Bo_R")]


def test_company_admin_still_cannot_enrol_a_folder_outside_its_company(folder_wired):
    """The widening must not cost the tenant pin for ordinary admins."""
    _, calls = folder_wired
    res = org.lambda_handler(folder_event(), None)
    assert res["statusCode"] == 404
    assert calls["set"] == []


def test_backfill_refuses_a_platform_admin_that_names_no_company(folder_wired):
    """A platform_admin's own company is the empty operator company, so the
    unfixed route would have answered 200 {"enrolled": []} -- a successful
    report of work that could not have happened."""
    wired, calls = folder_wired
    as_platform_admin(wired)
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill"), None)
    assert res["statusCode"] == 400
    assert calls["listed"] == []


def test_backfill_lets_a_platform_admin_name_the_company(folder_wired):
    wired, calls = folder_wired
    as_platform_admin(wired)
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill",
                                        body={"target_company_id": "c-other"}), None)
    assert res["statusCode"] == 200
    assert calls["listed"] == ["c-other"]
    assert calls["set"] == [("sub-2", "Bo_R")]


def test_backfill_refuses_a_company_admin_reaching_into_another_company(folder_wired):
    _, calls = folder_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill",
                                        body={"target_company_id": "c-other"}), None)
    assert res["statusCode"] == 403
    assert calls["listed"] == []


def test_backfill_for_a_company_admin_still_uses_its_own_company(folder_wired):
    _, calls = folder_wired
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill"), None)
    assert res["statusCode"] == 200
    assert calls["listed"] == ["c-uuid-1"]


def test_backfill_refuses_a_company_that_does_not_exist(folder_wired):
    """Mirrors create_member: a typo'd company id must not sweep nothing and
    call it a success."""
    wired, calls = folder_wired
    as_platform_admin(wired)
    wired.setattr(org.companies, "get_company_by_id", lambda conn, cid: None)
    res = org.lambda_handler(make_event("POST", "/api/org/members/enroll-backfill",
                                        body={"target_company_id": "c-nope"}), None)
    assert res["statusCode"] == 404
    assert calls["listed"] == []
