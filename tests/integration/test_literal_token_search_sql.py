"""Integration: a literal token in chunk_text is findable even when it loses
the vector top-k race, executed against a REAL Postgres.

Spec: docs/superpowers/specs/2026-09-20-a-literal-token-is-findable.md
Plan: docs/superpowers/plans/2026-09-20-a-literal-token-is-findable.md, Task 6

Every other test for this feature (test_search_sql.py, test_chunk_search_sql.py,
test_lambda_ask_agent_search.py) asserts on SQL TEXT or drives a fake connection
that records SQL without executing it. This repo has been bitten twice in
production by SQL that no test ever ran (project memory), so this file is the
one place the keyword arm's UNION, the GIN expression index, and the shared
_scope_predicate() are actually executed rather than string-matched.

Uses the `db` fixture from tests/conftest.py: migrated once per session,
rolled back per test (`tests/integration/test_scope_acl.py`'s pattern) rather
than the older raw-psycopg-connect-and-rollback style in
test_topic_decisions_sql.py -- this lets fixtures build real companies/sites
instead of depending on a pre-existing row to already be in the database.
"""
import pytest

from repositories import chunks, companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration


def _flat_embedding(seed):
    # A cheap 1024-dim vector that is FAR from every other fixture's vector by
    # construction (each fixture gets a distinct seed dimension bumped), so the
    # vector arm alone would never surface a chunk near a query embedding built
    # from an unrelated seed -- reproducing the measured gap (spec Sec1: "30+
    # other chunks... were closer... than this one was").
    v = [0.01] * 1024
    v[seed % 1024] = 5.0
    return v


def _site(db, name="Literal Token Co"):
    co = companies.create_company(db, name)
    return sites.create_site(db, co["id"], f"{name} Site")


def _insert(db, site_id, text, seed, chunk_type="topic", topic_id=None):
    return chunks.insert_chunk(db, site_id, "2026-09-03", chunk_type, text,
                               _flat_embedding(seed), topic_id=topic_id)


def _actor(db, company_id, sub):
    """A real user row -- redactions.actor_user_id is a real FK, not a free
    uuid; a synthetic id that names no row in `users` is rejected."""
    return users.upsert_user(db, sub, f"{sub}@example.test", company_id=company_id)


def _fill_with_closer_unrelated_chunks(db, site_id, query_seed, count=40):
    """Insert `count` chunks seeded to be CLOSE to the query embedding, none
    containing the literal term under test. Without this, a single-row site's
    only chunk is always returned by the vector arm's unconditional top-k
    (there is no distance cutoff), which would make a decoy's presence in the
    result set ambiguous between 'the keyword arm matched it' (the property
    under test) and 'it was the only candidate the vector arm had'. Filling
    the site with closer unrelated chunks removes that ambiguity: after this,
    the decoy can only appear if the KEYWORD arm (lexical_hit=True) put it there."""
    for i in range(count):
        _insert(db, site_id, f"Unrelated scaffolding note {i}", seed=query_seed)


def test_ps4_is_found_even_though_forty_unrelated_chunks_are_closer(db):
    """The measured gap, reproduced: PS4's own chunk is seeded FAR from the
    query embedding (seed 0), and forty unrelated 'electrical hold-up'-shaped
    chunks are seeded CLOSE to it (seed 1, shared), so the vector arm alone
    would never surface it within k=30. The keyword arm must still find it."""
    site = _site(db)
    target = _insert(db, site["id"],
                     "Light pole PS4 and electrical hold-up: provide PS4 for light poles",
                     seed=0)
    for i in range(40):
        _insert(db, site["id"], f"Unrelated electrical hold-up note {i}", seed=1)

    query_vec = _flat_embedding(1)  # near the 40 unrelated chunks, far from PS4's
    rows = chunks.search_chunks(db, query_vec, [site["id"]], k=30, query_text="PS4")
    ids = {str(r["id"]) for r in rows}
    assert str(target["id"]) in ids, \
        "PS4's chunk must be found by the keyword arm despite losing the vector race"
    hit = next(r for r in rows if str(r["id"]) == str(target["id"]))
    assert hit["lexical_hit"] is True
    assert hit["distance"] is None, \
        "a keyword-only hit must carry NULL distance, never a fabricated real-looking number"


def test_a_row_found_by_both_arms_appears_once_with_its_real_distance(db):
    """DISTINCT ON (id) must collapse the vector-arm and keyword-arm copies of
    the same row into one, keeping the vector arm's real cosine distance (not
    the keyword arm's NULL) -- spec property 5."""
    site = _site(db)
    # seed 9 makes this chunk close to its own query embedding (real distance
    # near 0) AND it contains the literal term, so both CTEs find the SAME id.
    both = _insert(db, site["id"], "PS4 light pole issue near the query itself",
                   seed=9)
    query_vec = _flat_embedding(9)

    rows = chunks.search_chunks(db, query_vec, [site["id"]], k=30, query_text="PS4")
    matches = [r for r in rows if str(r["id"]) == str(both["id"])]
    assert len(matches) == 1, "a row found by both arms must appear exactly once"
    assert matches[0]["distance"] is not None, \
        "the surviving copy must carry the vector arm's real distance, not NULL"
    assert matches[0]["distance"] < 0.01, \
        "seed 9's query and chunk embeddings are identical by construction"


def test_a_tombstoned_topics_chunk_containing_the_term_is_not_returned(db):
    """The keyword arm shares _scope_predicate with the vector arm -- it must
    honor visible_chunks_predicate exactly like the vector arm does today. A
    keyword arm that bypassed this would resurrect the deleted-recording leak
    visible_chunks_predicate was built to close (spec Sec7). This is the
    highest-stakes property in the whole plan: a looser keyword predicate
    would be a real deleted-content leak, not a cosmetic bug."""
    site = _site(db)
    topic = topics.upsert_topic(db, site["id"], "2026-09-03", "PS4 topic")
    target = _insert(db, site["id"], "PS4 light pole issue, tombstoned",
                     seed=2, topic_id=topic["id"])

    actor = _actor(db, site["company_id"], "sub-literal-token-actor-1")
    redactions.create_redaction(
        db, site["company_id"], topic["id"], "customer deleted this recording",
        actor["id"], "admin", target_type="topic", scope="deleted")

    rows = chunks.search_chunks(db, _flat_embedding(2), [site["id"]], k=30, query_text="PS4")
    assert str(target["id"]) not in {str(r["id"]) for r in rows}


def test_a_deleted_source_chunk_containing_the_term_is_not_returned(db):
    """The SOURCE arm of visible_chunks_predicate: `lambda_ingest` re-creates a
    superseded day's topics with new uuids, so a chunk with no topic-keyed
    tombstone can still carry a tombstoned recording's source_s3_key. This is
    the arm that survives the pipeline re-running overnight -- a keyword arm
    that only checked the topic arm would leak exactly this row."""
    site = _site(db)
    company_id = site["company_id"]
    prefix = "reports/tombstoned-source/2026-09-03/"
    target = chunks.insert_chunk(
        db, site["id"], "2026-09-03", "topic", "PS4 issue re-chunked after a delete",
        _flat_embedding(8), source_s3_key=prefix + "chunk-0.json")

    actor = _actor(db, company_id, "sub-literal-token-actor-2")
    redactions.create_recording_tombstone(
        db, company_id, prefix, "customer deleted this recording",
        actor["id"], "admin")

    rows = chunks.search_chunks(db, _flat_embedding(8), [site["id"]], k=30, query_text="PS4")
    assert str(target["id"]) not in {str(r["id"]) for r in rows}


def test_an_out_of_site_chunk_containing_the_term_is_not_returned(db):
    """Deny-by-default: the keyword arm must respect site_id = ANY(%(site_ids)s)
    exactly like the vector arm, not just filter tombstones."""
    mine = _site(db, "Literal Token Co A")
    other = _site(db, "Literal Token Co B")
    target = _insert(db, other["id"], "PS4 issue at a different site", seed=3)

    rows = chunks.search_chunks(db, _flat_embedding(3), [mine["id"]], k=30, query_text="PS4")
    assert str(target["id"]) not in {str(r["id"]) for r in rows}


def test_an_out_of_date_range_chunk_containing_the_term_is_not_returned(db):
    """The date_from/date_to arm of _scope_predicate must be honored by the
    keyword arm too -- a report outside the requested window must stay hidden
    even though it contains the literal term."""
    site = _site(db)
    target = _insert(db, site["id"], "PS4 issue from a much older report", seed=10)
    db.execute("UPDATE report_chunks SET report_date = '2020-01-01' WHERE id=%s",
              (target["id"],))

    rows = chunks.search_chunks(db, _flat_embedding(10), [site["id"]], k=30,
                                query_text="PS4", date_from="2026-01-01", date_to="2026-12-31")
    assert str(target["id"]) not in {str(r["id"]) for r in rows}


def test_a_conceptual_query_with_no_literal_terms_is_unaffected(db):
    """spec Sec9.2: a normal query relying purely on the ANN arm must return
    the same top result as before -- the keyword arm must retrieve ZERO
    additional rows when nothing in the corpus matches its tsquery."""
    site = _site(db)
    near = _insert(db, site["id"], "Fall from height near miss on the scaffold", seed=4)
    _insert(db, site["id"], "Unrelated note about paint colours", seed=5)

    query_vec = _flat_embedding(4)
    rows = chunks.search_chunks(db, query_vec, [site["id"]], k=30,
                                query_text="what safety issues came up this week")
    assert rows, "expected the semantically-near chunk to still be found"
    assert str(rows[0]["id"]) == str(near["id"])
    assert all(r["lexical_hit"] is False for r in rows), \
        "no fixture chunk_text contains any word from the control query verbatim"


def test_ps4_does_not_match_ps40_word_boundary(db):
    """The property ILIKE would have gotten wrong (spec Sec4): PS4 must not
    match inside PS40. This is the deciding factor the spec chose tsvector
    over pg_trgm for.

    The site is filled with 40 chunks seeded CLOSE to the query embedding so
    the vector arm's unconditional top-k (it has no distance cutoff) cannot
    put the decoy in the result set on its own -- if the decoy shows up here,
    it can only be because the KEYWORD arm matched it, which is exactly the
    property under test."""
    site = _site(db)
    decoy = _insert(db, site["id"], "PS40 was delivered and installed yesterday", seed=6)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=7)

    rows = chunks.search_chunks(db, _flat_embedding(7), [site["id"]], k=30, query_text="PS4")
    assert str(decoy["id"]) not in {str(r["id"]) for r in rows}, \
        "PS4 must not match inside PS40 -- this is the whole reason ILIKE was rejected"


def test_ps40_does_not_match_ps4_word_boundary(db):
    """The reverse direction of the same property: a query for the LONGER
    token must not match a chunk containing only the shorter one."""
    site = _site(db)
    decoy = _insert(db, site["id"], "PS4 was delivered and installed yesterday", seed=11)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=12)

    rows = chunks.search_chunks(db, _flat_embedding(12), [site["id"]], k=30, query_text="PS40")
    assert str(decoy["id"]) not in {str(r["id"]) for r in rows}, \
        "PS40 must not match a chunk containing only PS4"


def test_the_gin_expression_index_is_actually_used_by_the_planner(db):
    """Task 1 Step 5's check, run for real: on a table with enough rows that a
    sequential scan is not simply cheaper, the planner must choose
    idx_report_chunks_tsv for the keyword arm's own predicate, not a Seq Scan.
    If this ever goes red, the expression in build_search_sql() no longer
    matches the index's expression character-for-character -- the exact
    silent-fallback failure test_keyword_arm_expression_matches_the_index_exactly
    (unit suite) exists to catch before it reaches here."""
    site = _site(db)
    for i in range(3000):
        _insert(db, site["id"], f"Unrelated electrical hold-up note number {i}", seed=i)
    db.execute("ANALYZE report_chunks")

    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN SELECT id FROM report_chunks WHERE "
        "to_tsvector('english', chunk_text) @@ websearch_to_tsquery('english', 'PS4')"
    ).fetchall())
    assert "idx_report_chunks_tsv" in plan, (
        "the planner did not choose the GIN expression index on a "
        f"3000-row table -- got:\n{plan}")
    assert "Seq Scan" not in plan, f"planner fell back to a sequential scan:\n{plan}"
