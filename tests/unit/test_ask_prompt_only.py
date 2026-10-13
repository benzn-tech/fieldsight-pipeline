"""
Tests for the `prompt_only` mode of lambda_ask_agent (voice agent phase 2,
spec 2026-10-14 section 4): every Ask rule runs up to the single prompt build,
then the handler returns the prompt plus the resolved LLM request instead of
calling the model. A Node streaming endpoint does the synthesis.

Fixtures mirror test_lambda_ask_agent_rag.py (tests/unit has no package, so they are copied).
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
import ask_rewrite  # noqa: E402

CHUNK_A = {
    "id": "c-1", "chunk_text": "Backfill behind the east wall is due Friday.",
    "chunk_type": "report", "topic_id": "t-1",
    "source_s3_key": "reports/2026-02-09/Ben/daily_report.json", "metadata": {},
    "topic_title": "Backfill", "topic_summary": "Backfill schedule",
    "report_date": "2026-02-09", "site_id": "s-1", "site_name": "Ellesmere",
    "site_slug": "ellesmere", "distance": 0.1,
}
CHUNK_B = {
    "id": "c-2", "chunk_text": "Crew confirmed the backfill order.",
    "chunk_type": "transcript", "topic_id": "t-2",
    "source_s3_key": "transcripts/Ben/2026-02-09/seg1.json", "metadata": {},
    "topic_title": "Site walk", "topic_summary": "",
    "report_date": "2026-02-09", "site_id": "s-1", "site_name": "Ellesmere",
    "site_slug": "ellesmere", "distance": 0.2,
}


class FakeLambdaClient:
    """Stand-in for the rag-search lambda client (same shape as the rag tests)."""

    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "Payload": Payload})
        return {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}


def wire(monkeypatch, *, chunks):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    fake_client = FakeLambdaClient({"chunks": chunks})
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake_client)
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda prompt, max_tokens=4096, force_json=False, **kw: ("Grounded answer [1].", None))
    return fake_client


def make_event(question, caller_sub="sub-1"):
    return {"question": question, "caller_sub": caller_sub}


def invoke(event):
    resp = laa.lambda_handler(event, None)
    assert "body" in resp
    return json.loads(resp["body"])


CHUNKS = [CHUNK_A, CHUNK_B]


def _forbid_llm(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("call_llm must not run under prompt_only")
    monkeypatch.setattr(llm_utils, "call_llm", boom)


def _expected_llm(prompt, voice_model):
    req = llm_utils.build_qwen_payload(
        prompt, laa.MAX_ANSWER_TOKENS, False,
        enable_thinking=(False if voice_model else None), model=voice_model)
    return {k: req.get(k) for k in ("model", "reasoning", "max_tokens", "temperature")}


def _event(**extra):
    ev = make_event("when is the backfill due")
    ev.update({"mode": "voice", "prompt_only": True})
    ev.update(extra)
    return ev


@pytest.mark.parametrize("voice_env,voice_model", [("", None), ("none", None),
                                                   ("google/gemini-3.8-flash", "google/gemini-3.8-flash")])
def test_prompt_only_returns_prompt_and_resolved_llm(monkeypatch, voice_env, voice_model):
    wire(monkeypatch, chunks=CHUNKS)
    _forbid_llm(monkeypatch)
    monkeypatch.setenv("ASK_VOICE_MODEL", voice_env)
    body = invoke(_event())
    assert body["kind"] == "prompt"
    assert "backfill" in body["prompt"]
    assert body["chunk_ids"] == ["c-1", "c-2"]
    assert body["rewritten"] is False
    assert body["history_turns"] == 0
    assert body["llm"]["model"]  # resolved, never "none"
    assert body["llm"] == _expected_llm(body["prompt"], voice_model)
    if voice_model:
        assert body["llm"]["model"] == voice_model


def test_screen_mode_ignores_the_voice_model(monkeypatch):
    """The voice model only applies to mode=voice, exactly as on the normal path."""
    wire(monkeypatch, chunks=CHUNKS)
    _forbid_llm(monkeypatch)
    monkeypatch.setenv("ASK_VOICE_MODEL", "google/gemini-3.8-flash")
    body = invoke(_event(mode="ask"))
    assert body["kind"] == "prompt"
    assert body["llm"] == _expected_llm(body["prompt"], None)


def test_empty_retrieval_is_an_answer_kind(monkeypatch):
    wire(monkeypatch, chunks=[])
    _forbid_llm(monkeypatch)
    body = invoke(_event())
    assert body["kind"] == "answer" and body["answer"]


def test_rag_search_error_is_an_answer_kind(monkeypatch):
    wire(monkeypatch, chunks=CHUNKS)
    _forbid_llm(monkeypatch)
    fake = FakeLambdaClient({"errorMessage": "boom"})
    orig = fake.invoke

    def invoke_with_function_error(**kw):
        resp = orig(**kw)
        resp["FunctionError"] = "Unhandled"
        return resp
    fake.invoke = invoke_with_function_error
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake)
    body = invoke(_event())
    assert body["kind"] == "answer"


def test_without_prompt_only_the_path_is_unchanged(monkeypatch):
    wire(monkeypatch, chunks=CHUNKS)
    calls = []

    def fake_llm(prompt, max_tokens=4096, force_json=False, **kw):
        calls.append(prompt)
        return ("Grounded answer [1].", None)
    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    ev = _event()
    del ev["prompt_only"]
    body = invoke(ev)
    assert "kind" not in body
    assert len(calls) == 1
    assert body["answer"]


def test_history_in_the_node_shape_is_counted(monkeypatch):
    """Node sends {question, answer} turns; ask_history cleans exactly that shape."""
    wire(monkeypatch, chunks=CHUNKS)
    _forbid_llm(monkeypatch)
    body = invoke(_event(history=[{"question": "q1", "answer": "a1"}]))
    assert body["kind"] == "prompt"
    assert body["history_turns"] == 1


def _capture_rewrite_timeout(monkeypatch, ev):
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")
    wire(monkeypatch, chunks=CHUNKS)
    _forbid_llm(monkeypatch)
    seen = {}

    def fake_rewrite(q, h, **kw):
        seen["timeout"] = kw["timeout"]
        return ("when is the backfill due", False)
    monkeypatch.setattr(ask_rewrite, "standalone_question", fake_rewrite)
    invoke(ev)
    return seen["timeout"]


HIST = [{"question": "q1", "answer": "a1"}]


def test_deadline_s_replaces_the_base(monkeypatch):
    monkeypatch.setattr(laa, "ASK_DEADLINE_SECONDS", 24.0)
    assert _capture_rewrite_timeout(monkeypatch, _event(history=HIST, deadline_s=3)) <= 3


@pytest.mark.parametrize("bad", [None, 0, "abc", -5, "nan"])
def test_a_missing_or_invalid_deadline_s_falls_back_to_the_default(monkeypatch, bad):
    monkeypatch.setattr(laa, "ASK_DEADLINE_SECONDS", 24.0)
    ev = _event(history=HIST)
    if bad is not None:
        ev["deadline_s"] = bad
    # default base 24 -> the rewrite keeps its own 4s budget
    assert _capture_rewrite_timeout(monkeypatch, ev) == laa.ASK_REWRITE_BUDGET


def test_deadline_s_is_ignored_without_prompt_only(monkeypatch):
    monkeypatch.setattr(laa, "ASK_DEADLINE_SECONDS", 24.0)
    ev = _event(history=HIST, deadline_s=1)
    del ev["prompt_only"]
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")
    wire(monkeypatch, chunks=CHUNKS)
    seen = {}
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: (seen.update(t=kw["timeout"]) or (q, False)))
    invoke(ev)
    assert seen["t"] == laa.ASK_REWRITE_BUDGET
