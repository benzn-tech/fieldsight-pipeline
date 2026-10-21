"""Ask: say so when an explicit web search fails.

Spec: docs/superpowers/specs/2026-10-19-ask-explicit-web-failure-design.md §3, §5.
The judge, tools and web step are fakes (see test_ask_orchestrator_judge). No network.
"""
import logging
import threading

import ask_orchestrator as orch
import lambda_ask_agent as laa
import llm_utils
import web_answer
from llm_tools_client import ChatTurn
from tests.unit.test_ask_orchestrator_judge import env  # noqa: F401  (autouse traps)
from tests.unit.test_ask_orchestrator_judge import (ScriptChat, _route, body, final,
                                                    precheck_says, run, voice_body)
from tests.unit.test_ask_orchestrator_round1 import FakeTools, _block

ALONE = orch.WEB_UNREACHABLE + " " + orch.WEB_TRY_AGAIN
FAILED_WEB = {"status": "unverified", "answer": "", "sources": []}
ASK = "Search online what concrete cover does a slab on grade need"


def web_fails(monkeypatch, **kw):
    monkeypatch.setattr(web_answer, "general_answer", lambda q, b, **k:
                        web_answer._block(failed=True, **kw))


def no_records(call):
    return [], {"empty_reason": "no_match"}


# ---- main path

def test_no_records_answer_becomes_the_two_fixed_sentences(monkeypatch, caplog):
    web_fails(monkeypatch)
    chat = ScriptChat(final("The records do not cover concrete cover.", basis="none"))
    with caplog.at_level(logging.INFO):
        out = run(body(ASK), chat, tools=FakeTools(no_records))
    assert out["answer"] == ALONE
    assert out["citations"] == [] and out["grounded"] is False
    assert out["web"] == FAILED_WEB and not out.get("from_web")
    route = _route(caplog)
    assert route["policy"] == "required" and route["web_failed"] == "upstream"
    assert "concrete" not in str(route) and "cover" not in str(route)


def test_records_answer_keeps_its_cards_behind_the_prefix(monkeypatch):
    web_fails(monkeypatch)
    chat = ScriptChat(final("We poured the B2 slab [R1].", basis="records"))
    out = run(body(ASK), chat)
    assert out["answer"] == orch.WEB_UNREACHABLE + "\n\nWe poured the B2 slab [1]."
    assert [c["topic_title"] for c in out["citations"]] == ["Pour B2"]
    assert out["grounded"] is True and out["web"] == FAILED_WEB


def test_timeout_inside_the_budget_is_logged_as_timeout(monkeypatch, caplog):
    web_fails(monkeypatch, timed_out=True)
    with caplog.at_level(logging.INFO):
        out = run(body(ASK), ScriptChat(final("Nothing.", basis="none")),
                  tools=FakeTools(no_records))
    assert out["answer"] == ALONE
    assert _route(caplog)["web_failed"] == "timeout"


def test_web_late_fires_and_rides_beside_web_late(monkeypatch, caplog):
    release = threading.Event()

    def slow(q, b, **kw):
        release.wait(5)
        return _block()
    monkeypatch.setattr(web_answer, "general_answer", slow)
    monkeypatch.setattr(orch, "WEB_WAIT_S", 0.3)
    try:
        with caplog.at_level(logging.INFO):
            out = run(body(ASK), ScriptChat(final("Nothing.", basis="none")),
                      tools=FakeTools(no_records))
    finally:
        release.set()
    assert out["answer"] == ALONE
    route = _route(caplog)
    assert route["web_late"] is True and route["web_failed"] == "timeout"


# ---- voice

def test_voice_short_records_answer_is_prefixed(monkeypatch):
    web_fails(monkeypatch)
    chat = ScriptChat(final("We poured B2 [R1].", basis="records"))
    out = run(voice_body(ASK, web_intent=True), chat, channel="voice")
    assert out["answer"] == orch.WEB_UNREACHABLE + " We poured B2."
    assert "web" not in out


def test_voice_long_records_answer_keeps_its_first_two_sentences(monkeypatch):
    web_fails(monkeypatch)
    long = ("We poured B2 [R1]. The pump arrived late. " + "Then the crew cleaned up. " * 12)
    out = run(voice_body(ASK, web_intent=True), ScriptChat(final(long)), channel="voice")
    assert out["answer"] == orch.WEB_UNREACHABLE + " We poured B2. The pump arrived late."


def test_voice_unspeakable_head_says_only_the_fixed_sentences(monkeypatch):
    web_fails(monkeypatch)
    one = "We poured B2 [R1] " + "and then more work " * 20 + "today."
    out = run(voice_body(ASK, web_intent=True), ScriptChat(final(one)), channel="voice")
    assert out["answer"] == ALONE


# ---- _fallback exits

def test_fallback_synthesis_is_prefixed_and_keeps_its_cards(monkeypatch):
    web_fails(monkeypatch)
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: ("Synth [1].", None))
    out = run(body(ASK), ScriptChat(ChatTurn(error="HTTP 500")))
    assert out["answer"] == orch.WEB_UNREACHABLE + "\n\nSynth [1]."
    assert out["citations"][0]["topic_title"] == "Pour B2"
    assert out["web"] == FAILED_WEB


def test_fallback_no_records_drops_the_latest_before_sentence(monkeypatch):
    web_fails(monkeypatch)

    def rows(call):
        return [], {"empty_reason": "nothing_in_range",
                    "latest_before": {"date": "2026-10-12", "from": "topics"}}
    out = run(body(ASK), ScriptChat(ChatTurn(error="HTTP 500")), tools=FakeTools(rows))
    assert out["answer"] == ALONE and out["citations"] == []
    assert out["grounded"] is False


def test_fallback_without_synthesis_is_still_an_error(monkeypatch):
    web_fails(monkeypatch)
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: (None, "deadline"))
    out = run(body(ASK), ScriptChat(ChatTurn(error="deadline", timed_out=True)))
    assert out["error"] == "No answer" and out["answer"] == ""


# ---- unchanged

def test_verified_web_is_unchanged(monkeypatch):
    monkeypatch.setattr(web_answer, "general_answer", lambda q, b, **k: _block())
    out = run(body(ASK), ScriptChat(final("Cover is 40 mm [W1].", basis="web")))
    assert not out["answer"].startswith(orch.WEB_UNREACHABLE)
    assert out["from_web"] is True


def test_draft_only_unverified_web_is_unchanged(monkeypatch):
    monkeypatch.setattr(web_answer, "general_answer", lambda q, b, **k:
                        _block(answer="Cover is 40 mm.", n_sources=0, status="unverified",
                               failed=True))
    out = run(body(ASK), ScriptChat(final("Cover is 40 mm [W1].", basis="web")))
    assert not out["answer"].startswith(orch.WEB_UNREACHABLE)


def test_refused_web_is_unchanged(monkeypatch):
    monkeypatch.setattr(web_answer, "general_answer", lambda q, b, **k:
                        web_answer._block(failed=True, refused="empty"))
    out = run(body(ASK), ScriptChat(final("Nothing.", basis="none")),
              tools=FakeTools(no_records))
    assert out["answer"] == "Nothing."


def test_auto_policy_with_a_failed_web_is_unchanged(monkeypatch, caplog):
    precheck_says(monkeypatch, "yes")
    web_fails(monkeypatch)
    with caplog.at_level(logging.INFO):
        out = run(body("What concrete cover does a slab on grade need?"),
                  ScriptChat(final("Nothing.", basis="none")), tools=FakeTools(no_records))
    assert out["answer"] == "Nothing."
    assert _route(caplog)["policy"] == "auto" and "web_failed" not in _route(caplog)


def test_not_provisioned_is_unchanged(monkeypatch):
    out = run(body(ASK), ScriptChat(),
              tools=FakeTools(lambda call: ([], {"error": "not_provisioned"})))
    assert out["answer"] == orch.NO_RECORDS
