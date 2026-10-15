"""Pure SQL text for project-document search. MUST NOT import psycopg.

Spec 2026-10-16-project-documents-design, R11 (lexical expression) and R12 (exact scan).
"""

_VISIBLE = ("{a}.deleted_at IS NULL AND {a}.superseded_at IS NULL "
            "AND {a}.status = 'indexed'")

# The keyword arm's query side: an OR list built by the caller (lexical_terms.or_query),
# bound verbatim; "" is an empty tsquery that matches nothing.
LEXICAL_QUERY = "websearch_to_tsquery('simple', %(q_text)s)"


def visible_predicate(alias="d"):
    """The ONE rule for "this document's chunks may be searched" (contamination rule 5)."""
    return _VISIBLE.format(a=alias)


def lexical_vector(alias="c"):
    # MUST equal project_doc_chunks_tsv's expression in 0088 (modulo the alias).
    return f"to_tsvector('simple', coalesce({alias}.heading, '') || ' ' || {alias}.chunk_text)"


def _arm(lexical_hit):
    return (
        "SELECT c.id, c.document_id, d.series_id, d.revision, d.title, d.filename, "
        "d.content_type, c.site_id, s.name AS site_name, s.slug AS site_slug, c.heading, "
        "c.page_from, c.page_to, c.ordinal, c.chunk_text, "
        "c.embedding <=> %(q)s::vector AS distance, " + lexical_hit + " AS lexical_hit "
        "FROM project_doc_chunks c "
        "JOIN project_documents d ON d.id = c.document_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE c.site_id = ANY(%(site_ids)s::uuid[]) AND d.site_id = c.site_id "
        "AND " + visible_predicate("d") + " ")


def build_doc_search_sql():
    """Vector arm (exact cosine, gated at %(max_distance)s) UNION keyword arm, one row per
    chunk (the vector copy wins), vector rows first by distance, then keyword-only rows.

    `+ 0` makes the sort key an expression no index can serve (R12); there is no HNSW
    index on this table today, and this keeps it exact if one is ever added."""
    return (
        "WITH vec AS (" + _arm("false") +
        "AND (c.embedding <=> %(q)s::vector) <= %(max_distance)s "
        "ORDER BY (c.embedding <=> %(q)s::vector) + 0 LIMIT %(k)s), "
        "lex AS (" + _arm("true") +
        "AND (" + lexical_vector("c") + ") @@ " + LEXICAL_QUERY + " "
        "ORDER BY ts_rank(" + lexical_vector("c") + ", " + LEXICAL_QUERY + ") DESC "
        "LIMIT %(k)s) "
        "SELECT * FROM ("
        "SELECT DISTINCT ON (id) * FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u "
        "ORDER BY id, lexical_hit ASC) deduped "
        "ORDER BY lexical_hit ASC, distance ASC LIMIT %(k)s")
