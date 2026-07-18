import json

import pytest

sv = pytest.importorskip("lambda_ws_send_voice", reason="requires boto3 import path")


class _Body:
    def __init__(self, s): self._s = s.encode("utf-8")
    def read(self): return self._s


class _FakeLambda:
    """Fakes the resolve (RequestResponse) + fanout (Event) invokes."""
    def __init__(self, resolve_result):
        self.resolve_result = resolve_result
        self.calls = []

    def invoke(self, **kw):
        self.calls.append({**kw, "payload": json.loads(kw["Payload"])})
        if kw.get("InvocationType") == "RequestResponse":
            return {"Payload": _Body(json.dumps(self.resolve_result))}
        return {"StatusCode": 202}


def _event(body, sub="sub-1"):
    return {"body": json.dumps(body),
            "requestContext": {"connectionId": "conn-1", "domainName": "ws.example.com",
                               "stage": "prod", "authorizer": {"sub": sub} if sub else {}}}


def _wire(monkeypatch, resolve_result):
    monkeypatch.setattr(sv, "RESOLVE_FUNCTION", "fieldsight-test-voice-resolve")
    monkeypatch.setattr(sv, "FANOUT_FUNCTION", "fieldsight-test-voice-fanout")
    fake = _FakeLambda(resolve_result)
    monkeypatch.setattr(sv, "_lambda", lambda: fake)
    return fake


_OK_RESOLVE = {"statusCode": 200, "messageId": "m-1",
               "connectionIds": ["conn-b", "conn-c"],
               "payload": {"type": "voice", "messageId": "m-1", "siteId": "s-1",
                           "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2,
                           "senderUserId": "u-1", "createdAt": "2026-07-18T00:00:00Z"}}


def test_send_resolves_then_dispatches_fanout(monkeypatch):
    fake = _wire(monkeypatch, _OK_RESOLVE)
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2}), None)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"messageId": "m-1", "recipients": 2}
    assert fake.calls[0]["FunctionName"] == "fieldsight-test-voice-resolve"
    assert fake.calls[0]["InvocationType"] == "RequestResponse"
    assert fake.calls[0]["payload"] == {"sub": "sub-1", "siteId": "s-1",
                                        "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2}
    assert fake.calls[1]["FunctionName"] == "fieldsight-test-voice-fanout"
    assert fake.calls[1]["InvocationType"] == "Event"
    fp = fake.calls[1]["payload"]
    assert fp["endpoint"] == "https://ws.example.com/prod"
    assert fp["connectionIds"] == ["conn-b", "conn-c"]
    assert fp["payload"]["s3Key"] == "voice/c-1/s-1/x.wav"


def test_resolve_403_returns_403_no_fanout(monkeypatch):
    fake = _wire(monkeypatch, {"statusCode": 403})
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 403
    assert len(fake.calls) == 1 and fake.calls[0]["InvocationType"] == "RequestResponse"


def test_no_recipients_skips_fanout(monkeypatch):
    fake = _wire(monkeypatch, {"statusCode": 200, "messageId": "m-1",
                               "connectionIds": [], "payload": {}})
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 200
    assert len(fake.calls) == 1


def test_missing_fields_400(monkeypatch):
    fake = _wire(monkeypatch, _OK_RESOLVE)
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1"}), None)
    assert res["statusCode"] == 400
    assert fake.calls == []


def test_missing_sub_400(monkeypatch):
    fake = _wire(monkeypatch, _OK_RESOLVE)
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}, sub=None), None)
    assert res["statusCode"] == 400


def test_malformed_json_body_400(monkeypatch):
    fake = _wire(monkeypatch, _OK_RESOLVE)
    event = {"body": "{not json",
             "requestContext": {"connectionId": "conn-1", "domainName": "ws.example.com",
                                "stage": "prod", "authorizer": {"sub": "sub-1"}}}
    res = sv.lambda_handler(event, None)
    assert res["statusCode"] == 400
    assert fake.calls == []
