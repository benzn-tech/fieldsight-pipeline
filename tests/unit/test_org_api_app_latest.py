"""GET /api/org/app/latest -- the in-app update manifest, on the org-api request path.

The properties, in order of how much damage their absence would do:

1. A manifest the bucket will not let us read is LOUD (503), never "no update". A swallowed
   AccessDenied would read, on every device, as "you are up to date" -- forever, silently. This
   repo has shipped that exact shape before: `except ClientError: pass` turning a 403 into an
   empty 200.
2. Only a signed-in, provisioned caller gets a download link (the route sits after the caller
   guard).
3. The link is for exactly the key the manifest names.
"""
import io
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
from botocore.exceptions import ClientError  # noqa: E402

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "member", "created_at": "2026-08-04",
}

MANIFEST = {
    "versionCode": 41, "versionName": "0.7.15", "minVersionCode": 40,
    "sha256": "b" * 64, "sizeBytes": 26_000_000,
    "apkKey": "app-releases/41/fieldsight-PROD-abc1234.apk", "notes": "",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeS3:
    def __init__(self, body=None, error_code=None):
        self.body, self.error_code = body, error_code
        self.gets, self.presigned = [], []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.body)}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params["Key"], ExpiresIn))
        return f"https://signed.example/{Params['Key']}"


def call(monkeypatch, fake_s3, caller=CALLER):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller) if caller else None)
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org, "s3", lambda: fake_s3)
    resp = org.lambda_handler({
        "httpMethod": "GET", "path": "/api/org/app/latest", "queryStringParameters": None,
        "body": None, "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_a_published_release_comes_back_with_a_link_to_exactly_its_apk(monkeypatch):
    fake = FakeS3(body=json.dumps(MANIFEST).encode())
    status, body = call(monkeypatch, fake)
    assert status == 200
    assert body["available"] is True and body["versionCode"] == 41 and body["minVersionCode"] == 40
    assert fake.gets == ["app-releases/latest.json"]
    assert fake.presigned == [("get_object", MANIFEST["apkKey"], org.PRESIGNED_URL_EXPIRY)]
    assert body["url"].endswith(MANIFEST["apkKey"])


def test_no_release_published_yet_is_not_an_error(monkeypatch):
    status, body = call(monkeypatch, FakeS3(error_code="NoSuchKey"))
    assert (status, body) == (200, {"available": False})


def test_a_manifest_we_may_not_read_is_loud_never_up_to_date(monkeypatch, caplog):
    caplog.set_level("ERROR")
    fake = FakeS3(error_code="AccessDenied")
    status, body = call(monkeypatch, fake)
    assert status == 503
    assert body.get("available") is not False, "must not tell every device it is up to date"
    assert fake.presigned == []
    assert any("app-release manifest" in r.getMessage() for r in caplog.records)


def test_a_broken_manifest_offers_nothing(monkeypatch):
    fake = FakeS3(body=b'{"versionCode": 41}')
    status, body = call(monkeypatch, fake)
    assert (status, body) == (200, {"available": False})
    assert fake.presigned == []


def test_an_unprovisioned_caller_gets_no_link(monkeypatch):
    fake = FakeS3(body=json.dumps(MANIFEST).encode())
    status, _ = call(monkeypatch, fake, caller=None)
    assert status == 403
    assert fake.gets == [] and fake.presigned == []


def test_the_template_grants_read_on_app_releases_and_lists_it():
    """A missing GetObject grant fails the route; a missing ListBucket prefix turns "no release
    yet" (NoSuchKey) into AccessDenied, which this route rightly reports as 503 -- so both must be
    there, and both are only visible after a deploy."""
    import os.path
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "src", "template.yaml"), encoding="utf-8") as fh:
        t = fh.read()
    assert "arn:aws:s3:::${DataBucketName}/app-releases/*" in t
    assert '- "app-releases/*"' in t
