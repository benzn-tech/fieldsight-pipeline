"""Session scoping — the shared parse behind #11 (meeting-scoped export).

Design: docs/superpowers/specs/2026-07-25-meeting-scoped-action-export.md §3.

A *session* is one press-record → stop on the device. The pipeline already
encodes that identity end to end and this module is the ONLY place that
parses it, so no read path re-implements the key shape:

    one source media file
      == one `session_base`                    (lambda_extract_session.session_base_from_key)
      == one lambda_extract_session extraction
      == one `extractions/{folder}/{date}/{session_base}.json` object
      == one distinct `topics.source_s3_key`   (stamped by lambda_item_writer)

So every extraction-sourced topic row ALREADY carries an exact, durable
session identifier — session scoping needs zero write-path work.

AUTHORITATIVE vs COSMETIC (the design's load-bearing correctness rule):

  * **Membership and `session_id` come from the S3 KEY.** Deterministic,
    written by the pipeline, never guessed.
  * **A session's START comes from `session_base` itself**, parsed with
    `transcript_utils.extract_base_time_from_filename` (BUG-01/BUG-11 safe).
    Deterministic — independent of the LLM.
  * **`time_range` is LLM free text.** Its `HH:MM – HH:MM` format is enforced
    only by the extraction prompt; `repositories/topics.py` is explicit that
    it is "a free-text display field, not sortable as a real range". It may
    LABEL a session's end. It must NEVER decide which session a topic is in.

Note on where the parse used to live: `lambda_extract_session.session_base_from_key`
derives session_base from a `transcripts/…` key (the INPUT side). `topics.
source_s3_key` holds the `extractions/…` key (the OUTPUT side), which that
function rejects by design. The reusable parse for the read side is therefore
`EXTRACTION_KEY_RE` — lifted here out of `lambda_item_writer` (which now
imports it back, same extraction pattern `photo_binding`/`keyframe_selection`
already followed) so there is exactly one definition of the key shape.

Pure module: no boto3, no psycopg, no env — importable from any lambda.
"""
import calendar
import re
from datetime import datetime, timedelta, timezone

from transcript_utils import extract_base_time_from_filename


def to_nz(dt):
    """A UTC datetime -> Pacific/Auckland LOCAL (naive), DST-aware. meeting_session
    opened_at/closed_at are stored UTC (the device's /open sends a UTC startedAt),
    but the topic times, S3 keys, and the legacy session_start() are all the device's
    NZ wall clock -- so a chunk session's start must be converted to NZ or the picker
    mixes a UTC start with an NZ end (e.g. "06:36 - 18:39" for an 18:36-18:39 meeting).
    Prefers the IANA tz db (exact); falls back to NZ's fixed rule (NZDT +13 from the
    last Sunday of September to the first Sunday of April, else NZST +12) so it never
    crashes if the runtime lacks tzdata."""
    if not hasattr(dt, "strftime"):
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    try:
        from zoneinfo import ZoneInfo
        return aware.astimezone(ZoneInfo("Pacific/Auckland")).replace(tzinfo=None)
    except Exception:
        u = aware.astimezone(timezone.utc).replace(tzinfo=None)
        y = u.year

        def _last_sun(mo):
            d = datetime(y, mo, calendar.monthrange(y, mo)[1])
            return d - timedelta(days=(d.weekday() - 6) % 7)

        def _first_sun(mo):
            d = datetime(y, mo, 1)
            return d + timedelta(days=(6 - d.weekday()) % 7)

        dst_start = _last_sun(9) + timedelta(hours=2) - timedelta(hours=12)
        dst_end = _first_sun(4) + timedelta(hours=3) - timedelta(hours=13)
        return u + timedelta(hours=13 if (u >= dst_start or u < dst_end) else 12)


def to_utc(dt):
    """The inverse of to_nz: a Pacific/Auckland LOCAL (naive) datetime -> UTC.

    The chunk filename carries the DEVICE's NZ wall clock, but meeting_session
    .opened_at is a UTC column -- meeting_session.py says so outright ("opened_at
    is stored UTC, and comparing the two raw is BUG-37"), the idle sweep and the
    joinable check compare it against the server's now(), and the day filter does
    `opened_at AT TIME ZONE 'UTC' AT TIME ZONE 'Pacific/Auckland'`. A writer that
    stores the wall clock raw therefore plants a value every reader then shifts
    AGAIN.

    Prod 2026-08-10: session_activity did exactly that for any session whose
    first artifact was a chunk rather than the app's /open call (ensure_open
    COALESCEs opened_at, so whichever writer lands first sets the convention
    permanently). A 12:32 NZ recording was stored as 12:32 "UTC";
    lambda_finalize_claim._to_nz added the offset again and produced 2026-08-11
    00:32, so the confirmation email was dated TOMORROW and its summary
    re-gather read an empty S3 prefix -- the recipient got "No summary was
    generated for this recording."

    Same DST rule and same tzdata-optional fallback as to_nz. During the one
    ambiguous hour each April the earlier (DST) reading is taken, matching
    ZoneInfo's fold=0 default -- an hour's error in a rare window, versus the
    12-hour error this function exists to remove.
    """
    if not hasattr(dt, "strftime"):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        from zoneinfo import ZoneInfo
        return (dt.replace(tzinfo=ZoneInfo("Pacific/Auckland"))
                  .astimezone(timezone.utc).replace(tzinfo=None))
    except Exception:
        y = dt.year

        def _last_sun(mo):
            d = datetime(y, mo, calendar.monthrange(y, mo)[1])
            return d - timedelta(days=(d.weekday() - 6) % 7)

        def _first_sun(mo):
            d = datetime(y, mo, 1)
            return d + timedelta(days=(6 - d.weekday()) % 7)

        # Boundaries expressed in LOCAL time (this input is local): NZDT runs
        # from 02:00 on the last Sunday of September to 03:00 on the first
        # Sunday of April.
        dst_start = _last_sun(9) + timedelta(hours=2)
        dst_end = _first_sun(4) + timedelta(hours=3)
        return dt - timedelta(hours=13 if (dt >= dst_start or dt < dst_end) else 12)

# Depth-exact: extractions/{user_folder}/{date}/{session_base}.json — a key
# nested any deeper (or shallower, or not ending in .json) is not this
# contract's shape and must be skipped rather than guessed at. Moved here
# from lambda_item_writer (which re-exports it) so the writer and the readers
# share ONE definition.
EXTRACTION_KEY_RE = re.compile(r"^extractions/([^/]+)/([^/]+)/([^/]+)\.json$")

# Display-level merge threshold for a meeting split across a stop/restart
# (design §3.3). DELIBERATELY the same 15 minutes as `SESSION_GAP_MINUTES` in
# the in-flight 2026-07-23 session-continuity design, which moves session
# assembly UPSTREAM (chunk uploads grouped by an inactivity gap, one
# session_base per meeting by construction). When that work ships it
# SUPERSEDES the merge below — it becomes a rarely-used safety net for legacy
# days — and it must import this constant rather than introduce a second
# literal, so the two definitions of "one session" converge instead of
# fighting.
SESSION_GAP_MINUTES = 15

# session_kind values (see session_ref).
KIND_EXTRACTION = "extraction"
KIND_REPORT = "report"
KIND_UNKNOWN = "unknown"


def parse_extraction_key(key):
    """`extractions/{folder}/{date}/{session_base}.json` ->
    (user_folder, date, session_base), or None for any other shape."""
    m = EXTRACTION_KEY_RE.match(key or "")
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def session_ref(source_s3_key):
    """(session_id, session_kind) for one `topics.source_s3_key`.

    Three genuinely different states — a caller (and the UI) must be able to
    tell "no session exists" from "we could not tell":

      * extraction key  -> (session_base, "extraction")  — a real session
      * `reports/…` key -> (None, "report")   — report-sourced: ONE key for
        the whole day, no session granularity exists in the data. The UI
        renders a single "Whole day" row; it must not fake boundaries.
      * anything else   -> (None, "unknown")  — missing/unrecognised key.
        We decline to claim either way.

    Only the basename is ever returned: the raw S3 key never leaves the
    backend through this path.
    """
    if not source_s3_key:
        return None, KIND_UNKNOWN
    parsed = parse_extraction_key(source_s3_key)
    if parsed is not None:
        return parsed[2], KIND_EXTRACTION
    if source_s3_key.startswith("reports/"):
        return None, KIND_REPORT
    return None, KIND_UNKNOWN


def session_id_from_source_key(source_s3_key):
    """session_base for an extraction key, else None. Thin alias of
    session_ref for callers that don't need the kind."""
    return session_ref(source_s3_key)[0]


# A chunk session's session_base is the bare `sid{32hex}` token — extract_session
# collapses every ~1-min chunk of one device session onto that single base. A
# legacy whole-file base is `{device}_{YYYY-MM-DD}_{HH-MM-SS}` and has no device
# session. This regex tells them apart on the read side.
_CHUNK_SESSION_BASE_RE = re.compile(r"^sid([0-9a-f]{32})$")


def device_session_id(session_base):
    """The 32-hex device session id for a chunk session's base (`sid{32hex}`),
    else None (a legacy whole-file base has no device session). This is exactly
    the key `POST /sessions/{id}/open` stores on meeting_session, so the writer
    can recover a chunk session's picked site from its extraction base."""
    if not session_base:
        return None
    m = _CHUNK_SESSION_BASE_RE.match(session_base)
    return m.group(1) if m else None


def session_start(session_id):
    """AUTHORITATIVE session start: the wall-clock time encoded in
    session_base itself (e.g. `Benl1_2026-07-25_13-05-12` -> 13:05:12), via
    the shared BUG-01-safe extractor. Naive local (device) wall clock — the
    same clock `time_range` labels are written in — never a UTC instant.
    None when session_base carries no parseable base time."""
    if not session_id:
        return None
    return extract_base_time_from_filename(session_id)


def assign_blocks(sessions):
    """Gap-merge fallback (design §3.3) — PURE, DISPLAY-LEVEL ONLY.

    `sessions` is an ordered-by-start list of dicts each carrying `_start_dt`
    (datetime, authoritative) and `_end_dt` (datetime or None, best-effort).
    Writes a 1-based `block` index onto each: session n+1 joins n's block iff
    n's end is KNOWN and `start(n+1) - end(n) <= SESSION_GAP_MINUTES`.

    An unknown end never auto-merges (conservative: the two sessions render
    adjacent and the user multi-selects). A session with no parseable start
    never merges either. Blocks are a picker grouping — the export request
    always carries explicit session ids, so a wrong merge costs the user one
    untick, never a silently wrong scope.
    """
    gap = timedelta(minutes=SESSION_GAP_MINUTES)
    block = 0
    prev_end = None                       # end of the IMMEDIATELY preceding session
    for s in sessions:
        start = s.get("_start_dt")
        if block == 0 or start is None or prev_end is None or (start - prev_end) > gap:
            block += 1
        s["block"] = block
        prev_end = s.get("_end_dt")
    return sessions
