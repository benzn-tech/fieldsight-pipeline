"""Which metric a question asks for, or nothing.

Rules, not a classifier, for the reason `query_slots` is: a rule that MISSES
returns None and the question falls through to RAG — today's behaviour, so a miss
costs nothing new. A classifier that MISFIRES routes a retrieval question to a
counter and answers a different question, with a number, confidently.

Not `pytest.importorskip`: this module is ours and pure. importorskip would turn
"does not exist yet" into a green skip.
"""
import pytest

import metric_slots as ms


@pytest.mark.parametrize("q,want", [
    ("昨天我录制了多长时间", "duration"),
    ("how long did I record yesterday", "duration"),
    ("我录了多久", "duration"),
    ("上周总共录了多长时间", "duration"),
    ("how much time did I record last week", "duration"),

    ("昨天拍了多少张照片", "count_photos"),
    ("how many photos did I take", "count_photos"),
    ("前天几张照片", "count_photos"),
    ("how many pictures yesterday", "count_photos"),

    ("昨天录了几次", "count_sessions"),
    ("how many recordings yesterday", "count_sessions"),
    ("这周录了几段", "count_sessions"),

    ("昨天有多少 safety 的问题", "count_findings_safety"),
    ("how many safety issues yesterday", "count_findings_safety"),
    ("多少安全问题", "count_findings_safety"),

    ("昨天有多少 QA 的问题", "count_findings_quality"),
    ("how many quality issues", "count_findings_quality"),
    ("多少质量问题", "count_findings_quality"),
])
def test_a_metric_question_is_recognised(q, want):
    assert ms.detect(q) == want


@pytest.mark.parametrize("q", [
    "昨天发生了什么",
    "what happened yesterday",
    "混凝土的问题怎么解决",
    "who is responsible for the door schedule",
    # Asks WHAT, not HOW MANY. A countable noun is not a metric question.
    "上周的安全问题是什么",
    "tell me about the safety issues",
    "show me yesterday's photos",
    "",
    "   ",
    None,
])
def test_a_retrieval_question_falls_through(q):
    """None means "not mine" and the caller keeps doing what it does today.

    Every false positive here is a question answered with a count nobody asked
    for, which is strictly worse than a miss: the miss costs nothing new, and the
    false positive replaces a good answer with a wrong-shaped one.
    """
    assert ms.detect(q) is None


def test_a_question_wanting_both_a_count_and_the_items_falls_through():
    """"How many safety issues and what were they" wants prose. A counter answers
    half of it and looks like it answered all of it."""
    assert ms.detect("昨天有多少安全问题，都是什么") is None
    assert ms.detect("how many safety issues were there and what were they") is None
    assert ms.detect("多少质量问题？有哪些") is None


def test_safety_and_quality_are_not_confused():
    assert ms.detect("多少 QA 问题") == "count_findings_quality"
    assert ms.detect("多少 safety 问题") == "count_findings_safety"
    assert ms.detect("how many quality problems") == "count_findings_quality"


def test_a_domain_word_beats_the_generic_nouns():
    """"How many safety issues did I record" contains both a domain word and a
    recording word. It is a findings question — the domain is the subject and
    "record" is incidental."""
    assert ms.detect("how many safety issues did I record yesterday") == "count_findings_safety"


def test_a_non_string_is_not_a_crash():
    for junk in (None, 12, [], {}, 3.5, True):
        assert ms.detect(junk) is None
