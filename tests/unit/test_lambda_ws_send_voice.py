import json

import pytest

sv = pytest.importorskip("lambda_ws_send_voice", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeLambda:
    def __init__(self): self.calls = []
    def invoke(self, **kw):
        self.calls.append({**kw, "payload": json.loads(kw["Payload"])})
        return {"StatusCode": 202}


def _event(body, sub="sub-1"):
    return {"body": json.dumps(body),
            "requestContext": {"connectionId": "conn-1", "domainName": "ws.example.com",
                               "stage": "prod", "authorizer": {"sub": sub} if sub else {}}}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(sv, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(sv, "FANOUT_FUNCTION", "fieldsight-test-voice-fanout")
    monkeypatch.setattr(sv.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1", "global_role": "worker"})
    monkeypatch.setattr(sv.memberships, "accessible_site_ids", lambda c, uid, role: ["s-1"])
    monkeypatch.setattr(sv.voice_messages, "insert_message",
                        lambda c, coid, sid, uid, key, duration_s=None: {
                            "id": "m-1", "site_id": sid, "s3_key": key,
                            "duration_s": duration_s, "created_at": "2026-07-18T00:00:00Z"})
    monkeypatch.setattr(sv.ws_connections, "recipients_for_site",
                        lambda c, coid, sid, uid: ["conn-b", "conn-c"])
    fake = _FakeLambda()
    monkeypatch.setattr(sv, "_lambda", lambda: fake)
    return monkeypatch, fake


def test_send_inserts_and_dispatches_fanout(wired):
    mp, fake = wired
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2}), None)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"messageId": "m-1", "recipients": 2}
    inv = fake.calls[0]
    assert inv["FunctionName"] == "fieldsight-test-voice-fanout"
    assert inv["InvocationType"] == "Event"
    p = inv["payload"]
    assert p["endpoint"] == "https://ws.example.com/prod"
    assert p["connectionIds"] == ["conn-b", "conn-c"]
    assert p["payload"]["s3Key"] == "voice/c-1/s-1/x.wav" and p["payload"]["messageId"] == "m-1"


def test_non_member_site_403(wired):
    mp, fake = wired
    mp.setattr(sv.memberships, "accessible_site_ids", lambda c, uid, role: ["other-site"])
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 403 and fake.calls == []


def test_missing_fields_400(wired):
    mp, fake = wired
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1"}), None)
    assert res["statusCode"] == 400


def test_no_recipients_skips_fanout(wired):
    mp, fake = wired
    mp.setattr(sv.ws_connections, "recipients_for_site", lambda c, coid, sid, uid: [])
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 200 and fake.calls == []   # inserted, but nobody online


def test_unprovisioned_caller_403(wired):
    mp, fake = wired
    mp.setattr(sv.users, "get_user_by_sub", lambda c, sub: None)
    res = sv.lambda_handler(_event({"action": "sendVoice", "siteId": "s-1",
                                    "s3Key": "voice/x.wav"}), None)
    assert res["statusCode"] == 403
