"""Pure search-SQL construction. MUST NOT import psycopg."""


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates). Optional per-author allow-set (graded roles
    # visibility spec §3.1): author_ids NULL => no author filter (ALL/SITE),
    # else restrict to chunks whose c.user_id is in the set (SELF / SELF+
    # WORKERS) -- same ::uuid[] IS-NULL guard idiom as the date range above and
    # topics.list_topics_for_date.
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       s.slug AS site_slug, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "AND (%(author_ids)s::uuid[] IS NULL OR c.user_id = ANY(%(author_ids)s::uuid[])) "
        "AND (%(date_from)s::date IS NULL OR c.report_date >= %(date_from)s::date) "
        "AND (%(date_to)s::date IS NULL OR c.report_date <= %(date_to)s::date) "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )
