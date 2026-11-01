"""Integration: the company gate in org-api dispatch against real Postgres
(spec 2026-11-01-company-offboarding section 5.1, Task 3).

A real suspended company, the real dispatch, the real get_user_by_sub and the real
tenancy_status read of companies.status inside the request's transaction. Nothing is
committed (the `db` fixture rolls back), so no company_status_events cleanup is needed.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import uuid

import psycopg
import pytest

from repositories import companies, sites, users

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

FLAG = "COMPANY_STATUS_ENFORCE"


def _seed(db, role="admin"):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-gate-{tag}")
    site = sites.create_site(db, co["id"], f"Site-gate-{tag}")
    u = users.upsert_user(db, f"sub-gate-{tag}", f"gate-{tag}@example.com",
                          company_id=co["id"], global_role=role)
    return co, site, u


def _suspend(db, co):
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (co["id"],))


def _call(db, u, method, route):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": None,
          "body": None, "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "on")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def _usable(db):
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert db.execute("SELECT 1").fetchone()[0] == 1


def test_flag_on_suspended_company_refuses_a_normal_route(db, flag_on, monkeypatch):
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    co, site, u = _seed(db)
    status, body = _call(db, u, "GET", "/sites")
    assert status == 200 and str(site["id"]) in {str(s["id"]) for s in body["sites"]}
    _suspend(db, co)
    assert _call(db, u, "GET", "/sites") == (403, {"error": "company_inactive"})
    _usable(db)


def test_flag_on_suspended_company_still_gets_me_with_blocked(db, flag_on):
    co, _, u = _seed(db)
    status, before = _call(db, u, "GET", "/me")
    assert status == 200 and "blocked" not in before
    _suspend(db, co)
    status, body = _call(db, u, "GET", "/me")
    assert status == 200
    assert body["blocked"] == {"reason": "company_inactive"}
    assert body["cognito_sub"] == u["cognito_sub"]
    _usable(db)


def test_flag_on_platform_admin_in_a_suspended_company_is_unaffected(db, flag_on, monkeypatch):
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    co, _, u = _seed(db, role="platform_admin")
    _, sites_before = _call(db, u, "GET", "/sites")
    _suspend(db, co)
    status, body = _call(db, u, "GET", "/sites")
    assert status == 200 and body == sites_before
    status, me = _call(db, u, "GET", "/me")
    assert status == 200 and "blocked" not in me


def test_flag_off_suspended_company_behaves_as_today(db, flag_off, monkeypatch, caplog):
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    co, _, u = _seed(db)
    before_sites = _call(db, u, "GET", "/sites")
    before_me = _call(db, u, "GET", "/me")
    _suspend(db, co)
    assert _call(db, u, "GET", "/sites") == before_sites
    after_me = _call(db, u, "GET", "/me")
    # last_seen_at is stamped by the first /me and throttled after, so the bodies match.
    assert after_me == before_me and "blocked" not in after_me[1]
    assert any("COMPANY_GATE_WOULD_BLOCK" in r.getMessage() and "where=org-api" in r.getMessage()
               for r in caplog.records)


def test_flag_on_exited_company_refuses_too(db, flag_on):
    co, _, u = _seed(db)
    db.execute("UPDATE companies SET status='exited' WHERE id=%s", (co["id"],))
    assert _call(db, u, "GET", "/sites") == (403, {"error": "company_inactive"})
