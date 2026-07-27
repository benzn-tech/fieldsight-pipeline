"""repositories/meeting_session.py — the rolling session-lifecycle state machine
behind the voice-timeliness paradigm.

State machine (spec 2026-07-27-asr-switch-stop-continuity §3.2, §8.4):

    open --stop--> pending_close --grace elapsed / claim--> finalizing --> sent
      ^                  |
      +---- resume ------+   (a chunk arriving within the grace window; bumps
                              `version` so the scheduled finalize no-ops)

All writes are optimistic-lock guarded so a mis-touch (stop -> immediate resume)
never finalizes early and never double-sends. FakeConn-testable: every function
is a single `conn.cursor(row_factory=dict_row).execute(...)`.
"""
from psycopg.rows import dict_row

_COLS = (
    "session_id, company_id, user_id, site_id, kind, status, version, "
    "opened_at, last_segment_at, closed_at, close_intent, rolling_summary, "
    "rolling_summary_version, segment_count, word_count, created_at, updated_at"
)


def ensure_open(conn, session_id, company_id, user_id, site_id, kind, opened_at) -> dict:
    """Create the session if new, else keep it. Idempotent — safe whether the
    device's best-effort record-start signal or the first uploaded chunk reaches
    the backend first (either may open the session). Only fills `opened_at`/
    `site_id` if not already set; never regresses status."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO meeting_session "
        f"(session_id, company_id, user_id, site_id, kind, opened_at, status) "
        f"VALUES (%s, %s, %s, %s, %s, %s, 'open') "
        f"ON CONFLICT (session_id) DO UPDATE SET "
        f"opened_at = COALESCE(meeting_session.opened_at, EXCLUDED.opened_at), "
        f"site_id = COALESCE(meeting_session.site_id, EXCLUDED.site_id), "
        f"updated_at = now() "
        f"RETURNING {_COLS}",
        (session_id, company_id, user_id, site_id, kind, opened_at),
    ).fetchone()


def touch_segment(conn, session_id, at) -> dict | None:
    """A chunk arrived. Advance last_segment_at + segment_count. If the session
    was `pending_close`, this arrival is a RESUME within the grace window: flip
    back to `open`, clear the close, and bump `version` so the scheduled finalize
    no-ops. All SET expressions read the OLD row, so the version bump and status
    flip both key off the pre-update status."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET "
        f"last_segment_at = GREATEST(COALESCE(last_segment_at, %s), %s), "
        f"segment_count = segment_count + 1, "
        f"version = CASE WHEN status = 'pending_close' THEN version + 1 ELSE version END, "
        f"closed_at = CASE WHEN status = 'pending_close' THEN NULL ELSE closed_at END, "
        f"close_intent = CASE WHEN status = 'pending_close' THEN NULL ELSE close_intent END, "
        f"status = CASE WHEN status = 'pending_close' THEN 'open' ELSE status END, "
        f"updated_at = now() "
        f"WHERE session_id = %s RETURNING {_COLS}",
        (at, at, session_id),
    ).fetchone()


def mark_pending_close(conn, session_id, closed_at, intent) -> dict | None:
    """Stop signal received -> `pending_close`; bump `version` (the grace timer is
    scheduled against the returned version). No-op if already finalizing/sent."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET "
        f"status = 'pending_close', closed_at = %s, close_intent = %s, "
        f"version = version + 1, updated_at = now() "
        f"WHERE session_id = %s AND status IN ('open', 'pending_close') "
        f"RETURNING {_COLS}",
        (closed_at, intent, session_id),
    ).fetchone()


def claim_finalize(conn, session_id, expected_version) -> dict | None:
    """CAS: `pending_close` -> `finalizing` ONLY if `version` still equals what the
    grace timer was scheduled against. Returns the row when claimed; returns None
    when a resume bumped the version (mis-touch) or the session already moved on —
    the scheduled finalize then simply no-ops. This is the idempotency guard."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET status = 'finalizing', updated_at = now() "
        f"WHERE session_id = %s AND version = %s AND status = 'pending_close' "
        f"RETURNING {_COLS}",
        (session_id, expected_version),
    ).fetchone()


def mark_sent(conn, session_id) -> dict | None:
    """Confirmation email delivered — close the session out."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET status = 'sent', updated_at = now() "
        f"WHERE session_id = %s RETURNING {_COLS}",
        (session_id,),
    ).fetchone()


def mark_failed(conn, session_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET status = 'failed', updated_at = now() "
        f"WHERE session_id = %s RETURNING {_COLS}",
        (session_id,),
    ).fetchone()


def update_rolling_summary(conn, session_id, summary, expected_version) -> dict | None:
    """CAS on `rolling_summary_version` so concurrent Tier-1 refines don't clobber
    each other (spec §3.1 strong-consistency requirement). Returns the row when
    applied, None on a version race (caller re-reads and retries)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET rolling_summary = %s, "
        f"rolling_summary_version = rolling_summary_version + 1, updated_at = now() "
        f"WHERE session_id = %s AND rolling_summary_version = %s "
        f"RETURNING {_COLS}",
        (summary, session_id, expected_version),
    ).fetchone()


def get(conn, session_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM meeting_session WHERE session_id = %s",
        (session_id,),
    ).fetchone()
