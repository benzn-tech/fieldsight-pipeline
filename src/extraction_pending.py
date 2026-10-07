"""extraction_pending/{sessionBase}.json -- a FINAL extraction that has not succeeded yet.

Written by extract-session when the model call fails on every model in the chain,
re-driven by the scheduled extraction-backlog lambda (re-put the original request),
read by the finalize send worker (so the recorder is told the truth), and deleted by
extract-session when a final pass succeeds. Spec 2026-10-07 D4/D5.

Fields: userFolder, date, sessionBase, request_key (the S3 key whose notification
triggered the failed pass -- re-putting it fires the same trigger), attempts (re-drives
done so far), first_failed_at, last_failed_at, last_error, next_attempt_at, expedite
(set by the web's Expedite button, D6; the re-driver clears it once it has re-driven),
started_at / ended_at / time_range (NZ wall clock of the session, from the transcript
names), expedite_requested_at / expedite_by / expedite_by_name / expedite_by_company /
expedite_notified_at (D6: org-api writes the first four, the backlog lambda stamps the
last after telling the alert topic), error_email_sent_at (set once the "your notes
are delayed" email went out; its presence is what makes the later success send the
notes email once).

Pure helpers over an injected boto3 S3 client, so every lambda that touches the marker
shares one definition of the key, the backoff and the timestamp format.
"""
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

PENDING_PREFIX = "extraction_pending/"
#: Minutes to wait after failure number N (N = re-drives done so far). The last entry
#: repeats forever: a session is never given up on.
BACKOFF_MINUTES = (5, 15, 30, 60)
#: A marker older than this counts toward the ExtractionPending metric (a blip that
#: heals on the first re-drive must not page anyone).
ALARM_AGE_MINUTES = 30
#: After this long of failing, a model-kind marker is re-driven only every
#: LATE_BACKOFF_MINUTES: a vendor that has been down for a day is not helped by an hourly
#: full-chain run on every stuck session (cost, and the herd when it comes back).
LATE_AFTER_HOURS = 24
LATE_BACKOFF_MINUTES = 360
#: failure_kind "model": the model call failed on every model (an outage; re-driven forever).
#: failure_kind "parse": a model answered but the answer was unusable JSON (truncated,
#: fenced, malformed); a re-drive may land on a better answer, so it is tried through the
#: full chain, but only PARSE_REDRIVE_MAX times -- then `gave_up_at` is set and the
#: re-driver stops. The marker stays (alarm, banner); Expedite allows one more re-drive.
KIND_MODEL = "model"
KIND_PARSE = "parse"
PARSE_REDRIVE_MAX = 6
#: A `promised_only` marker (the recorder was told "notes on the way", nothing failed)
#: that is still there after this long was never resolved: it is deleted, loudly.
PROMISED_ONLY_TTL_HOURS = 24
#: Re-drive limits per backlog tick, and the spread added to every new next_attempt_at.
REDRIVE_PER_TICK = 20
JITTER_MAX_SECONDS = 120
#: extraction_empty/{sessionBase}.json: a FINAL pass found no usable speech (zero
#: transcript turns). A success, not a failure: finalize reads it to say, truthfully,
#: "Nothing was captured for this recording."
EMPTY_PREFIX = "extraction_empty/"


def marker_key(session_base):
    return f"{PENDING_PREFIX}{session_base}.json"


def backoff_minutes(attempts):
    return BACKOFF_MINUTES[min(max(int(attempts or 0), 0), len(BACKOFF_MINUTES) - 1)]


def empty_key(session_base):
    return f"{EMPTY_PREFIX}{session_base}.json"


def next_delay_minutes(marker, attempts, now):
    """Minutes until the next re-drive: the 5/15/30/60 ladder, then every 6 h once the
    marker has been failing for LATE_AFTER_HOURS."""
    first = parse_iso(marker.get("first_failed_at"))
    if first and now - first >= timedelta(hours=LATE_AFTER_HOURS):
        return LATE_BACKOFF_MINUTES
    return backoff_minutes(attempts)


def jittered(dt, rng=None):
    """dt plus 0..JITTER_MAX_SECONDS, so markers that failed together do not come due
    together."""
    return dt + timedelta(seconds=(rng or random).uniform(0, JITTER_MAX_SECONDS))


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


def _code(e):
    return getattr(e, "response", {}).get("Error", {}).get("Code", "")


def update(s3, bucket, session_base, mutate):
    """Read-modify-write one marker with an S3 conditional write (If-Match on the ETag
    just read), one more read on a lost race. `mutate(marker)` edits the dict in place and
    returns False to abort (nothing is written). Returns the written marker, or None when
    the marker is gone (the extraction recovered meanwhile -- it must not be resurrected)
    or `mutate` aborted. A lost race twice raises.

    If the SDK or the fake predates If-Match (ParamValidationError) the write is a plain
    put, last-writer-wins -- the pre-existing behaviour, and a noted limitation."""
    key = marker_key(session_base)
    for _ in range(2):
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
        except Exception as e:
            if _missing(e):
                return None
            raise
        etag = obj.get("ETag")
        marker = json.loads(obj["Body"].read().decode("utf-8"))
        if mutate(marker) is False:
            return None
        body = json.dumps(marker, ensure_ascii=False)
        try:
            kw = {"IfMatch": etag} if etag else {}
            s3.put_object(Bucket=bucket, Key=key, Body=body,
                          ContentType="application/json", **kw)
        except Exception as e:
            if _code(e) in ("PreconditionFailed", "ConditionalRequestConflict"):
                continue
            if type(e).__name__ == "ParamValidationError":
                s3.put_object(Bucket=bucket, Key=key, Body=body,
                              ContentType="application/json")
            else:
                raise
        return marker
    raise RuntimeError(f"marker {session_base} changed twice while updating it")


_STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})")
_SPAN_RE = re.compile(r"_off(\d+(?:\.\d+)?)_to(\d+(?:\.\d+)?)")


def time_range_from_keys(keys):
    """(started_at, ended_at, "HH:MM–HH:MM") from the transcript keys of one session,
    or (None, None, None). The names carry `YYYY-MM-DD_HH-MM-SS` (the device's wall clock,
    NZ); a `_off{a}_to{b}` span adds seconds to it. The end is the latest span end when
    the names carry one, else the latest start. A session that starts and ends in the
    same minute reads "HH:MM"."""
    starts, ends = [], []
    for key in keys or []:
        name = str(key).rsplit("/", 1)[-1]
        m = _STAMP_RE.search(name)
        if not m:
            continue
        try:
            base = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y-%m-%d_%H-%M-%S")
        except ValueError:
            continue
        span = _SPAN_RE.search(name)
        if span:
            starts.append(base + timedelta(seconds=float(span.group(1))))
            ends.append(base + timedelta(seconds=float(span.group(2))))
        else:
            starts.append(base)
            ends.append(base)
    if not starts:
        return None, None, None
    first, last = min(starts), max(ends)
    a, b = first.strftime("%H:%M"), last.strftime("%H:%M")
    return (first.strftime("%Y-%m-%dT%H:%M:%S"), last.strftime("%Y-%m-%dT%H:%M:%S"),
            a if a == b else f"{a}–{b}")


def record_failure(s3, bucket, *, user_folder, date, session_base, request_key, error,
                   now=None, segment_keys=None, kind=KIND_MODEL):
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
    if not marker.get("time_range"):
        started, ended, label = time_range_from_keys(segment_keys)
        if label:
            marker.update(started_at=started, ended_at=ended, time_range=label)
    marker["last_failed_at"] = iso(now)
    marker["last_error"] = str(error)[:500]
    marker["failure_kind"] = kind
    if kind != KIND_PARSE:
        marker.pop("gave_up_at", None)      # an outage is never given up on
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
