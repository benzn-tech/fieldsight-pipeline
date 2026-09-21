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

HOW TO GET A REAL POSTGRES FOR THIS FILE (2026-09-21)
----------------------------------------------------
These are skipped unless TEST_DATABASE_URL is set. Aurora is in-VPC and not
reachable from a developer machine, so the route that works locally is an
embedded Postgres:

    uv run --with pgserver python -c "import pgserver, tempfile;         print(pgserver.get_server(tempfile.mkdtemp()).get_uri())"

then export that URI as TEST_DATABASE_URL.

USE `uv run --with pgserver`, NOT `uv pip install pgserver` into a venv. The two
install routes ship DIFFERENT extension sets, measured on the same machine on
the same day: under `uv run --with pgserver`, `pgcrypto` is present and
`CREATE EXTENSION pgcrypto` succeeds, so migration 0001 applies as written.
Under a venv install, `pgcrypto.control` is absent and 0001 fails, and the only
way forward is hand-stubbing a no-op control file inside the package tree --
which works, but makes a green run depend on an undocumented local hack. If you
hit that failure, you are on the wrong install route; switch rather than stub.

(The one thing this repo uses pgcrypto for is `gen_random_uuid()` as a column
default, which has been a Postgres core builtin since v13 and needs no
extension. That is why stubbing works at all. It is not a reason to stub.)

Two caveats kept deliberately, neither checked by anyone:
  * This is local PostgreSQL 16.2, not TEST/prod Aurora. If Aurora's text-search
    configuration were ever changed from the stock 'english', the tokenisation
    these tests rely on would not carry over.
  * This pgserver build ships no tzdata, so `SHOW timezone` is GMT and
    `pg_timezone_names` raises. Five tests in
    tests/integration/test_closure_kpi_and_batch_lookup.py fail here for that
    reason alone; they fail identically on a clean checkout of origin/develop,
    so they are the environment, not this branch.

Uses the `db` fixture from tests/conftest.py: migrated once per session,
rolled back per test (`tests/integration/test_scope_acl.py`'s pattern) rather
than the older raw-psycopg-connect-and-rollback style in
test_topic_decisions_sql.py -- this lets fixtures build real companies/sites
instead of depending on a pre-existing row to already be in the database.
"""
import pytest

from lexical_terms import QUERY_STOPWORDS, or_query, query_terms
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
    idx_report_chunks_tsv_multi_config (migration 0061, 2026-09-22 fix: "the
    keyword arm can actually fire") for the keyword arm's own predicate, not
    a Seq Scan. If this ever goes red, the expression in build_search_sql()
    no longer matches the index's expression character-for-character -- the
    exact silent-fallback failure
    test_keyword_arm_expression_matches_the_index_exactly (unit suite)
    exists to catch before it reaches here.

    The EXPLAIN'd predicate below is the combined-vector expression + the
    'simple'-config query side, not the old 'english'-only one -- migration
    0061 DROPs idx_report_chunks_tsv, so the old expression would now have
    nothing to match and would legitimately Seq Scan."""
    site = _site(db)
    for i in range(3000):
        _insert(db, site["id"], f"Unrelated electrical hold-up note number {i}", seed=i)
    db.execute("ANALYZE report_chunks")

    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN SELECT id FROM report_chunks WHERE "
        "(to_tsvector('english', chunk_text) || to_tsvector('simple', "
        "regexp_replace(chunk_text, '[^a-zA-Z0-9]+', ' ', 'g'))) "
        "@@ websearch_to_tsquery('simple', 'PS4')"
    ).fetchall())
    assert "idx_report_chunks_tsv_multi_config" in plan, (
        "the planner did not choose the GIN expression index on a "
        f"3000-row table -- got:\n{plan}")
    assert "Seq Scan" not in plan, f"planner fell back to a sequential scan:\n{plan}"


def test_the_dropped_idx_report_chunks_tsv_is_actually_gone(db):
    """Migration 0061 DROP INDEXes idx_report_chunks_tsv (0059) because its
    'english'-only expression no longer appears anywhere in
    build_search_sql()'s output -- the planner would never choose it again,
    so it would only keep costing writes to maintain. This is the one place
    that runs the migration against a real Postgres and checks the drop
    actually took, rather than trusting the migration file's own comment."""
    row = db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_report_chunks_tsv'"
    ).fetchone()
    assert row is None, "idx_report_chunks_tsv should have been dropped by migration 0061"

    row = db.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname = 'idx_report_chunks_tsv_multi_config'"
    ).fetchone()
    assert row is not None, "idx_report_chunks_tsv_multi_config should exist after migration 0061"


# ---------------------------------------------------------------------------
# The six acceptance cases from the 2026-09-22 fix ("the keyword arm can
# actually fire"), run through chunks.search_chunks -- the actual code path,
# not a hand-written EXPLAIN -- with query_text already OR-ready (a single
# bare term), mirroring what lambda_rag_search.py now sends after
# lexical_terms.or_query(lexical_terms.lexical_terms(question)).
# ---------------------------------------------------------------------------

def test_ps4_finds_a_chunk_where_ps4_touches_punctuation(db):
    """The motivating case (Cause B): the owner's real data has 'PS4/light
    pole issues', where PS4 touches a slash rather than whitespace. Under
    'english' alone, to_tsvector('english', 'pricing and PS4/light pole
    issues') yields the single lexeme 'ps4/light' -- a bare 'PS4' query never
    matched it, even after Cause A (the AND-vs-OR fix) is fixed. This is the
    case that only migration 0061's 'simple' + regexp_replace vector fixes."""
    site = _site(db)
    target = _insert(db, site["id"], "pricing and PS4/light pole issues", seed=13)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=14)

    rows = chunks.search_chunks(db, _flat_embedding(14), [site["id"]], k=30, query_text="PS4")
    ids = {str(r["id"]) for r in rows}
    assert str(target["id"]) in ids, \
        "PS4 must find 'PS4/light' after the punctuation split -- this is the owner's own case"
    hit = next(r for r in rows if str(r["id"]) == str(target["id"]))
    assert hit["lexical_hit"] is True


def test_bare_1042_finds_rfi_1042(db):
    """A bare numeric identifier must find it inside a hyphenated compound
    ('RFI-1042') the same way PS4 must find 'PS4/light' -- the same
    Cause B fix, a different punctuation character."""
    site = _site(db)
    target = _insert(db, site["id"], "RFI-1042 raised today", seed=15)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=16)

    rows = chunks.search_chunks(db, _flat_embedding(16), [site["id"]], k=30, query_text="1042")
    ids = {str(r["id"]) for r in rows}
    assert str(target["id"]) in ids, "bare 1042 must find RFI-1042 after the punctuation split"
    hit = next(r for r in rows if str(r["id"]) == str(target["id"]))
    assert hit["lexical_hit"] is True


def test_1042_does_not_match_rfi_1043(db):
    """The word-boundary property (same reasoning as PS4/PS40) applied to a
    numeric identifier: 1042 must not match a chunk that only contains 1043."""
    site = _site(db)
    decoy = _insert(db, site["id"], "RFI-1043 raised today", seed=17)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=18)

    rows = chunks.search_chunks(db, _flat_embedding(18), [site["id"]], k=30, query_text="1042")
    assert str(decoy["id"]) not in {str(r["id"]) for r in rows}, \
        "1042 must not match a chunk containing only 1043"


def test_exact_rfi_1042_still_matches(db):
    """The full identifier, hyphen and all, typed exactly as it appears in
    the text, must still be found -- the punctuation split must not break the
    exact-match case it is meant to widen."""
    site = _site(db)
    target = _insert(db, site["id"], "RFI-1042 raised today", seed=19)
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=20)

    rows = chunks.search_chunks(db, _flat_embedding(20), [site["id"]], k=30,
                                query_text="RFI-1042")
    ids = {str(r["id"]) for r in rows}
    assert str(target["id"]) in ids, "the exact identifier 'RFI-1042' must still be found"
    hit = next(r for r in rows if str(r["id"]) == str(target["id"]))
    assert hit["lexical_hit"] is True


# ---------------------------------------------------------------------------
# 2026-09-22 round-2 fix ("the keyword arm can actually fire", again): the
# controller ran the OWNER'S REAL QUESTION -- a full natural-language
# sentence, not a bare token -- through the real path and measured three
# chunks about bathroom tile, Twizel booking, and a concrete pour all match,
# none related to the question. Cause: lexical_terms() has only a 3-char
# floor and no stopword filter, so 'when'/'did'/'and'/'why' all survive, and
# one OR'd stopword matches nearly every chunk in the corpus. Every test
# above this point drives search_chunks with a BARE TOKEN as query_text
# ("PS4", "1042", ...) -- that shape cannot see this failure, because a bare
# token was never a stopword to begin with. These tests are the ones that can.
# ---------------------------------------------------------------------------

def test_every_query_stopword_is_actually_dropped_by_to_tsvector(db):
    """Pins lexical_terms.QUERY_STOPWORDS against its own authority: a word
    belongs in that list exactly when `to_tsvector('english', word)` yields
    nothing for it. QUERY_STOPWORDS is a hard-coded constant (query_terms()
    is pure Python -- no psycopg, no DB access, by the module's own design),
    so nothing else keeps it from silently drifting away from what a real
    Postgres actually calls a stopword except this test."""
    for word in sorted(QUERY_STOPWORDS):
        row = db.execute("SELECT to_tsvector('english', %s)", (word,)).fetchone()
        assert row[0] == "", (
            f"{word!r} is in QUERY_STOPWORDS but to_tsvector('english', {word!r}) "
            f"did not come back empty -- got {row[0]!r}. Either Postgres no longer "
            "treats it as a stopword, or it never was one and must not be filtered."
        )


def test_a_real_sentence_finds_its_chunk_and_not_three_unrelated_ones(db):
    """The exact shape that failed twice: a FULL NATURAL-LANGUAGE SENTENCE,
    not a bare token, run through the actual code path
    (query_terms -> or_query -> chunks.search_chunks), asserted in BOTH
    directions against a real Postgres:

      * it still finds the chunk that genuinely contains the identifier
        ("...pricing and PS4/light pole issues"), AND
      * it does NOT match chunks that merely share common/stop words --
        the exact three decoys the controller measured matching, verbatim.

    Token-level cases (the ones above) are necessary but were NOT sufficient
    to catch this -- this is the evidence for that."""
    site = _site(db)
    question = "when did request ps4? and why?"

    target = _insert(db, site["id"], "pricing and PS4/light pole issues", seed=21)
    decoy_tile = _insert(
        db, site["id"],
        "Bathroom tile and finish options were discussed with the client.",
        seed=22)
    decoy_twizel = _insert(
        db, site["id"],
        "Twizel booking and subs coordination for the week ahead.",
        seed=23)
    decoy_pour = _insert(
        db, site["id"],
        "The concrete pour is scheduled and the pump has been booked.",
        seed=24)
    # Filled with chunks close to the query embedding, matching NONE of
    # question's terms in chunk_text -- so if a decoy shows up, it can only
    # be because the keyword arm matched it, exactly the property under test.
    _fill_with_closer_unrelated_chunks(db, site["id"], query_seed=25)

    query_text = or_query(query_terms(question))
    # The bug this test exists to catch would make query_text carry a bare
    # stopword like 'and' -- assert the real extraction does not, so a
    # regression in query_terms itself fails HERE, not three lines down in a
    # confusing decoy-match failure.
    assert "and" not in query_text.split(" or ")
    assert "why" not in query_text.split(" or ")
    assert "did" not in query_text.split(" or ")
    assert "when" not in query_text.split(" or ")

    rows = chunks.search_chunks(db, _flat_embedding(25), [site["id"]], k=30,
                                query_text=query_text)
    ids = {str(r["id"]) for r in rows}

    assert str(target["id"]) in ids, \
        "the sentence must still find the chunk that genuinely contains PS4"
    assert str(decoy_tile["id"]) not in ids, \
        "a bare stopword must not match the bathroom-tile chunk"
    assert str(decoy_twizel["id"]) not in ids, \
        "a bare stopword must not match the Twizel-booking chunk"
    assert str(decoy_pour["id"]) not in ids, \
        "a bare stopword must not match the concrete-pour chunk"
