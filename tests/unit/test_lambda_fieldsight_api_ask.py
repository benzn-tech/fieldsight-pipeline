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


class FakeLambdaClient:
    """Stand-in for boto3.client('lambda') — records the invoke() call and
    returns a botocore-shaped {"Payload": <stream>} response."""

    def __init__(self, response_payload=None):
        self.response_payload = response_payload if response_payload is not None else {
            "answer": "stub", "citations": [], "model": "stub"
        }
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload),
        })
        return {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}


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


def test_worker_self_scoping_preserved(monkeypatch):
    """Worker asking about themself (no explicit user) still resolves to
    their own display_name — self-scoping is untouched by the date change."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What did I do today?"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["user"] == "Ben_Test"


def test_worker_cannot_impersonate_other_user_via_ask(monkeypatch):
    """Even if a worker supplies a different user in the body, self-scoping
    forces it back to their own display_name (unchanged legacy behavior)."""
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "Q?", "user": "Someone_Else"}, WORKER_CALLER)

    assert fake_client.calls[0]["Payload"]["user"] == "Ben_Test"
