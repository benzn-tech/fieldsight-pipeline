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
import re
from datetime import timedelta

from transcript_utils import extract_base_time_from_filename

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
