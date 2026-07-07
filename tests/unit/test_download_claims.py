"""
Tests for the download claim lock (Phase 4b, Task 1) in
src/lambda_orchestrator.py:

  claim_download(s3, bucket, s3_key) -> bool
  release_claim(s3, bucket, s3_key)

and the process_file() insertion point that uses them.

Style mirrors tests/unit/test_lambda_ingest.py / test_lambda_org_api.py:
a stub boto3-like client class recording calls, monkeypatch over the
module's functions rather than hitting real AWS.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

# lambda_orchestrator creates its S3/Lambda clients eagerly at import time
# (module-level `s3_client = boto3.client('s3')`). In an environment whose
# default AWS profile resolves through a credential provider needing the
# optional `botocore[crt]` extra (e.g. an SSO "login" provider), that import
# blows up before we ever get a chance to monkeypatch anything. Dummy static
# credentials make boto3 pick the plain env-var provider instead; no test
# here makes a real AWS call (all S3 interaction goes through the stub
# client below or is monkeypatched).
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

orch = pytest.importorskip("lambda_orchestrator", reason="requires boto3 (installed in CI)")


def precondition_failed_error():
    """A ClientError shaped like S3's response to a failed IfNoneMatch/IfMatch put."""
    return ClientError(
        {
            "Error": {"Code": "PreconditionFailed", "Message": "At least one of the pre-conditions you specified did not hold."},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        },
        "PutObject",
    )


def not_found_error():
    """A ClientError shaped like S3's response to a HEAD on a missing key."""
    return ClientError(
        {
            "Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )


class StubS3:
    """Minimal S3 client double: records every call it receives."""

    def __init__(self, put_error=None, head_response=None, head_error=None,
                 takeover_put_error=None):
        self.calls = []
        self._put_error = put_error
        self._head_response = head_response
        self._head_error = head_error
        self._takeover_put_error = takeover_put_error

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        if "IfMatch" in kwargs and self._takeover_put_error is not None:
            raise self._takeover_put_error
        if self._put_error is not None and kwargs.get("IfNoneMatch") == "*":
            raise self._put_error
        return {}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if self._head_error is not None:
            raise self._head_error
        return self._head_response

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        return {}


BUCKET = "test-bucket"
S3_KEY = "users/John_Smith/audio/2026-07-06/Benl1_2026-07-06_10-00-00.wav"
CLAIM_KEY = orch.CLAIM_PREFIX + S3_KEY + ".claim"


def test_claim_success():
    """No existing claim -> conditional put succeeds -> True, single call."""
    s3 = StubS3()
    assert orch.claim_download(s3, BUCKET, S3_KEY) is True
    assert s3.calls == [
        ("put_object", {"Bucket": BUCKET, "Key": CLAIM_KEY, "Body": b"", "IfNoneMatch": "*"})
    ]


def test_claim_contended_fresh_refused():
    """412 + claim modified 5 min ago (not stale) -> refused, no takeover put."""
    s3 = StubS3(
        put_error=precondition_failed_error(),
        head_response={"LastModified": datetime.now(timezone.utc) - timedelta(minutes=5)},
    )
    assert orch.claim_download(s3, BUCKET, S3_KEY) is False
    kinds = [c[0] for c in s3.calls]
    assert kinds == ["put_object", "head_object"]  # no second (unconditioned) put


def test_claim_stale_takeover():
    """412 + claim modified 31 min ago (stale) -> takeover: put conditioned
    on the ETag read from the HEAD (M-2), True."""
    s3 = StubS3(
        put_error=precondition_failed_error(),
        head_response={
            "LastModified": datetime.now(timezone.utc) - timedelta(minutes=31),
            "ETag": '"abc123"',
        },
    )
    assert orch.claim_download(s3, BUCKET, S3_KEY) is True
    kinds = [c[0] for c in s3.calls]
    assert kinds == ["put_object", "head_object", "put_object"]
    # Second put_object is the takeover write, conditioned on the HEAD'd ETag.
    second_put_kwargs = s3.calls[2][1]
    assert "IfNoneMatch" not in second_put_kwargs
    assert second_put_kwargs["Key"] == CLAIM_KEY
    assert second_put_kwargs["IfMatch"] == '"abc123"'


def test_claim_vanished_before_head_returns_false():
    """M-1: 412 on the conditional put, then the claim is GONE by the time we
    HEAD it (404) -- the downloader that held it just released it. Must
    return False, never propagate (a raise here would kill the whole
    orchestrator sweep over one key that already resolved itself)."""
    s3 = StubS3(put_error=precondition_failed_error(), head_error=not_found_error())
    assert orch.claim_download(s3, BUCKET, S3_KEY) is False
    kinds = [c[0] for c in s3.calls]
    assert kinds == ["put_object", "head_object"]  # no takeover put attempted


def test_claim_takeover_loses_race_returns_false():
    """M-2: claim is stale, but another sweep wins the conditioned takeover
    put first (412 on our IfMatch put) -> we back off, return False."""
    s3 = StubS3(
        put_error=precondition_failed_error(),
        head_response={
            "LastModified": datetime.now(timezone.utc) - timedelta(minutes=31),
            "ETag": '"abc123"',
        },
        takeover_put_error=precondition_failed_error(),
    )
    assert orch.claim_download(s3, BUCKET, S3_KEY) is False
    kinds = [c[0] for c in s3.calls]
    assert kinds == ["put_object", "head_object", "put_object"]


def test_release_deletes_right_key():
    s3 = StubS3()
    orch.release_claim(s3, BUCKET, S3_KEY)
    assert s3.calls == [("delete_object", {"Bucket": BUCKET, "Key": CLAIM_KEY})]


def test_release_swallows_errors():
    class RaisingS3(StubS3):
        def delete_object(self, **kwargs):
            self.calls.append(("delete_object", kwargs))
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "boom"}}, "DeleteObject"
            )

    s3 = RaisingS3()
    # Must not raise.
    orch.release_claim(s3, BUCKET, S3_KEY)
    assert s3.calls == [("delete_object", {"Bucket": BUCKET, "Key": CLAIM_KEY})]


def test_process_file_skips_when_claimed(monkeypatch):
    """When claim_download refuses, process_file must not invoke the
    downloader and must record the skip in stats['in_progress']."""
    monkeypatch.setattr(orch, "check_s3_exists", lambda bucket, key: False)
    monkeypatch.setattr(orch, "get_display_name", lambda device, bucket: "Test User")
    monkeypatch.setattr(orch, "claim_download", lambda s3, bucket, key: False)

    invoked = []
    monkeypatch.setattr(
        orch, "invoke_downloader",
        lambda function_name, file_info, s3_bucket: invoked.append(file_info),
    )

    stats = {
        "total_found": 0, "already_exists": 0, "triggered": 0, "in_progress": 0,
        "by_type": {"video": 0, "audio": 0, "upload": 0}, "by_user": {},
    }
    config = {"s3_bucket": BUCKET, "downloader_function": "fieldsight-downloader"}
    file_info = {"type": "audio", "user_name": "Benl1", "time": "2026-07-06 10:00:00"}

    orch.process_file(file_info, stats, config)

    assert invoked == []
    assert stats["in_progress"] == 1
    assert stats["triggered"] == 0
