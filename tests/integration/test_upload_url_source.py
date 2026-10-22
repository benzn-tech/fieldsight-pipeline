"""`/upload-url` records where a recording came from: device, phone PWA or website.

Real Postgres: the value lands in `recordings.source` behind a CHECK constraint
(migration 0089), which a connection double would never exercise.
"""
import json
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, users

pytestmark = pytest.mark.integration

_DEVICE = {"asset_tag": "FS-01", "device_uuid": "u", "app_version": "1"}


class _S3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]


@pytest.fixture(autouse=True)
def _s3(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: _S3())


def _user(db):
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"SrcCo-{tag}")
    u = users.upsert_user(db, f"sub-src-{tag}", f"src-{tag}@a.nz", company_id=co["id"],
                          first_name="Sam", last_name="Src", global_role="worker")
    users.set_folder_name(db, f"sub-src-{tag}", f"Sam_Src_{tag}")
    return users.get_by_id(db, u["id"])


def _body(client_uuid=None, **extra):
    body = {"kind": "audio", "clientUuid": client_uuid or uuid.uuid4().hex,
            "fileName": f"Sam_{uuid.uuid4().hex[:8]}.m4a", "contentType": "audio/mp4",
            "startedAt": "2026-10-21T10:00:00Z"}
    body.update(extra)
    return body


def _source(db, rec_id):
    return db.execute("SELECT source FROM recordings WHERE id=%s", (rec_id,)).fetchone()[0]


def _ok(res):
    assert res["statusCode"] == 200, res
    return json.loads(res["body"])["recordingId"]


def test_phone_source_is_stored(db):
    u = _user(db)
    rec_id = _ok(api.create_recording_upload_url(db, u, _body(source="phone")))
    assert _source(db, rec_id) == "phone"


def test_device_headers_mean_device(db):
    u = _user(db)
    rec_id = _ok(api.create_recording_upload_url(db, u, _body(), device_ident=dict(_DEVICE)))
    assert _source(db, rec_id) == "device"


def test_no_headers_no_source_is_null(db):
    u = _user(db)
    rec_id = _ok(api.create_recording_upload_url(db, u, _body()))
    assert _source(db, rec_id) is None


def test_body_wins_over_headers(db):
    u = _user(db)
    rec_id = _ok(api.create_recording_upload_url(db, u, _body(source="web"),
                                                 device_ident=dict(_DEVICE)))
    assert _source(db, rec_id) == "web"


@pytest.mark.parametrize("bad", ["device", "tablet"])
def test_unknown_source_is_400(db, bad):
    u = _user(db)
    cu = uuid.uuid4().hex
    res = api.create_recording_upload_url(db, u, _body(client_uuid=cu, source=bad))
    assert res["statusCode"] == 400, res
    assert db.execute("SELECT count(*) FROM recordings WHERE user_id=%s AND client_uuid=%s",
                      (u["id"], cu)).fetchone()[0] == 0


def test_resend_keeps_first_source(db):
    u = _user(db)
    cu = uuid.uuid4().hex
    first = _ok(api.create_recording_upload_url(db, u, _body(client_uuid=cu, source="phone")))
    again = _ok(api.create_recording_upload_url(db, u, _body(client_uuid=cu, source="web")))
    assert again == first
    assert _source(db, first) == "phone"
