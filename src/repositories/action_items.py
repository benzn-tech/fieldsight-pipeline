import uuid

import psycopg
from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate

# Whitelist of columns PATCH /api/org/action-items/{id} may set. text/site_id/
# topic_id/created_at are intentionally NOT here -- editing a task must never
# re-home it to another site (ACL bypass) or rewrite its body.
_EDITABLE = ("priority", "status", "deadline", "deadline_text", "responsible")
_RET = ("id, topic_id, site_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at, updated_at, updated_by")

# Display name of the last writer, for the "Checked by <name> · <time>" caption.
# updated_by holds a COGNITO SUB (migration 0017: `updated_by text`), not a
# users.id, so the join is on users.cognito_sub -- NOT users.id.
# NULLIF(TRIM(CONCAT_WS(' ', ...))) is the exact NULL/blank-safe form already
# used by content_edits.list_content_edits and topics.*: CONCAT_WS skips NULL
# parts, so an account with no last_name yields "Ada" (never "Ada "), and an
# account with neither name collapses to NULL instead of "". Do not hand-roll
# `first || ' ' || last` -- that yields NULL for a missing surname.
_UPDATED_BY_NAME = ("NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '') "
                    "AS updated_by_name")


def get_action_item(conn, action_item_id) -> dict | None:
    """One action item joined to its site's company_id (the tenant guard the
    handler checks against caller.company_id). Returns None if not found or
    action_item_id is not a valid UUID -- malformed id == missing (404
    semantics), same posture as observations.get_observation.

    Also carries topic_user_id: who RECORDED the topic this task came from.
    _is_assignee needs it to decide whether an UNASSIGNED task is the caller's
    own work. LEFT JOIN, because the topic row is not guaranteed to outlive the
    action item -- a missing topic yields NULL, which that predicate treats as
    "not mine" (fail-closed)."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT a.id, a.topic_id, a.site_id, a.text, a.responsible, a.deadline, "
            "a.deadline_text, a.priority, a.status, a.created_at, a.updated_at, "
            "a.updated_by, a.stable_id, s.company_id, t.user_id AS topic_user_id "
            "FROM action_items a JOIN sites s ON s.id = a.site_id "
            "LEFT JOIN topics t ON t.id = a.topic_id WHERE a.id=%s",
            (action_item_id,),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def list_for_carry_forward(conn, topic_ids, site_id) -> list[dict]:
    """Action items on a set of topics -- new OR retired -- shaped for Track B Task 4's
    carry_forward.match: `id`, `text`, `stable_id` plus a computed `human_touched`.
    Batched with ANY(%s), same pattern as findings.list_for_topics; scoped to `site_id`
    (Ruling R9) so a stray topic id can never pull another tenant's -- or another site's --
    rows into the match pool.

    human_touched = updated_by IS NOT NULL OR status <> 'open' -- either half of a person's
    footprint on the row: a status flip with no updated_by (a pre-0017 row, or a write that
    predates the audit column) must still count, and an updated_by with status still 'open'
    (priority/deadline edited without closing it) must too. For a freshly-inserted row
    (the "new" pool) this is always False -- exactly the answer wanted, since nothing has
    touched it yet.
    """
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, text, stable_id, status, priority, deadline, "
        "deadline_text, responsible, updated_by, updated_at, audience, item_id, "
        "(updated_by IS NOT NULL OR status <> 'open') AS human_touched "
        "FROM action_items WHERE topic_id = ANY(%s) AND site_id = %s",
        (list(topic_ids), site_id),
    ).fetchall()


def carry_identity(conn, new_id, old_row) -> None:
    """Carry `old_row`'s stable identity onto the new action item `new_id` that replaced it
    (Track B Task 4). Always moves stable_id/carried_from; when the old row was
    human-touched, also moves the fields a person can set on it (PATCH's own _EDITABLE, plus
    the updated_by/updated_at audit pair and audience) so a tick, a reassignment or a
    deadline edit survives the re-extraction that reworded the row's text around it. A row
    nobody touched carries only its identity -- its status/priority/etc stay whatever the
    fresh extraction just wrote, because there is nothing human to preserve."""
    if old_row.get("human_touched"):
        conn.execute(
            "UPDATE action_items SET stable_id=%s, carried_from=%s, status=%s, priority=%s, "
            "deadline=%s, deadline_text=%s, responsible=%s, updated_by=%s, updated_at=%s, "
            "audience=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], old_row["status"], old_row["priority"],
             old_row["deadline"], old_row["deadline_text"], old_row["responsible"],
             old_row["updated_by"], old_row["updated_at"], old_row["audience"], new_id),
        )
    else:
        conn.execute(
            "UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], new_id),
        )


def update_action_item_fields(conn, action_item_id, fields, updated_by) -> dict | None:
    """Whitelisted partial update + last-writer audit (spec §3.6). Only keys
    in _EDITABLE are written, in the caller's own dict order (fields is
    already handler-validated -- this just filters, it doesn't reorder);
    updated_at=now(), updated_by=caller sub. Empty editable set -> None
    without a round-trip. None on missing / malformed uuid (mirrors
    observations.set_status).

    The UPDATE is wrapped in a CTE so the RETURNING row can be LEFT JOINed to
    users for `updated_by_name` -- the resolved display name of the last writer
    -- in ONE round-trip. updated_by alone is a cognito sub, which no UI can
    render; the caption needs a person. NULL when the sub matches no users row
    (e.g. a pre-0017 row, or a login that was never provisioned)."""
    cols = [c for c in fields if c in _EDITABLE]
    if not cols:
        return None
    set_sql = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now(), updated_by=%s"
    params = [fields[c] for c in cols] + [updated_by, action_item_id]
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"WITH upd AS ("
            f"UPDATE action_items SET {set_sql} WHERE id=%s RETURNING {_RET}"
            f") SELECT upd.*, {_UPDATED_BY_NAME} FROM upd "
            f"LEFT JOIN users u ON u.cognito_sub = upd.updated_by",
            params,
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def _as_uuid(value):
    """`value` as a uuid.UUID, or None when it is not one (None, '', '69', 'abc')."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def get_live_by_stable_id(conn, stable_id) -> dict | None:
    """The one LIVE action item addressed by `stable_id` (action packs, spec §14 S12) --
    mirrors topic_questions.get_live_by_stable_id: joined to its topic for
    `visible_topics_predicate` (a superseded pass of the same source key shares this
    stable_id through carry_forward's carried_from chain, and only the live topic's row is
    the one to act on; a deleted topic or recording hides it too) and to `sites` for
    `company_id`, the tenant guard callers check.

    `ORDER BY created_at DESC LIMIT 1`: two rows can share a stable_id for one commit's width
    in a concurrent-pass race; the newest wins, as supersede_topics_for_source already does.

    Also carries the topic's `source_s3_key` (the writer's advisory-lock key, which apply
    takes before writing) and `topic_user_id` (who recorded it, for the assignee check).

    None on missing OR a malformed id. Unlike topic_questions', the malformed case is caught
    in Python before any SQL runs, so it never rolls back: this is called inside the apply
    transaction, where a `conn.rollback()` would discard the caller's own work."""
    sid = _as_uuid(stable_id)
    if sid is None:
        return None
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT a.id, a.topic_id, a.site_id, a.stable_id, a.carried_from, a.text, "
        f"a.responsible, a.deadline, a.deadline_text, a.priority, a.status, a.audience, "
        f"a.item_id, a.created_at, a.updated_at, a.updated_by, "
        f"s.company_id, t.source_s3_key, t.user_id AS topic_user_id "
        f"FROM action_items a "
        f"JOIN topics t ON t.id = a.topic_id "
        f"JOIN sites s ON s.id = a.site_id "
        f"WHERE a.stable_id = %s AND {visible_topics_predicate('t')} "
        f"ORDER BY a.created_at DESC LIMIT 1",
        (sid,),
    ).fetchone()
