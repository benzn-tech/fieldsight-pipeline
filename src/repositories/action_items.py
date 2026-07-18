import psycopg
from psycopg.rows import dict_row

# Whitelist of columns PATCH /api/org/action-items/{id} may set. text/site_id/
# topic_id/created_at are intentionally NOT here -- editing a task must never
# re-home it to another site (ACL bypass) or rewrite its body.
_EDITABLE = ("priority", "status", "deadline", "deadline_text", "responsible")
_RET = ("id, topic_id, site_id, text, responsible, deadline, deadline_text, "
        "priority, status, created_at, updated_at, updated_by")


def get_action_item(conn, action_item_id) -> dict | None:
    """One action item joined to its site's company_id (the tenant guard the
    handler checks against caller.company_id). Returns None if not found or
    action_item_id is not a valid UUID -- malformed id == missing (404
    semantics), same posture as observations.get_observation."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            "SELECT a.id, a.topic_id, a.site_id, a.text, a.responsible, a.deadline, "
            "a.deadline_text, a.priority, a.status, a.created_at, a.updated_at, "
            "a.updated_by, s.company_id "
            "FROM action_items a JOIN sites s ON s.id = a.site_id WHERE a.id=%s",
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
    observations.set_status)."""
    cols = [c for c in fields if c in _EDITABLE]
    if not cols:
        return None
    set_sql = ", ".join(f"{c}=%s" for c in cols) + ", updated_at=now(), updated_by=%s"
    params = [fields[c] for c in cols] + [updated_by, action_item_id]
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE action_items SET {set_sql} WHERE id=%s RETURNING {_RET}",
            params,
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
