"""Tests for lambda_fieldsight_api.search_topics — /api/search forwarder.
Mirrors test_lambda_fieldsight_api_ask.py (FakeLambdaClient double, dummy env)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3")

ADMIN_CALLER = {"sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
                "role": "admin", "display_name": "Ada_Admin", "device_id": "",
                "sites": [], "managed_sites": [], "company_id": "c-1"}


class FakeLambdaClient:
    def __init__(self, response_payload=None, function_error=None):
        self.response_payload = response_payload if response_payload is not None else {
            "results": [], "count": 0}
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "Payload": json.loads(Payload)})
        resp = {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, **kw):
    fc = FakeLambdaClient(**kw)
    monkeypatch.setattr(fapi, "lambda_client", fc)
    return fc


def body_of(res):
    return json.loads(res["body"])


def test_forwards_mode_search_and_sub(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": "door damage"}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    p = fc.calls[0]["Payload"]
    assert p["mode"] == "search"
    assert p["caller_sub"] == "sub-admin-1"
    assert p["question"] == "door damage"


def test_short_query_returns_empty_without_invoke(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": "d"}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res) == {"results": [], "count": 0}
    assert fc.calls == []


def test_forwards_date_range(monkeypatch):
    fc = wire(monkeypatch)
    fapi.search_topics({"question": "door", "date_from": "2026-02-01", "date_to": "2026-03-31"},
                       ADMIN_CALLER)
    p = fc.calls[0]["Payload"]
    assert p["date_from"] == "2026-02-01"
    assert p["date_to"] == "2026-03-31"


def test_function_error_returns_500_no_leak(monkeypatch):
    wire(monkeypatch,
         response_payload={"errorMessage": "boom", "errorType": "RuntimeError",
                           "stackTrace": ["  File \"lambda_ask_agent.py\", line 1"]},
         function_error="Unhandled")
    res = fapi.search_topics({"question": "door damage"}, ADMIN_CALLER)
    assert res["statusCode"] == 500
    assert "stackTrace" not in res["body"]
    assert "lambda_ask_agent.py" not in res["body"]


def test_invalid_date_from_returns_400_without_invoke(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": "door", "date_from": "last week"}, ADMIN_CALLER)
    assert res["statusCode"] == 400
    assert fc.calls == []


def test_null_question_returns_empty_not_500(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": None}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res) == {"results": [], "count": 0}
    assert fc.calls == []


def test_router_dispatches_search(monkeypatch):
    wire(monkeypatch)
    event = {"httpMethod": "POST", "path": "/api/search",
             "body": json.dumps({"question": "door damage"}),
             "requestContext": {"authorizer": {"claims": {
                 "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin"}}}}
    # user_mapping lookup during caller-build may miss; admin role isn't required
    # for search (ACL is downstream in rag-search) — a viewer caller still routes.
    res = fapi.lambda_handler(event, None)
    assert res["statusCode"] in (200, 500)  # routed (not 404); 200 with stub payload
    assert res["statusCode"] == 200


def test_search_forwards_site(monkeypatch):
    fc = wire(monkeypatch)
    fapi.search_topics({"question": "door damage", "site": "s-xyz"}, ADMIN_CALLER)
    assert fc.calls[0]["Payload"]["site"] == "s-xyz"
