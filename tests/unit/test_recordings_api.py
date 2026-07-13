import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.last = {"op": op, "params": Params, "expires": ExpiresIn}
        return "https://s3.example/" + Params["Key"]


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "global_role": "pm", "created_at": "2026-07-04", "archived_at": None}


def make_event(method, path, sub="sub-1", body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": None,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}}}


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    fake = FakeS3()
    monkeypatch.setattr(org, "_s3_client", fake)
    return monkeypatch, fake


# ---- upload-url ----

def test_upload_url_creates_row_and_presigns(wired):
    mp, fake = wired
    created = {}

    def fake_insert(conn, **kw):
        created.update(kw)
        return {"id": "rec-1", **kw, "uploaded_at": None}

    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.recordings, "insert_pending", fake_insert)
    mp.setattr(org.sites, "get_site", lambda c, sid: {"id": sid, "company_id": "c-1"})

    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-1", "siteId": "s-1",
        "fileName": "Ada_L_20260713_160158.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z", "durationS": 122,
        "resolution": "1920x1080", "codec": "h264"}), None)

    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["recordingId"] == "rec-1"
    assert b["s3Key"] == "users/Ada_L/video/2026-07-13/Ada_L_20260713_160158.mp4"
    assert b["uploadUrl"].endswith(b["s3Key"])
    assert fake.last["op"] == "put_object" and fake.last["params"]["ContentType"] == "video/mp4"
    assert created["kind"] == "video" and created["site_id"] == "s-1" and created["user_id"] == "u-1"


def test_photo_maps_to_pictures_folder(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.recordings, "insert_pending", lambda conn, **kw: {"id": "rec-2", **kw})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "photo", "clientUuid": "cap-2", "siteId": None,
        "fileName": "Ada_L_20260713_160314.jpg", "contentType": "image/jpeg",
        "startedAt": "2026-07-13T16:03:14Z"}), None)
    assert body_of(res)["s3Key"] == "users/Ada_L/pictures/2026-07-13/Ada_L_20260713_160314.jpg"


def test_upload_url_idempotent_on_client_uuid(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid",
               lambda c, u, cu: {"id": "rec-existing", "s3_key": "users/Ada_L/video/2026-07-13/old.mp4"})
    called = {"insert": False}
    mp.setattr(org.recordings, "insert_pending",
               lambda conn, **kw: called.__setitem__("insert", True) or {"id": "x"})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-1", "siteId": None,
        "fileName": "new.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)
    b = body_of(res)
    assert b["recordingId"] == "rec-existing" and called["insert"] is False
    assert b["s3Key"] == "users/Ada_L/video/2026-07-13/old.mp4"


def test_site_from_other_company_rejected(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)
    mp.setattr(org.sites, "get_site", lambda c, sid: {"id": sid, "company_id": "OTHER"})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-9", "siteId": "s-x",
        "fileName": "a.mp4", "contentType": "video/mp4", "startedAt": "2026-07-13T16:01:58Z"}), None)
    assert res["statusCode"] == 403


def test_bad_kind_rejected(wired):
    mp, fake = wired
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "movie", "clientUuid": "c", "fileName": "a.x", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)
    assert res["statusCode"] == 400


# ---- complete ----

def test_complete_marks_uploaded(wired):
    mp, fake = wired
    seen = {}
    mp.setattr(org.recordings, "mark_uploaded",
               lambda c, rid, cid, sz: seen.update(rid=rid, cid=cid, sz=sz) or
               {"id": rid, "uploaded_at": "2026-07-13T16:10:00Z", "size_bytes": sz})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-1/complete",
                                        body={"sizeBytes": 12345}), None)
    assert res["statusCode"] == 200 and body_of(res)["ok"] is True
    assert seen == {"rid": "rec-1", "cid": "c-1", "sz": 12345}


def test_complete_unknown_or_wrong_company_404(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "mark_uploaded", lambda c, rid, cid, sz: None)
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-x/complete",
                                        body={}), None)
    assert res["statusCode"] == 404
