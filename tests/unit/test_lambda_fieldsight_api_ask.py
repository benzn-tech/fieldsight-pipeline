"""
Tests for src/lambda_fieldsight_api.py ask_question — Phase 5, Task 4.

Style mirrors tests/unit/test_lambda_ask_agent_rag.py (dummy AWS env vars so
an eager boto3.client('s3')/boto3.client('lambda')/boto3.resource('dynamodb')
at import time never blows up on a missing credential provider; a FakeLambda
double records the invoke() call instead of hitting a real Lambda).

Covers Task 4: ask_question forwards caller_sub (the Cognito sub bridge to
rag-search's get_user_by_sub) on every invoke, and no longer hard-requires
`date` (RAG retrieval is global across the caller's accessible sites) while
still requiring `question` and preserving worker self-scoping.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")


ADMIN_CALLER = {
    "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
    "role": "admin", "display_name": "Ada_Admin", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "c-1",
}

WORKER_CALLER = {
    "sub": "sub-worker-1", "email": "w@x.nz", "name": "Ben Test",
    "role": "worker", "display_name": "Ben_Test", "device_id": "Benl1",
    "sites": ["s-1"], "managed_sites": [], "company_id": "c-1",
}

# Org-provisioned account: absent from the legacy DynamoDB user mapping
# entirely -- no display_name to resolve, no legacy sites/role mapping.
ORG_CALLER = {
    "sub": "sub-ucpk", "email": "", "name": "",
    "role": "viewer", "display_name": "", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "",
}


class FakeLambdaClient:
    """Stand-in for boto3.client('lambda') — records the invoke() call and
    returns a botocore-shaped {"Payload": <stream>} response."""

    def __init__(self, response_payload=None, function_error=None):
        self.response_payload = response_payload if response_payload is not None else {
            "answer": "stub", "citations": [], "model": "stub"
        }
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload),
        })
        resp = {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, **kwargs):
    fake_client = FakeLambdaClient(**kwargs)
    monkeypatch.setattr(fapi, "lambda_client", fake_client)
    return fake_client


def body_of(res):
    return json.loads(res["body"])


def test_payload_includes_caller_sub_admin(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What happened?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-admin-1"


def test_payload_includes_caller_sub_worker(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What happened at my site?"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_ask_without_date_no_longer_400(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "Door inspection on Feb 9?"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_client.calls) == 1
    # date omitted entirely from payload when caller doesn't supply one (soft context only)
    assert "date" not in fake_client.calls[0]["Payload"]


def test_date_still_forwarded_when_supplied(monkeypatch):
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "Q?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert fake_client.calls[0]["Payload"]["date"] == "2026-02-09"


def test_missing_question_still_400(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 400
    assert "question" in body_of(res)["error"].lower()
    assert fake_client.calls == []  # never invoked ask-agent


def test_worker_self_scoping_now_via_caller_sub(monkeypatch):
    """UPDATED (BUG-39 WS2): worker self-scoping used to be forced server-side
    here via resolve_user_display_name(); that legacy gate is removed and the
    scoping now happens downstream in rag-search via caller_sub. This proxy
    forwards 'user' as empty soft context (none supplied) plus the caller_sub
    that actually gates access."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What did I do today?"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["user"] == ""
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_worker_impersonation_prevention_now_via_caller_sub(monkeypatch):
    """UPDATED (BUG-39 WS2): this proxy no longer rewrites a client-supplied
    'user' field back to the worker's own display_name -- that was the
    legacy pre-gate. Impersonation prevention now lives downstream in
    rag-search, which scopes retrieval by caller_sub (immutable, from the
    Cognito token), not by the client-suppliable 'user' field. This proxy
    forwards both unchanged; caller_sub is what actually gates access."""
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "Q?", "user": "Someone_Else"}, WORKER_CALLER)

    assert fake_client.calls[0]["Payload"]["user"] == "Someone_Else"
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_ask_org_caller_no_dynamo_profile_not_403(monkeypatch):
    """Org-provisioned caller absent from the legacy DynamoDB user mapping
    (role='viewer', no sites, no display_name) must not be blocked by the
    legacy pre-gate -- the RAG ACL is enforced downstream by caller_sub ->
    rag-search (BUG-39 WS2)."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "what happened on site?"}, ORG_CALLER)

    assert res["statusCode"] != 403
    assert res["statusCode"] != 400
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-ucpk"


def test_ask_global_no_user_not_400(monkeypatch):
    """A global Ask (no 'user' in body, from an org account with no legacy
    display_name to resolve) must not 400 -- 'user' is optional soft context
    only; downstream ACL scopes by caller_sub."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "site-wide question"}, ORG_CALLER)

    assert res["statusCode"] != 400
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-ucpk"


def test_function_error_returns_500_without_stack_trace_leak(monkeypatch):
    """I1: if the Ask Agent lambda itself raised an unhandled exception,
    boto3 reports it via resp['FunctionError'] with a Payload containing
    {errorMessage, errorType, stackTrace}. ApiFunction must never pass that
    payload straight through to the client -- it leaks internals."""
    wire(monkeypatch,
         response_payload={
             "errorMessage": "RuntimeError: dashscope upstream 503",
             "errorType": "RuntimeError",
             "stackTrace": ["  File \"lambda_ask_agent.py\", line 525, in _rag_answer"],
         },
         function_error="Unhandled")

    res = fapi.ask_question({"question": "What happened?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 500
    body_text = res["body"]
    assert "stackTrace" not in body_text
    assert "lambda_ask_agent.py" not in body_text
