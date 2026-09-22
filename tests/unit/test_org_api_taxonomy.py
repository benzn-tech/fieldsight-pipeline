"""GET/POST/PATCH /api/org/tags — who may read the taxonomy, and who may edit it.

The permission shape, decided against the roles that actually exist in this
codebase rather than the ones a product conversation used:

  * the GLOBAL base set is readable by everyone and writable by nobody;
  * a COMPANY-wide tag is written by `admin` / `gm` -- the company-level gate
    `_MANAGER_ROLES` already draws everywhere else in this file;
  * a SITE-level child is written by those three plus `site_manager`, and a
    site_manager only for a site they are a member of. ("Project lead" is
    product language; `pm` in this codebase is a COMPANY-wide role and the
    per-site one is `site_manager`.)
  * everybody else reads, and picks from what exists.

Every one of these is asserted on the RESPONSE of the real handler, not on a
helper, because the failure this file exists to prevent is an endpoint that
ships with no gate at all -- which has happened here before. A test that calls
the permission helper directly would pass on a route that never calls it.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c-uuid-1"
SITE = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"


def caller(role, **over):
    base = {"id": "u-1", "cognito_sub": "sub-1", "company_id": COMPANY,
            "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
            "folder_name": "Ada_L", "avatar_s3_key": None,
            "global_role": role, "created_at": "2026-09-23"}
    base.update(over)
    return base


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_event(method, path, body=None, params=None, sub="sub-1"):
    return {"httpMethod": method, "path": path, "queryStringParameters": params,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def body_of(res):
    return json.loads(res["body"])


def _tag(**over):
    base = {"id": "t-1", "company_id": COMPANY, "site_id": None, "parent_id": None,
            "slug": "prefab", "label": "Prefab", "is_active": True,
            "sort_order": 0, "created_at": "2026-09-23", "created_by": "u-1"}
    base.update(over)
    return base


@pytest.fixture
def wired(monkeypatch):
    state = {"role": "admin", "created": [], "updated": [],
             "sites": {SITE}, "visible": [_tag()]}

    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: caller(state["role"]))
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, c: set(state["sites"]))
    monkeypatch.setattr(org.tags, "list_visible",
                        lambda conn, company_id, site_ids, **kw: list(state["visible"]))
    monkeypatch.setattr(org.tags, "get", lambda conn, tid: _tag(id=tid))

    def _create(conn, **kw):
        state["created"].append(kw)
        return _tag(**{k: v for k, v in kw.items()
                       if k in ("company_id", "site_id", "parent_id", "slug", "label")})

    def _update(conn, tid, **kw):
        state["updated"].append((tid, kw))
        return _tag(id=tid, **{k: v for k, v in kw.items()
                               if k in ("label", "is_active", "sort_order")})

    monkeypatch.setattr(org.tags, "create", _create)
    monkeypatch.setattr(org.tags, "update", _update)
    return state


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_every_role_can_read_the_taxonomy(wired):
    for role in ("admin", "gm", "pm", "site_manager", "worker"):
        wired["role"] = role
        res = org.lambda_handler(make_event("GET", "/api/org/tags"), None)
        assert res["statusCode"] == 200, role
        assert body_of(res)["tags"][0]["slug"] == "prefab"


def test_the_read_is_scoped_to_the_callers_own_sites(wired):
    seen = {}
    org.tags.list_visible = lambda conn, company_id, site_ids, **kw: (
        seen.update({"company": company_id, "sites": sorted(site_ids)}) or [])
    wired["sites"] = {SITE, OTHER_SITE}
    org.lambda_handler(make_event("GET", "/api/org/tags"), None)
    assert seen["company"] == COMPANY
    assert seen["sites"] == sorted([SITE, OTHER_SITE])


def test_inactive_tags_are_only_returned_when_asked_for(wired):
    seen = {}
    org.tags.list_visible = lambda conn, company_id, site_ids, **kw: (
        seen.update(kw) or [])
    org.lambda_handler(make_event("GET", "/api/org/tags"), None)
    assert not seen.get("include_inactive")
    org.lambda_handler(
        make_event("GET", "/api/org/tags", params={"includeInactive": "1"}), None)
    assert seen.get("include_inactive") is True


# ---------------------------------------------------------------------------
# Creating a COMPANY-wide tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "gm"])
def test_admin_and_gm_can_add_a_company_tag(wired, role):
    wired["role"] = role
    res = org.lambda_handler(make_event(
        "POST", "/api/org/tags", {"slug": "prefab", "label": "Prefab"}), None)
    assert res["statusCode"] == 200, body_of(res)
    assert wired["created"][0]["company_id"] == COMPANY
    assert wired["created"][0]["site_id"] is None


@pytest.mark.parametrize("role", ["pm", "site_manager", "worker"])
def test_nobody_else_can_add_a_company_tag(wired, role):
    wired["role"] = role
    res = org.lambda_handler(make_event(
        "POST", "/api/org/tags", {"slug": "prefab", "label": "Prefab"}), None)
    assert res["statusCode"] == 403, role
    assert wired["created"] == []


# ---------------------------------------------------------------------------
# Creating a SITE-level child
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "gm", "pm", "site_manager"])
def test_a_site_child_can_be_added_by_anyone_who_runs_that_site(wired, role):
    wired["role"] = role
    res = org.lambda_handler(make_event("POST", "/api/org/tags", {
        "slug": "architecture.soffits", "label": "Soffits",
        "parentId": "p-1", "siteId": SITE}), None)
    assert res["statusCode"] == 200, (role, body_of(res))
    assert wired["created"][0]["site_id"] == SITE
    assert wired["created"][0]["company_id"] == COMPANY, (
        "a site tag without its company is invisible to the company read")


def test_a_site_manager_cannot_add_a_child_to_a_site_they_are_not_on(wired):
    wired["role"] = "site_manager"
    wired["sites"] = {SITE}
    res = org.lambda_handler(make_event("POST", "/api/org/tags", {
        "slug": "x.y", "label": "Y", "parentId": "p-1", "siteId": OTHER_SITE}), None)
    assert res["statusCode"] == 403
    assert wired["created"] == []


def test_a_worker_cannot_add_a_site_child_even_on_their_own_site(wired):
    wired["role"] = "worker"
    res = org.lambda_handler(make_event("POST", "/api/org/tags", {
        "slug": "x.y", "label": "Y", "parentId": "p-1", "siteId": SITE}), None)
    assert res["statusCode"] == 403
    assert wired["created"] == []


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

def test_renaming_a_company_tag_is_a_manager_action(wired):
    wired["role"] = "worker"
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/tags/t-1", {"label": "Renamed"}), None)
    assert res["statusCode"] == 403
    assert wired["updated"] == []


def test_the_base_set_is_read_only_through_the_route_too(wired, monkeypatch):
    """The repo raises NotWritable; the route must turn that into a 403 rather
    than a 500, or the one thing nobody may do looks like a server fault."""
    wired["role"] = "admin"

    def _boom(conn, tid, **kw):
        raise org.tags.NotWritable("the base set is read-only")

    monkeypatch.setattr(org.tags, "update", _boom)
    monkeypatch.setattr(org.tags, "get",
                        lambda conn, tid: _tag(id=tid, company_id=None))
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/tags/global-1", {"label": "Ours"}), None)
    assert res["statusCode"] == 403, body_of(res)


def test_a_tag_that_does_not_exist_is_a_404_not_a_403(wired, monkeypatch):
    """Answering 403 for both would tell a stranger which ids are real."""
    wired["role"] = "admin"
    monkeypatch.setattr(org.tags, "get", lambda conn, tid: None)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/tags/nope", {"label": "X"}), None)
    assert res["statusCode"] == 404


def test_the_slug_cannot_be_changed_through_the_route(wired):
    """Accepted-and-ignored is worse than refused: the caller is told their
    rename worked and every tagged row silently keeps the old word."""
    wired["role"] = "admin"
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/tags/t-1", {"slug": "something-else"}), None)
    assert res["statusCode"] == 400, body_of(res)
    assert wired["updated"] == []


def test_a_create_without_a_slug_or_label_is_rejected(wired):
    wired["role"] = "admin"
    for bad in ({"label": "No slug"}, {"slug": "no-label"}, {}):
        res = org.lambda_handler(make_event("POST", "/api/org/tags", bad), None)
        assert res["statusCode"] == 400, bad
    assert wired["created"] == []
