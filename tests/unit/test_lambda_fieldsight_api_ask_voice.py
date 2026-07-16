"""
Tests for src/lambda_fieldsight_api.py ask_voice — SP-Ask Task 1.

Style mirrors tests/unit/test_lambda_fieldsight_api_ask.py exactly (dummy AWS
env vars so eager boto3 clients import cleanly; FakeLambdaClient records the
invoke instead of hitting a real Lambda).
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")


WORKER_CALLER = {
    "sub": "sub-worker-1", "email": "w@x.nz", "name": "Ben Test",
    "role": "worker", "display_name": "Ben_Test", "device_id": "Benl1",
    "sites": ["s-1"], "managed_sites": [], "company_id": "c-1",
}

VOICE_OK_PAYLOAD = {
    "transcript": "what happened at ellesmere today",
    "answerText": "The crane arrived and the slab pour finished.",
    "audioBase64": "UklGRg==",
    "audioFormat": "wav",
}


class FakeLambdaClient:
    def __init__(self, response_payload=None, function_error=None):
        self.response_payload = response_payload if response_payload is not None else VOICE_OK_PAYLOAD
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


def test_forwards_mode_voice_audio_and_caller_sub(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_voice({"audio": "QUJD", "format": "m4a", "mode": "voice"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["FunctionName"] == fapi.ASK_AGENT_FUNCTION
    assert call["InvocationType"] == "RequestResponse"
    assert call["Payload"] == {
        "mode": "voice", "audio": "QUJD", "format": "m4a", "caller_sub": "sub-worker-1",
    }


def test_response_passthrough(monkeypatch):
    wire(monkeypatch)

    res = fapi.ask_voice({"audio": "QUJD"}, WORKER_CALLER)

    body = body_of(res)
    assert body["audioBase64"] == "UklGRg=="
    assert body["answerText"] == VOICE_OK_PAYLOAD["answerText"]
    assert body["transcript"] == VOICE_OK_PAYLOAD["transcript"]


def test_format_defaults_to_m4a(monkeypatch):
    fake_client = wire(monkeypatch)

    fapi.ask_voice({"audio": "QUJD"}, WORKER_CALLER)

    assert fake_client.calls[0]["Payload"]["format"] == "m4a"


def test_caller_sub_is_never_client_supplied(monkeypatch):
    fake_client = wire(monkeypatch)

    fapi.ask_voice({"audio": "QUJD", "caller_sub": "sub-EVIL"}, WORKER_CALLER)

    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_missing_audio_400_never_invokes(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_voice({"format": "m4a"}, WORKER_CALLER)

    assert res["statusCode"] == 400
    assert "audio" in body_of(res)["error"].lower()
    assert fake_client.calls == []


def test_oversized_audio_413(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_voice({"audio": "A" * (fapi.MAX_VOICE_AUDIO_B64 + 1)}, WORKER_CALLER)

    assert res["statusCode"] == 413
    assert fake_client.calls == []


def test_missing_sub_401(monkeypatch):
    fake_client = wire(monkeypatch)
    caller = dict(WORKER_CALLER, sub="")

    res = fapi.ask_voice({"audio": "QUJD"}, caller)

    assert res["statusCode"] == 401
    assert fake_client.calls == []


def test_function_error_500_without_stack_trace_leak(monkeypatch):
    wire(monkeypatch,
         response_payload={
             "errorMessage": "RuntimeError: dashscope upstream 503",
             "errorType": "RuntimeError",
             "stackTrace": ["  File \"lambda_ask_agent.py\", line 900"],
         },
         function_error="Unhandled")

    res = fapi.ask_voice({"audio": "QUJD"}, WORKER_CALLER)

    assert res["statusCode"] == 500
    assert "stackTrace" not in res["body"]
    assert "lambda_ask_agent.py" not in res["body"]
