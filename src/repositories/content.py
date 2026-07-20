"""Generalized editable free-text content fields for the item store
(spec §3 / §5.2). One allow-list + two dumb accessors, shared by
PATCH /api/org/content/{table}/{id}. Excludes categorical/enum fields
(domain/severity/category/status/priority/deadline) -- those are extraction
judgments or task metadata, not transcription errors (spec §3). Generalizes
action_items.get_action_item / update_action_item_fields.

Phase F Task 24 (D8 retirement, spec §8): safety_observations was removed
from EDITABLE (and its _SELECT entry dropped) -- findings is now the single
editable source for safety/quality content. A correction to a safety/quality
finding's `observation` (etc.) already lands on the findings row via the
`findings` entry below, and Tasks 21/22 make that the row SAFETY/QUALITY
reads FROM -- so the correction propagates automatically. Editing
safety_observations directly would target the unlinked legacy table and
never be seen again. The safety_observations TABLE itself is untouched
(still exists, unread here, for rollback)."""
import psycopg
from psycopg.rows import dict_row

# table_name -> editable free-text columns (spec §3)
EDITABLE = {
    "topics": {"title", "summary"},
    "action_items": {"text", "responsible"},
    "findings": {"observation", "recommended_action", "entity_name", "entity_trade"},
}

# Per-table SELECT that returns id, site_id, company_id, author_user_id, plus
# every editable field's current value. Every table reaches company_id via
# site_id -> sites.company_id; author_user_id is the owning topic's user_id
# (for `topics` the row IS the topic). Table names come only from EDITABLE
# keys -- never raw request input -- so the interpolation is injection-safe.
_SELECT = {
    "topics": (
        "SELECT x.id, x.site_id, s.company_id, x.user_id AS author_user_id, "
        "x.title, x.summary "
        "FROM topics x JOIN sites s ON s.id = x.site_id WHERE x.id=%s"),
    "action_items": (
        "SELECT x.id, x.site_id, s.company_id, tp.user_id AS author_user_id, "
        "x.text, x.responsible "
        "FROM action_items x JOIN sites s ON s.id = x.site_id "
        "JOIN topics tp ON tp.id = x.topic_id WHERE x.id=%s"),
    "findings": (
        "SELECT x.id, x.site_id, s.company_id, tp.user_id AS author_user_id, "
        "x.observation, x.recommended_action, x.entity_name, x.entity_trade "
        "FROM findings x JOIN sites s ON s.id = x.site_id "
        "JOIN topics tp ON tp.id = x.topic_id WHERE x.id=%s"),
}


def is_editable(table, field):
    return table in EDITABLE and field in EDITABLE[table]


def get_content_row(conn, table, row_id):
    """id/site_id/company_id/author_user_id + current editable values for one
    row. None on unknown table, missing row, or malformed uuid (404 semantics,
    same posture as observations.get_observation)."""
    if table not in _SELECT:
        return None
    try:
        return conn.cursor(row_factory=dict_row).execute(
            _SELECT[table], (row_id,)).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def update_content_field(conn, table, row_id, field, value):
    """Whitelisted single-field UPDATE (D3 materialize-in-place). Returns the
    updated row (id + the field), or None on non-whitelisted table/field or
    malformed uuid. No updated_at bump -- content_edits IS the audit trail."""
    if not is_editable(table, field):
        return None
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE {table} SET {field}=%s WHERE id=%s RETURNING id, {field}",
            (value, row_id),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None
