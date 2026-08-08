"""Unit: a truncated response says so instead of looking like bad JSON
(P1-2 Task 6).

Output truncation is invisible today. It surfaces as unparseable JSON ->
RuntimeError -> S3-event retry -> the full, paid LLM call re-runs into the exact
same wall, forever. That is BUG-43's shape: an expensive operation whose
precondition can never be met on retry.

`transcript_stats` does NOT cover this -- that records INPUT truncation only.
"""
import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def test_a_response_that_stops_mid_json_is_recognised():
    assert ex.looks_truncated('{"topics": [{"title": "Slab", "summary": "the pour wa')


def test_complete_json_is_not_a_ceiling_hit():
    assert ex.looks_truncated('{"topics": []}') is False
    assert ex.looks_truncated('  {"topics": []}  \n') is False


def test_prose_around_the_json_is_not_a_ceiling_hit():
    # Models wrap JSON in ``` fences and preamble; extract_json handles it. A
    # complete-but-wrapped response is not truncation and must not be logged as
    # one, or the log line stops meaning anything.
    assert ex.looks_truncated('Here you go:\n```json\n{"topics": []}\n```') is False


def test_an_empty_response_is_not_reported_as_a_ceiling_hit():
    # Empty is its own failure (refusal, filter, transport) and raising the
    # token limit would not touch it.
    assert ex.looks_truncated("") is False
    assert ex.looks_truncated(None) is False


def test_the_anthropic_ceiling_scales_past_the_old_8000():
    # claude-sonnet-4-6 supports far more output, and Timeout 600 /
    # LLM_HTTP_TIMEOUT 540 leave room for it.
    assert ex.max_tokens_for(n_segments=40) > 8000


def test_the_ceiling_is_still_bounded():
    assert ex.max_tokens_for(n_segments=10_000) <= 16000
