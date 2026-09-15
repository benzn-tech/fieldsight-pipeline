"""A day's recorded segments, one row per (user, date). See migration 0056.

Written only by lambda_recording_segments; read only by org-api's GET /sessions.
Repositories never commit -- the caller owns the transaction.
"""
import json

from psycopg.rows import dict_row

_COLS = "user_id, report_date, folder_name, segments, source_object_count, dirty, computed_at"


def get(conn, user_id, report_date) -> dict | None:
    """The stored row, with `segments` always a list, or None when the day was never computed."""
    row = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM day_recording_segments WHERE user_id = %s AND report_date = %s",
        (str(user_id), report_date),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row["segments"], str):     # psycopg returns jsonb as str on some paths
        row["segments"] = json.loads(row["segments"])
    return row


def upsert_monotonic(conn, user_id, report_date, folder_name, segments,
                     source_object_count) -> bool:
    """Write the day's segments unless the stored row was computed from MORE objects.

    Returns True when the row was written (and `dirty` cleared), False when an older,
    smaller listing was refused. A refused write leaves `dirty` as it was on purpose:
    clearing it would drop the mark a newer transcript left.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO day_recording_segments "
        "(user_id, report_date, folder_name, segments, source_object_count, dirty, computed_at) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, false, now()) "
        "ON CONFLICT (user_id, report_date) DO UPDATE SET "
        "folder_name = EXCLUDED.folder_name, "
        "segments = EXCLUDED.segments, "
        "source_object_count = EXCLUDED.source_object_count, "
        "dirty = false, "
        "computed_at = now() "
        "WHERE EXCLUDED.source_object_count >= day_recording_segments.source_object_count "
        "RETURNING user_id",
        (str(user_id), report_date, folder_name, json.dumps(segments),
         int(source_object_count)),
    ).fetchone()
    return row is not None


def mark_dirty(conn, user_id, report_date) -> bool:
    """Flag an existing row for the trailing pass. False when there is no row to flag."""
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE day_recording_segments SET dirty = true "
        "WHERE user_id = %s AND report_date = %s RETURNING user_id",
        (str(user_id), report_date),
    ).fetchone()
    return row is not None


def list_dirty(conn, limit=50) -> list[dict]:
    """Dirty rows, least recently computed first: [{user_id, report_date, folder_name}]."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT user_id, report_date, folder_name FROM day_recording_segments "
        "WHERE dirty ORDER BY computed_at, user_id LIMIT %s",
        (int(limit),),
    ).fetchall()


def deleted_group_session_ids(conn, session_ids) -> set[str]:
    """Which of these device session ids belong to a DELETED merged meeting.

    Deleting a multi-device meeting writes ONE tombstone, on the merged artifact's prefix:
    `extractions/{lead_folder}/{date}/grp{group_id}`. The group id IS the lead device's
    session id (migration 0031/0036; lambda_finalize_claim.group_merged_key), and every
    member's meeting_session row carries it in `group_id`. The lead carries no group_id of
    its own, and its row may not exist at all (its /open is best-effort), so a candidate
    equal to the group id is matched directly, without the join.

    Keyed on session ids, NOT on folder or date, on purpose. The tombstone names the LEAD's
    folder, so `deleted_source_prefixes(conn, folder, date)` -- narrowed to
    `%/{folder}/{date}/%` -- never shows it to a member's day, and a member may be a
    different person recording under a different folder. Device session ids are random
    32-hex values, so an id match cannot reach another company's recording.

    Reverted tombstones hide nothing. Returns a subset of `session_ids`.
    """
    ids = sorted({s for s in (session_ids or []) if s})
    if not ids:
        return set()
    rows = conn.cursor(row_factory=dict_row).execute(
        "WITH dead AS ("
        "  SELECT DISTINCT substring(target_key from '/grp([0-9a-f]{32})(\\.json)?$') AS gid "
        "  FROM redactions "
        "  WHERE target_type = 'recording' AND scope = 'deleted' "
        "  AND reverted_at IS NULL AND target_key LIKE '%%/grp%%'"
        ") "
        "SELECT DISTINCT c.sid FROM unnest(%s::text[]) AS c(sid) "
        "LEFT JOIN meeting_session m ON m.session_id = c.sid "
        "JOIN dead d ON d.gid IS NOT NULL AND (d.gid = c.sid OR d.gid = m.group_id)",
        (ids,),
    ).fetchall()
    return {r["sid"] for r in rows}
