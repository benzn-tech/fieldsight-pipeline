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
# Diversity: the thing that makes widening mean anything
# --------------------------------------------------------------------------

def test_one_recording_cannot_fill_the_candidate_list(monkeypatch):
    """Adjacent transcript windows overlap by two turns and one session's
    windows are near-identical, so positions 6..32 of a cosine ranking are
    dominated by neighbours of the same few sessions. Without this cap the
    reranker is handed near-duplicates and re-orders duplicates."""
    monkeypatch.setattr(agent, "RERANK_PER_SESSION_CAP", 2)
    same = [_chunk("s%d" % i, "one-session") for i in range(6)]
    other = [_chunk("other", "second-session")]
    assert [c["chunk_text"] for c in agent._diversify(same + other, 2)] == \
        ["s0", "s1", "other"]


def test_diversify_keeps_the_incoming_order(monkeypatch):
    """It is a filter, not a sort. Reordering here would silently become the
    ranking whenever the reranker declines."""
    mixed = [_chunk("a", "k1"), _chunk("b", "k2"), _chunk("c", "k1"),
             _chunk("d", "k3")]
    assert [c["chunk_text"] for c in agent._diversify(mixed, 5)] == \
        ["a", "b", "c", "d"]


def test_chunks_with_no_source_key_are_not_collapsed_together(monkeypatch):
    """A missing key must not make every such chunk look like one session and
    get capped away. Distinct objects stay distinct."""
    anon = [{"chunk_text": "x%d" % i} for i in range(4)]
    assert len(agent._diversify(anon, 1)) == 4


def test_the_cap_is_applied_before_the_reranker_sees_anything(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(agent, "RERANK_PER_SESSION_CAP", 2)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda q, docs, n: seen.update(n=len(docs)) or list(range(len(docs))))
    dupes = [_chunk("s%d" % i, "one-session") for i in range(8)]
    agent._rerank_chunks("q", dupes, 5)
    assert seen["n"] == 2
