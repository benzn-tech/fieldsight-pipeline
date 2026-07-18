import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}


def make_event(method, path, sub="sub-1", body=None, qs=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}}}


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    monkeypatch.setattr(org, "S3_BUCKET", "fieldsight-data-test-509194952652")
    return monkeypatch, fake


def test_voice_upload_url_presigns_only_no_row(wired):
    mp, fake = wired
    # upload-url must NOT write voice_messages — sendVoice is the sole writer,
    # so an abandoned recording leaves no orphan/duplicate backfill row.
    mp.setattr(org.voice_messages, "insert_message",
               lambda *a, **k: (_ for _ in ()).throw(AssertionError("upload-url must not insert")))
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "audio/wav", "siteId": "s-1", "durationS": 2.0}), None)
    assert res["statusCode"] == 200
    b = body_of(res)
    assert "messageId" not in b
    assert b["s3Key"].startswith("voice/c-1/s-1/") and b["s3Key"].endswith(".wav")
    assert b["uploadUrl"].endswith(b["s3Key"])
    assert fake.last["op"] == "put_object" and fake.last["params"]["ContentType"] == "audio/wav"


def test_voice_upload_url_bad_content_type_400(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "video/mp4", "siteId": "s-1"}), None)
    assert res["statusCode"] == 400


def test_voice_upload_url_site_not_accessible_403(wired):
    mp, fake = wired
    mp.setattr(org, "_allowed_site_ids", lambda conn, caller: {"other"})
    res = org.lambda_handler(make_event("POST", "/api/org/voice/upload-url", body={
        "contentType": "audio/wav", "siteId": "s-1"}), None)
    assert res["statusCode"] == 403


def test_voice_asset_url_scoped_to_company_prefix(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("GET", "/api/org/voice/asset-url",
                                        qs={"key": "voice/c-1/s-1/x.wav"}), None)
    assert res["statusCode"] == 200 and fake.last["op"] == "get_object"
    # a key outside the caller's company prefix is refused
    bad = org.lambda_handler(make_event("GET", "/api/org/voice/asset-url",
                                        qs={"key": "voice/OTHER/s-1/x.wav"}), None)
    assert bad["statusCode"] == 400


def test_site_voice_backfill_lists_since(wired):
    mp, fake = wired
    mp.setattr(org.voice_messages, "list_since",
               lambda c, coid, sid, since: [{"id": "m-1", "s3_key": "voice/c-1/s-1/x.wav",
                                             "sender_user_id": "u-9", "duration_s": 3,
                                             "site_id": sid, "created_at": since}])
    res = org.lambda_handler(make_event("GET", "/api/org/sites/s-1/voice",
                                        qs={"since": "2026-07-18T00:00:00Z"}), None)
    assert res["statusCode"] == 200
    item = body_of(res)["items"][0]
    assert item["s3Key"] == "voice/c-1/s-1/x.wav"
    assert item["senderUserId"] == "u-9" and item["durationS"] == 3
    assert "s3_key" not in item  # camelCase only — no snake_case leaks across the API


def test_site_voice_backfill_acl_403(wired):
    mp, fake = wired
    mp.setattr(org, "_allowed_site_ids", lambda conn, caller: {"other"})
    res = org.lambda_handler(make_event("GET", "/api/org/sites/s-1/voice", qs={}), None)
    assert res["statusCode"] == 403
