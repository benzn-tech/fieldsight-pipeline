import os

import pytest

from askv2 import harness

import ask_orchestrator
import lambda_ask_agent
import lambda_rag_search


def test_import_env_is_the_eval_one():
    assert lambda_ask_agent.RAG_SEARCH_FUNCTION == "eval-in-process"
    assert lambda_rag_search.INDEX_FAMILY == "report"
    assert os.environ["ENABLE_WEB_ANSWER"] == "false"
    assert os.environ["ASK_CONVERSATION_MEMORY"] == "false"
    assert "ASK_DEADLINE_SECONDS" not in os.environ


def test_fallback_synthesis_has_no_anthropic_key_so_it_fails_without_a_call():
    import llm_utils
    assert os.environ["ANTHROPIC_API_KEY"] == "" and llm_utils.ANTHROPIC_API_KEY == ""


def test_assert_seams_passes_on_the_exported_source():
    harness.assert_seams("report", rerank=False)


def test_a_moved_seam_stops_the_run(monkeypatch):
    monkeypatch.delattr(ask_orchestrator, "_cited_card_refs")
    with pytest.raises(SystemExit, match="seam moved: ask_orchestrator._cited_card_refs"):
        harness.assert_seams("report", rerank=False)


def test_a_wrong_family_stops_the_run():
    with pytest.raises(SystemExit, match="INDEX_FAMILY"):
        harness.assert_seams("session", rerank=False)


def test_answer_without_the_tools_seam_stops_the_run(monkeypatch):
    def answer(body, *, channel, on_state, chat=None, turn_id=None):
        return {}
    monkeypatch.setattr(ask_orchestrator, "answer", answer)
    with pytest.raises(SystemExit, match="ask_orchestrator.answer"):
        harness.assert_seams("report", rerank=False)
