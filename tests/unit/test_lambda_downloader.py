"""
Tests for src/lambda_downloader.py — Phase 4b claim heartbeat (I-1).

Style mirrors tests/unit/test_download_claims.py: dummy AWS env vars (this
Lambda creates its S3 client eagerly at import time), a stub S3 double that
records every call it receives, monkeypatch over the module's own helper
functions rather than hitting real AWS/HTTP.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

dl = pytest.importorskip("lambda_downloader", reason="requires boto3 (installed in CI)")


class StubS3:
    """Minimal S3 client double: records every call it receives, in order."""

    def __init__(self):
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        return {}


BUCKET = "test-bucket"
S3_KEY = "users/John_Smith/audio/2026-07-06/Benl1_2026-07-06_10-00-00.wav"
CLAIM_KEY = dl.CLAIM_PREFIX + S3_KEY + ".claim"


def test_refresh_claim_puts_empty_body_marker(monkeypatch):
    s3 = StubS3()
    monkeypatch.setattr(dl, "s3_client", s3)

    dl.refresh_claim(BUCKET, S3_KEY)

    assert s3.calls == [("put_object", {"Bucket": BUCKET, "Key": CLAIM_KEY, "Body": b""})]


def test_refresh_claim_swallows_errors(monkeypatch):
    class RaisingS3(StubS3):
        def put_object(self, **kwargs):
            self.calls.append(("put_object", kwargs))
            raise Exception("boom")

    s3 = RaisingS3()
    monkeypatch.setattr(dl, "s3_client", s3)

    # Must not raise -- a heartbeat failure can never kill the download.
    dl.refresh_claim(BUCKET, S3_KEY)
    assert len(s3.calls) == 1


# ---------------------------------------------------------------------------
# I-1 regression test: the handler must heartbeat the claim before any
# download work (size check / actual download) begins -- otherwise Lambda's
# hidden async-invoke retries could stretch a download attempt sequence past
# STALE_MINUTES while the claim's LastModified never advances.
# ---------------------------------------------------------------------------

def test_handler_heartbeats_claim_before_download_begins(monkeypatch):
    order = []
    s3 = StubS3()

    def tracking_put(**kwargs):
        s3.calls.append(("put_object", kwargs))
        if kwargs.get("Key") == CLAIM_KEY:
            order.append("claim_refresh")
        return {}

    monkeypatch.setattr(s3, "put_object", tracking_put)
    monkeypatch.setattr(dl, "s3_client", s3)

    monkeypatch.setattr(dl, "create_http_client", lambda: object())
    monkeypatch.setattr(
        dl, "check_file_size",
        lambda http, url: order.append("check_file_size") or 1.0,
    )
    monkeypatch.setattr(
        dl, "download_file",
        lambda http, url: order.append("download_file") or (True, b"data", None),
    )
    monkeypatch.setattr(
        dl, "upload_to_s3",
        lambda bucket, key, data, content_type=None: order.append("upload_to_s3") or (True, None),
    )
    monkeypatch.setattr(dl, "release_claim", lambda bucket, key: order.append("release_claim"))

    event = {
        "file_info": {
            "type": "audio",
            "s3_key": S3_KEY,
            "download_url": "https://example.com/file.wav",
            "display_name": "John Smith",
            "device_account": "Benl1",
        },
        "s3_bucket": BUCKET,
    }

    result = dl.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert order[0] == "claim_refresh"
    assert order.index("claim_refresh") < order.index("check_file_size")
    assert order.index("claim_refresh") < order.index("download_file")
