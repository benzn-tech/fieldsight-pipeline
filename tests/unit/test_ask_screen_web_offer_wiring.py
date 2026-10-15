"""Ask screen web offer: `offer_web` / `web_intent` reach `_rag_answer`
(spec 2026-10-16 ask-screen-web-offer §2).

Two hops, each of which used to drop the fields: the API whitelist
(`_build_ask_payload`, shared by /api/ask and /api/ask/start) and the worker's
`_SCREEN_DROP`. The seam test drives the real `start_screen` -> stored state ->
`_work_screen` path rather than a hand-built state dict.
"""
import io
import json
import os
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")
laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")

from botocore.exceptions import ClientError  # noqa: E402

import ask_voice_turn as avt  # noqa: E402

CALLER = {"sub": "sub-1", "email": "", "name": "", "role": "worker", "display_name": ""}
WORKER_SUB = "sub-from-worker-event"
BUCKET = "test-bucket"
WEB_FIELDS = ("offer_web", "web_intent")


# ---- payload whitelist ----

def test_payload_forwards_both_fields_when_json_true():
    payload, err = fapi._build_ask_payload(
        {"question": "q", "offer_web": True, "web_intent": True}, CALLER)
    assert err is None
    assert payload["offer_web"] is True
    assert payload["web_intent"] is True


@pytest.mark.parametrize("value", ["true", 1, False])
def test_payload_drops_anything_but_json_true(value):
    payload, err = fapi._build_ask_payload(
        {"question": "q", "offer_web": value, "web_intent": value}, CALLER)
    assert err is None
    for k in WEB_FIELDS:
        assert k not in payload


def test_payload_absent_stays_absent():
    payload, err = fapi._build_ask_payload({"question": "q"}, CALLER)
    assert err is None
    for k in WEB_FIELDS:
        assert k not in payload


class FakeAgent:
    """Stands in for both boto3 lambda clients the API module holds."""

    def __init__(self, reply):
        self.reply, self.payloads = reply, []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.payloads.append(json.loads(Payload))
        return {"Payload": io.BytesIO(json.dumps(self.reply).encode())}


def _ev(path, b):
    return {"httpMethod": "POST", "path": path, "queryStringParameters": None,
            "body": json.dumps(b),
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


@pytest.mark.parametrize("path,client_attr,reply", [
    ("/api/ask", "ask_lambda_client", {"answer": "a"}),
    ("/api/ask/start", "ask_start_client", {"turn_id": "f" * 32}),
])
def test_handler_forwards_fields_to_agent(monkeypatch, path, client_attr, reply):
    agent = FakeAgent(reply)
    monkeypatch.setattr(fapi, client_attr, agent)
    r = fapi.lambda_handler(
        _ev(path, {"question": "q", "offer_web": True, "web_intent": True}), None)
    assert r["statusCode"] == 200
    sent = agent.payloads[0]
    assert sent["offer_web"] is True and sent["web_intent"] is True
    assert sent["caller_sub"] == "sub-1"


@pytest.mark.parametrize("path,client_attr,reply", [
    ("/api/ask", "ask_lambda_client", {"answer": "a"}),
    ("/api/ask/start", "ask_start_client", {"turn_id": "f" * 32}),
])
def test_handler_drops_non_true_fields(monkeypatch, path, client_attr, reply):
    agent = FakeAgent(reply)
    monkeypatch.setattr(fapi, client_attr, agent)
    fapi.lambda_handler(
        _ev(path, {"question": "q", "offer_web": "true", "web_intent": 1}), None)
    sent = agent.payloads[0]
    for k in WEB_FIELDS:
        assert k not in sent


# ---- seam: start_screen -> stored state -> _work_screen -> _rag_answer ----

class FakeS3:
    """Same shape as the fake in test_ask_screen_turn.py (copied, not imported:
    importing a test module couples collection order)."""

    def __init__(self):
        self.objs = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objs[Key] = (Body, datetime.now(timezone.utc), ContentType)

    def get_object(self, Bucket, Key):
        if Key not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[Key][0])}

    def delete_object(self, Bucket, Key):
        self.objs.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k, "LastModified": v[1]}
                             for k, v in sorted(self.objs.items()) if k.startswith(Prefix)],
                "IsTruncated": False}


class FakeLambda:
    def __init__(self):
        self.payloads = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.payloads.append(json.loads(Payload))
        return {"StatusCode": 202}


def test_seam_start_screen_to_rag_answer_carries_web_fields(monkeypatch):
    monkeypatch.setenv("ASK_TURN_BUCKET", BUCKET)
    monkeypatch.setenv("ASK_VOICE_WORKER_FUNCTION", "worker-fn")
    monkeypatch.delenv("ASK_AGENTIC", raising=False)
    s3, lam = FakeS3(), FakeLambda()
    monkeypatch.setattr(avt, "_get_s3", lambda: s3)
    monkeypatch.setattr(avt, "_get_lambda", lambda: lam)
    captured = []
    monkeypatch.setattr(laa, "_rag_answer",
                        lambda body, on_web_start=None: captured.append(body) or {"answer": "a"})

    # The body /api/ask/start would send, plus a voice-only key a client might forge.
    payload, err = fapi._build_ask_payload(
        {"question": "q", "offer_web": True, "web_intent": True}, CALLER)
    assert err is None
    body = dict(payload, mode="turn_start", voice_web=True)
    turn_id = avt.start_screen(body)["turn_id"]

    event = lam.payloads[0]
    state = avt.get_state(s3, BUCKET, event["caller_sub"], turn_id)
    assert state["status"] == "pending"
    # The worker's identity is its own argument, never the stored request's.
    avt._work_screen(s3, BUCKET, WORKER_SUB, turn_id, state, laa, time.perf_counter())

    assert len(captured) == 1
    rag_body = captured[0]
    assert rag_body["offer_web"] is True
    assert rag_body["web_intent"] is True
    assert rag_body["caller_sub"] == WORKER_SUB
    assert "mode" not in rag_body
    assert "voice_web" not in rag_body
