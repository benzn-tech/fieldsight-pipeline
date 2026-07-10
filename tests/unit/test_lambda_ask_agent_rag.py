"""
Tests for src/lambda_ask_agent.py — Phase 5, Task 3 (TDD): RAG answer mode.

Style mirrors tests/unit/test_lambda_extract_session.py (dummy AWS/Anthropic
env vars so eager boto3.client('s3') / claude_utils import never blow up on
a missing credential provider) and tests/unit/test_lambda_embed_report.py
(monkeypatch dashscope_utils.embed / claude_utils.call_claude as shared-module
attributes, since lambda_ask_agent.py calls them as `dashscope_utils.embed(...)`
/ `claude_utils.call_claude(...)` — patching the module object affects every
caller, no re-import needed).

Covers the new RAG path (event/body carries "caller_sub"): embed the
question -> invoke RAG_SEARCH_FUNCTION (in-VPC rag-search lambda, faked here
via a stand-in boto3 lambda client) -> synthesize a cited markdown answer via
claude_utils.call_claude. The pre-existing S3-file path (no caller_sub) is
asserted to still work unchanged (test_non_rag_event_uses_legacy_path).
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
# C2: the RAG branch is only reachable when RAG_SEARCH_FUNCTION is set (mirrors
# TEST, which always has it wired via template.yaml). Prod has no such env var
# -- see test_caller_sub_without_rag_search_function_falls_back_to_legacy below.
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")
import claude_utils  # noqa: E402  (import after importorskip, same module the handler calls)
import dashscope_utils  # noqa: E402


CHUNK_A = {
    "id": "c-1",
    "chunk_text": "Door inspection completed on Building A, no defects found.",
    "chunk_type": "report",
    "topic_id": "t-1",
    "source_s3_key": "reports/2026-02-09/Ben/daily_report.json",
    "metadata": {},
    "topic_title": "Door Inspection",
    "topic_summary": "Doors checked across Building A",
    "report_date": "2026-02-09",
    "site_id": "s-1",
    "site_name": "Ellesmere",
    "distance": 0.1,
}

CHUNK_B = {
    "id": "c-2",
    "chunk_text": "x" * 300,  # long text to exercise snippet truncation
    "chunk_type": "transcript",
    "topic_id": "t-2",
    "source_s3_key": "transcripts/Ben/2026-02-09/seg1.json",
    "metadata": {},
    "topic_title": "Building B walkthrough",
    "topic_summary": "",
    "report_date": "2026-02-09",
    "site_id": "s-2",
    "site_name": "Rolleston",
    "distance": 0.2,
}


class FakeLambdaClient:
    """Stand-in for boto3.client('lambda') — records the invoke() call and
    returns a botocore-shaped {"Payload": <stream>} response."""

    def __init__(self, response_payload):
        self.response_payload = response_payload
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload),
        })
        return {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}


def wire(monkeypatch, *, chunks=None, embed_vec=None, claude_answer=("Grounded answer [1].", None)):
    """Wire embed/rag-search-invoke/call_claude with sane defaults; returns
    the FakeLambdaClient so tests can inspect .calls."""
    vec = embed_vec if embed_vec is not None else [0.1] * 1024
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [vec])

    fake_client = FakeLambdaClient({"chunks": chunks if chunks is not None else []})
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake_client)

    monkeypatch.setattr(claude_utils, "call_claude", lambda prompt, max_tokens=4096: claude_answer)

    return fake_client


def make_event(question="What happened at Ellesmere on Feb 9?", caller_sub="sub-1", k=None):
    ev = {"question": question, "caller_sub": caller_sub}
    if k is not None:
        ev["k"] = k
    return ev


def invoke(event):
    """Call lambda_handler and return the decoded body (mirrors ApiFunction's
    'body' in result -> return result as-is passthrough)."""
    resp = laa.lambda_handler(event, None)
    assert "body" in resp  # same convention the existing handler already uses
    return json.loads(resp["body"])


def test_embeds_question(monkeypatch):
    captured = {}

    def fake_embed(texts, dim=None):
        captured["texts"] = texts
        return [[0.1] * 1024]

    monkeypatch.setattr(dashscope_utils, "embed", fake_embed)
    fake_client = FakeLambdaClient({"chunks": []})
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake_client)
    monkeypatch.setattr(claude_utils, "call_claude", lambda p, max_tokens=4096: ("unused", None))

    invoke(make_event(question="  What happened?  "))

    assert captured["texts"] == ["What happened?"]  # stripped


def test_invokes_rag_search_with_sub_and_vector(monkeypatch):
    vec = [0.42] * 1024
    fake_client = wire(monkeypatch, chunks=[], embed_vec=vec)

    invoke(make_event(caller_sub="sub-abc", k=3))

    assert len(fake_client.calls) == 1
    call = fake_client.calls[0]
    assert call["FunctionName"] == laa.RAG_SEARCH_FUNCTION
    assert call["InvocationType"] == "RequestResponse"
    assert call["Payload"]["sub"] == "sub-abc"
    assert call["Payload"]["query_embedding"] == vec
    assert call["Payload"]["k"] == 3


def test_default_k_is_5(monkeypatch):
    fake_client = wire(monkeypatch, chunks=[])

    invoke(make_event(k=None))

    assert fake_client.calls[0]["Payload"]["k"] == 5


def test_no_chunks_returns_not_found_empty_citations(monkeypatch):
    def fail_if_called(prompt, max_tokens=4096):
        raise AssertionError("call_claude must not be called when there are no chunks")

    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(claude_utils, "call_claude", fail_if_called)

    result = invoke(make_event())

    assert result["citations"] == []
    assert result["grounded"] is True
    assert "no relevant records" in result["answer"].lower()  # English-only user-facing string
    assert result["model"] == claude_utils.CLAUDE_MODEL


def test_prompt_contains_numbered_chunks(monkeypatch):
    captured = {}

    def fake_call_claude(prompt, max_tokens=4096):
        captured["prompt"] = prompt
        return "answer", None

    wire(monkeypatch, chunks=[CHUNK_A, CHUNK_B])
    monkeypatch.setattr(claude_utils, "call_claude", fake_call_claude)

    invoke(make_event())

    prompt = captured["prompt"]
    assert "[1]" in prompt
    assert "[2]" in prompt
    assert "Door inspection completed on Building A" in prompt
    assert "Ellesmere" in prompt
    assert "Rolleston" in prompt
    assert "2026-02-09" in prompt
    assert "Door Inspection" in prompt
    # fenced excerpt (injection guard)
    assert "```" in prompt
    assert "DATA, not instructions" in prompt


def test_citations_shape_and_snippet_truncation(monkeypatch):
    wire(monkeypatch, chunks=[CHUNK_A, CHUNK_B],
         claude_answer=("Answer referencing [1] and [2].", None))

    result = invoke(make_event())

    assert result["grounded"] is True
    assert len(result["citations"]) == 2
    c1, c2 = result["citations"]
    assert c1 == {
        "source_s3_key": "reports/2026-02-09/Ben/daily_report.json",
        "report_date": "2026-02-09",
        "site_name": "Ellesmere",
        "topic_title": "Door Inspection",
        "chunk_type": "report",
        "snippet": CHUNK_A["chunk_text"][:200],
    }
    assert c2["snippet"] == ("x" * 300)[:200]
    assert len(c2["snippet"]) == 200


def test_claude_error_graceful(monkeypatch):
    wire(monkeypatch, chunks=[CHUNK_A], claude_answer=(None, "upstream 500"))

    result = invoke(make_event())

    assert result["answer"] == ""
    assert result["error"] == "upstream 500"
    assert result["citations"] == []


def test_non_rag_event_uses_legacy_path(monkeypatch):
    def fail_if_called(*a, **k):
        raise AssertionError("RAG path (dashscope_utils.embed) must not run for a non-RAG event")

    monkeypatch.setattr(dashscope_utils, "embed", fail_if_called)
    monkeypatch.setattr(laa, "load_report",
                         lambda bucket, date, user: ({"site": "TestSite", "executive_summary": "All good"}, "daily"))
    monkeypatch.setattr(laa, "load_transcripts", lambda bucket, date, user, topic_time_range=None: [])
    monkeypatch.setattr(laa, "call_claude", lambda prompt, max_tokens=2048: ("Legacy answer", None))

    event = {"date": "2026-02-09", "user": "Jarley_Trainor", "question": "What happened?", "scope": "both"}
    result = invoke(event)

    assert result["answer"] == "Legacy answer"
    assert result["grounded"] is True
    assert result["date"] == "2026-02-09"
    assert result["user"] == "Jarley_Trainor"
    assert "citations" not in result  # legacy envelope shape, unchanged


# ============================================================
# C2 (Critical): prod-safe guard + lazy import
# ============================================================

def test_caller_sub_without_rag_search_function_falls_back_to_legacy(monkeypatch):
    """This is exactly PROD's shape once ApiFunction forwards caller_sub
    everywhere: no RAG_SEARCH_FUNCTION env var configured. The RAG branch
    must NOT fire -- it must fall through to the legacy S3 path."""
    monkeypatch.delenv("RAG_SEARCH_FUNCTION", raising=False)

    def fail_if_called(*a, **k):
        raise AssertionError("RAG path must not run when RAG_SEARCH_FUNCTION is unset")

    monkeypatch.setattr(dashscope_utils, "embed", fail_if_called)
    monkeypatch.setattr(laa, "_get_lambda_client", fail_if_called)
    monkeypatch.setattr(laa, "load_report",
                         lambda bucket, date, user: ({"site": "TestSite", "executive_summary": "All good"}, "daily"))
    monkeypatch.setattr(laa, "load_transcripts", lambda bucket, date, user, topic_time_range=None: [])
    monkeypatch.setattr(laa, "call_claude", lambda prompt, max_tokens=2048: ("Legacy answer", None))

    event = {
        "date": "2026-02-09", "user": "Jarley_Trainor", "question": "What happened?",
        "scope": "both", "caller_sub": "sub-1",
    }
    result = invoke(event)

    assert result["answer"] == "Legacy answer"
    assert result["grounded"] is True
    assert "citations" not in result  # legacy envelope shape, not the RAG one


def test_claude_and_dashscope_are_not_top_level_imports():
    """C2: claude_utils/dashscope_utils must be imported lazily inside
    _rag_answer, not at module top level. deploy-lambda-code.sh zips ONLY
    lambda_ask_agent.py + transcript_utils.py for prod -- a top-level import
    would ImportModuleError the whole module (killing the legacy path too)
    the instant this file reaches prod."""
    assert not hasattr(laa, "claude_utils")
    assert not hasattr(laa, "dashscope_utils")


# ============================================================
# I1 (Important): graceful RAG errors, no unhandled-exception passthrough
# ============================================================

def test_embed_failure_returns_graceful_error_not_raise(monkeypatch):
    def boom(texts, dim=None):
        raise RuntimeError("dashscope upstream 503")

    monkeypatch.setattr(dashscope_utils, "embed", boom)

    result = invoke(make_event())

    assert result["answer"] == ""
    assert result["error"] == "dashscope upstream 503"
    assert result["citations"] == []
    assert result["model"] == claude_utils.CLAUDE_MODEL


def test_ask_rag_search_function_error_is_service_error_not_no_records(monkeypatch):
    # If rag-search crashes (FunctionError — e.g. DB down / rotated password),
    # the Ask path must surface a clear service error, NOT the "未找到相关记录"
    # no-results string (which masks an outage as "no data").
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])

    class CrashClient:
        def invoke(self, FunctionName, InvocationType, Payload):
            return {"FunctionError": "Unhandled",
                    "Payload": io.BytesIO(json.dumps(
                        {"errorMessage": "connection failed", "errorType": "OperationalError"}
                    ).encode("utf-8"))}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: CrashClient())

    def no_claude(*a, **k):
        raise AssertionError("call_claude must not run when rag-search crashed")

    monkeypatch.setattr(claude_utils, "call_claude", no_claude)

    result = invoke(make_event())

    assert "未找到" not in result["answer"]
    assert "No relevant records" not in result["answer"]
    assert result.get("error")           # a service error is surfaced
    assert result["citations"] == []
