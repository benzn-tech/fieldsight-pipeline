"""Pure search-SQL construction. MUST NOT import psycopg."""
from deleted_predicates import visible_chunks_predicate


def _scope_predicate(alias: str = "c") -> str:
    """The WHERE clause every read of report_chunks must carry: deny-by-default
    site ACL, optional per-author narrowing, optional inclusive date range, and
    both tombstone arms. Used verbatim by the vector arm of build_search_sql()
    today; Task 3 of this plan wires the same string into the keyword arm it
    adds, so the two arms cannot drift apart.

    A recording the customer deleted must not come back through the search box or
    through Ask -- both run this predicate. This was missing when the delete endpoint
    was first written, which meant every other surface hid the content and the two
    the customer is most likely to try still returned it verbatim.
    """
    return (
        f"{alias}.site_id = ANY(%(site_ids)s) "
        f"AND (%(author_ids)s::uuid[] IS NULL OR {alias}.user_id = ANY(%(author_ids)s::uuid[])) "
        f"AND (%(date_from)s::date IS NULL OR {alias}.report_date >= %(date_from)s::date) "
        f"AND (%(date_to)s::date IS NULL OR {alias}.report_date <= %(date_to)s::date) "
        f"AND " + visible_chunks_predicate(alias)
    )


def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates).
    scope = _scope_predicate("c")
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       s.slug AS site_slug, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE " + scope + " "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )


def build_latest_date_sql() -> str:
    """The most recent day this caller can see, at or before a bound.

    Asking "what happened yesterday" on a day with no recording must not answer
    nothing -- it must answer the nearest day there IS one and say so. That
    nearest day has to be found under the SAME visibility rules as the search
    itself, or the widening becomes a way to learn that a deleted recording
    existed: the chunks stay hidden, but the date it was made on is disclosed
    by the answer widening onto it.

    So the WHERE clause is deliberately the search's own, minus the vector and
    the range: same site pinning, same per-author grading, same
    `visible_chunks_predicate`. `on_or_before` is bounded rather than open
    because widening FORWARD would answer a question about last week with
    something recorded after it.
    """
    return (
        "SELECT max(c.report_date) AS latest "
        "FROM report_chunks c "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "AND (%(author_ids)s::uuid[] IS NULL OR c.user_id = ANY(%(author_ids)s::uuid[])) "
        "AND (%(on_or_before)s::date IS NULL OR c.report_date <= %(on_or_before)s::date) "
        "AND " + visible_chunks_predicate("c")
    )
