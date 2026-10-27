"""Status suggestions from speech (migration 0090, spec 2026-10-27).

A proposal, never a status: nothing in this module writes action_items.status. The writer stores
what the extractor kept; org-api serves the pending ones beside their items and records a
person's accept/dismiss."""
import uuid

from psycopg.rows import dict_row

_COLS = ("id, company_id, site_id, stable_id, suggested_status, quote, segment_key, "
         "offset_sec, run, state, resolved_by, resolved_at, created_at")


def target_for_item_id(conn, new_topic_ids, item_id):
    """This pass's row for an extractor `item_id`, ONLY when it was carried by identity.

    Part C.3, second guard: `carried_from` alone is not enough -- carry_identity sets it for
    exact/fuzzy TEXT carries as well. The predecessor must wear the SAME item_id, which only the
    item_id pairing pass produces. None when there is no such row (fresh item, an id cleaned to
    NULL as malformed/duplicated, or a text-only carry)."""
    if not new_topic_ids or not item_id:
        return None
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT n.id, n.stable_id, n.status, n.site_id FROM action_items n "
        "JOIN action_items o ON o.id = n.carried_from AND o.item_id = n.item_id "
        "WHERE n.topic_id = ANY(%s::uuid[]) AND n.item_id = %s",
        ([str(t) for t in new_topic_ids], str(item_id))).fetchone()


def insert_suggestion(conn, company_id, site_id, stable_id, status, quote, segment_key,
                      offset_sec, run):
    """Idempotent. Returns the new row, or None when this item already has a suggestion of this
    status in ANY state -- which is how a dismissed one stays dismissed."""
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO action_status_suggestions (company_id, site_id, stable_id, "
        "suggested_status, quote, segment_key, offset_sec, run) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        f"ON CONFLICT (stable_id, suggested_status) DO NOTHING RETURNING {_COLS}",
        (company_id, site_id, stable_id, status, quote, segment_key, offset_sec, run),
    ).fetchone()


def pending_for_stable_ids(conn, stable_ids):
    """{stable_id: [pending rows, newest first]} for many items in one query. Empty in, empty
    out -- never "every suggestion"."""
    ids = sorted({str(s) for s in stable_ids if s})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM action_status_suggestions "
        "WHERE stable_id = ANY(%s::uuid[]) AND state = 'pending' "
        "ORDER BY created_at DESC, id",
        (ids,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(str(r["stable_id"]), []).append(r)
    return out


def pending_for_action_item_ids(conn, action_item_ids):
    """{action_item_id: [pending suggestions for that row's stable_id, newest first]} in one
    query. By ROW id rather than stable_id so a reader needs nothing it does not already hold:
    the day read's rows (and a collapsed survivor's `collapsed_ids`) are row ids. Empty in,
    empty out."""
    ids = sorted({str(i) for i in action_item_ids if i})
    if not ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT a.id AS action_item_id, s.id, s.stable_id, s.suggested_status, s.quote, "
        "       s.segment_key, s.offset_sec, s.created_at "
        "FROM action_items a JOIN action_status_suggestions s "
        "  ON s.stable_id = a.stable_id AND s.state = 'pending' "
        "WHERE a.id = ANY(%s::uuid[]) ORDER BY s.created_at DESC, s.id",
        (ids,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(str(r["action_item_id"]), []).append(r)
    return out


def get_suggestion(conn, suggestion_id):
    """None for a malformed or unknown id (404 semantics). Validated here rather than by a
    failing cast, which would abort the caller's transaction."""
    try:
        sid = str(uuid.UUID(str(suggestion_id)))
    except (ValueError, TypeError, AttributeError):
        return None
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM action_status_suggestions WHERE id = %s", (sid,)).fetchone()


def resolve_suggestion(conn, suggestion_id, state, user_id):
    """pending -> accepted|dismissed, once. The `state = 'pending'` predicate is the CAS: a
    second click (or two people at once) gets None."""
    return conn.cursor(row_factory=dict_row).execute(
        "UPDATE action_status_suggestions SET state = %s, resolved_by = %s, resolved_at = now() "
        f"WHERE id = %s AND state = 'pending' RETURNING {_COLS}",
        (state, user_id, str(suggestion_id))).fetchone()
