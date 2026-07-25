"""Endpoint tests for the durable safety/quality resolved-state routes and the
content-edit re-key hook (spec 2026-07-26 §3/§3.1/§4).

Harness mirrors test_lambda_org_api.py (make_event / FakeConn / wired), kept
local so this file stands alone.
"""
import json

import pytest

import content_hash as ch

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
          "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04"}


def make_event(method, path, sub="sub-1", body=None, params=None):
    return {
        "httpMethod": method, "path": path, "queryStringParameters": params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}},
    }


def body_of(res):
    return json.loads(res["body"])


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.memberships, "caller_site_roles", lambda conn, uid: {})
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": "c-uuid-1"})
    return monkeypatch


# ==========================================================================
# PATCH /api/org/compliance/resolution
# ==========================================================================
GOOD_BODY = {"domain": "safety", "site": SITE_ID, "report_date": "2026-07-20",
             "user_folder": "Ada_L", "text": "Loose handrail on level 3",
             "resolved": True}


def _wire_upsert(wired, returns=None):
    seen = {}
    row = returns or {"id": "r-1", "resolved": True, "resolved_by": "u-uuid-1",
                      "resolved_at": "2026-07-20T00:00:00Z", "content_hash": "H",
                      "content_sample": "loose handrail on level 3",
                      "resolved_by_name": "Ada L"}

    def fake_upsert(conn, company_id, site_id, report_date, domain, user_folder,
                    content_hash, content_sample, resolved, resolved_by):
        seen.update(company_id=company_id, site_id=site_id, report_date=report_date,
                    domain=domain, user_folder=user_folder, content_hash=content_hash,
                    content_sample=content_sample, resolved=resolved, resolved_by=resolved_by)
        return row

    wired.setattr(org.compliance_resolutions, "upsert_resolution", fake_upsert)
    return seen


def test_write_admin_resolves_and_computes_hash_server_side(wired):
    seen = _wire_upsert(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 200
    # hash + sample computed SERVER-SIDE from the text (client never sends them).
    assert seen["content_hash"] == ch.content_hash(GOOD_BODY["text"])
    assert seen["content_sample"] == ch.normalize(GOOD_BODY["text"])
    assert seen["resolved"] is True and seen["resolved_by"] == "u-uuid-1"
    assert seen["user_folder"] == "Ada_L" and seen["domain"] == "safety"
    b = body_of(res)
    assert b["resolved"] is True and b["resolved_by"] == "Ada L"
    assert b["content_hash"] == "H" and "content_sample" in b


def test_write_ignores_client_supplied_hash(wired):
    seen = _wire_upsert(wired)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/compliance/resolution",
        body={**GOOD_BODY, "content_hash": "ATTACKER-CONTROLLED"}), None)
    assert res["statusCode"] == 200
    assert seen["content_hash"] == ch.content_hash(GOOD_BODY["text"])   # never the body's


def test_write_site_manager_of_site_may_resolve(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {SITE_ID: "site_manager"})
    seen = _wire_upsert(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 200 and seen["domain"] == "safety"


def test_write_worker_on_site_denied_403(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    wired.setattr(org.memberships, "caller_site_roles",
                  lambda conn, uid: {SITE_ID: "worker"})   # on the site, no authority
    called = []
    wired.setattr(org.compliance_resolutions, "upsert_resolution",
                  lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 403 and called == []       # never written


def test_write_site_out_of_reach_403(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {OTHER_SITE_ID})
    called = []
    wired.setattr(org.compliance_resolutions, "upsert_resolution",
                  lambda *a, **k: called.append(1))
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 403 and called == []


def test_write_cross_company_platform_admin_keys_under_site_company(wired):
    # platform_admin resolving another company's site: company_id must be the
    # SITE's tenant (where the owner's read will find it), not the caller's.
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": "c-platform",
                                     "global_role": "platform_admin"})
    wired.setattr(org.sites, "get_site",
                  lambda conn, sid: {"id": sid, "company_id": "c-south"})
    seen = _wire_upsert(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 200
    assert seen["company_id"] == "c-south"                 # site's tenant, not "c-platform"


def test_write_unknown_slug_404(wired):
    wired.setattr(org.sites, "get_company_site_by_slug", lambda conn, cid, slug: None)
    res = org.lambda_handler(make_event(
        "PATCH", "/api/org/compliance/resolution",
        body={**GOOD_BODY, "site": "ghost-slug"}), None)
    assert res["statusCode"] == 404


def test_write_null_safe_resolver_name_passthrough(wired):
    _wire_upsert(wired, returns={"id": "r-1", "resolved": True, "resolved_by": "u-1",
                                 "resolved_at": "t", "content_hash": "H",
                                 "content_sample": "s", "resolved_by_name": None})
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body=GOOD_BODY), None)
    assert res["statusCode"] == 200
    assert body_of(res)["resolved_by"] is None             # not " ", not "" -> null


def test_write_collision_requires_same_user_folder(wired):
    # Identical text under TWO different recorders keys to two DISTINCT rows:
    # same content_hash, different user_folder (the 1a fix). Two recorders no
    # longer collide.
    keys = []
    wired.setattr(org.compliance_resolutions, "upsert_resolution",
                  lambda conn, company_id, site_id, report_date, domain, user_folder,
                  content_hash, content_sample, resolved, resolved_by:
                  keys.append((user_folder, content_hash)) or
                  {"id": "r", "resolved": resolved, "resolved_by": resolved_by,
                   "resolved_at": "t", "content_hash": content_hash,
                   "content_sample": content_sample, "resolved_by_name": None})
    for folder in ("Ada_L", "Ben_K"):
        res = org.lambda_handler(make_event(
            "PATCH", "/api/org/compliance/resolution",
            body={**GOOD_BODY, "user_folder": folder}), None)
        assert res["statusCode"] == 200
    (f1, h1), (f2, h2) = keys
    assert h1 == h2                                        # identical text -> identical hash
    assert f1 != f2                                        # ...but distinct folders -> distinct keys


@pytest.mark.parametrize("bad", [
    {"domain": "progress"}, {"report_date": "07-20-2026"}, {"report_date": "nope"},
    {"user_folder": ""}, {"user_folder": "   "}, {"text": ""}, {"resolved": "yes"},
])
def test_write_validation_400(wired, bad):
    _wire_upsert(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution",
                                        body={**GOOD_BODY, **bad}), None)
    assert res["statusCode"] == 400


def test_write_malformed_body_400(wired):
    _wire_upsert(wired)
    res = org.lambda_handler(make_event("PATCH", "/api/org/compliance/resolution"), None)
    assert res["statusCode"] == 400


# ==========================================================================
# GET /api/org/compliance/resolutions
# ==========================================================================
def test_read_returns_resolutions_scoped_to_reach(wired):
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID, OTHER_SITE_ID})
    wired.setattr(org.compliance_resolutions, "list_resolutions",
                  lambda conn, company_id, site_ids, dfrom, dto, domain=None:
                  seen.update(company_id=company_id, site_ids=site_ids, dfrom=dfrom,
                              dto=dto, domain=domain) or [{"site_id": SITE_ID, "resolved": True}])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/compliance/resolutions",
        params={"from": "2026-07-01", "to": "2026-07-31"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"resolutions": [{"site_id": SITE_ID, "resolved": True}]}
    assert seen["site_ids"] == {SITE_ID, OTHER_SITE_ID}    # full reach when no ?site
    assert seen["company_id"] == "c-uuid-1" and seen["domain"] is None


def test_read_with_site_param_scopes_to_that_site(wired):
    seen = {}
    wired.setattr(org.compliance_resolutions, "list_resolutions",
                  lambda conn, company_id, site_ids, dfrom, dto, domain=None:
                  seen.update(site_ids=site_ids, domain=domain) or [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/compliance/resolutions",
        params={"from": "2026-07-01", "to": "2026-07-31", "site": SITE_ID,
                "domain": "quality"}), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == {SITE_ID} and seen["domain"] == "quality"


def test_read_empty_reach_returns_empty_without_query(wired):
    # []-means-no-filter trap: empty reach -> empty list, and the REAL repo fn
    # must not run a query. Use the real list_resolutions (unmocked).
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: set())
    res = org.lambda_handler(make_event(
        "GET", "/api/org/compliance/resolutions",
        params={"from": "2026-07-01", "to": "2026-07-31"}), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {"resolutions": []}


def test_read_cross_company_platform_admin_drops_tenant_pin(wired):
    seen = {}
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "company_id": "c-platform",
                                     "global_role": "platform_admin"})
    wired.setattr(org.compliance_resolutions, "list_resolutions",
                  lambda conn, company_id, *a, **k: seen.update(company_id=company_id) or [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/compliance/resolutions",
        params={"from": "2026-07-01", "to": "2026-07-31"}), None)
    assert res["statusCode"] == 200
    assert seen["company_id"] is None                      # tenant pin dropped for cross-company


@pytest.mark.parametrize("params,code", [
    ({"to": "2026-07-31"}, 400),                            # from missing
    ({"from": "2026-07-01"}, 400),                          # to missing
    ({"from": "2026-07-31", "to": "2026-07-01"}, 400),      # to precedes from
    ({"from": "2026-07-01", "to": "2026-07-31", "domain": "progress"}, 400),
])
def test_read_validation_400(wired, params, code):
    wired.setattr(org.compliance_resolutions, "list_resolutions", lambda *a, **k: [])
    res = org.lambda_handler(make_event(
        "GET", "/api/org/compliance/resolutions", params=params), None)
    assert res["statusCode"] == code


# ==========================================================================
# item 1b — content-edit re-key hook (_rekey_compliance_mark)
# ==========================================================================
CTX = {"company_id": "c-uuid-1", "site_id": SITE_ID, "report_date": "2026-07-20",
       "user_folder": "Ada_L"}


def test_rekey_hook_moves_mark_for_findings_observation(monkeypatch):
    monkeypatch.setattr(org, "_compliance_key_context", lambda conn, table, rid: dict(CTX))
    seen = {}
    monkeypatch.setattr(org.compliance_resolutions, "rekey_resolution",
                        lambda conn, old_key, new_hash, new_sample:
                        seen.update(old_key=old_key, new_hash=new_hash, new_sample=new_sample))
    org._rekey_compliance_mark(FakeConn(), "findings", "observation", "f-1",
                               "Old hazard text", "New hazard text")
    assert seen["old_key"]["domain"] == "safety"
    assert seen["old_key"]["content_hash"] == ch.content_hash("Old hazard text")
    assert seen["old_key"]["user_folder"] == "Ada_L"
    assert seen["new_hash"] == ch.content_hash("New hazard text")
    assert seen["new_sample"] == ch.normalize("New hazard text")


def test_rekey_hook_maps_topics_title_to_quality(monkeypatch):
    monkeypatch.setattr(org, "_compliance_key_context", lambda conn, table, rid: dict(CTX))
    seen = {}
    monkeypatch.setattr(org.compliance_resolutions, "rekey_resolution",
                        lambda conn, old_key, *a: seen.update(domain=old_key["domain"]))
    org._rekey_compliance_mark(FakeConn(), "topics", "title", "t-1", "Old", "New")
    assert seen["domain"] == "quality"


def test_rekey_hook_noop_for_non_key_field(monkeypatch):
    called = []
    monkeypatch.setattr(org.compliance_resolutions, "rekey_resolution",
                        lambda *a, **k: called.append(1))
    # recommended_action is editable but not part of any compliance key.
    org._rekey_compliance_mark(FakeConn(), "findings", "recommended_action",
                               "f-1", "before", "after")
    assert called == []


def test_rekey_hook_noop_when_text_unchanged(monkeypatch):
    called = []
    monkeypatch.setattr(org.compliance_resolutions, "rekey_resolution",
                        lambda *a, **k: called.append(1))
    # whitespace/case-only edit -> same normalized hash -> nothing to move.
    org._rekey_compliance_mark(FakeConn(), "findings", "observation", "f-1",
                               "Loose rail", "  loose   RAIL ")
    assert called == []


def test_rekey_hook_noop_when_unattributed(monkeypatch):
    monkeypatch.setattr(org, "_compliance_key_context",
                        lambda conn, table, rid: {**CTX, "user_folder": None})
    called = []
    monkeypatch.setattr(org.compliance_resolutions, "rekey_resolution",
                        lambda *a, **k: called.append(1))
    org._rekey_compliance_mark(FakeConn(), "findings", "observation", "f-1", "a", "b")
    assert called == []


def test_rekey_hook_never_raises_on_failure(monkeypatch):
    # best-effort: a re-key failure must be swallowed (the caller's edit is kept).
    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(org, "_compliance_key_context", boom)
    # must NOT raise
    org._rekey_compliance_mark(FakeConn(), "findings", "observation", "f-1", "a", "b")


# ---- the hook is actually wired into patch_content, and is best-effort ----
def _wire_patch_content(wired):
    wired.setattr(org.content, "get_content_row",
                  lambda conn, table, rid: {"id": rid, "company_id": "c-uuid-1",
                                            "site_id": SITE_ID, "author_user_id": None,
                                            "observation": "Old hazard text"})
    wired.setattr(org.content, "update_content_field",
                  lambda conn, table, rid, field, value: {"id": rid, field: value})
    wired.setattr(org.content_edits, "append_content_edit", lambda conn, *a: {"id": "e-1"})
    wired.setattr(org, "_enqueue_content_reindex", lambda conn, table, rid: None)
    wired.setattr(org, "_compliance_key_context", lambda conn, table, rid: dict(CTX))


def test_patch_content_findings_observation_invokes_rekey(wired):
    _wire_patch_content(wired)
    seen = {}
    wired.setattr(org.compliance_resolutions, "rekey_resolution",
                  lambda conn, old_key, new_hash, new_sample:
                  seen.update(old_key=old_key, new_hash=new_hash))
    res = org.lambda_handler(make_event("PATCH", "/api/org/content/findings/f-1",
                                        body={"observation": "New hazard text"}), None)
    assert res["statusCode"] == 200
    assert seen["old_key"]["content_hash"] == ch.content_hash("Old hazard text")
    assert seen["new_hash"] == ch.content_hash("New hazard text")


def test_patch_content_rekey_failure_never_fails_the_edit(wired):
    _wire_patch_content(wired)

    def boom(*a, **k):
        raise RuntimeError("rekey exploded")

    wired.setattr(org.compliance_resolutions, "rekey_resolution", boom)
    res = org.lambda_handler(make_event("PATCH", "/api/org/content/findings/f-1",
                                        body={"observation": "New hazard text"}), None)
    assert res["statusCode"] == 200                        # edit kept despite re-key failure
