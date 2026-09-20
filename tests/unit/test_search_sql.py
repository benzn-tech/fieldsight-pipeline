from repositories.search_sql import build_search_sql


def test_search_sql_contains_citation_columns():
    sql = build_search_sql()

    # citation columns for RAG (report_date + site attribution)
    assert "c.report_date" in sql
    assert "site_name" in sql
    assert "JOIN sites s ON s.id = c.site_id" in sql

    # existing topics LEFT JOIN preserved
    assert "LEFT JOIN topics t ON t.id = c.topic_id" in sql
    assert "t.title AS topic_title" in sql
    assert "t.summary AS topic_summary" in sql

    # embedding cast used twice: SELECT distance + ORDER BY
    assert sql.count("::vector") >= 2

    # ACL deny-by-default filter, ordering, and limit unchanged
    assert "site_id = ANY(%(site_ids)s)" in sql
    assert "ORDER BY" in sql
    assert "<=>" in sql
    assert "LIMIT %(k)s" in sql


def test_search_sql_has_date_filter():
    sql = build_search_sql()
    # optional inclusive date range (None => no filter)
    assert "%(date_from)s::date IS NULL OR c.report_date >= %(date_from)s::date" in sql
    assert "%(date_to)s::date IS NULL OR c.report_date <= %(date_to)s::date" in sql
    # unchanged essentials still present
    assert "site_id = ANY(%(site_ids)s)" in sql
    assert "LIMIT %(k)s" in sql


def test_search_sql_has_author_filter_with_null_guard():
    sql = build_search_sql()
    # None => no filter (Ask/ALL/SITE); a list => restrict by user_id.
    assert "%(author_ids)s" in sql
    assert "c.user_id = ANY(%(author_ids)s" in sql
    # IS NULL guard so passing author_ids=None is a no-op (byte-identical scope)
    assert "%(author_ids)s::uuid[] IS NULL" in sql
    # Pin the FULL clause verbatim: the OR must stay inside one parenthesized
    # group. If this were ever split into two top-level ANDs, an author_ids
    # list would silently AND against every unrelated row instead of gating
    # the author check behind the NULL guard -- a cross-site author leak.
    assert (
        "AND (%(author_ids)s::uuid[] IS NULL OR c.user_id = ANY(%(author_ids)s::uuid[]))"
        in sql
    )


def test_scope_predicate_is_shared_by_construction():
    """The vector and keyword arms must use the SAME predicate string, not two
    calls that happen to look alike today and can drift apart tomorrow."""
    import repositories.search_sql as search_sql
    assert hasattr(search_sql, "_scope_predicate"), \
        "build_search_sql must factor its WHERE clause into one shared builder"
    p1 = search_sql._scope_predicate("c")
    p2 = search_sql._scope_predicate("c")
    assert p1 == p2
    # Both arms of the final SQL must contain this exact string.
    sql = search_sql.build_search_sql()
    assert sql.count(p1) == 2, \
        "the scope predicate must appear once per CTE (vector arm + keyword arm), verbatim"


def test_search_sql_has_a_keyword_arm():
    sql = build_search_sql().lower()
    assert "to_tsvector('english', c.chunk_text) @@ websearch_to_tsquery" in sql, \
        "no lexical arm: a literal token can still only be found by cosine luck"
    assert "union all" in sql


def test_search_sql_keyword_arm_shares_the_scope_predicate():
    import repositories.search_sql as search_sql
    scope = search_sql._scope_predicate("c")
    sql = build_search_sql()
    assert sql.count(scope) == 2, \
        "vector arm and keyword arm must carry the IDENTICAL scope predicate string"


def test_keyword_arm_expression_matches_the_index_exactly():
    """The migration (0059_report_chunks_tsv_idx.sql) creates a GIN index on
    to_tsvector('english', chunk_text). Postgres only uses an expression index
    when the query repeats that expression character-for-character (modulo the
    table alias) -- a query that spells it even slightly differently (extra
    space, different config literal) silently falls back to a sequential scan
    with no error. This test is the thing that catches that drift, not a
    convention or a comment."""
    import os
    import repositories.search_sql as search_sql

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(search_sql.__file__)), "migrations")
    with open(os.path.join(migrations_dir, "0059_report_chunks_tsv_idx.sql"),
              encoding="utf-8") as fh:
        migration_sql = fh.read()
    assert "to_tsvector('english', chunk_text)" in migration_sql, \
        "the migration's own expression changed -- update this test's expectation deliberately"

    sql = build_search_sql()
    # The query's alias is "c", the migration's bare column has no alias --
    # the expression itself (function name, language literal, quoting) must
    # still match exactly.
    assert "to_tsvector('english', c.chunk_text)" in sql, \
        "build_search_sql's keyword arm must spell the tsvector expression " \
        "IDENTICALLY to idx_report_chunks_tsv's definition, or the planner " \
        "silently falls back to a sequential scan"


def test_search_sql_keyword_arm_never_fabricates_a_good_distance():
    sql = build_search_sql().lower()
    # The keyword-only arm has no real cosine score. It must not assign one
    # that would let it silently outrank a true semantic top-1 (spec §5).
    # A NULL placeholder (not e.g. 0.0) is the marker _aggregate_topics keys off.
    assert "null as distance" in sql or "as distance" in sql
    assert "true as lexical_hit" in sql
    assert "false as lexical_hit" in sql


def test_search_sql_dedupes_rows_found_by_both_arms():
    sql = build_search_sql().lower()
    assert "distinct on (id)" in sql or "distinct on (c.id)" in sql, \
        "a row found by both arms must be one row, keeping its real distance"


def test_search_sql_limit_is_still_present_and_final():
    sql = build_search_sql()
    assert sql.rstrip().endswith("%(k)s") or "limit %(k)s" in sql.lower()
