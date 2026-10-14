"""Voice rendering and intent for web answers (spec 2026-10-15 section 8).

The prompt tests drive the real general_answer/compose against the scripted
vendor from test_ask_knowledge_then_verify, so what is asserted is the prompt
text that would actually reach the model.
"""
import json

import pytest

import corroboration_client as client
import voice_web
import web_answer
from tests.unit.test_ask_knowledge_then_verify import (
    DRAFT_TEXT, RECORDS_ANSWER, SOURCES, SPRINKLER_Q, VERIFIED_TEXT, Vendor, reply)


# ------------------------------------------------------------- detect_intent

def test_search_online_is_stripped_from_the_question():
    assert voice_web.detect_intent(
        "Searching online. Find out the maximum length of the fire sprinklers.") == (
        True, "Find out the maximum length of the fire sprinklers.")


def test_look_it_up_online_mid_sentence():
    intent, q = voice_web.detect_intent("Can you look it up online what NZS 4541 says about hose length")
    assert intent is True
    assert "look" not in q.lower() and "NZS 4541" in q


@pytest.mark.parametrize("said", [
    "search the web for the minimum cover over a gas main",
    "Search the internet for pipe bedding rules",
    "please google that for me",
    "what does it say on the internet about slab curing",
    "check online what the clearance is",
    "SEARCH IT ONLINE what is the fall on a drain",
])
def test_every_phrase_in_the_list_is_recognised(said):
    assert voice_web.detect_intent(said)[0] is True


def test_a_phrase_that_is_the_whole_utterance_keeps_the_original():
    assert voice_web.detect_intent("google that") == (True, "google that")


def test_leading_connective_left_by_the_strip_is_trimmed():
    assert voice_web.detect_intent("And search online, what is the maximum fall on a drain") == (
        True, "what is the maximum fall on a drain")


@pytest.mark.parametrize("said", [
    "What did we search for yesterday",
    "online induction booked?",
    "who is on the site today",
    "",
])
def test_ordinary_questions_are_not_web_intent(said):
    assert voice_web.detect_intent(said) == (False, said)


def test_non_string_transcript_is_not_intent():
    assert voice_web.detect_intent(None) == (False, "")


# ----------------------------------------------------------------- speakable

def test_a_clean_two_sentence_answer_is_speakable():
    assert voice_web.speakable("According to NZS 4541, flexible sprinkler hose is limited to 1.8 metres. "
                               "I could not confirm that online.") is True


@pytest.mark.parametrize("text", [
    None, "", "   ",
    "See https://standards.govt.nz for details",
    "Go to www.standards.govt.nz",
    "Read [the standard](x) first",
    "Two things:\n- one\n- two",
    "   * indented bullet",
    "• a bullet",
    " ".join(["word"] * 61),
])
def test_each_reject_reason_is_not_speakable(text):
    assert voice_web.speakable(text) is False


def test_sixty_words_is_the_limit():
    assert voice_web.speakable(" ".join(["word"] * 60)) is True


def test_no_clear_answer_text():
    assert voice_web.NO_CLEAR_ANSWER == "I couldn't get a clear answer online for that."


def test_voice_rules_are_short_and_say_what_the_spec_asks():
    assert len(voice_web.VOICE_RULES.split()) < 80
    low = voice_web.VOICE_RULES.lower()
    for needle in ("two short", "according to", "url", "list", "online"):
        assert needle in low, needle


# ------------------------------------------------- prompts reach the model

def _run_general(monkeypatch, **kw):
    vendor = Vendor(
        web_draft=[reply(text=DRAFT_TEXT)],
        web_verify=[reply(text=VERIFIED_TEXT, results=SOURCES)])
    monkeypatch.setattr(client, "call", vendor)
    out = web_answer.general_answer(SPRINKLER_Q, 30, **kw)
    return vendor, out


def test_voice_false_leaves_both_prompts_byte_identical(monkeypatch):
    vendor, _ = _run_general(monkeypatch)
    assert vendor.of("web_draft")[0]["prompt"] == web_answer.DRAFT_PROMPT.format(question=SPRINKLER_Q)
    assert vendor.of("web_verify")[0]["prompt"] == web_answer.VERIFY_PROMPT.format(
        question=SPRINKLER_Q, draft=DRAFT_TEXT)
    for call in vendor.calls:
        assert voice_web.VOICE_RULES not in call["prompt"]


def test_voice_true_appends_the_rules_to_draft_and_verify_only(monkeypatch):
    plain, _ = _run_general(monkeypatch)
    voiced, out = _run_general(monkeypatch, voice=True)
    for tag in ("web_draft", "web_verify"):
        base = plain.of(tag)[0]["prompt"]
        assert voiced.of(tag)[0]["prompt"] == base + "\n\n" + voice_web.VOICE_RULES
    # Nothing else about the chain changes: same calls, same result.
    assert voiced.callers == plain.callers
    assert out["status"] == "verified"


def _run_compose(monkeypatch, **kw):
    vendor = Vendor(web_compose=[reply(text=json.dumps(
        {"answer": RECORDS_ANSWER + " NZS 4541 says 1.8 m.", "conflicts": []}))])
    monkeypatch.setattr(client, "call", vendor)
    general = {"answer": VERIFIED_TEXT}
    out = web_answer.compose(RECORDS_ANSWER, general, SPRINKLER_Q, 30, **kw)
    return vendor, out


def test_compose_voice_false_is_byte_identical(monkeypatch):
    vendor, out = _run_compose(monkeypatch)
    assert out["composed"] is True
    general_text = web_answer._MARKER.sub("", VERIFIED_TEXT).strip()
    assert vendor.of("web_compose")[0]["prompt"] == web_answer.COMPOSE_PROMPT.format(
        question=SPRINKLER_Q, records=RECORDS_ANSWER, general=general_text)


def test_compose_voice_true_appends_the_rules(monkeypatch):
    plain, _ = _run_compose(monkeypatch)
    voiced, out = _run_compose(monkeypatch, voice=True)
    assert voiced.of("web_compose")[0]["prompt"] == (
        plain.of("web_compose")[0]["prompt"] + "\n\n" + voice_web.VOICE_RULES)
    assert out["composed"] is True


@pytest.mark.parametrize("said", [
    "can you google that",
    "is it on the internet",
    "Did the survey say to search online",
])
def test_a_fragment_left_after_stripping_keeps_the_original(said):
    assert voice_web.detect_intent(said) == (True, said)


def test_a_hyphen_after_the_phrase_is_not_the_phrase():
    said = "where is the internet-connected panel"
    assert voice_web.detect_intent(said) == (False, said)
    said = "the on the internet-facing router"
    assert voice_web.detect_intent(said)[0] is False


@pytest.mark.parametrize("said", [
    "check online induction",
    "Check online induction status for Hanlon",
])
def test_check_online_needs_a_clause_word_after_it(said):
    assert voice_web.detect_intent(said) == (False, said)


def test_check_online_before_a_clause_word_or_at_the_end_is_intent():
    assert voice_web.detect_intent("check online whether the span is 1.8 metres") == (
        True, "whether the span is 1.8 metres")
    # A remainder ending on a stopword is a fragment, so the original is kept.
    assert voice_web.detect_intent("check online what the max span is") == (
        True, "check online what the max span is")
    assert voice_web.detect_intent("what is the max span, check online")[0] is True


def test_a_leading_minus_sign_is_not_a_bullet():
    assert voice_web.speakable("-5 degrees overnight") is True
    assert voice_web.speakable("-  a dash bullet") is False
    assert voice_web.speakable("*bold*") is True
