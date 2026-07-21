"""Redaction tombstones for life-conversation separation (2026-07-21 spec §4).
A redaction soft-excludes a topic from company-tier reads without deleting it;
reverted_at IS NULL = active. company_excluded_topic_ids is the single choke
point every company-tier read routes through (rollup, RAG embed/reindex)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, target_type, target_id, reason, actor_user_id, "
         "actor_role, scope, created_at, reverted_at")


def create_redaction(conn, company_id, target_id, reason, actor_user_id, actor_role,
                     *, target_type="topic", scope="analysis"):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO redactions (company_id, target_type, target_id, reason, "
        f"actor_user_id, actor_role, scope) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        f"RETURNING {_COLS}",
        (company_id, target_type, target_id, reason, actor_user_id, actor_role, scope),
    ).fetchone()


def get_redaction(conn, redaction_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE id=%s", (redaction_id,)).fetchone()


def revert_redaction(conn, redaction_id, company_id):
    """Un-tombstone (spec §4). Company-guarded; sets reverted_at so audit
    survives. None if missing / wrong company / already reverted."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE redactions SET reverted_at=now() "
        f"WHERE id=%s AND company_id=%s AND reverted_at IS NULL RETURNING {_COLS}",
        (redaction_id, company_id)).fetchone()


def is_topic_redacted(conn, topic_id) -> bool:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM redactions WHERE target_type='topic' AND target_id=%s "
        "AND reverted_at IS NULL LIMIT 1", (topic_id,)).fetchone() is not None


def company_excluded_topic_ids(conn, site_ids) -> set:
    """Topic ids a COMPANY-tier read excludes across site_ids: any topic
    classified non_work (auto-held) OR carrying an active redaction. The
    site/self tier does NOT use this. Empty site_ids -> empty set (no query)."""
    if not site_ids:
        return set()
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id FROM topics WHERE site_id = ANY(%s) AND ("
        "  work_class='non_work' "
        "  OR id IN (SELECT target_id FROM redactions "
        "            WHERE target_type='topic' AND reverted_at IS NULL))",
        (list(site_ids),)).fetchall()
    return {r["id"] for r in rows}


def list_active_for_topics(conn, topic_ids) -> dict:
    """Active redaction row keyed by target topic id (UI 'removed' area). {}."""
    if not topic_ids:
        return {}
    return {r["target_id"]: r for r in conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE target_type='topic' "
        f"AND target_id = ANY(%s) AND reverted_at IS NULL", (list(topic_ids),)).fetchall()}
