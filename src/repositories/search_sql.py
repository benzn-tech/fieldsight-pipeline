"""Pure search-SQL construction. MUST NOT import psycopg."""


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates).
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "AND (%(date_from)s::date IS NULL OR c.report_date >= %(date_from)s::date) "
        "AND (%(date_to)s::date IS NULL OR c.report_date <= %(date_to)s::date) "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )
