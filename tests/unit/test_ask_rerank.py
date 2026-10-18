"""Reranking on the Ask path, and the rule that it may never cost an answer.

Ask is synchronous behind a 29s API Gateway ceiling and already measured ~24s
end to end at k=8, which is why its default k is 5. Reranking is a ~4s fixed
round trip (measured live: K=16 -> 3.6s, K=100 -> 4.6s -- near-flat in
candidate count). It improves an ordering on an answer that is already
shippable, so every failure path here returns the cosine order the caller
already had rather than failing the question.

It lives in ask-agent, not rag-search: rag-search is in-VPC and this account
has no NAT and only S3 / DynamoDB / cognito-idp endpoints, so a DashScope call
from there would hang to the socket timeout rather than fail fast.
"""
import pytest

agent = pytest.importorskip("lambda_ask_agent")
# `lambda_ask_agent` imports dashscope_utils INSIDE its functions (the legacy
# hand-built prod zip does not carry the module, so a top-level import would
# break that deploy). Patch the real module, not an attribute of the agent.
dashscope_utils = pytest.importorskip("dashscope_utils")


def _chunk(text, key="extractions/A/2026-09-01/sid1.json"):
    return {"chunk_text": text, "source_s3_key": key, "topic_id": None}


CHUNKS = [_chunk("c%d" % i, "key%d" % i) for i in range(10)]


# --------------------------------------------------------------------------
# The safety rule
# --------------------------------------------------------------------------

def test_disabled_is_byte_for_byte_the_old_behaviour(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reranked while disabled")))
    assert agent._rerank_chunks("q", CHUNKS, 5) == CHUNKS[:5]


# --------------------------------------------------------------------------
# Task 4, second downstream (CONTROLLER AMENDMENT, 2026-09-20/21): with
# reranking off (both environments today -- see src/repositories/search_sql.py
# and lambda_ask_agent.py's own comment above RERANK_ENABLED), build_search_sql
# now APPENDS keyword-only rows (lexical_hit=True) after every
# vector row. `chunks[:keep]` alone silently discards all of them whenever the
# vector arm is saturated at `keep` -- Ask's answering path would then never
# see the literal token this whole plan exists to surface, even though the
# search box (via _aggregate_topics) does. These tests pin the chosen policy:
# reserve exactly ONE slot in the kept window for the first keyword-only row
# arrival order would otherwise cut, at the cost of the single weakest
# (last) vector-arm row in that window -- never a wholesale swap.
# --------------------------------------------------------------------------

def _lex_chunk(text, key):
    c = _chunk(text, key)
    c["lexical_hit"] = True
    c["distance"] = 0.3  # real cosine distance; only the flag marks it keyword-only
    return c


def test_a_keyword_only_row_beyond_k_is_admitted_when_vector_arm_saturates_k(monkeypatch):
    """The exact CONTROLLER AMENDMENT scenario: k vector rows fill the window,
    one keyword-only row arrives after all of them (Task 3's
    append-only ordering). Naive chunks[:keep] would drop it -- the fix must
    reserve a slot for it."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    vector_rows = [_chunk("v%d" % i, "vkey%d" % i) for i in range(5)]
    lex_row = _lex_chunk("ps4-mention", "lexkey")
    out = agent._rerank_chunks("PS4", vector_rows + [lex_row], 5)
    assert len(out) == 5
    assert any(c is lex_row for c in out), \
        "a keyword-only row past the k cut must not be silently discarded"
    # Only the single weakest (last) vector row is displaced -- never all of them.
    assert [c["chunk_text"] for c in out] == ["v0", "v1", "v2", "v3", "ps4-mention"]


def test_a_keyword_only_row_already_inside_k_is_left_alone(monkeypatch):
    """No truncation needed to admit it -- nothing should be reordered."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    lex_row = _lex_chunk("ps4-mention", "lexkey")
    chunks = [_chunk("v0", "k0"), lex_row, _chunk("v1", "k1")]
    out = agent._rerank_chunks("PS4", chunks, 5)
    assert out == chunks


def test_keep_zero_admits_nothing(monkeypatch):
    """Critical #3 (2026-09-20 review): `keep=0` reaches this function via
    `k=int(body.get("k", 5))`, which has no floor. The old
    `head[:-1] + [tail_hit]` computed `[] + [tail_hit]` at keep=0 -- one row
    despite `keep == 0`, violating "never return more than `keep` rows".
    There is no slot to reserve and nothing to swap into at keep=0."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    vector_rows = [_chunk("v%d" % i, "vkey%d" % i) for i in range(3)]
    lex_row = _lex_chunk("ps4-mention", "lexkey")
    out = agent._rerank_chunks("PS4", vector_rows + [lex_row], 0)
    assert out == []


def test_keep_one_never_displaces_the_top_semantic_match(monkeypatch):
    """Critical #2 (2026-09-20 review): the docstring claimed "a strong
    semantic match at position 0 can never be displaced by this rule", but at
    keep=1, head[-1] IS head[0] -- the old code swapped it out for the
    keyword-only row anyway. `k=1` is client-reachable the same way `k=0` is.
    Chosen behaviour: at keep=1 there is only one slot and it is also
    position 0, so the guarantee wins -- no admission happens and the
    semantic top match is returned unchanged. (Accepted cost: at keep=1 a
    keyword-only hit beyond the cut is never admitted -- there is no second,
    less valuable slot to give up instead.)"""
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    v0 = _chunk("v0", "vkey0")
    lex_row = _lex_chunk("ps4-mention", "lexkey")
    out = agent._rerank_chunks("PS4", [v0, lex_row], 1)
    assert out == [v0]


def test_reservation_never_touches_more_than_one_vector_row(monkeypatch):
    """Multiple keyword-only rows beyond k: only one is admitted, capping the
    cost at exactly one displaced vector row -- a residual gap, accepted on
    purpose (see the policy note above _rerank_chunks)."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", False)
    vector_rows = [_chunk("v%d" % i, "vkey%d" % i) for i in range(5)]
    lex_rows = [_lex_chunk("lex-a", "lexkeyA"), _lex_chunk("lex-b", "lexkeyB")]
    out = agent._rerank_chunks("q", vector_rows + lex_rows, 5)
    assert len(out) == 5
    assert sum(1 for c in out if c.get("lexical_hit")) == 1
    assert [c["chunk_text"] for c in out] == ["v0", "v1", "v2", "v3", "lex-a"]


@pytest.mark.parametrize("outcome", [None, []])
def test_a_reranker_that_cannot_answer_costs_nothing(monkeypatch, outcome):
    """`rerank` returns None on timeout, non-200, a bad shape, or a missing
    key. All of them mean: keep going with what we had."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(dashscope_utils, "rerank", lambda *a, **k: outcome)
    out = agent._rerank_chunks("q", CHUNKS, 5)
    assert [c["chunk_text"] for c in out] == ["c0", "c1", "c2", "c3", "c4"]


def test_it_reorders_and_truncates_to_the_context_size(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda q, docs, n: [9, 3, 0, 7, 1])
    out = agent._rerank_chunks("q", CHUNKS, 5)
    assert [c["chunk_text"] for c in out] == ["c9", "c3", "c0", "c7", "c1"]


def test_nothing_to_choose_from_is_left_alone(monkeypatch):
    """Fewer candidates than the context size means the reranker has no
    decision to make -- spending 4s to confirm that is pure latency."""
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reranked a set smaller than k")))
    assert agent._rerank_chunks("q", CHUNKS[:3], 5) == CHUNKS[:3]


# --------------------------------------------------------------------------
# Candidate selection: per search arm, not per source (2026-10-16 spec)
# --------------------------------------------------------------------------

def _row(text, lex=False, key="one-session"):
    return {"chunk_text": text, "source_s3_key": key, "topic_id": None,
            "lexical_hit": lex}


def test_select_spills_unused_keyword_slots_to_vector_rows():
    vec = [_row("v%d" % i) for i in range(40)]
    out = agent._select_candidates(vec, total=32)
    assert [c["chunk_text"] for c in out] == ["v%d" % i for i in range(32)]


def test_select_caps_keyword_rows_and_puts_vector_rows_first(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_KEYWORD_CANDIDATES", 8)
    rows = [_row("v%d" % i) for i in range(32)] +         [_row("k%d" % i, lex=True) for i in range(20)]
    out = [c["chunk_text"] for c in agent._select_candidates(rows, total=32)]
    assert out == ["v%d" % i for i in range(24)] + ["k%d" % i for i in range(8)]


def test_select_reaches_a_keyword_row_past_position_32(monkeypatch):
    """build_search_sql appends every keyword-only row after every vector
    row, so an arrival-order total would cut this one first (v1-35)."""
    monkeypatch.setattr(agent, "RERANK_KEYWORD_CANDIDATES", 8)
    rows = [_row("v%d" % i) for i in range(33)] + [_row("kw", lex=True)]
    out = [c["chunk_text"] for c in agent._select_candidates(rows, total=32)]
    assert len(out) == 32 and out[-1] == "kw"


def test_select_keeps_many_rows_from_one_source():
    """One source key is a whole day or session; capping it was the defect."""
    rows = [_row("s%d" % i, key="same") for i in range(10)]
    assert len(agent._select_candidates(rows, total=32)) == 10


def test_select_collapses_duplicate_text_and_keeps_the_keyword_flag():
    rows = [_row("same"), _row("other"), _row("same", lex=True)]
    out = agent._select_candidates(rows, total=32)
    assert [c["chunk_text"] for c in out] == ["same", "other"]
    assert out[0]["lexical_hit"] is True


def test_select_does_not_collapse_empty_texts():
    rows = [_row(""), _row(""), _row("")]
    assert len(agent._select_candidates(rows, total=32)) == 3
