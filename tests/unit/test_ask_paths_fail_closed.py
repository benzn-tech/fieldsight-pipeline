"""Ask/search proxies fail closed: no sign-in, no answer. The gateway refuses
an empty sub on all four proxy routes, and ask-agent never reaches its
company-blind S3 path without a caller_sub."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")
laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")


class RecLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append(json.loads(Payload))
        return {"StatusCode": 200,
                "Payload": io.BytesIO(json.dumps(
                    {"statusCode": 200, "body": json.dumps({"answer": "x", "results": []})}
                ).encode())}


ROUTES = {
    "/api/ask": {"question": "q?", "date": "2026-01-01", "user": "u"},
    "/api/ask/voice": {"audio": "AAAA", "format": "wav"},
    "/api/ask/corroborate": {"question": "q?", "answer": "a"},
    "/api/search": {"question": "door inspection"},
}


def _event(path, body):
    return {"httpMethod": "POST", "path": path, "body": json.dumps(body),
            "requestContext": {"authorizer": {"claims": {}}}}


def _caller(sub):
    return {"sub": sub, "role": "gm", "email": "", "name": "", "display_name": "",
            "device_id": "", "sites": [], "managed_sites": [], "company_id": "c-1"}


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_gateway_refuses_empty_sub(monkeypatch, path):
    rec = RecLambda()
    monkeypatch.setattr(fapi, "lambda_client", rec)
    monkeypatch.setattr(fapi, "ask_lambda_client", rec)
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: _caller(""))
    res = fapi.lambda_handler(_event(path, ROUTES[path]), None)
    assert res["statusCode"] == 401
    assert json.loads(res["body"]) == {"error": "sign-in required"}
    assert rec.calls == []


@pytest.mark.parametrize("path", sorted(ROUTES))
def test_gateway_with_sub_still_invokes_and_forwards_sub(monkeypatch, path):
    rec = RecLambda()
    monkeypatch.setattr(fapi, "lambda_client", rec)
    monkeypatch.setattr(fapi, "ask_lambda_client", rec)
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: _caller("sub-9"))
    res = fapi.lambda_handler(_event(path, ROUTES[path]), None)
    assert res["statusCode"] != 401
    assert len(rec.calls) == 1
    if path != "/api/ask/corroborate":  # corroborate reads no recordings, has no sub to forward
        assert rec.calls[0].get("caller_sub") == "sub-9"


def _s3_spies(monkeypatch):
    hits = []
    for name in ("load_report", "load_transcripts", "download_json_from_s3"):
        monkeypatch.setattr(laa, name, lambda *a, _n=name, **k: hits.append(_n) or None,
                            raising=False)
    return hits


def test_ask_agent_without_caller_sub_is_401_and_never_reads_s3(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag")
    hits = _s3_spies(monkeypatch)
    res = laa.lambda_handler({"body": json.dumps(
        {"question": "q?", "date": "2026-01-01", "user": "Ben"})}, None)
    assert res["statusCode"] == 401
    assert json.loads(res["body"])["error"] == "sign-in required"
    assert hits == []


def test_ask_agent_without_caller_sub_and_without_rag_env_is_401(monkeypatch):
    monkeypatch.delenv("RAG_SEARCH_FUNCTION", raising=False)
    hits = _s3_spies(monkeypatch)
    res = laa.lambda_handler({"body": json.dumps(
        {"question": "q?", "date": "2026-01-01", "user": "Ben"})}, None)
    assert res["statusCode"] == 401
    assert hits == []


def test_ask_agent_with_caller_sub_takes_rag_branch(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag")
    seen = []
    monkeypatch.setattr(laa, "_rag_answer", lambda b: seen.append(b) or {"answer": "ok"})
    res = laa.lambda_handler({"body": json.dumps(
        {"question": "q?", "caller_sub": "sub-1"})}, None)
    assert res["statusCode"] == 200
    assert len(seen) == 1
