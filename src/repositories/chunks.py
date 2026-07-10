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
        # A bound Python list arrives as float8[] (register_vector only adds
        # numpy dumpers), and pgvector casts double precision[] -> vector; a
        # bound '[...]' string casts text -> vector. Both callers work.
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) "
        "RETURNING id, site_id, topic_id, chunk_type, report_date, created_at",
        (site_id, user_id, source_s3_key, topic_id, report_date, chunk_type,
         chunk_text, embedding, Jsonb(metadata or {})),
    ).fetchone()


def search_chunks(conn, query_embedding, accessible_site_ids, k=5,
                  date_from=None, date_to=None) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to},
    ).fetchall()


def delete_chunks_for_source(conn, source_s3_key) -> int:
    """Delete report_chunks rows produced from one source report.

    Keyed on source_s3_key — the only key that is UNIQUE per report and
    immune to identity-resolution drift. A (site, date, user_id) scope key
    was tried first and failed review: two same-site/same-date reports whose
    user bridge both miss (user_id NULL — real case: MPI1 + MPI2 share
    primary_site 'mpi') would silently delete each other's rows, and a
    remediation rerun after fixing a mapping would duplicate instead of
    replace (Fable Phase 4a review C1/I1)."""
    cur = conn.execute(
        "DELETE FROM report_chunks WHERE source_s3_key=%s",
        (source_s3_key,),
    )
    return cur.rowcount
