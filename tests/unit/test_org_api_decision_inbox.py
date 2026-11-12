"""org-api: the decision-card inbox routes, flag and routing checks (spec 2026-11-12 rev 2 §2).

  GET  /api/org/decision-cards
  POST /api/org/decision-cards/{card_id}/answer

Pinned here with a connection that refuses any SQL: either flag off -> 404 like an unknown
route (read per call), before anything touches the database; a wrong method or path -> 404.
tests/integration/test_decision_inbox_get.py / _answer.py run the real queries.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "c0000000-0000-0000-0000-000000000001"
CARD = "33333333-3333-3333-3333-333333333333"


class NoSql:
    def __getattr__(self, name):
        raise AssertionError(f"database touched: conn.{name}")


def _caller():
    return {"id": "11111111-1111-1111-1111-111111111111", "company_id": CO,
            "global_role": "worker", "archived_at": None}


def _route(method, route, body=None):
    ev = {"httpMethod": method, "body": None if body is None else json.dumps(body)}
    return org.decision_cards_route(NoSql(), _caller(), ev, method, route)


@pytest.mark.parametrize("inbox,loop", [(None, "shadow"), ("off", "shadow"),
                                        ("internal", "shadow"), ("on", "shadow"),
                                        ("companies", None), ("companies", "off"),
                                        ("companies", "on")])
@pytest.mark.parametrize("method,route", [("GET", "/decision-cards"),
                                          ("POST", f"/decision-cards/{CARD}/answer")])
def test_flags_off_404_without_touching_the_db(monkeypatch, inbox, loop, method, route):
    for var, value in (("DECISION_INBOX", inbox), ("SITE_DECISION_LOOP", loop)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", CO)
    res = _route(method, route, {"option_id": "done"})
    assert res["statusCode"] == 404
    assert json.loads(res["body"]) == {"error": "not found"}


def test_flag_values_are_case_and_space_tolerant(monkeypatch):
    monkeypatch.setenv("DECISION_INBOX", " Companies ")
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    assert org._decision_inbox_on() is True


@pytest.mark.parametrize("method,route", [
    ("POST", "/decision-cards"), ("DELETE", "/decision-cards"),
    ("GET", f"/decision-cards/{CARD}/answer"), ("GET", f"/decision-cards/{CARD}"),
    ("POST", f"/decision-cards/{CARD}/answer/x"), ("POST", "/decision-cards//answer")])
def test_wrong_method_or_path_404_without_db(monkeypatch, method, route):
    monkeypatch.setenv("DECISION_INBOX", "companies")
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", CO)
    assert _route(method, route)["statusCode"] == 404


def test_allow_list_parsing(monkeypatch):
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", f" {CO.upper()},, {CARD} ")
    assert org._decision_inbox_companies() == {CO, CARD}
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", "")
    assert org._decision_inbox_companies() == set()
    monkeypatch.delenv("DECISION_INBOX_COMPANIES")
    assert org._decision_inbox_companies() == set()


def test_dispatch_routes_decision_cards_to_the_inbox():
    import inspect
    src = inspect.getsource(org.dispatch)
    assert 'route == "/decision-cards" or route.startswith("/decision-cards/")' in src
    assert "decision_cards_route(conn, caller, event, method, route)" in src
