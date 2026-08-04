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
    "rolling_summary_version, segment_count, word_count, created_at, updated_at, "
    "group_id"
)


def ensure_open(conn, session_id, company_id, user_id, site_id, kind, opened_at,
                group_id=None) -> dict:
    """Create the session if new, else keep it. Idempotent — safe whether the
    device's best-effort record-start signal or the first uploaded chunk reaches
    the backend first (either may open the session). Only fills `opened_at`/
    `site_id` if not already set; never regresses status.

    `group_id` is the LEAD device's session_id (multi-device merge, spec
    2026-08-04); NULL for a solo recording, which is every pre-existing row.
    Like the other columns here it is only ever filled in, never cleared: /open
    is best-effort and may legitimately arrive twice, and a second call that
    omits the group must not orphan a device that already joined."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO meeting_session "
        f"(session_id, company_id, user_id, site_id, kind, opened_at, status, group_id) "
        f"VALUES (%s, %s, %s, %s, %s, %s, 'open', %s) "
        f"ON CONFLICT (session_id) DO UPDATE SET "
        f"opened_at = COALESCE(meeting_session.opened_at, EXCLUDED.opened_at), "
        f"site_id = COALESCE(meeting_session.site_id, EXCLUDED.site_id), "
        f"group_id = COALESCE(meeting_session.group_id, EXCLUDED.group_id), "
        f"updated_at = now() "
        f"RETURNING {_COLS}",
        (session_id, company_id, user_id, site_id, kind, opened_at, group_id),
    ).fetchone()


def list_group_members(conn, group_id) -> list[dict]:
    """Every session in one multi-device group, oldest first.

    The lead is its own member — it sets its group_id to its own session_id —
    so this returns the whole meeting, not just the joiners."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM meeting_session WHERE group_id = %s "
        f"ORDER BY opened_at NULLS FIRST, session_id",
        (group_id,),
    ).fetchall()


def group_is_settled(conn, group_id, idle_grace_seconds) -> bool:
    """True when no member of the group could still be recording: each one is
    either terminal (sent/failed) or has gone quiet past the idle grace.

    Deliberately reuses the SAME idle judgement a solo session uses rather than
    inventing a multi-device window. A group must not outlive the sessions
    inside it, and a second timeout concept is a second thing to get wrong. The
    case this exists for is real and common: an inspector forgets to press
    stop, or walks off and syncs hours later — that must not hold everyone
    else's report hostage."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT COUNT(*) AS unsettled FROM meeting_session "
        "WHERE group_id = %s "
        "AND status NOT IN ('sent','failed') "
        "AND COALESCE(last_segment_at, opened_at, created_at) "
        "    > now() - make_interval(secs => %s)",
        (group_id, idle_grace_seconds),
    ).fetchone()
    return int((row or {}).get("unsettled") or 0) == 0


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


def list_due_finalize(conn, idle_grace_seconds) -> list[dict]:
    """Sessions whose grace has elapsed and are ready to finalize: still
    `pending_close`, and either a deliberate End (grace 0 — due the moment it
    closed) or an idle stop whose `closed_at` is now older than the grace window.
    Returns [{session_id, version}] — the (id, version) the scheduled sweeper
    CAS-claims against (claim_finalize no-ops if a resume bumped version in between,
    so reading the current version here is safe)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT session_id, version FROM meeting_session "
        "WHERE status = 'pending_close' "
        "AND (close_intent = 'end' OR closed_at <= now() - make_interval(secs => %s))",
        (idle_grace_seconds,),
    ).fetchall()


def list_idle_open(conn, idle_seconds) -> list[dict]:
    """Sessions still `open` whose last activity — `last_segment_at` (server time,
    advanced by touch_segment on every chunk), or `opened_at` if no chunk has
    touched yet — is older than `idle_seconds`. The device stopped (or crashed)
    without a `/close`, so the server INFERS close (spec §8.4: "if close is
    missing, infer close when the session sees no new chunk for
    SESSION_GAP_MINUTES"). Returns [{session_id, version, last_activity}] — the
    sweep mark_pending_close's each at `last_activity` with intent 'idle'. Keys on
    `last_segment_at`, never `opened_at` alone, so a long ACTIVE meeting (chunks
    still arriving) is never mistaken for idle. A row whose activity time is NULL
    (never opened with a timestamp) is excluded — nothing to anchor a close on."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT session_id, version, "
        "COALESCE(last_segment_at, opened_at) AS last_activity "
        "FROM meeting_session WHERE status = 'open' "
        "AND COALESCE(last_segment_at, opened_at) <= now() - make_interval(secs => %s)",
        (idle_seconds,),
    ).fetchall()


def list_finalizing(conn) -> list[dict]:
    """Sessions the sweep has claimed (status='finalizing'). The reconcile pass moves
    each to sent/failed once the non-VPC send worker records its outcome (that worker
    can't touch Aurora itself — CLAUDE.md BUG-36)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT session_id, version FROM meeting_session WHERE status = 'finalizing'",
    ).fetchall()


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
