"""Unit tests for src/lexical_terms.py -- split out of lambda_ask_agent.py
(2026-09-22, "the keyword arm can actually fire") so lambda_rag_search.py can
build the keyword arm's OR query from the same extraction lambda_ask_agent.py
already used for its title-substring lexical ranking."""
import pytest

from lexical_terms import QUERY_STOPWORDS, lexical_terms, or_query, query_terms


def test_short_stopwords_are_dropped():
    assert lexical_terms("is it") == []


def test_identifier_shaped_short_tokens_survive_the_floor():
    # "b2" is 2 characters but letter+digit, exempt from the 3-char floor.
    assert "b2" in lexical_terms("check zone b2 today")


def test_two_char_words_below_the_floor_are_dropped():
    # "is"/"it" (2 chars, not identifier-shaped) are below the floor;
    # "was"/"the" (3 chars) clear it on length alone -- the floor is a raw
    # length check, not a stopword list.
    terms = lexical_terms("is it PS4 was requested")
    assert "is" not in terms
    assert "it" not in terms
    assert "ps4" in terms
    assert "was" in terms


def test_or_query_joins_with_the_literal_word_or():
    assert or_query(["ps4", "light", "pole"]) == "ps4 or light or pole"


def test_or_query_of_a_single_term_is_the_term_itself():
    assert or_query(["ps4"]) == "ps4"


def test_or_query_of_empty_list_is_empty_string():
    assert or_query([]) == ""


def test_lambda_ask_agent_reuses_this_exact_function():
    """lambda_ask_agent._lexical_terms must be THIS function, not a second
    hand-maintained copy that can drift from it -- that was the whole point
    of extracting it."""
    laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3")
    assert laa._lexical_terms is lexical_terms


# ---------------------------------------------------------------------------
# query_terms (2026-09-22, round-2 fix): lexical_terms() minus stopwords, for
# the keyword arm's OR query only. lexical_terms() itself is UNCHANGED --
# _aggregate_topics' title ranking in lambda_ask_agent.py still gets today's
# behavior (the tests above, and lambda_ask_agent's own English/Chinese
# ranking tests, still pass unmodified).
# ---------------------------------------------------------------------------

def test_query_terms_drops_stopwords_that_clear_the_length_floor():
    # The exact case that reached prod-shaped code and matched noise: 'when',
    # 'did', 'and', 'why' all clear lexical_terms's 3-char floor and are not
    # identifier-shaped, so lexical_terms alone lets them through.
    terms = query_terms("when did request ps4? and why?")
    for stop in ("when", "did", "and", "why"):
        assert stop not in terms
    assert "ps4" in terms
    assert "request" in terms


def test_query_terms_keeps_identifier_shaped_tokens():
    assert "b2" in query_terms("check zone b2 today")


def test_query_terms_of_an_all_stopword_sentence_is_empty():
    # Every word here is >= 3 chars and a stopword -- lexical_terms alone
    # would NOT reduce this to [], which is exactly the bug.
    assert query_terms("when was the and why") == []
    assert or_query(query_terms("when was the and why")) == ""


def test_query_terms_never_removes_a_non_stopword():
    assert query_terms("PS4 light pole") == lexical_terms("PS4 light pole")


def test_lexical_terms_itself_is_unaffected_by_query_terms_existing():
    # lexical_terms() must keep letting stopwords through -- _aggregate_topics'
    # title-substring ranking (lambda_ask_agent.py) depends on this today,
    # including two existing Chinese-question tests that pin its behavior.
    assert "was" in lexical_terms("is it PS4 was requested")


def test_query_stopwords_are_pure_alphabetic_disjoint_from_identifier_shapes():
    # lexical_terms's _IDENTIFIER exemption only fires on a token mixing a
    # letter and a digit; every stopword must be pure alphabetic so the two
    # never collide (see query_terms's docstring).
    for w in QUERY_STOPWORDS:
        assert w.isalpha(), f"{w!r} is not pure alphabetic"
