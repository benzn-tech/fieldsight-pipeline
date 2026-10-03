"""PATCH /api/org/companies/{id} -- rename a tenant. platform_admin only.

Safe because nothing finds a company by its name any more (spec 2026-10-03,
section 1); the guard excludes the company itself, so changing only the case of
its own name is allowed.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "2104bcd3-bbdf-421f-bfb2-b451fb8eba54"
OTHER = {"id": "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92", "name": "Southbase"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def rollback(self):
        pass


def call(monkeypatch, role, body, *, cid=CO, exists=True, clash=None):
    writes = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": role, "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, i: {"id": i, "name": "Frequency"} if exists else None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, n: dict(clash) if clash else None)
    monkeypatch.setattr(org.companies, "update_company",
                        lambda conn, i, **f: writes.append((i, f)) or {
                            "id": i, "name": f.get("name", "Frequency"),
                            "industry": f.get("industry"), "created_at": "x"})
    res = org.lambda_handler({
        "httpMethod": "PATCH", "path": f"/api/org/companies/{cid}",
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None, "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    return res["statusCode"], json.loads(res["body"]), writes


def test_platform_admin_renames(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "  Frequency NZ "})
    assert code == 200, body
    assert body == {"company": {"id": CO, "name": "Frequency NZ"}}
    assert writes == [(CO, {"name": "Frequency NZ"})]


@pytest.mark.parametrize("role", ["admin", "gm", "pm", "site_manager", "worker",
                                  "regional_manager", "member", "", "nonsense"])
def test_only_platform_admin_may_rename(monkeypatch, role):
    code, _, writes = call(monkeypatch, role, {"name": "X"})
    assert code == 403 and writes == []


def test_a_name_another_tenant_holds_is_409_with_that_tenant(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "southbase"}, clash=OTHER)
    assert code == 409 and writes == []
    assert body["existing"] == OTHER


def test_changing_only_the_case_of_its_own_name_is_allowed(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "FREQUENCY"},
                              clash={"id": CO, "name": "Frequency"})
    assert code == 200, body
    assert writes == [(CO, {"name": "FREQUENCY"})]


def test_an_unknown_company_is_404(monkeypatch):
    code, _, writes = call(monkeypatch, "platform_admin", {"name": "X"}, exists=False)
    assert code == 404 and writes == []


@pytest.mark.parametrize("cid", ["not-a-uuid", "123"])
def test_a_malformed_id_is_400(monkeypatch, cid):
    code, _, writes = call(monkeypatch, "platform_admin", {"name": "X"}, cid=cid)
    assert code == 400 and writes == []


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": 7},
                                  {"industry": 7}, {"other": "x"}])
def test_nothing_valid_to_change_is_400(monkeypatch, body):
    code, _, writes = call(monkeypatch, "platform_admin", body)
    assert code == 400 and writes == []


def test_a_blank_industry_clears_it(monkeypatch):
    code, _, writes = call(monkeypatch, "platform_admin", {"industry": "   "})
    assert code == 200
    assert writes == [(CO, {"industry": None})]


def test_the_response_is_only_id_and_name(monkeypatch):
    code, body, _ = call(monkeypatch, "platform_admin", {"name": "N", "industry": "construction"})
    assert set(body["company"]) == {"id", "name"}
