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

# table_name -> editable free-text columns, in a STABLE order (spec §3). The
# ordered tuples are the single source of truth; EDITABLE below is the set view
# every existing caller already uses. Order matters only for the intra-topic
# propagation preview, which lists proposed changes field by field.
_EDITABLE_ORDERED = {
    "topics": ("title", "summary"),
    "action_items": ("text", "responsible"),
    "findings": ("observation", "recommended_action", "entity_name", "entity_trade"),
}
EDITABLE = {t: set(f) for t, f in _EDITABLE_ORDERED.items()}

# Which column ties a row to its topic: `topics` IS the topic, children carry
# topic_id. Used only by list_topic_content_fields (intra-topic propagation);
# both table and column names come from these constants, never from request
# input, so the interpolation is injection-safe.
_TOPIC_KEY = {"topics": "id", "action_items": "topic_id", "findings": "topic_id"}

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


def list_topic_content_fields(conn, topic_id):
    """Every editable free-text CELL belonging to ONE topic, as
    [{table, row_id, field, value}] -- the complete blast radius of an
    intra-topic correction (the topic row itself + its action_items +
    its findings). Ordered topics -> action_items -> findings, and within a
    table by _EDITABLE_ORDERED, so a preview is stable across calls.

    Only columns listed in EDITABLE are selected, so nothing outside the
    allow-list can be previewed and therefore nothing outside it can be
    rewritten. NULL cells are skipped (they can never contain the wrong
    term). Returns [] on a missing/malformed topic id -- same 404-friendly
    posture as get_content_row."""
    out = []
    for table, fields in _EDITABLE_ORDERED.items():
        cols = ", ".join(fields)
        try:
            rows = conn.cursor(row_factory=dict_row).execute(
                f"SELECT id, {cols} FROM {table} WHERE {_TOPIC_KEY[table]}=%s "
                f"ORDER BY id",
                (topic_id,),
            ).fetchall()
        except psycopg.Error:
            conn.rollback()
            return []
        for row in rows:
            for field in fields:
                if row[field] is None:
                    continue
                out.append({"table": table, "row_id": row["id"],
                            "field": field, "value": row[field]})
    return out


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
