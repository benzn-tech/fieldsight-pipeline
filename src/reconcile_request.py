"""reconcile_request.py -- the cross-session status-reconcile request (R0, Task 4).

After a session's FINAL extraction is written, the in-VPC side looks up the recorder's open
items from OTHER sessions on the same site and day (Track C's `find_items`) and hands them to
the non-VPC MatcherFunction, which reads this session's transcript and proposes status changes
on those items. The two halves are split on purpose:

* `build(conn, ...)` runs in-VPC, inside the caller's transaction, and returns the artifact
  body or None. item_writer calls it after the extraction's rows are written; a nightly
  re-match job calls it with a re-read extraction, so it depends on nothing but its arguments.
* `emit(s3, bucket, body)` writes the body AFTER the caller's connection block commits, under
  a deterministic key (re-processing the same extraction overwrites, never piles up), exactly
  as match_request.emit does.

What the caller still owns: the RECONCILE_STATUS flag and the group-member check.

The body carries only the whitelisted candidate fields. No `origin`, no `company_id` (the
writer takes the company from the re-read live row), and no other session's transcript.
Logs carry ids and counts only, never item text.
"""
import datetime as _dt
import hashlib
import json
import logging
import os

from lake_put import lake_put
from repositories import item_history

logger = logging.getLogger(__name__)

#: How many open items one request may offer the model. find_items' `limit`.
CANDIDATE_CAP = int(os.environ.get("RECONCILE_CANDIDATE_CAP", "40"))

RUNG = "R0"
KINDS = ("action_item",)
STATUSES = {"action_item": ["open", "in_progress", "blocked"]}
PREFIX = "reconcile_requests/"


def _iso(value):
    if value is None:
        return None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)


def _candidate(row):
    """One find_items row -> the artifact's candidate, JSON-safe. find_items returns
    stable_id as uuid.UUID and deadline as date; json.dumps refuses both."""
    source = row.get("source") or {}
    sid = row.get("stable_id")
    return {
        "kind": row.get("kind"),
        "stable_id": str(sid) if sid is not None else None,
        "text": row.get("text"),
        "status": row.get("status"),
        "responsible": row.get("responsible"),
        "deadline": _iso(row.get("deadline")),
        "first_seen_date": _iso(source.get("date")),
        "recorded_by": row.get("recorded_by"),
        "score": float(row.get("score") or 0.0),
        "matched_terms": list(row.get("matched_terms") or []),
        "place": row.get("place"),
    }


def build(conn, extraction, extraction_key, site, owner_company_id, *, user_folder, date,
          session_base):
    """The reconcile_requests/ artifact body for one extraction, or None.

    None when the extraction is not a complete FINAL pass, when there are no candidates, or
    when anything fails. `owner_company_id` is the SITE's company (project-owned tenancy):
    find_items returns [] for any other company, so passing the recorder's home company would
    silently disable this for everyone recording on another company's project.

    find_items runs in its own savepoint. A failed statement without one leaves the caller's
    psycopg transaction aborted, and every later write in it would fail -- reconcile must
    never cost the item write."""
    if extraction.get("tier") != "final" or extraction.get("incomplete"):
        return None
    site_id = site["id"]
    try:
        with conn.transaction():
            rows = item_history.find_items(
                conn, company_id=owner_company_id, site_id=site_id, kinds=KINDS,
                statuses=STATUSES, since=date, until=date, recorded_by=user_folder,
                exclude_source_keys=[extraction_key], limit=CANDIDATE_CAP)
        candidates = [_candidate(r) for r in rows]
    except Exception:  # noqa: BLE001 -- see the docstring: never fatal
        logger.exception("reconcile: candidate lookup failed for %s", extraction_key)
        return None
    logger.info("reconcile site=%s candidates=%d", site_id, len(candidates))
    if not candidates:
        return None
    return {
        "site_id": str(site_id),
        "date": str(date),
        "user_folder": user_folder,
        "session_base": session_base,
        "extraction_key": extraction_key,
        "extracted_at": extraction.get("extracted_at"),
        "rung": RUNG,
        "candidates": candidates,
    }


def request_key(site_id, date, extraction_key):
    digest = hashlib.sha256(extraction_key.encode("utf-8")).hexdigest()[:16]
    return f"{PREFIX}{site_id}/{date}/{digest}.json"


def emit(s3, bucket, body):
    """Write `body` (from build) and return its key; None, with no S3 call, when there is
    nothing to send."""
    if not body or not body.get("candidates"):
        return None
    site_id = str(body["site_id"])
    key = request_key(site_id, body["date"], body["extraction_key"])
    lake_put(bucket, key, json.dumps(body), client=s3, owner_hint={"site_id": site_id},
             ContentType="application/json")
    return key
