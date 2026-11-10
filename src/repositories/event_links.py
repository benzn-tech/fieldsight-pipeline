"""event_links: the edge table linking extracted items to transcript turns and to each other.

Spec: docs/superpowers/specs/2026-11-08-track-c-item-history-and-evidence-chain-design.md
§5.1/§5.4; migration 0098_event_links.sql. Only `supported_by` (item -> transcript turn) is
written by this slice; `resolves`, `continues` and `supersedes` are reserved for their
owners' writers, and links_for_stable_id already reads them.

The table has no company_id: a link belongs to the company of its site (sites.company_id,
project-owned tenancy), like every other row on a site.

Deletion reach, both kinds:
* physical -- a supported_by row carries the FK of the exact item row it was written for
  (ON DELETE CASCADE), so topics.delete_topics_for_source / _prefix, and a site delete
  (site_id ON DELETE CASCADE), remove the link rows themselves;
* revocable tombstone -- nothing is deleted, so every read here resolves the link's
  endpoint(s) to an item row whose topic passes deleted_predicates.visible_topics_predicate:
  both deletion arms (topic id and recording source prefix) plus the live arm. Not
  CHILD_OF_VISIBLE_TOPIC: it lacks the source-prefix arm, so a recording delete would leak
  (item_history.find_items made the same choice for the same reason). Setting the
  tombstone's reverted_at brings the links back with no write here.

A supported_by link follows the exact row its FK names. Item -> item endpoint checks are
EXISTS over every carrier of a stable_id, never a scalar subquery: after
one carry-forward a stable_id names two rows (deleted_predicates, Ruling R17).
"""
import json

from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate

# The one kind -> table mapping every Track C module imports.
KIND_TABLE = {
    "action_item": "action_items",
    "finding": "findings",
    "decision": "topic_decisions",
    "question": "topic_questions",
}

# kind -> the supported_by FK column naming the exact source row (migration 0098).
KIND_SRC_COLUMN = {
    "action_item": "src_action_item_id",
    "finding": "src_finding_id",
    "decision": "src_decision_id",
    "question": "src_question_id",
}

LINK_TYPES = ("supported_by", "resolves", "continues", "supersedes")

# One link per source row: the same order item_history.find_items uses for `origin`, so the
# two reads can never name different turns for one row.
BEST_LINK_ORDER = "e.score DESC NULLS LAST, e.created_at DESC, e.id ASC"

_COLS = ("id, site_id, link_type, src_kind, src_stable_id, src_action_item_id, "
         "src_finding_id, src_decision_id, src_question_id, source_s3_key, dst_kind, "
         "dst_stable_id, dst_turn, method, score, rule_version, decision_record_id, "
         "created_at")


def _kind(kind):
    if kind not in KIND_TABLE:
        raise ValueError(f"unknown kind {kind!r}; expected one of {tuple(KIND_TABLE)}")
    return kind


def insert_supported_by(conn, *, site_id, src_kind, src_row_id, src_stable_id=None,
                        source_s3_key,
                        dst_turn: dict, score: float, rule_version: str,
                        method="lexical_v1"):
    """Write one item -> turn link; sets the FK column of `src_kind` to `src_row_id`.
    Returns the new link id.

    The link's site is the source row's own site: a `site_id` that differs from it (or a
    `src_row_id` that names no row of `src_kind`) raises ValueError instead of writing a
    link another site's reader would resolve.

    The link's src_stable_id is likewise the row's own CURRENT stable_id, read here: the
    item writer links after carry-forward, which may have rewritten it. Pass None to take
    the row's value; a non-None `src_stable_id` that differs from it raises ValueError."""
    kind = _kind(src_kind)
    table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    if not isinstance(dst_turn, dict):
        raise ValueError("dst_turn must be a dict")
    row = conn.execute(f"SELECT site_id::text, stable_id FROM {table} WHERE id = %s",
                       (src_row_id,)).fetchone()
    if row is None:
        raise ValueError(f"no {kind} row {src_row_id}")
    if str(site_id) != row[0]:
        raise ValueError(f"site_id {site_id} is not the {kind} row's site {row[0]}")
    if src_stable_id is not None and str(src_stable_id) != str(row[1]):
        raise ValueError(f"src_stable_id is not the {kind} row's current stable_id")
    return conn.execute(
        f"INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, {col}, "
        f"source_s3_key, dst_kind, dst_turn, method, score, rule_version) "
        f"VALUES (%s,'supported_by',%s,%s,%s,%s,'turn',%s::jsonb,%s,%s,%s) RETURNING id",
        (row[0], kind, row[1], src_row_id, source_s3_key, json.dumps(dst_turn),
         method, score, rule_version),
    ).fetchone()[0]


def insert_continues(conn, *, site_id, src_kind, src_row_id, src_stable_id, dst_stable_id,
                     source_s3_key, rule_version, decision_record_id, method="llm_echo"):
    """Write one item -> item `continues` edge (cross-day continuity, spec 2026-11-09 X8).
    Returns the new link id, or None when the (src_stable_id, dst_stable_id) edge already
    exists (ON CONFLICT DO NOTHING on migration 0101's partial unique index).

    The FK column of `src_kind` is set to `src_row_id`, so deleting the source pass
    (topics.delete_topics_for_source) cascades the edge. The dst is the same kind as the src
    and is named by stable_id only. As insert_supported_by: the site must be the source
    row's own site and `src_stable_id` its current stable_id, else ValueError."""
    kind = _kind(src_kind)
    table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    if dst_stable_id is None:
        raise ValueError("dst_stable_id is required")
    row = conn.execute(f"SELECT site_id::text, stable_id FROM {table} WHERE id = %s",
                       (src_row_id,)).fetchone()
    if row is None:
        raise ValueError(f"no {kind} row {src_row_id}")
    if str(site_id) != row[0]:
        raise ValueError(f"site_id {site_id} is not the {kind} row's site {row[0]}")
    if str(src_stable_id) != str(row[1]):
        raise ValueError(f"src_stable_id is not the {kind} row's current stable_id")
    hit = conn.execute(
        f"INSERT INTO event_links (site_id, link_type, src_kind, src_stable_id, {col}, "
        f"source_s3_key, dst_kind, dst_stable_id, method, rule_version, decision_record_id) "
        f"VALUES (%s,'continues',%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        f"ON CONFLICT (src_stable_id, dst_stable_id) WHERE link_type = 'continues' "
        f"DO NOTHING RETURNING id",
        (row[0], kind, row[1], src_row_id, source_s3_key, kind, dst_stable_id, method,
         rule_version, decision_record_id),
    ).fetchone()
    return hit[0] if hit else None


def live_supported_by(conn, src_kind, src_row_ids: list) -> dict:
    """{row_id: {"dst_turn": ..., "score": ...}} for the rows among `src_row_ids` that are
    live and visible (their topic passes visible_topics_predicate) and have a supported_by
    turn link. One link per row, BEST_LINK_ORDER. Keys are the caller's own values."""
    kind = _kind(src_kind)
    ids = list(src_row_ids or ())
    if not ids:
        return {}
    back = {str(i): i for i in ids}
    table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    rows = conn.execute(
        f"SELECT c.id::text, o.dst_turn, o.score FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id "
        f"JOIN LATERAL (SELECT e.dst_turn, e.score FROM event_links e "
        f"WHERE e.{col} = c.id AND e.link_type = 'supported_by' AND e.dst_kind = 'turn' "
        f"ORDER BY {BEST_LINK_ORDER} LIMIT 1) o ON true "
        f"WHERE c.id = ANY(%s::uuid[]) AND {visible_topics_predicate('t')}",
        (list(back),),
    ).fetchall()
    return {back[rid]: {"dst_turn": turn, "score": score} for rid, turn, score in rows}


def _src_row_visible():
    """SQL: a supported_by link's source is the exact row its FK names, and that row's
    topic is visible and live. Spec §5.1: readers take the links of the live row;
    superseded passes keep theirs as history, and a deleted pass's locators never show
    through a live carrier of the same stable_id."""
    live = visible_topics_predicate("vt")
    return "(" + " OR ".join(
        f"(e.src_kind = '{kind}' AND EXISTS (SELECT 1 FROM {table} vc "
        f"JOIN topics vt ON vt.id = vc.topic_id WHERE vc.id = e.{KIND_SRC_COLUMN[kind]} "
        f"AND {live}))"
        for kind, table in KIND_TABLE.items()) + ")"


def _endpoint_visible(kind_col, stable_col):
    """SQL: the endpoint (kind in `kind_col`, stable id in `stable_col`, both on alias e)
    resolves to at least one visible live row on the link's site."""
    live = visible_topics_predicate("vt")
    return "(" + " OR ".join(
        f"(e.{kind_col} = '{kind}' AND EXISTS (SELECT 1 FROM {table} vc "
        f"JOIN topics vt ON vt.id = vc.topic_id WHERE vc.stable_id = e.{stable_col} "
        f"AND vc.site_id = e.site_id AND {live}))"
        for kind, table in KIND_TABLE.items()) + ")"


def links_for_stable_id(conn, *, site_id, stable_id, link_types=None) -> list[dict]:
    """Every link on `site_id` with `stable_id` at either end, oldest first.

    Shown only when each endpoint resolves to a visible live row. supported_by: the src
    is the exact row its FK names (an earlier pass's link is history, not shown here);
    item -> item links: the src through src_stable_id, the dst through dst_stable_id. A
    turn dst needs only the src side.
    `link_types` restricts the types; None = all, an empty collection raises."""
    if link_types is not None:
        if isinstance(link_types, str) or not link_types:
            raise ValueError("link_types must be None or a non-empty collection")
        unknown = [t for t in link_types if t not in LINK_TYPES]
        if unknown:
            raise ValueError(f"unknown link type(s) {unknown}; expected {LINK_TYPES}")
        link_types = list(link_types)
    sql = (
        f"SELECT {_COLS} FROM event_links e "
        f"WHERE e.site_id = %(site)s "
        f"AND (e.src_stable_id = %(sid)s OR e.dst_stable_id = %(sid)s) "
        f"AND (%(types)s::text[] IS NULL OR e.link_type = ANY(%(types)s::text[])) "
        f"AND CASE WHEN e.link_type = 'supported_by' THEN {_src_row_visible()} "
        f"ELSE {_endpoint_visible('src_kind', 'src_stable_id')} END "
        f"AND (e.dst_kind = 'turn' OR {_endpoint_visible('dst_kind', 'dst_stable_id')}) "
        f"ORDER BY e.created_at ASC, e.id ASC"
    )
    cur = conn.cursor(row_factory=dict_row)
    return cur.execute(sql, {"site": site_id, "sid": stable_id, "types": link_types}).fetchall()
