"""POST /api/org/companies -- who may mint a tenant, and the duplicate name guard.

Creating a tenant is a platform operation, not something a customer does inside
their own company, so the gate is is_cross_company (platform_admin alone) --
every other role, including a company `admin`, is refused.

The duplicate guard carries most of the weight here. `companies.name` has NO
unique constraint (0002_core_relational.sql), so nothing in the database stops
two tenants being called the same thing -- and two tenants with one name is not
cosmetic: every list shows them identically, their sites and users scatter
between two uuids, and nothing can safely merge them afterwards. A double-clicked
button is enough to cause it. So the endpoint is the only guard there is, and it
has to be case- and whitespace-insensitive to be one.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

EXISTING = {"id": "c-old", "name": "Frequency", "industry": None,
            "created_at": "2026-01-01"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def call(monkeypatch, role, body, existing=None):
    """POST the body as `role`. `existing` is what the case-insensitive lookup
    finds, i.e. the tenant that already holds the name. Records every create."""
    created = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": role, "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, name: (dict(existing) if existing else None))
    monkeypatch.setattr(
        org.companies, "create_company",
        lambda conn, name, industry=None: (created.append((name, industry))
                                           or {"id": "c-new", "name": name,
                                               "industry": industry,
                                               "created_at": "2026-10-02"}))
    resp = org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None, "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    return resp["statusCode"], json.loads(resp["body"]), created


def test_platform_admin_creates_a_company(monkeypatch):
    code, body, created = call(monkeypatch, "platform_admin", {"name": "Frequency"})
    assert code == 201, body
    assert body["company"] == {"id": "c-new", "name": "Frequency"}
    assert created == [("Frequency", None)]


def test_the_name_is_stored_trimmed(monkeypatch):
    # Otherwise the tenant is called " Frequency " forever, and the NEXT create
    # of "Frequency" would be a near-miss the eye cannot catch in a list.
    code, body, created = call(monkeypatch, "platform_admin", {"name": "  Frequency  "})
    assert code == 201, body
    assert created == [("Frequency", None)]
    assert body["company"]["name"] == "Frequency"


@pytest.mark.parametrize("role", ["admin", "gm", "pm", "site_manager", "worker",
                                  "regional_manager", "member", "", "nonsense"])
def test_only_platform_admin_may_mint_a_tenant(monkeypatch, role):
    # A company `admin` passes resolve_scope()==ALL. If this gate ever used that
    # instead, every customer admin could create tenants. Each role asserted,
    # not one of them, because "refused" has to be the default.
    code, body, created = call(monkeypatch, role, {"name": "Frequency"})
    assert code == 403, body
    assert created == [], "no tenant may be written on a refused call"


@pytest.mark.parametrize("name", ["Frequency", "frequency", "FREQUENCY",
                                  "  Frequency", "Frequency  ", " frequency "])
def test_a_name_already_taken_is_refused_whatever_its_case_or_spacing(monkeypatch, name):
    code, body, created = call(monkeypatch, "platform_admin", {"name": name},
                               existing=EXISTING)
    assert code == 409, body
    assert created == [], "a second tenant with the same name must not be written"


def test_the_refusal_names_the_company_that_already_exists(monkeypatch):
    # A 409 that says only "taken" leaves the caller -- often a retry after a
    # timeout -- with no way to reach the company they wanted.
    code, body, _ = call(monkeypatch, "platform_admin", {"name": "frequency"},
                         existing=EXISTING)
    assert code == 409
    assert "Frequency" in body["error"], body["error"]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "},
                                  {"name": None}, {"name": 7}, {"name": ["x"]}])
def test_a_missing_or_empty_or_non_string_name_is_400(monkeypatch, body):
    code, resp, created = call(monkeypatch, "platform_admin", body)
    assert code == 400, resp
    assert created == []


def test_industry_is_optional_and_normalised(monkeypatch):
    code, _, created = call(monkeypatch, "platform_admin",
                            {"name": "Frequency", "industry": "  construction  "})
    assert code == 201
    assert created == [("Frequency", "construction")]

    code, _, created = call(monkeypatch, "platform_admin",
                            {"name": "Frequency", "industry": "   "})
    assert code == 201
    assert created == [("Frequency", None)], "blank industry is absent, not empty text"


def test_a_non_string_industry_is_refused_rather_than_written(monkeypatch):
    code, resp, created = call(monkeypatch, "platform_admin",
                               {"name": "Frequency", "industry": {"a": 1}})
    assert code == 400, resp
    assert created == []


def test_the_duplicate_check_runs_before_the_insert(monkeypatch):
    """Order matters, not just presence. If create ran first and the check only
    decorated the response, the second tenant would already exist."""
    calls = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, name: calls.append("check") or None)
    monkeypatch.setattr(org.companies, "create_company",
                        lambda conn, name, industry=None: calls.append("create") or {
                            "id": "c-new", "name": name, "industry": industry,
                            "created_at": "2026-10-02"})
    org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps({"name": "Frequency"}), "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    assert calls == ["check", "create"]


def test_the_response_carries_only_id_and_name(monkeypatch):
    """create_company also returns industry and created_at, and the row may grow.
    The projection is explicit so a widened repo query cannot leak a new column."""
    code, body, _ = call(monkeypatch, "platform_admin",
                         {"name": "Frequency", "industry": "construction"})
    assert code == 201
    assert set(body["company"]) == {"id", "name"}, body["company"]


def test_a_malformed_json_body_is_400_not_a_crash(monkeypatch):
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    resp = org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": "{not json", "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    assert resp["statusCode"] == 400, resp["body"]


def test_get_still_works_on_the_same_route(monkeypatch):
    """The dispatch line grew a method branch; the GET it used to be must not
    have been displaced by it."""
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "list_companies",
                        lambda conn: [dict(EXISTING)])
    resp = org.lambda_handler({
        "httpMethod": "GET", "path": "/api/org/companies", "queryStringParameters": None,
        "body": None, "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["companies"] == [{"id": "c-old", "name": "Frequency"}]


def test_the_refusal_carries_the_existing_company_as_data(monkeypatch):
    code, body, _ = call(monkeypatch, "platform_admin", {"name": "frequency"},
                         existing=EXISTING)
    assert code == 409
    assert body["existing"] == {"id": "c-old", "name": "Frequency"}


@pytest.mark.parametrize("name", ["Frequency	", "Frequency ", " Frequency"])
def test_tabs_and_nonbreaking_spaces_are_stripped_before_the_check(monkeypatch, name):
    """REGRESSION PIN, not a proof: the handler already strips with str.strip(),
    so this passes against the code before this task. It pins the order that
    matters -- Postgres btrim() strips spaces only, so if the strip ever moved
    after the lookup, "Frequency\t" would miss "Frequency" and become a tenant."""
    seen = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, n: seen.append(n) or dict(EXISTING))
    org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps({"name": name}), "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    assert seen == ["Frequency"]


def test_a_race_that_reaches_the_index_is_a_409_not_a_500(monkeypatch):
    """The endpoint's check passed, then a concurrent create took the name."""
    from psycopg.errors import UniqueViolation
    lookups = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}

    class RollbackConn(FakeConn):
        rolled_back = False

        def rollback(self):
            RollbackConn.rolled_back = True

    monkeypatch.setattr(org, "get_connection", lambda *a, **k: RollbackConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)

    def lookup(conn, n):
        lookups.append(n)
        return None if len(lookups) == 1 else dict(EXISTING)

    def insert(conn, n, industry=None):
        raise UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(org.companies, "find_company_by_name_ci", lookup)
    monkeypatch.setattr(org.companies, "create_company", insert)
    res = org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps({"name": "Frequency"}), "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    assert res["statusCode"] == 409, res["body"]
    assert json.loads(res["body"])["existing"]["id"] == "c-old"
    assert RollbackConn.rolled_back, "the aborted transaction must be rolled back before the re-read"
