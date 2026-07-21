"""
Tests for lambda_ask_agent voice-prompt selection — SP-Ask Task 4 (TDD).
Env setup mirrors test_lambda_ask_agent_rag.py (dummy AWS/Anthropic keys +
RAG_SEARCH_FUNCTION so the RAG branch is reachable).
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")
import llm_utils  # noqa: E402
import dashscope_utils  # noqa: E402


CHUNKS = [{
    "chunk_text": "Slab pour finished on Building A.",
    "topic_title": "Slab Pour", "report_date": "2026-02-09",
    "site_name": "Ellesmere", "topic_id": "t-1", "site_id": "s-1",
    "source_s3_key": "reports/2026-02-09/Ben/daily_report.json",
}]


def test_screen_mode_is_byte_identical_to_before():
    # mode absent and mode=None must both produce the EXISTING screen prompt.
    default = laa.build_rag_prompt("q?", CHUNKS)
    explicit_none = laa.build_rag_prompt("q?", CHUNKS, mode=None)
    assert default == explicit_none
    assert laa.RAG_SYSTEM_CONTEXT in default
    assert laa.RAG_SYSTEM_CONTEXT_VOICE not in default


def test_voice_mode_selects_voice_context():
    voice = laa.build_rag_prompt("q?", CHUNKS, mode="voice")
    assert laa.RAG_SYSTEM_CONTEXT_VOICE in voice
    assert laa.RAG_SYSTEM_CONTEXT not in voice
    # excerpts + question still present (same retrieval body)
    assert "Slab pour finished on Building A." in voice
    assert "q?" in voice


def test_voice_prompt_is_speech_shaped():
    # no markdown / no [n] citation instruction in the spoken prompt
    v = laa.RAG_SYSTEM_CONTEXT_VOICE
    assert "[n]" not in v
    assert "markdown" not in v.lower()


def test_unknown_mode_falls_back_to_screen():
    assert laa.build_rag_prompt("q?", CHUNKS, mode="search") == laa.build_rag_prompt("q?", CHUNKS)


def test_rag_answer_threads_mode_voice(monkeypatch):
    captured = {}

    def fake_call_llm(prompt, max_tokens=4096, force_json=False):
        captured["prompt"] = prompt
        return "Spoken answer.", None

    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])

    class FakeClient:
        def invoke(self, FunctionName, InvocationType, Payload):
            return {"Payload": io.BytesIO(json.dumps({"chunks": CHUNKS}).encode("utf-8"))}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeClient())
    monkeypatch.setattr(llm_utils, "call_llm", fake_call_llm)

    result = laa._rag_answer({"question": "q?", "caller_sub": "sub-1", "mode": "voice"})

    assert laa.RAG_SYSTEM_CONTEXT_VOICE in captured["prompt"]
    assert result["answer"] == "Spoken answer."
