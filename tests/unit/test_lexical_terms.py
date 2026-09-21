"""Unit tests for src/lexical_terms.py -- split out of lambda_ask_agent.py
(2026-09-22, "the keyword arm can actually fire") so lambda_rag_search.py can
build the keyword arm's OR query from the same extraction lambda_ask_agent.py
already used for its title-substring lexical ranking."""
import pytest

from lexical_terms import lexical_terms, or_query


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
