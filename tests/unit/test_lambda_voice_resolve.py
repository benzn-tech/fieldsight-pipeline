import pytest

vr = pytest.importorskip("lambda_voice_resolve", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(vr, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(vr.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1", "global_role": "worker"})
    monkeypatch.setattr(vr.memberships, "accessible_site_ids", lambda c, uid, role: ["s-1"])
    monkeypatch.setattr(vr.voice_messages, "insert_message",
                        lambda c, coid, sid, uid, key, duration_s=None: {
                            "id": "m-1", "site_id": sid, "s3_key": key,
                            "duration_s": duration_s, "created_at": "2026-07-18T00:00:00Z"})
    monkeypatch.setattr(vr.ws_connections, "recipients_for_site",
                        lambda c, coid, sid, uid: ["conn-b", "conn-c"])
    return monkeypatch


def test_resolve_inserts_and_returns_recipients(wired):
    res = vr.lambda_handler({"sub": "sub-1", "siteId": "s-1",
                             "s3Key": "voice/c-1/s-1/x.wav", "durationS": 1.2}, None)
    assert res["statusCode"] == 200
    assert res["connectionIds"] == ["conn-b", "conn-c"]
    assert res["messageId"] == "m-1"
    p = res["payload"]
    assert p["type"] == "voice" and p["s3Key"] == "voice/c-1/s-1/x.wav"
    assert p["messageId"] == "m-1" and p["senderUserId"] == "u-1"
    assert p["durationS"] == 1.2 and p["createdAt"] == "2026-07-18T00:00:00Z"


def test_resolve_non_member_403(wired):
    wired.setattr(vr.memberships, "accessible_site_ids", lambda c, uid, role: ["other"])
    res = vr.lambda_handler({"sub": "sub-1", "siteId": "s-1", "s3Key": "voice/x.wav"}, None)
    assert res["statusCode"] == 403


def test_resolve_unprovisioned_403(wired):
    wired.setattr(vr.users, "get_user_by_sub", lambda c, sub: None)
    res = vr.lambda_handler({"sub": "sub-1", "siteId": "s-1", "s3Key": "voice/x.wav"}, None)
    assert res["statusCode"] == 403


def test_resolve_missing_fields_400(wired):
    res = vr.lambda_handler({"sub": "sub-1", "siteId": "s-1"}, None)
    assert res["statusCode"] == 400
