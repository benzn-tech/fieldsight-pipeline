"""Pure search-SQL construction. MUST NOT import psycopg."""
from deleted_predicates import visible_chunks_predicate


def family_predicate(alias: str = "c") -> str:
    # NULL family = no filter (callers that predate the switch stay byte-identical).
    #
    # 'session' reads session-family rows (extractions/ keys) and ALSO falls back to
    # report-family rows for any (site, user, day) the session index does not cover yet:
    # a day that only ever had a nightly report (no extraction) has zero session rows, and
    # a pure family filter would make search/Ask answer "No relevant records" for it.
    # "Covered" = at least one session row exists for the same (site_id, user_id,
    # report_date); user_id is compared IS NOT DISTINCT FROM so NULL-user rows match each
    # other. The coverage test deliberately ignores tombstones/supersession: a deleted
    # session row must keep its day covered, or deleting a recording would resurface the
    # report-family copy of the same day. Correlated NOT EXISTS so it rides
    # idx_report_chunks_site_date. The 'report' arm is unchanged.
    return (f"(%(source_family)s::text IS NULL "
            f"OR (%(source_family)s::text = 'session' AND ({alias}.source_s3_key LIKE 'extractions/%%' "
            f"OR NOT EXISTS (SELECT 1 FROM report_chunks s WHERE s.site_id = {alias}.site_id "
            f"AND s.report_date = {alias}.report_date "
            f"AND s.user_id IS NOT DISTINCT FROM {alias}.user_id "
            f"AND s.source_s3_key LIKE 'extractions/%%'))) "
            f"OR (%(source_family)s::text = 'report' AND ({alias}.source_s3_key IS NULL "
            f"OR {alias}.source_s3_key NOT LIKE 'extractions/%%')))")


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
    return _visibility_predicate(
        alias,
        f"(%(date_from)s::date IS NULL OR {alias}.report_date >= %(date_from)s::date) "
        f"AND (%(date_to)s::date IS NULL OR {alias}.report_date <= %(date_to)s::date)")


def _visibility_predicate(alias: str, date_sql: str) -> str:
    """The ONE visibility rule for report_chunks: site pin, per-author narrowing,
    both tombstone arms + supersession (visible_chunks_predicate), and the index
    family (family_predicate, coverage fallback included). `date_sql` is the
    caller's own date bound, spliced in where the date clauses have always sat so
    the search SQL stays byte-identical.

    _scope_predicate (search, and Ask's latest_matching_date), and
    build_latest_date_sql all build their WHERE through here, so a date that a
    deleted, superseded, other-author or other-family row would have produced
    cannot leak through one of them while the others hide it.
    """
    return (
        f"{alias}.site_id = ANY(%(site_ids)s) "
        f"AND (%(author_ids)s::uuid[] IS NULL OR {alias}.user_id = ANY(%(author_ids)s::uuid[])) "
        f"AND {date_sql} "
        f"AND " + visible_chunks_predicate(alias) + " "
        f"AND " + family_predicate(alias)
    )


def _lexical_vector(alias: str = "c") -> str:
    # MUST equal idx_report_chunks_tsv_multi_config's expression (0061) character for
    # character (modulo alias) -- test_keyword_arm_expression_matches_the_index_exactly.
    return (f"to_tsvector('english', {alias}.chunk_text) || to_tsvector('simple', "
            f"regexp_replace({alias}.chunk_text, '[^a-zA-Z0-9]+', ' ', 'g'))")


# The keyword arm's query side: the caller's %(q_text)s bound verbatim (see
# build_search_sql's header -- an OR list built by lexical_terms.or_query, or "",
# which is an empty tsquery that matches nothing).
_LEXICAL_QUERY = "websearch_to_tsquery('simple', %(q_text)s)"


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
    # the exact string a user typed can lose to forty unrelated chunks.
    #
    # 2026-09-22 fix ("the keyword arm can actually fire"): the FIRST shipped
    # version of this arm (PR #886) bound the caller's whole question as one
    # websearch_to_tsquery input, which ANDs every non-stopword term together
    # -- a natural-language question essentially never satisfies that -- and
    # matched only on 'english', which hands one lexeme per whitespace run,
    # so an identifier touching punctuation ("PS4/light") never matched a bare
    # "PS4" query either. Both are fixed now:
    #   * the CALLER (lambda_rag_search.py, fed by lambda_ask_agent.py's
    #     _lexical_terms via the shared lexical_terms module) is responsible
    #     for turning the question into an OR query BEFORE it reaches here --
    #     this function still just binds whatever %(q_text)s it is given
    #     verbatim; it does not know or care whether that text is one term,
    #     an "a or b or c" OR list, or (for a caller that wants literal
    #     AND/phrase behavior) an unmodified sentence. An empty %(q_text)s
    #     yields an empty tsquery that matches NOTHING, never everything.
    #   * the EXPRESSION below now matches BOTH the stemmed 'english' vector
    #     (today's behavior) and a 'simple' (non-stemming) vector over the
    #     same text with every run of non-alnum characters collapsed to a
    #     space -- so "PS4/light" contributes 'ps4' and 'light' as separate
    #     literal simple-config lexemes, restoring the word boundary the raw
    #     punctuation destroyed. The query side uses websearch_to_tsquery
    #     with the 'simple' config to match that half of the vector (see
    #     migration 0061's own header for the six measured cases this fixes).
    # The expression here MUST match idx_report_chunks_tsv_multi_config's
    # definition (0061 migration) character for character or Postgres
    # silently falls back to a sequential scan --
    # test_keyword_arm_expression_matches_the_index_exactly pins this.
    #
    # The keyword arm's rows carry their REAL cosine distance to the query
    # embedding (2026-10-14 change; the 2026-09-20 spec used NULL so a keyword
    # hit could never look like a semantic match). A real `<=>` value is not
    # fabricated, and NULL made _aggregate_topics score such rows 1.0, ranking
    # a relevant keyword-only row below dozens of weak vector rows. The cost
    # is one extra vector distance per matched row (<= the arm's LIMIT).
    # lexical_hit, NOT `distance IS NULL`, is the only way to tell the arms
    # apart; downstream ranking (_aggregate_topics in lambda_ask_agent.py)
    # must still admit a lexical_hit=True row past its distance gate.
    #
    # DISTINCT ON (id): a row found by BOTH arms is one row. Its own
    # ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST keeps that row's
    # vector-arm copy (lexical_hit=false) over the keyword copy when both
    # exist; the two copies carry the same distance, so the tiebreak is
    # unchanged and decided by lexical_hit alone.
    #
    # This SQL has TWO consumers, not one, and they need different things:
    #   - the search box goes through _aggregate_topics, which re-sorts by
    #     (lexical-first, then distance) itself, so row order arriving from
    #     here was never load-bearing for it;
    #   - Ask (_rag_answer -> _rerank_chunks, lambda_ask_agent.py) does NOT
    #     re-sort while reranking is off, and it is off. ENABLE_RERANK IS
    #     wired (src/template.yaml:434 declares the EnableRerank parameter,
    #     :1923 passes it through, and both deploy workflows set it from a
    #     repo variable defaulting to 'false'), but no PROD_ENABLE_RERANK or
    #     TEST_ENABLE_RERANK variable is set, so it resolves to 'false' in
    #     both environments and _rerank_chunks's first line returns
    #     chunks[:keep] in the order it received them. If reranking is ever
    #     turned on, _rerank_chunks reorders explicitly and stops depending
    #     on arrival order -- the ordering below is correct either way. Before this branch, that order was
    #     the vec CTE's cosine order, because there was only one arm. Now
    #     that DISTINCT ON forces an ORDER BY id first, the dedup subquery's
    #     row order is by UUID -- arrival order at the caller becomes
    #     effectively random with respect to relevance -- unless something
    #     outside the DISTINCT ON re-imposes it.
    #
    # So the outer SELECT re-sorts the deduped rows by lexical_hit ASC,
    # distance ASC NULLS LAST (keyword-only rows now order by real distance
    # among themselves). Since lexical_hit=false sorts before true and
    # the vec CTE is unchanged, this reproduces the pre-this-branch vec-only
    # order EXACTLY for the vector rows, then appends keyword-only rows
    # after all of them (never interleaved). It does not decide how a
    # keyword-only row should rank against a semantic one -- that ranking
    # policy is deliberately left to the caller (Task 4's job), not smuggled
    # in here.
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
        "  ORDER BY (c.embedding <=> %(q)s::vector) + 0 "  # exact; see test_the_vector_arm_cannot_use_the_hnsw_index
        "  LIMIT %(k)s"
        "), "
        "lex AS ("
        "  SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "         c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "         s.slug AS site_slug, "
        "         t.title AS topic_title, t.summary AS topic_summary, "
        "         c.embedding <=> %(q)s::vector AS distance, true AS lexical_hit "
        "  FROM report_chunks c "
        "  LEFT JOIN topics t ON t.id = c.topic_id "
        "  LEFT JOIN sites s ON s.id = c.site_id "
        "  WHERE " + scope + " "
        "  AND (" + _lexical_vector("c") + ") "
        "  @@ " + _LEXICAL_QUERY + " "
        # 2026-09-22 round-2 fix ("the keyword arm can actually fire"): this
        # LIMIT used to have NO ORDER BY, which was fine when the arm's query
        # was an AND of every word (Cause A) -- matches were rare, so
        # whichever plan-dependent k rows Postgres happened to return were
        # usually ALL of the matches anyway. Cause A's fix turned the query
        # into an OR, which makes matches common, so an un-ordered LIMIT now
        # returns an arbitrary k out of however many rows matched -- not the
        # arm's best k. ORDER BY ts_rank DESC over the SAME indexed
        # expression (character-for-character, so the planner can still use
        # it for the scan) before the LIMIT makes the k rows this arm
        # contributes its best k by relevance, not whichever k the planner
        # happened to visit first.
        "  ORDER BY ts_rank(" + _lexical_vector("c") + ", " + _LEXICAL_QUERY + ") DESC "
        "  LIMIT %(k)s"
        ") "
        "SELECT id, chunk_text, chunk_type, topic_id, source_s3_key, "
        "       metadata, report_date, site_id, site_name, site_slug, "
        "       topic_title, topic_summary, distance, lexical_hit "
        "FROM ("
        "  SELECT DISTINCT ON (id) id, chunk_text, chunk_type, topic_id, source_s3_key, "
        "         metadata, report_date, site_id, site_name, site_slug, "
        "         topic_title, topic_summary, distance, lexical_hit "
        "  FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u "
        "  ORDER BY id, lexical_hit ASC, distance ASC NULLS LAST"
        ") deduped "
        "ORDER BY lexical_hit ASC, distance ASC NULLS LAST "
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
        "WHERE " + _visibility_predicate(
            "c", "(%(on_or_before)s::date IS NULL OR c.report_date <= %(on_or_before)s::date)")
    )


def subject_tsquery(n: int) -> str:
    """AND of `n` subject words, each matching its english stem OR its exact
    token (Ask tools, spec 2026-10-20 §3.2, `latest_activity` with a query).

    Parameters %(st0)s..%(st{n-1})s, one `lexical_terms.subject_terms` word each
    (bind them with subject_params). Those words are [a-z0-9]+, so to_tsquery's
    operator syntax cannot be injected through them.

    A fold PER TERM, not `websearch_to_tsquery('english', all) ||
    websearch_to_tsquery('simple', all)`: that ORs two whole AND-queries, and the
    english half drops english stopwords -- 'crane' | 'don' & 'crane' matched
    "the crane arrived" for the subject "don crane". Here to_tsquery('english',
    'don') is empty, so that term reduces to the simple 'don' and the AND holds.
      * english half: stems (scaffolding ~ scaffold, panels ~ panel);
      * simple half: exact tokens, incl. what the english parser splits
        differently ("RFI-108": english 'rfi' '-108', simple '108').
    Spacing variants ("rfi108" vs "RFI 108") match neither half: a stated no_match.
    """
    if n < 1:
        raise ValueError("subject_tsquery needs at least one term")
    return "(" + " && ".join(
        f"(to_tsquery('english', %(st{i})s) || to_tsquery('simple', %(st{i})s))"
        for i in range(n)) + ")"


def subject_params(terms) -> dict:
    """The %(st*)s bindings for subject_tsquery(len(terms))."""
    return {f"st{i}": t for i, t in enumerate(terms)}


def build_latest_match_date_sql(n: int) -> str:
    """The most recent day with a visible chunk NAMING a subject (Ask tools,
    spec 2026-10-15 §3.1 item 3 / 2026-10-20 §3.2, `latest_activity` with a query).

    WHERE = the search's own scope (`_scope_predicate`, with date_from/date_to as
    the bounded window) AND the chunk text contains every one of the `n` subject
    words (subject_tsquery). The day used to be chosen by a cosine-distance gate
    OR an or_query keyword hit; at 0.55 almost every site chunk was "near", and
    "last"/"time" alone was a keyword hit, so the latest recording day won.

    The left side is _lexical_vector, unchanged, so the GIN expression index
    (idx_report_chunks_tsv_multi_config) serves the match whatever the tsquery's
    shape; the window (date_from) still bounds what is left.
    """
    return (
        "SELECT max(c.report_date) AS latest "
        "FROM report_chunks c "
        "WHERE " + _scope_predicate("c") + " "
        "AND (" + _lexical_vector("c") + ") @@ " + subject_tsquery(n)
    )


def build_subject_recheck_sql(n: int) -> str:
    """Which of the given chunk ids name all `n` subject words (Ask tools,
    `latest_activity` with a query; bound %(ids)s plus subject_params).

    build_search_sql's rows say nothing about whether a row's text names the
    subject: the day's search ranks by distance and by ts_rank of an OR list.
    This re-check uses the SAME subject_tsquery fragment as
    build_latest_match_date_sql, so "names the subject" means here exactly what
    it meant when the day was chosen. Ids only: the rows already passed the
    search's visibility predicate.
    """
    return (
        "SELECT c.id FROM report_chunks c "
        "WHERE c.id = ANY(%(ids)s::uuid[]) "
        "AND (" + _lexical_vector("c") + ") @@ " + subject_tsquery(n)
    )
