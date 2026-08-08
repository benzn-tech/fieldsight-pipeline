"""session_group.py — per-group merge state for the multi-device merge.

Spec: docs/superpowers/specs/2026-08-08-group-merge-phase-c-design.md (Phase C).

One row per group, created when the FIRST JOINER joins — NOT columns on the
lead's meeting_session row. A lead may never upload (its /open is fire-and-forget
at record-start and a site is routinely offline then), a lead carries no group_id
of its own so a scan keyed on it cannot be bounded, and the merged artifact's key
must be written once rather than re-derived by three separate consumers.

All statements go through conn.cursor(row_factory=dict_row) rather than
conn.execute, matching every other repository here — the unit test doubles only
implement the cursor path.
"""
from psycopg.rows import dict_row

_COLS = ("group_id, company_id, merged_at, merge_count, merge_result, "
         "merged_key, created_at")


def ensure_row(conn, group_id, company_id) -> None:
    """Register a group. Idempotent.

    Called from the JOINER's /open, not the lead's: the group must be
    discoverable even if the lead never reaches the backend."""
    conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO session_group (group_id, company_id) VALUES (%s, %s) "
        "ON CONFLICT (group_id) DO NOTHING",
        (group_id, company_id),
    )


def get(conn, group_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM session_group WHERE group_id = %s",
        (group_id,),
    ).fetchone()


def list_due(conn, idle_grace_seconds) -> list[dict]:
    """Groups ready to merge. Asked on EVERY sweep tick.

    This is a STANDING scan, deliberately not a check hung on a finalize event.
    A group becomes mergeable at a moment when nothing is firing: on the tick
    that finalizes the last member, that member is still `finalizing` with a
    fresh last_segment_at, so the settled test is necessarily false; it becomes
    true a tick later when reconcile flips it to `sent`, and sweep() only
    iterates DUE sessions, of which there are then none. The first design asked
    at the one instant the answer could not be yes.

    `merge_result IS NULL` is on session_group itself so idx_session_group_pending
    bounds the scan — a resolved group leaves the candidate set for good.

    Membership is `group_id = g.group_id OR session_id = g.group_id` in both
    subqueries: the LEAD carries no group_id of its own (the group id IS its
    session id), so testing group_id alone would exclude it, and a group whose
    lead is still recording would look settled.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT g.group_id, g.company_id, g.merged_at, g.merge_count, "
        "       g.merge_result, g.merged_key, g.created_at "
        "FROM session_group g "
        # Unresolved AND unclaimed. Both are needed and they mean different
        # things: merge_result is the terminal state, merged_at is "a merge is
        # in flight". Without the merged_at test a claimed group is returned on
        # every tick forever -- claim() then fails each time, so nothing breaks
        # loudly, but the candidate set never shrinks and the index that bounds
        # this scan stops bounding it.
        #
        # This is also what makes rearm() meaningful: clearing merged_at is
        # exactly how a late arrival puts its group back in front of the scan.
        "WHERE g.merge_result IS NULL AND g.merged_at IS NULL "
        # Some member actually recorded. segment_count is maintained by
        # touch_segment, so this stays one indexed query and never lists S3.
        "AND EXISTS (SELECT 1 FROM meeting_session m "
        "            WHERE (m.group_id = g.group_id OR m.session_id = g.group_id) "
        "            AND m.segment_count > 0) "
        # Settled: every member terminal, or gone quiet past the grace. The same
        # idle judgement a solo session uses -- a group must not outlive the
        # sessions inside it, and a second timeout concept is a second thing to
        # get wrong.
        "AND NOT EXISTS (SELECT 1 FROM meeting_session m "
        "            WHERE (m.group_id = g.group_id OR m.session_id = g.group_id) "
        "            AND m.status NOT IN ('sent','failed') "
        "            AND COALESCE(m.last_segment_at, m.opened_at, m.created_at) "
        "                > now() - make_interval(secs => %s)) "
        "ORDER BY g.created_at",
        (idle_grace_seconds,),
    ).fetchall()


def claim(conn, group_id, merged_key) -> bool:
    """CAS the merge. True for exactly one caller.

    A second overlapping sweep tick gets False and does nothing, which is what
    stops a group merging -- and emailing every member -- twice.

    merge_count increments HERE, not on re-arm: two late members (or one late
    member's live and final passes) landing together would each re-arm, and
    counting there would burn the cap twice for a single merge.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE session_group SET merged_at = now(), merged_key = %s, "
        "merge_count = merge_count + 1 "
        "WHERE group_id = %s AND merged_at IS NULL "
        "RETURNING group_id",
        (merged_key, group_id),
    ).fetchone()
    return row is not None


def list_stuck(conn, stale_seconds) -> list[dict]:
    """Groups CLAIMED long enough ago that the merge cannot still be running.

    The gap this closes: `claim` sets merged_at, but the only thing that ever
    sets merge_result is item-writer, when the merged ARTIFACT lands. If
    extract_group returns None -- an LLM failure, a truncated response,
    unparseable JSON -- no artifact is written, so merge_result stays NULL
    forever while merged_at is set. `list_due` requires both to be NULL, so the
    group is never looked at again and the meeting silently never merges.

    `rearm` exists but its only caller is item-writer, which needs the very
    artifact that was never produced.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT group_id, merge_count FROM session_group "
        "WHERE merge_result IS NULL AND merged_at IS NOT NULL "
        "AND merged_at < now() - make_interval(secs => %s) "
        "ORDER BY merged_at",
        (stale_seconds,),
    ).fetchall()


def mark_result(conn, group_id, result) -> None:
    """Terminal state: 'merged', 'rejected' (span/company guard) or 'empty'
    (settled with nothing usable). Any of them removes the group from the
    standing scan.

    An operator clearing merge_result + merged_at puts it back, which is the
    recovery path for a company mismatch caused by fixable data (the BUG-41
    residue shows mis-companied legacy rows exist). A span rejection is
    genuinely permanent -- opened_at is server-side and immutable.
    """
    conn.cursor(row_factory=dict_row).execute(
        "UPDATE session_group SET merge_result = %s WHERE group_id = %s",
        (result, group_id),
    )


def rearm(conn, group_id) -> bool:
    """A late device brought genuinely new content: clear merged_at so the next
    standing scan re-merges.

    Conditional, so two late arrivals landing together re-arm once. Returns
    whether this call is the one that did it.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE session_group SET merged_at = NULL "
        "WHERE group_id = %s AND merged_at IS NOT NULL "
        "RETURNING group_id",
        (group_id,),
    ).fetchone()
    return row is not None
