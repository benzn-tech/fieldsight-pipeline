"""extraction_pending/{sessionBase}.json -- a FINAL extraction that has not succeeded yet.

Written by extract-session when the model call fails on every model in the chain,
re-driven by the scheduled extraction-backlog lambda (re-put the original request),
read by the finalize send worker (so the recorder is told the truth), and deleted by
extract-session when a final pass succeeds. Spec 2026-10-07 D4/D5.

Fields: userFolder, date, sessionBase, request_key (the S3 key whose notification
triggered the failed pass -- re-putting it fires the same trigger), attempts (re-drives
done so far), first_failed_at, last_failed_at, last_error, next_attempt_at, expedite
(set by the web's Expedite button, D6), error_email_sent_at (set once the "your notes
are delayed" email went out; its presence is what makes the later success send the
notes email once).

Pure helpers over an injected boto3 S3 client, so every lambda that touches the marker
shares one definition of the key, the backoff and the timestamp format.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PENDING_PREFIX = "extraction_pending/"
#: Minutes to wait after failure number N (N = re-drives done so far). The last entry
#: repeats forever: a session is never given up on.
BACKOFF_MINUTES = (5, 15, 30, 60)
#: A marker older than this counts toward the ExtractionPending metric (a blip that
#: heals on the first re-drive must not page anyone).
ALARM_AGE_MINUTES = 30


def marker_key(session_base):
    return f"{PENDING_PREFIX}{session_base}.json"


def backoff_minutes(attempts):
    return BACKOFF_MINUTES[min(max(int(attempts or 0), 0), len(BACKOFF_MINUTES) - 1)]


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _missing(e):
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404")


def read(s3, bucket, session_base):
    """The marker, or None when there is none. Any other failure raises: an unreadable
    marker must not be mistaken for an absent one."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=marker_key(session_base))
    except Exception as e:
        if _missing(e):
            return None
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


def write(s3, bucket, marker):
    s3.put_object(Bucket=bucket, Key=marker_key(marker["sessionBase"]),
                  Body=json.dumps(marker, ensure_ascii=False),
                  ContentType="application/json")


def record_failure(s3, bucket, *, user_folder, date, session_base, request_key, error,
                   now=None):
    """Create the marker, or on a repeat failure only refresh last_error/last_failed_at:
    the schedule (attempts, next_attempt_at) belongs to the re-driver, and a failure that
    IS a re-drive's outcome must not push its own next attempt back to the first rung."""
    now = now or datetime.now(timezone.utc)
    try:
        marker = read(s3, bucket, session_base)
    except Exception:
        logger.exception("extraction_pending: cannot read existing marker for %s", session_base)
        marker = None
    if marker is not None and marker.get("promised_only"):
        # The recorder was told "notes are on the way" before any extraction had failed
        # (finalize's backstop, no marker). The extraction has now failed for real: this
        # becomes a full marker -- keeping error_email_sent_at, so the recovery still
        # sends the follow-up -- and the re-driver takes it from here.
        promised_at = marker.get("error_email_sent_at")
        marker = None
    else:
        promised_at = None
    if marker is None:
        marker = {"userFolder": user_folder, "date": date, "sessionBase": session_base,
                  "request_key": request_key, "attempts": 0,
                  "first_failed_at": iso(now),
                  "next_attempt_at": iso(now + timedelta(minutes=backoff_minutes(0))),
                  "expedite": False}
        if promised_at:
            marker["error_email_sent_at"] = promised_at
    marker["last_failed_at"] = iso(now)
    marker["last_error"] = str(error)[:500]
    write(s3, bucket, marker)
    return marker


def clear(s3, bucket, session_base):
    s3.delete_object(Bucket=bucket, Key=marker_key(session_base))


def mark_error_email_sent(s3, bucket, session_base, now=None):
    """Record that the delayed-notes email went out. False when the marker is already
    gone (the extraction recovered meanwhile)."""
    marker = read(s3, bucket, session_base)
    if marker is None:
        return False
    marker["error_email_sent_at"] = iso(now or datetime.now(timezone.utc))
    write(s3, bucket, marker)
    return True


def promise_notes(s3, bucket, session_base, now=None):
    """The recorder was told their notes are on the way, and no failed extraction explains
    why (the rolling backstop fired with nothing to show). Stamp the marker key so the final
    extraction's success sends the follow-up notes email: an existing marker is simply
    stamped; otherwise a `promised_only` marker is created. It has no request_key, so the
    re-driver never touches it, and record_failure upgrades it if the extraction fails."""
    now = now or datetime.now(timezone.utc)
    marker = read(s3, bucket, session_base)
    if marker is None:
        marker = {"sessionBase": session_base, "promised_only": True,
                  "first_failed_at": iso(now)}
    marker["error_email_sent_at"] = iso(now)
    write(s3, bucket, marker)
    return marker


def list_markers(s3, bucket):
    """Every marker body. Unreadable ones are skipped with a warning."""
    out = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": PENDING_PREFIX}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents") or []:
            try:
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                m = json.loads(body.decode("utf-8"))
                if isinstance(m, dict) and m.get("sessionBase"):
                    out.append(m)
                else:
                    logger.warning("extraction_pending: malformed marker %s", obj["Key"])
            except Exception:
                logger.warning("extraction_pending: unreadable marker %s", obj["Key"],
                               exc_info=True)
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")
