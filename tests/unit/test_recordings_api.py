import json

import pytest
from psycopg.errors import UniqueViolation

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        # Real psycopg conn.transaction() opens a savepoint; the fake just
        # needs to be a no-op context manager so `with conn.transaction():`
        # works in the code under test.
        return _NoopCtx()


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


def test_upload_url_duplicate_clientuuid_via_race_returns_existing(wired):
    # First get_by_client_uuid call (pre-insert check) finds nothing, so the
    # handler takes the insert path; insert_pending then hits a concurrent
    # request's row and raises UniqueViolation; the handler re-queries
    # (second get_by_client_uuid call) and must return the row that won the
    # race, 200, idempotently — no 500.
    mp, fake = wired
    lookups = [None, {"id": "rec-won-race", "s3_key": "users/Ada_L/video/2026-07-13/race.mp4"}]

    def fake_get_by_client_uuid(conn, user_id, client_uuid):
        return lookups.pop(0)

    def fake_insert_pending(conn, **kw):
        raise UniqueViolation("duplicate key value violates unique constraint")

    mp.setattr(org.recordings, "get_by_client_uuid", fake_get_by_client_uuid)
    mp.setattr(org.recordings, "insert_pending", fake_insert_pending)

    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-race", "siteId": None,
        "fileName": "race.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)

    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["recordingId"] == "rec-won-race"
    assert b["s3Key"] == "users/Ada_L/video/2026-07-13/race.mp4"
    assert lookups == []  # both lookups consumed


def test_upload_url_s3key_collision_returns_409(wired):
    # insert_pending hits a genuine s3_key collision (different recording,
    # same key) — the re-query for this caller's clientUuid finds nothing,
    # so it's not a race with self; must return 409, not 500.
    mp, fake = wired
    mp.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)

    def fake_insert_pending(conn, **kw):
        raise UniqueViolation("duplicate key value violates unique constraint")

    mp.setattr(org.recordings, "insert_pending", fake_insert_pending)

    res = org.lambda_handler(make_event("POST", "/api/org/recordings/upload-url", body={
        "kind": "video", "clientUuid": "cap-collide", "siteId": None,
        "fileName": "collide.mp4", "contentType": "video/mp4",
        "startedAt": "2026-07-13T16:01:58Z"}), None)

    assert res["statusCode"] == 409


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
               lambda c, rid, cid, sz=None, gps_track=None: seen.update(
                   rid=rid, cid=cid, sz=sz, gps_track=gps_track) or
               {"id": rid, "uploaded_at": "2026-07-13T16:10:00Z", "size_bytes": sz})
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-1/complete",
                                        body={"sizeBytes": 12345}), None)
    assert res["statusCode"] == 200 and body_of(res)["ok"] is True
    assert seen == {"rid": "rec-1", "cid": "c-1", "sz": 12345, "gps_track": None}


def test_complete_unknown_or_wrong_company_404(wired):
    mp, fake = wired
    mp.setattr(org.recordings, "mark_uploaded", lambda c, rid, cid, sz=None, gps_track=None: None)
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rec-x/complete",
                                        body={}), None)
    assert res["statusCode"] == 404


def test_complete_persists_gps_track(wired):
    # body carries a gpsTrack array -> complete_recording must pass it through
    # to mark_uploaded unchanged (camelCase body key -> Python list).
    mp, fake = wired
    captured = {}

    def fake_mark(conn, rid, cid, sz=None, gps_track=None):
        captured["gps_track"] = gps_track
        return {"id": rid}

    mp.setattr(org.recordings, "mark_uploaded", fake_mark)
    body = {"sizeBytes": 10, "gpsTrack": [{"t": 1, "lat": -36.85, "lon": 174.76}]}
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rid/complete", body=body), None)
    assert res["statusCode"] == 200
    assert captured["gps_track"] == [{"t": 1, "lat": -36.85, "lon": 174.76}]


def test_complete_without_gps_track_is_backward_compatible(wired):
    # Old clients that omit gpsTrack entirely must behave exactly as today:
    # mark_uploaded is called with gps_track=None (COALESCE keeps existing value).
    mp, fake = wired
    captured = {}

    def fake_mark(conn, rid, cid, sz=None, gps_track=None):
        captured["gps_track"] = gps_track
        return {"id": rid}

    mp.setattr(org.recordings, "mark_uploaded", fake_mark)
    res = org.lambda_handler(make_event("POST", "/api/org/recordings/rid/complete",
                                        body={"sizeBytes": 10}), None)
    assert res["statusCode"] == 200
    assert captured["gps_track"] is None
