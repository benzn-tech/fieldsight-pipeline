"""continuity_xday_request.py -- the cross-day continuity request artifact (spec 2026-11-09 X3).

Hop 1 of cross-day item continuity: the in-VPC item writer builds the request
(`lambda_item_writer.build_xday_request`, shape from `continuity_xday.build_request`) and,
AFTER its connection block commits, puts it here for the non-VPC cross-day linker's S3
trigger -- the `match_requests/` pattern (match_request.py): in-VPC lambdas have no egress
and cannot invoke (BUG-36), so the request rides S3.

Key: continuity_xday_requests/{site_id}/{date}/{run_hash}.json. `run_hash` hashes the
extraction key + extracted_at + optional nonce, so a re-delivered pass overwrites its own
request while a deliberate re-emit (the nightly re-match's nonce) gets its own object.
Registered in lake_keys as the site-scoped kind `xday_request`, like `match`.
"""
import datetime as _dt
import json

from lake_put import lake_put

PREFIX = "continuity_xday_requests/"


def _day(date):
    if isinstance(date, _dt.datetime):
        return date.date().isoformat()
    if isinstance(date, _dt.date):
        return date.isoformat()
    return str(date)


def key_for(site_id, date, run_hash):
    return f"{PREFIX}{site_id}/{_day(date)}/{run_hash}.json"


def emit(s3, bucket, request):
    """Put `request` at its key; returns the key. Raises on a request missing a key field
    or on a failed put -- the caller swallows (X10), this helper does not decide that."""
    site_id, date, run_hash = (request or {}).get("site_id"), (request or {}).get("date"), \
        (request or {}).get("run_hash")
    if not site_id or not date or not run_hash:
        raise ValueError("request lacks site_id/date/run_hash")
    key = key_for(site_id, date, run_hash)
    lake_put(bucket, key, json.dumps(request, ensure_ascii=False), client=s3,
             owner_hint={"site_id": str(site_id)}, ContentType="application/json")
    return key
