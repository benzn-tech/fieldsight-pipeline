"""Auto-retention sweep for non-work (personal) topics.

Life-conversation separation, phase 2 (decided with the user 2026-08-04).

A topic the classifier marked `non_work` is visible to its recorder for a
buffer window so they can rescue a misclassification, and is then removed from
the product permanently. "Removed" here means what the user chose: **excluded,
not erased**.

    tombstone the topic (redactions)  ->  it leaves every company-tier read
    delete-only reindex               ->  it leaves RAG, so Ask/Search cannot surface it
    transcript + audio                ->  UNTOUCHED

Why nothing is hard-deleted:

  * The recorder can be wrong-footed by the classifier. A tombstone is
    revertible; a DELETE is not. The redactions table already models exactly
    this (a tombstone with reverted_at), so this sweep adds no new mechanism —
    it reuses the one the manual "confirm personal" button writes.
  * The audit the tombstone leaves (who/when/why, no content) is what lets an
    engineer answer "where did my record go" a month later, and lets the
    company show a customer the policy actually ran. It costs nothing here
    because the row exists anyway.
  * Deleting the underlying audio/transcript was considered and rejected: a
    topic has no clean boundary in the audio (one 30s chunk routinely holds
    both work and personal talk, and a topic's time_range is inferred from
    text, not measured), so a "precise" cut is not available — it would either
    destroy work evidence or leave the personal part behind. The derived
    artifacts are what make content findable, and those are what this removes.

RESCUE PATH — no new UI. Inside the buffer the topic is still in the
recorder's own timeline, where the existing review control flips work_class to
'work'. That drops it out of this sweep's query permanently.

SAFETY — the sweep is inert unless BOTH env vars are set. NONWORK_EXPIRY_SINCE
has no default on purpose: the policy applies to new topics only, and a
missing floor would tombstone every historical non_work topic on the first
run. Absent config logs and no-ops rather than guessing a floor.
"""
import datetime
import logging
import os

import boto3

import reindex
from repositories import redactions, topics

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ENABLED = os.environ.get("NONWORK_EXPIRY_ENABLED", "false").lower() == "true"
BUFFER_HOURS = float(os.environ.get("NONWORK_EXPIRY_HOURS", "24"))
SINCE = os.environ.get("NONWORK_EXPIRY_SINCE", "").strip()
BATCH_LIMIT = int(os.environ.get("NONWORK_EXPIRY_BATCH", "500"))
LAKE_BUCKET = os.environ.get("S3_BUCKET", "")

# Distinguishes an automatic expiry from a human's "confirm personal" in the
# same table. Anything reading redactions can tell the two apart, and a future
# policy change can find exactly the rows this sweep wrote.
REASON = "non_work_auto_expiry"

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def parse_since(raw):
    """The floor, as an aware UTC datetime, or None when unset/unparseable.
    Accepts a date (2026-08-04) or a full ISO timestamp. None means the sweep
    refuses to run — see the module docstring."""
    if not raw:
        return None
    try:
        if len(raw) == 10:
            d = datetime.date.fromisoformat(raw)
            return datetime.datetime(d.year, d.month, d.day, tzinfo=datetime.timezone.utc)
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
            datetime.timezone.utc)
    except ValueError:
        return None


def expire_batch(conn, *, now, since, buffer_hours, limit):
    """Tombstone + de-index one batch. Returns a counts dict.

    Each topic is independent: a failure to enqueue the reindex leaves the
    tombstone in place (the topic is already out of every company-tier read,
    which is the privacy-critical half) and is counted, not raised. Raising
    would abort the batch and leave the rest of the day's topics exposed —
    the opposite of what this sweep is for."""
    cutoff = now - datetime.timedelta(hours=buffer_hours)
    rows = topics.list_expired_non_work(
        conn, older_than=cutoff, created_since=since, limit=limit)

    counts = {"scanned": len(rows), "tombstoned": 0, "reindex_failed": 0}
    for r in rows:
        redactions.create_redaction(
            conn, r["company_id"], r["id"], REASON,
            # No actor: the policy did this, not a person. actor_role records
            # which agent, so the audit still reads unambiguously.
            None, "system", scope="analysis")
        counts["tombstoned"] += 1
        try:
            reindex.enqueue_topic_reindex(
                s3(), LAKE_BUCKET, conn, r["id"], r["folder_name"], str(r["report_date"]))
        except Exception:
            counts["reindex_failed"] += 1
            logger.exception("nonwork-expiry: reindex enqueue failed for topic %s "
                             "(tombstone kept)", r["id"])
    return counts


def lambda_handler(event, context):
    if not ENABLED:
        logger.info("nonwork-expiry: disabled (NONWORK_EXPIRY_ENABLED != true)")
        return {"status": "disabled"}

    since = parse_since(SINCE)
    if since is None:
        logger.warning("nonwork-expiry: NONWORK_EXPIRY_SINCE is unset or unparseable "
                       "(%r) — refusing to run. Set it to the date the policy starts; "
                       "without a floor this would tombstone every historical "
                       "non_work topic.", SINCE)
        return {"status": "no_floor"}

    # Imported inside the handler, as the other in-VPC sweeps do — module
    # import time is before the VPC network is usable.
    from db.connection import get_connection

    now = datetime.datetime.now(datetime.timezone.utc)
    with get_connection() as conn:
        counts = expire_batch(conn, now=now, since=since,
                              buffer_hours=BUFFER_HOURS, limit=BATCH_LIMIT)

    logger.info("nonwork-expiry: scanned=%s tombstoned=%s reindex_failed=%s "
                "(buffer=%sh, since=%s)", counts["scanned"], counts["tombstoned"],
                counts["reindex_failed"], BUFFER_HOURS, since.isoformat())
    return {"status": "ok", **counts}
