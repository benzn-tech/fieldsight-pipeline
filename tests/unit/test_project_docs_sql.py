"""The documents search SQL, as text: the lexical expression must equal the GIN index's
(R11) or Postgres silently falls back to a sequential scan; the vector arm must sort
exactly (R12); both arms must carry the site pin and the visibility predicate."""
import os

from repositories import project_docs_sql as q

MIGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src", "migrations", "0088_project_documents.sql")


def _migration():
    return open(MIGRATION, encoding="utf-8").read()


def test_the_lexical_expression_is_the_index_expression_exactly():
    expr = q.lexical_vector("c").replace("c.", "")
    assert expr == "to_tsvector('simple', coalesce(heading, '') || ' ' || chunk_text)"
    assert "USING gin ((" + expr + "))" in _migration()


def test_the_lexical_expression_does_not_strip_non_latin_text():
    # 0061's regexp_replace('[^a-zA-Z0-9]+') erases Chinese; documents may be Chinese.
    assert "regexp_replace" not in q.lexical_vector("c")
    assert "regexp_replace" not in _migration()


def test_no_hnsw_index_on_document_chunks():
    assert "hnsw" not in _migration().lower()


def test_the_vector_arm_sorts_exactly_and_is_gated():
    sql = q.build_doc_search_sql()
    vec = sql.split("lex AS (")[0]
    assert "ORDER BY (c.embedding <=> %(q)s::vector) + 0" in vec
    assert "<= %(max_distance)s" in vec


def test_both_arms_carry_the_site_pin_and_the_visibility_predicate():
    sql = q.build_doc_search_sql()
    vec, lex = sql.split("lex AS (")
    for arm in (vec, lex):
        assert "c.site_id = ANY(%(site_ids)s::uuid[])" in arm
        assert "d.site_id = c.site_id" in arm
        assert q.visible_predicate("d") in arm


def test_the_visibility_predicate_is_the_spec_rule():
    assert q.visible_predicate("d") == (
        "d.deleted_at IS NULL AND d.superseded_at IS NULL AND d.status = 'indexed'")


def test_the_keyword_arm_uses_the_same_expression_for_match_and_rank():
    lex = q.build_doc_search_sql().split("lex AS (")[1]
    assert lex.count(q.lexical_vector("c")) == 2
    assert lex.count(q.LEXICAL_QUERY) == 2
