"""Pure search-SQL construction. MUST NOT import psycopg."""


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: return the parent topic's title/summary via LEFT JOIN.
    # Citations (Phase 5 RAG ask) need report_date/site_id/site_name, so
    # also LEFT JOIN sites for the human-readable site name.
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )
