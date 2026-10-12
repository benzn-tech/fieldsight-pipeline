"""Shared write helper for the data lake: put_object plus an append-only manifest line.

Lambdas outside the VPC cannot reach Aurora, so each tenant-scoped put records one
manifest object (``lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl``, one JSON line). A later
drain turns those lines into ``lake_objects`` rows.

Contract (pinned by tests/unit/test_lake_put.py):
- Every put kwarg (conditional headers, ContentType, Metadata, ...) goes to put_object
  unchanged, and put_object's response is returned unchanged. No ``Tagging`` is ever sent.
- The object put raises exactly as before; no manifest line is written when it raises.
- The manifest line is written only after the object put succeeded. A failure while
  writing it is logged as a WARNING and never raised.
- Only tenant-scope keys get a manifest line (global keys: config/, download_claims/ ...).
- ``LAKE_MANIFEST=off`` (read at call time) is the kill switch.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import lake_keys

logger = logging.getLogger(__name__)
_CLIENT = None

MANIFEST_PREFIX = "lake_manifest/"


def _client(client):
    global _CLIENT
    if client is not None:
        return client
    if _CLIENT is None:
        import boto3
        _CLIENT = boto3.client("s3")
    return _CLIENT


def manifest_key(now=None, uid=None):
    """lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl (UTC date)."""
    n = now or datetime.now(timezone.utc)
    if n.tzinfo is not None:
        n = n.astimezone(timezone.utc)
    return f"{MANIFEST_PREFIX}{n:%Y/%m/%d}/{uid or uuid.uuid4()}.jsonl"


def _manifest_enabled():
    return os.environ.get("LAKE_MANIFEST", "on") == "on"


def _wants_manifest(key):
    try:
        p = lake_keys.parse(key)
    except Exception:  # noqa: BLE001 - the object is already stored; never fail the writer
        logger.warning("lake_put: key parse failed for %s; recording it anyway", key, exc_info=True)
        return True
    # Unknown shapes (None) ARE recorded: the drain reports them.
    return p is None or p["scope"] != "global"


def _size_of(body):
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode("utf-8"))
    return None


def _write_manifest(c, bucket, key, size, hint):
    line = {"v": 1, "bucket": bucket, "key": key, "size": size,
            "t": datetime.now(timezone.utc).isoformat(),
            "hint": {k: str(v) for k, v in (hint or {}).items() if v}}
    try:
        c.put_object(Bucket=bucket, Key=manifest_key(), Body=(json.dumps(line) + "\n").encode(),
                     ContentType="application/x-ndjson")
    except Exception:  # noqa: BLE001 - the object is already stored; the backfill is the safety net
        logger.warning("lake_put: manifest write failed for %s", key, exc_info=True)


def lake_put(bucket, key, body, *, owner_hint=None, client=None, manifest=True, **put_kwargs):
    """put_object, then (tenant keys only) one manifest line. Returns put_object's response.

    Kill switch: LAKE_MANIFEST=off (read at call time) stops the manifest lines. It is NOT a
    template Parameter or environment entry, so a value set by hand in the Lambda console is
    temporary: the next `sam deploy` erases it. To make it stick, wire it like LakeIndexReads."""
    c = _client(client)
    resp = c.put_object(Bucket=bucket, Key=key, Body=body, **put_kwargs)  # raises exactly as before
    if manifest and _manifest_enabled() and _wants_manifest(key):
        _write_manifest(c, bucket, key, _size_of(body), owner_hint)
    return resp


def lake_upload_file(bucket, key, filename, *, owner_hint=None, client=None, manifest=True, ExtraArgs=None):
    """upload_file twin of lake_put (vad/keyframe). ExtraArgs pass through unchanged."""
    c = _client(client)
    if ExtraArgs is None:
        resp = c.upload_file(filename, bucket, key)
    else:
        resp = c.upload_file(filename, bucket, key, ExtraArgs=ExtraArgs)
    if manifest and _manifest_enabled() and _wants_manifest(key):
        try:
            size = os.path.getsize(filename)
        except OSError:
            size = None
        _write_manifest(c, bucket, key, size, owner_hint)
    return resp
