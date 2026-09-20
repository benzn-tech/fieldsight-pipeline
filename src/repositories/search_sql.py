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
    #
    # TWO arms, unioned before the final LIMIT (2026-09-20 spec: "a literal
    # token is findable"). A rare identifier like "PS4" or an RFI number
    # carries almost no embedding signal and systematically loses the top-k
    # cosine race against topically-similar prose -- the chunk that contains
    # the exact string a user typed can lose to forty unrelated chunks. The
    # keyword arm runs to_tsvector('english', c.chunk_text) @@
    # websearch_to_tsquery on the SAME scope predicate as the vector arm
    # (never a looser one -- see _scope_predicate's own docstring on why that
    # would resurrect a closed deleted-recording leak). The expression here
    # MUST match idx_report_chunks_tsv's definition (0059 migration) character
    # for character or Postgres silently falls back to a sequential scan --
    # test_keyword_arm_expression_matches_the_index_exactly pins this.
    #
    # The keyword arm's rows carry NO meaningful cosine distance (never scored
    # against the query embedding) -- NULL, not a fabricated "good" number, so
    # a keyword hit cannot silently outrank a true semantic top-1. lexical_hit
    # distinguishes the two arms; downstream ranking (_aggregate_topics in
    # lambda_ask_agent.py) must admit a lexical_hit=True row past its distance
    # gate rather than dropping it one hop downstream of this fix.
    #
    # DISTINCT ON (id): a row found by BOTH arms is one row. ORDER BY id,
    # lexical_hit ASC keeps that row's vector-arm copy (lexical_hit=false,
    # with a real distance) over the keyword copy's NULL when both exist. The
    # outer query has no further ORDER BY guaranteeing final rank beyond the
    # dedup -- _aggregate_topics already does the caller-visible ranking
    # (lexical-first, then distance); this SQL's job ends at which rows
    # survive.
    scope = _scope_predicate("c")
    return (
        "WITH vec AS ("
        "  SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "         c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "         s.slug AS site_slug, "
        "         t.title AS topic_title, t.summary AS topic_summary, "
        "         c.embedding <=> %(q)s::vector AS distance, false AS lexical_hit "
        "  FROM report_chunks c "
        "  LEFT JOIN topics t ON t.id = c.topic_id "
        "  LEFT JOIN sites s ON s.id = c.site_id "
        "  WHERE " + scope + " "
        "  ORDER BY c.embedding <=> %(q)s::vector "
        "  LIMIT %(k)s"
        "), "
        "lex AS ("
        "  SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "         c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "         s.slug AS site_slug, "
        "         t.title AS topic_title, t.summary AS topic_summary, "
        "         NULL::float8 AS distance, true AS lexical_hit "
        "  FROM report_chunks c "
        "  LEFT JOIN topics t ON t.id = c.topic_id "
        "  LEFT JOIN sites s ON s.id = c.site_id "
        "  WHERE " + scope + " "
        "  AND to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery('english', %(q_text)s) "
        "  LIMIT %(k)s"
        ") "
        "SELECT DISTINCT ON (id) id, chunk_text, chunk_type, topic_id, source_s3_key, "
        "       metadata, report_date, site_id, site_name, site_slug, "
        "       topic_title, topic_summary, distance, lexical_hit "
        "FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u "
        "ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST "
        "LIMIT %(k)s * 2"
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
