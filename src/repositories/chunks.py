from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from repositories.search_sql import build_search_sql  # re-export

__all__ = ["build_search_sql", "insert_chunk", "search_chunks", "delete_chunks_for_scope"]


def insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, *,
                 user_id=None, source_s3_key=None, topic_id=None, metadata=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_chunks (site_id, user_id, source_s3_key, topic_id, report_date, "
        "chunk_type, chunk_text, embedding, metadata) "
        # embedding cast to %s::vector for consistency with search_sql's %(q)s::vector; with the
        # pgvector adapter registered a bound Python list already serializes to vector, so
        # vector::vector is a no-op cast and existing callers/tests binding a list are unaffected.
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) "
        "RETURNING id, site_id, topic_id, chunk_type, report_date, created_at",
        (site_id, user_id, source_s3_key, topic_id, report_date, chunk_type,
         chunk_text, embedding, Jsonb(metadata or {})),
    ).fetchone()


def search_chunks(conn, query_embedding, accessible_site_ids, k=5) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k},
    ).fetchall()


def delete_chunks_for_scope(conn, site_id, report_date, user_id) -> int:
    """Delete report_chunks rows for a (site_id, report_date, user_id) scope.

    user_id is nullable: pass None to target rows with no user (user_id IS NULL)
    rather than matching all users. Used by callers to achieve scope-delete
    idempotency before re-inserting chunks for a rerun (Phase 4a)."""
    user_clause = "user_id=%s" if user_id is not None else "user_id IS NULL"
    params = [site_id, report_date] + ([user_id] if user_id is not None else [])
    cur = conn.execute(
        f"DELETE FROM report_chunks WHERE site_id=%s AND report_date=%s AND {user_clause}",
        params,
    )
    return cur.rowcount
