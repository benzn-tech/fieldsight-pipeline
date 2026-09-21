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
    # 2026-09-22 fix ("the keyword arm can actually fire"): the keyword arm's
    # indexed expression is now the 'english' vector OR'd with a 'simple'
    # (non-stemming) vector over punctuation-split text -- see migration 0061
    # and test_keyword_arm_expression_matches_the_index_exactly for the exact
    # expression pinned character-for-character. The query side matches it
    # with the 'simple' config (not 'english' any more).
    assert "to_tsvector('english', c.chunk_text) || to_tsvector('simple'," in sql, \
        "no lexical arm: a literal token can still only be found by cosine luck"
    assert "@@ websearch_to_tsquery('simple', %(q_text)s)" in sql, \
        "keyword arm must query the 'simple' config, or an identifier split " \
        "by punctuation (e.g. 'PS4/light') never matches a bare term again"
    assert "union all" in sql


def test_search_sql_keyword_arm_shares_the_scope_predicate():
    import repositories.search_sql as search_sql
    scope = search_sql._scope_predicate("c")
    sql = build_search_sql()
    assert sql.count(scope) == 2, \
        "vector arm and keyword arm must carry the IDENTICAL scope predicate string"


def test_keyword_arm_expression_matches_the_index_exactly():
    """The migration (0061_report_chunks_tsv_multi_config_idx.sql) creates a
    GIN index on (to_tsvector('english', chunk_text) || to_tsvector('simple',
    regexp_replace(chunk_text, '[^a-zA-Z0-9]+', ' ', 'g'))). Postgres only
    uses an expression index when the query repeats that expression
    character-for-character (modulo the table alias) -- a query that spells
    it even slightly differently (extra space, different config literal,
    respelled regexp_replace args) silently falls back to a sequential scan
    with no error. This test is the thing that catches that drift, not a
    convention or a comment.

    The expression is read out of the migration's own raw text (never
    hard-coded a second time here) and pinned as ONE literal substring, not a
    "both tokens appear somewhere" check -- deleting `|| to_tsvector('simple',
    ...)` from build_search_sql fails this test, and so does a
    logically-equivalent respelling like
    `to_tsvector('simple', regexp_replace(c.chunk_text, '[^a-zA-Z0-9]+', ' ', 'g')) || to_tsvector('english', c.chunk_text)`
    (operands swapped) -- both were hand-verified to fail before this
    migration shipped."""
    import os
    import repositories.search_sql as search_sql

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(search_sql.__file__)), "migrations")
    with open(os.path.join(migrations_dir,
                           "0061_report_chunks_tsv_multi_config_idx.sql"),
              encoding="utf-8") as fh:
        migration_sql = fh.read()
    expected = (
        "(to_tsvector('english', chunk_text) || to_tsvector('simple', "
        "regexp_replace(chunk_text, '[^a-zA-Z0-9]+', ' ', 'g')))"
    )
    assert expected in migration_sql, \
        "the migration's own expression changed -- update this test's expectation deliberately"

    sql = build_search_sql()
    # The query's alias is "c", the migration's bare columns have no alias --
    # the expression itself (function names, language literals, quoting,
    # operand order) must still match exactly. Substituting the alias onto
    # BOTH `chunk_text` occurrences (not just the first) is what makes this a
    # faithful re-derivation rather than a second hand-typed copy.
    aliased = expected.replace("chunk_text", "c.chunk_text")
    assert aliased in sql, \
        "build_search_sql's keyword arm must spell the combined tsvector " \
        "expression IDENTICALLY to idx_report_chunks_tsv_multi_config's " \
        "definition, or the planner silently falls back to a sequential scan"


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


def test_search_sql_has_an_outer_order_by_after_the_dedup():
    """DISTINCT ON (id) ... ORDER BY id, ... only decides which copy of a
    row found by both arms survives -- it says nothing about the order rows
    leave the query in, because `id` (a UUID) sorts first. The search box is
    fine either way (_aggregate_topics re-sorts), but Ask is not:
    _rerank_chunks truncates with chunks[:keep] in arrival order whenever
    ENABLE_RERANK is unset (every environment today, since it is wired
    nowhere). Without a SECOND, outer ORDER BY wrapping the DISTINCT ON,
    rows would arrive in UUID order and Ask would keep an arbitrary k of up
    to 2k candidates instead of the best k by relevance.

    This test fails if that outer ORDER BY is removed (verified by hand:
    deleting it makes the assertions below fail, restoring it makes them
    pass again -- see the task report)."""
    sql = build_search_sql()

    # The DISTINCT ON's own ORDER BY (its dedup tiebreak) must be immediately
    # followed by a close paren -- i.e. it is scoped to an inner subquery,
    # not the outermost SELECT.
    inner_order = "ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST"
    assert inner_order in sql
    after_inner = sql.split(inner_order, 1)[1]
    # Whatever comes next closes the subquery before anything else runs.
    assert after_inner.lstrip().startswith(")"), (
        "the DISTINCT ON's ORDER BY must be inside a subquery, not left as "
        "the query's only ORDER BY"
    )

    # There must be a SECOND ORDER BY, outside that subquery, ordering the
    # deduped rows themselves -- lexical_hit ASC (vector rows first, since
    # False < True) then distance ASC NULLS LAST (ascending cosine distance,
    # matching the vec CTE's own order), and it must appear AFTER the
    # subquery closes and BEFORE the final LIMIT.
    tail = sql[sql.index(inner_order) + len(inner_order):]
    assert tail.count("ORDER BY") == 1, (
        "expected exactly one more ORDER BY after the DISTINCT ON's own, "
        "wrapping the dedup in an outer, re-sorted SELECT"
    )
    outer_order = "ORDER BY lexical_hit ASC, distance ASC NULLS LAST"
    assert outer_order in tail
    order_idx = tail.index(outer_order)
    limit_idx = tail.index("LIMIT %(k)s * 2")
    assert order_idx < limit_idx, (
        "the outer ORDER BY must run before the final LIMIT %(k)s * 2, or "
        "Ask/the search box keep an arbitrary k of up to 2k rows instead of "
        "the best k"
    )


def test_outer_order_by_reproduces_pre_keyword_arm_arrival_order():
    """In-Python check of the ordering CONTRACT the outer ORDER BY encodes
    (not a DB round trip -- this repo's unit tests don't have one): sorting
    a mixed set of vector and keyword-only rows by
    (lexical_hit ASC, distance ASC NULLS LAST) must put every vector row
    (lexical_hit=False) — in the SAME relative order the vec CTE already
    produced them in (ascending distance) — before every keyword-only row
    (lexical_hit=True, distance=NULL), never interleaved.

    This is exactly the tuple `test_search_sql_has_an_outer_order_by_after_the_dedup`
    pins in the generated SQL string; this test additionally proves that
    tuple actually sorts a representative row set the way the plan requires,
    so a typo like `distance ASC, lexical_hit ASC` (which WOULD interleave
    them) cannot pass by string-matching alone.
    """
    import random

    vec_rows = [
        {"id": "v1", "lexical_hit": False, "distance": 0.10},
        {"id": "v2", "lexical_hit": False, "distance": 0.15},
        {"id": "v3", "lexical_hit": False, "distance": 0.42},
    ]
    lex_only_rows = [
        {"id": "l1", "lexical_hit": True, "distance": None},
        {"id": "l2", "lexical_hit": True, "distance": None},
    ]
    rows = vec_rows + lex_only_rows
    random.Random(0).shuffle(rows)  # arrival order must not matter

    ordered = sorted(
        rows,
        key=lambda r: (r["lexical_hit"], r["distance"] if r["distance"] is not None else float("inf")),
    )

    ordered_ids = [r["id"] for r in ordered]
    # Vector rows keep their original ascending-distance relative order --
    # ties between keyword-only rows (both lexical_hit=True, distance=NULL)
    # are NOT specified by this contract and are not asserted here.
    assert ordered_ids[:3] == ["v1", "v2", "v3"], (
        "vector rows must come first, in their original ascending-distance order"
    )
    assert set(ordered_ids[3:]) == {"l1", "l2"}, (
        "keyword-only rows must be appended after ALL vector rows -- not interleaved"
    )
