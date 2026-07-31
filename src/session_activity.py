"""session_activity.py — drive the meeting_session lifecycle from the CHUNK
STREAM (voice-timeliness spec 2026-07-27 §8.4).

The Tier-0 confirmation email needs a session that is (a) OPEN — so the recipient
+ site resolve — and (b) eventually CLOSED — so the grace elapses and it
finalizes. Today the ONLY things that open/close a session are the device's
best-effort live `POST /sessions/{id}/open|close` (org-api). Per §8.4 those are an
optimization, never a requirement: "start = first chunk; if close is missing,
infer close when the session sees no new chunk for SESSION_GAP_MINUTES."

This module is the OPEN + TOUCH half of that durable path. On every transcript
chunk it:
  * OPENs the session from the stream (idempotent ensure_open — a later, or an
    earlier, live `/open` never regresses it), and
  * TOUCHes it at SERVER time so `last_segment_at` tracks server-observed activity
    (robust to device clock skew, BUG-37) — the sweep's inferred idle-close keys
    off that (repositories/meeting_session.list_idle_open).
A late resume chunk flips a `pending_close` session back to `open` + bumps
`version`, so any stale scheduled finalize no-ops (the §8.4 idempotency guard,
finally exercised).

In-VPC (touches Aurora); SEPARATE from the non-VPC rolling summary (BUG-36),
though both trigger on the same `transcripts/` arrival. Pure core takes injected
conn + resolvers + now for unit tests; lambda_handler wires the real ones.
"""
import logging
import re
from urllib.parse import unquote_plus

from repositories import meeting_session

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_TRANSCRIPT_KEY_RE = re.compile(r"^transcripts/([^/]+)/([^/]+)/(.+)$")


def _kind_from_key(basename):
    """Best-effort audio|video from the chunk's `_src{ext}` token, else None.
    kind is informational on meeting_session (finalize doesn't depend on it); a
    live /open with an explicit kind is authoritative on first insert anyway."""
    low = basename.lower()
    if "srcmp4" in low or ".mp4" in low:
        return "video"
    if "srcwav" in low or "srcm4a" in low or ".wav" in low or ".m4a" in low:
        return "audio"
    return None


def process_transcript_key(conn, key, *, resolve_company, resolve_user, now):
    """A transcript chunk landed. If its key carries a device session (`sid{32hex}`),
    ensure_open the session (opened_at = the chunk's device wall-clock start, T1)
    and touch_segment it (at = `now`, SERVER time). Returns the 32-hex session id it
    opened/touched, or None when the key isn't a device-session transcript (legacy/
    whole-file — no sid) or the company can't be resolved. resolve_company(conn,
    folder) / resolve_user(conn, company_id, folder) are injected."""
    from transcript_utils import (extract_base_time_from_filename,
                                  extract_session_id_from_filename)
    sid = extract_session_id_from_filename(key)
    if not sid:
        return None                                  # legacy/whole-file — no device session
    m = _TRANSCRIPT_KEY_RE.match(key)
    if not m:
        return None
    folder, _date, basename = m.group(1), m.group(2), m.group(3)
    company = resolve_company(conn, folder)
    if not company:
        return None
    company_id = company["id"]
    user_id = resolve_user(conn, company_id, folder)     # may be None (recorder unmapped)
    opened_at = extract_base_time_from_filename(basename)  # device wall-clock (T1); may be None
    meeting_session.ensure_open(
        conn, sid, company_id, user_id, None, _kind_from_key(basename), opened_at)
    meeting_session.touch_segment(conn, sid, now)         # server time -> skew-proof idle
    return sid


def _keys_from_event(event):
    """Transcript key(s) this invocation is about — accept both the EventBridge
    "Object Created" shape ({detail:{object:{key}}}, key already decoded) and an S3
    notification ({Records:[{s3:{object:{key}}}]}, key url-encoded), so the handler
    works under either trigger (mirrors lambda_rolling_summary)."""
    keys = []
    obj = (event.get("detail") or {}).get("object") or {}
    if obj.get("key"):
        keys.append(obj["key"])
    for record in event.get("Records", []):
        keys.append(unquote_plus(record["s3"]["object"]["key"]))
    return keys


def lambda_handler(event, context):
    """A transcript landed under transcripts/ — open + touch its device session so
    the finalize works from the durable chunk stream, not just the device's live
    /open|/close. In-VPC. Best-effort per key: one key failing never sinks the
    batch (this only sharpens finalize timing; it must never block the pipeline)."""
    from datetime import datetime

    import lambda_ingest
    from db.connection import get_connection
    touched = []
    with get_connection() as conn:
        now = datetime.utcnow()
        for key in _keys_from_event(event):
            try:
                sid = process_transcript_key(
                    conn, key, resolve_company=lambda_ingest.resolve_company,
                    resolve_user=lambda_ingest.resolve_user, now=now)
                if sid:
                    touched.append(sid)
            except Exception:
                logger.exception("session_activity: failed for %s", key)
    # Log only when it did something (fires on every transcript — no per-idle noise),
    # so prod can trace which sessions the chunk stream opened/touched.
    if touched:
        logger.info("session_activity: opened/touched %d session(s): %s",
                    len(touched), ", ".join(sorted(set(touched))))
    return {"touched": touched}
