from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from repositories.search_sql import build_search_sql  # re-export

__all__ = ["build_search_sql", "insert_chunk", "search_chunks"]


def insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, *,
                 user_id=None, source_s3_key=None, topic_id=None, metadata=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_chunks (site_id, user_id, source_s3_key, topic_id, report_date, "
        "chunk_type, chunk_text, embedding, metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, site_id, topic_id, chunk_type, report_date, created_at",
        (site_id, user_id, source_s3_key, topic_id, report_date, chunk_type,
         chunk_text, embedding, Jsonb(metadata or {})),
    ).fetchone()


def search_chunks(conn, query_embedding, accessible_site_ids, k=5) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k},
    ).fetchall()
