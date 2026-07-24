import psycopg
from psycopg.rows import dict_row

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
            "a.updated_by, s.company_id, t.user_id AS topic_user_id "
            "FROM action_items a JOIN sites s ON s.id = a.site_id "
            "LEFT JOIN topics t ON t.id = a.topic_id WHERE a.id=%s",
            (action_item_id,),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


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
