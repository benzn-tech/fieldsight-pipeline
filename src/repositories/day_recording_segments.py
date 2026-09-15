"""A day's recorded segments, one row per (user, date). See migration 0056.

Written only by lambda_recording_segments; read only by org-api's GET /sessions.
Repositories never commit -- the caller owns the transaction.
"""
import json

from psycopg.rows import dict_row

_COLS = ("user_id, report_date, folder_name, segments, source_object_count, "
        "dirty, dirty_since, computed_at")


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
                     source_object_count, listed_at) -> bool:
    """Write the day's segments unless the stored row was computed from MORE objects.

    `listed_at` is an aware datetime taken immediately BEFORE the S3 LIST that produced
    `segments` started. The count rule is unchanged: a listing with fewer objects than
    the stored one is refused outright (see below). A write that is NOT refused clears
    `dirty`/`dirty_since` only when the mark predates that LIST (dirty_since IS NULL, or
    dirty_since <= listed_at) -- i.e. this LIST could have seen whatever raised the
    mark. Otherwise the segments/count/computed_at still land, but dirty/dirty_since are
    left exactly as stored: the mark was raised by something this LIST could not have
    observed, so the trailing pass must still revisit the day (I1 -- two concurrent
    computes racing a debounce mark).

    Returns True when the row was written, False when an older, smaller listing was
    refused. A refused write leaves dirty/dirty_since as they were on purpose: clearing
    them would drop the mark a newer transcript left -- EXCEPT (I1) that a refused
    write whose own `listed_at` is fresh enough to have seen the mark still clears it
    with a second statement below: the `WHERE` above means the whole UPDATE, CASE
    clauses included, never runs when the write is refused, so without this second
    statement a row that keeps losing the count race (SWEEP_LIMIT or more of them)
    would stay dirty forever, sort first in `list_dirty` by its stale `computed_at`,
    starve newer dirty days out of every sweep, and keep re-flagging the trailing
    pass's own flag (SWEEP_LIMIT reached every tick), which keeps connecting to Aurora
    every 5 minutes and defeats auto-pause.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO day_recording_segments "
        "(user_id, report_date, folder_name, segments, source_object_count, "
        "dirty, dirty_since, computed_at) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, false, NULL, now()) "
        "ON CONFLICT (user_id, report_date) DO UPDATE SET "
        "folder_name = EXCLUDED.folder_name, "
        "segments = EXCLUDED.segments, "
        "source_object_count = EXCLUDED.source_object_count, "
        "dirty = CASE WHEN day_recording_segments.dirty_since IS NULL "
        "             OR day_recording_segments.dirty_since <= %s "
        "             THEN false ELSE day_recording_segments.dirty END, "
        "dirty_since = CASE WHEN day_recording_segments.dirty_since IS NULL "
        "             OR day_recording_segments.dirty_since <= %s "
        "             THEN NULL ELSE day_recording_segments.dirty_since END, "
        "computed_at = now() "
        "WHERE EXCLUDED.source_object_count >= day_recording_segments.source_object_count "
        "RETURNING user_id",
        (str(user_id), report_date, folder_name, json.dumps(segments),
         int(source_object_count), listed_at, listed_at),
    ).fetchone()
    written = row is not None
    if not written:
        # I1: the write itself was refused (an older, smaller listing), but this call's
        # `listed_at` still records when ITS view of the day started, independently of
        # whether its segments landed. If that view is fresh enough to have seen
        # whatever raised the current mark, clear the mark now -- segments, count and
        # computed_at are left exactly as stored; only dirty/dirty_since move.
        conn.execute(
            "UPDATE day_recording_segments SET dirty = false, dirty_since = NULL "
            "WHERE user_id = %s AND report_date = %s AND dirty AND dirty_since <= %s",
            (str(user_id), report_date, listed_at),
        )
    return written


def mark_dirty(conn, user_id, report_date) -> bool:
    """Flag an existing row for the trailing pass. False when there is no row to flag.

    dirty_since keeps the LATEST mark (I2, corrected from the original EARLIEST-mark
    rule, which was backwards). upsert_monotonic's clearing test is "did this write's
    `listed_at` (taken before its LIST started) happen at or after dirty_since" -- i.e.
    could this write's view have seen whatever raised the mark. The mark that question
    needs is the newest one: mark1 at t1, a LIST starting at t2 > t1, then mark2 at
    t3 > t2 for an object written AFTER that LIST began. Keeping the earliest mark
    (the old COALESCE(dirty_since, now()) rule) would leave dirty_since == t1, and the
    write's listed_at == t2 >= t1 would clear the row -- losing mark2's transcript with
    the row reading clean, even though this write's LIST could not have seen it.
    Keeping the latest mark (t3 > t2) correctly leaves the row dirty for that write.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE day_recording_segments SET dirty = true, "
        "dirty_since = now() "
        "WHERE user_id = %s AND report_date = %s RETURNING user_id",
        (str(user_id), report_date),
    ).fetchone()
    return row is not None


def db_now(conn):
    """The database's own clock (M1). `listed_at` is compared against `dirty_since`,
    which mark_dirty stamps with Aurora's `now()` -- comparing an Aurora timestamp
    against the Lambda host's wall clock makes I1/I2's ordering only as reliable as
    clock sync between the two machines. Taking `listed_at` from this same connection
    instead (autocommit, so this is statement time, not a frozen transaction snapshot)
    removes that dependency entirely. A small seam so tests can inject a clock without
    a real connection.
    """
    return conn.execute("SELECT now()").fetchone()[0]


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


def group_member_session_ids(conn, lead_sids) -> set[str]:
    """The lead ids themselves plus every device session_id merged under one of them.

    Companion to `deleted_group_session_ids`, but for EXCLUSION rather than deletion:
    a merged meeting's `grp{lead_sid}` extraction base names no device session directly
    (recording_blocks §5.4 fix round 1, I1) -- a caller that has decided a `grp{lead_sid}`
    base should be hidden (e.g. every one of its topics was redacted/non_work) still needs
    every device session id it covers, lead included, to exclude the right segments. No
    tombstone condition here on purpose -- this is not about what was deleted, only about
    what group membership IS, right now.
    """
    leads = sorted({s for s in (lead_sids or []) if s})
    if not leads:
        return set()
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT session_id FROM meeting_session WHERE group_id = ANY(%s)",
        (leads,),
    ).fetchall()
    return set(leads) | {r["session_id"] for r in rows}
