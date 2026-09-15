"""lambda_recording_segments.py -- keep day_recording_segments current, in the background.

Design: docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md
§5.4 as amended by §11 F1, F4, F8, F9.

Two triggers, one function:
  * an EventBridge "Object Created" on `transcripts/` -- recompute that (folder, date);
  * a `rate(5 minutes)` schedule -- the trailing pass: recompute every dirty day.

On a transcript: resolve the folder to a user (never guess), then DEBOUNCE -- a day
computed less than DEBOUNCE_SECONDS ago is only marked dirty, because a recording
lands a transcript roughly every 30 s and each one would otherwise take a LIST and a
slot of account concurrency shared with finalize. The trailing pass picks the mark
up, so the last transcripts of a day are never lost to the debounce (F4).

THE TRAILING PASS MUST NOT KEEP AURORA AWAKE. SecondsUntilAutoPause is 600, and a
scheduled client that connects every 300 s would pin the cluster's floor 24/7
(tests/unit/test_sweep_cadence_vs_autopause.py). So the tick connects only when this
feature's own DynamoDB flag (sweep_state, key FLAG_KEY -- a separate item from the
finalize sweep's) says a day was marked dirty, plus once an hour unconditionally in
the safety window, which heals a flag write that was lost. sweep_state fails open.

Compute is one paginated LIST and arithmetic on names: no GetObject, no model call.
Times come only from transcript_utils (BUG-01/BUG-09) -- never a hand-written regex.

In-VPC (Aurora). Its other calls are S3 ListObjectsV2 and the DynamoDB flag, both
through the VPC's existing gateway endpoints. Anything else black-holes (BUG-36).

NOT on the finalize/email path (D5): SessionActivityFunction is untouched.

Every invocation logs one line saying what happened. A guard that passes silently
cannot be told apart from one that never ran.
"""
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import sweep_state
from repositories import day_recording_segments, users
from transcript_utils import (extract_base_time_from_filename,
                              extract_session_id_from_filename,
                              extract_vad_offsets_from_filename)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# A code constant, not a Parameter: it is a load-shedding interval, not a product
# threshold, and the trailing pass bounds its effect to one schedule period.
DEBOUNCE_SECONDS = 30
SWEEP_LIMIT = 50

S3_BUCKET = os.environ.get("S3_BUCKET", "")
STAGE = os.environ.get("STAGE", "prod")
# Passed to sweep_state as its "stage", so the item is SWEEP_STATE#RECORDING_BLOCKS#{stage}:
# this feature's flag can never wake the finalize sweep, nor be cleared by it.
FLAG_KEY = f"RECORDING_BLOCKS#{STAGE}"
# The five-minute window in which one tick per hour connects regardless of the flag.
# It contains the finalize sweep's SAFETY_SWEEP_MINUTE (7): both stages share one
# cluster, and unaligned hourly passes would shrink its idle window.
SAFETY_WINDOW_START_MINUTE = 5

# Structure of the KEY only (folder, date directory). Time of day is never read here.
_DAY_KEY_RE = re.compile(r"^transcripts/([^/]+)/(\d{4}-\d{2}-\d{2})/[^/]+$")


def _utcnow():
    return datetime.now(timezone.utc)


def keys_from_event(event):
    """Object keys this invocation is about, from either trigger shape.

    EventBridge "Object Created": {detail: {object: {key}}}, key already decoded.
    S3 notification: {Records: [{s3: {object: {key}}}]}, key url-encoded.
    Same contract as session_activity._keys_from_event, copied rather than imported so
    this function does not load a finalize-path module (D5).
    """
    keys = []
    obj = (event.get("detail") or {}).get("object") or {}
    if obj.get("key"):
        keys.append(obj["key"])
    for record in event.get("Records", []) or []:
        keys.append(unquote_plus(record["s3"]["object"]["key"]))
    return keys


def is_schedule_event(event):
    """The rate(5 minutes) trailing pass, as opposed to an object event."""
    return event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event"


def is_safety_tick(now):
    """Exactly one tick of a rate(5 minutes) schedule falls in this window each hour,
    whatever minute the schedule happens to be phased on."""
    return SAFETY_WINDOW_START_MINUTE <= now.minute < SAFETY_WINDOW_START_MINUTE + 5


def segment_from_key(key):
    """One transcript object's segment, or None when its name yields no base time.

    start = filename base time (seconds since midnight) + VAD offset start
    end   = start + (offset end - offset start)
    A whole-file transcript has no offsets, so its end equals its start: its length is
    not in its name and is not guessed. A batch (`_bn{K}`) reads as its first chunk,
    which is what batch_stitch.build_batch_name guarantees; its span is the audio kept,
    which can be shorter than the wall clock it covers -- immaterial at a 600 s gap.
    """
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    base = extract_base_time_from_filename(name)
    if base is None:
        return None
    off_start, off_end = extract_vad_offsets_from_filename(name)
    start = base.hour * 3600 + base.minute * 60 + base.second + off_start
    end = start + max(0.0, off_end - off_start)
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "session_id": extract_session_id_from_filename(name),
        "key": key,
    }


def list_day_keys(s3, bucket, folder, date):
    """Every object key under transcripts/{folder}/{date}/, across all pages."""
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"transcripts/{folder}/{date}/"):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    return keys


def compute_day(conn, s3, bucket, user_id, folder, date):
    """LIST the day, derive segments, write monotonically. Returns a summary dict.

    `listed_at` is taken right before the LIST starts (not after, and not at the top
    of handle_object_key) so it marks the earliest moment this call's view of the day
    could be stale -- what upsert_monotonic compares against dirty_since to decide
    whether this write's LIST could have seen whatever raised the mark (I1).
    """
    listed_at = _utcnow()
    keys = list_day_keys(s3, bucket, folder, date)
    segments = []
    skipped = 0
    for key in keys:
        seg = segment_from_key(key)
        if seg is None:
            skipped += 1
            continue
        segments.append(seg)
    if skipped:
        logger.info("recording_segments: %s/%s skipped %d object(s) with no base time",
                    folder, date, skipped)
    segments.sort(key=lambda s: (s["start"], s["key"]))
    written = day_recording_segments.upsert_monotonic(
        conn, user_id, date, folder, segments, len(keys), listed_at)
    return {"objects": len(keys), "segments": len(segments),
            "skipped": skipped, "written": written}


def handle_object_key(conn, s3, bucket, key, now):
    """One transcript landed. Returns the outcome: "ignored", "skipped-unresolved",
    "debounced-dirty" or "computed". `now` is an aware UTC datetime."""
    m = _DAY_KEY_RE.match(key)
    if not m:
        logger.info("recording_segments: ignored key=%s (not transcripts/{folder}/{date}/{file})",
                    key)
        return "ignored"
    folder, date = m.group(1), m.group(2)
    user = users.get_by_folder_name_global(conn, folder)
    if user is None:
        logger.warning("recording_segments: skipped-unresolved folder=%s date=%s "
                       "(no users row; not guessing)", folder, date)
        return "skipped-unresolved"
    row = day_recording_segments.get(conn, user["id"], date)
    if row is not None and (now - row["computed_at"]).total_seconds() < DEBOUNCE_SECONDS:
        marked = day_recording_segments.mark_dirty(conn, user["id"], date)
        # AFTER the row is marked (autocommit), so a sweep that reads the flag always
        # finds the row it was raised for.
        sweep_state.mark_pending(FLAG_KEY)
        if marked:
            logger.info("recording_segments: debounced-dirty folder=%s date=%s", folder, date)
        else:
            # M5: mark_dirty's return value was silently ignored -- a race where the
            # row was gone by the time the UPDATE ran (e.g. between get() and here)
            # still logged "debounced-dirty" as though the mark had landed.
            logger.warning("recording_segments: debounce mark found no row folder=%s date=%s "
                           "(row gone between get() and mark_dirty())", folder, date)
        return "debounced-dirty"
    result = compute_day(conn, s3, bucket, user["id"], folder, date)
    logger.info("recording_segments: computed folder=%s date=%s objects=%d segments=%d "
                "skipped=%d written=%s", folder, date, result["objects"],
                result["segments"], result["skipped"], result["written"])
    return "computed"


def sweep_dirty(conn, s3, bucket, limit=SWEEP_LIMIT):
    """The trailing pass. Recompute every dirty day; one failing day never stops the rest."""
    rows = day_recording_segments.list_dirty(conn, limit)
    done = 0
    for row in rows:
        try:
            result = compute_day(conn, s3, bucket, row["user_id"], row["folder_name"],
                                 row["report_date"])
            done += 1
            if not result["written"]:
                logger.info("recording_segments: sweep kept the larger stored row "
                            "folder=%s date=%s objects=%d", row["folder_name"],
                            row["report_date"], result["objects"])
        except Exception:
            logger.exception("recording_segments: sweep failed folder=%s date=%s",
                             row["folder_name"], row["report_date"])
    logger.info("recording_segments: swept %d of %d dirty day(s)", done, len(rows))
    if len(rows) >= limit:
        # M4: list_dirty is capped at `limit`, so a day left over past that cap was
        # never seen this tick. Without re-raising the flag, an idle tick right after
        # would read "not pending" and skip, stranding whatever did not fit.
        sweep_state.mark_pending(FLAG_KEY)
        logger.info("recording_segments: sweep limit (%d) reached; re-flagged for next tick",
                    limit)
    return done


def lambda_handler(event, context):
    import boto3

    from db.connection import get_connection

    s3 = boto3.client("s3")
    if is_schedule_event(event):
        now = _utcnow()
        if not is_safety_tick(now) and not sweep_state.is_pending(FLAG_KEY):
            # Must be logged: without this line the skip path is unverifiable.
            logger.info("recording_segments: sweep skipped (no dirty days flagged)")
            return {"swept": 0, "skipped": "no-pending"}
        with get_connection(autocommit=True) as conn:
            # Cleared only once the connection is open, immediately before listing
            # (I2): clearing it earlier meant a connect failure -- likely while Aurora
            # is still resuming -- or any exception before this point left the flag
            # cleared with nothing swept, and the next EventBridge retry would read
            # "not pending" and skip. Also BEFORE listing within this block: a day
            # marked while this pass runs raises the flag again and is picked up next
            # tick, instead of being cleared away unseen.
            sweep_state.clear_pending(FLAG_KEY)
            return {"swept": sweep_dirty(conn, s3, S3_BUCKET)}
    keys = keys_from_event(event)
    if not keys:
        logger.info("recording_segments: no object key in event; nothing to do")
        return {"outcomes": []}
    now = _utcnow()
    outcomes = []
    with get_connection(autocommit=True) as conn:
        for key in keys:
            try:
                outcomes.append(handle_object_key(conn, s3, S3_BUCKET, key, now))
            except Exception:
                logger.exception("recording_segments: failed for key=%s", key)
                outcomes.append("failed")
    return {"outcomes": outcomes}
