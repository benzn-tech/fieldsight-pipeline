"""Unit: both answer paths hand the worker a source manifest (`_sources`), and the
sync /api/ask path never sends it to a client.

Spec 2026-10-22 ask conversation history §4 "Manifest": every record source the
answer was built from, not only the cited cards.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")
import ask_orchestrator as orch  # noqa: E402
import ask_threads  # noqa: E402
import corroboration_client  # noqa: E402
import dashscope_utils  # noqa: E402
import llm_utils  # noqa: E402
import web_answer  # noqa: E402
from llm_tools_client import ChatTurn  # noqa: E402
from tests.unit.test_ask_orchestrator_round1 import FakeTools, _block  # noqa: E402

SUB = "sub-secret-123"
DAY_KEY = "reports/2026-10-20/ben_lin/daily_report.json"
SESSION_KEY = "extractions/ben_lin/2026-10-20/sid" + "a" * 32
OTHER_KEY = "extractions/amy/2026-10-19/sid" + "b" * 32


def _chunk(cid, key, text="Poured the B2 slab."):
    return {"id": cid, "chunk_text": text, "chunk_type": "report", "topic_id": "t-" + cid,
            "source_s3_key": key, "metadata": {}, "topic_title": "Pour " + cid,
            "report_date": "2026-10-20", "site_id": "s-1", "site_name": "Riverside",
            "site_slug": "riverside", "distance": 0.1}


class FakeLambdaClient:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, FunctionName, InvocationType, Payload):
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def wire(monkeypatch, chunks, answer=("We poured B2 [1].", None)):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    monkeypatch.delenv("ASK_CONVERSATION_MEMORY", raising=False)
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeLambdaClient({"chunks": chunks}))
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: answer)


# ------------------------------------------------------------- the builder

def test_sources_from_chunks_distinct_non_null_and_report_days():
    src = ask_threads.sources_from_chunks(
        [{"source_s3_key": DAY_KEY}, {"source_s3_key": SESSION_KEY},
         {"source_s3_key": None}, {"source_s3_key": ""}, {"source_s3_key": DAY_KEY},
         {"source_s3_key": "reports/2026-10-20/ben_lin/other.json"}],
        [{"document_id": "d-1", "site_id": "s-1", "title": "Spec"},
         {"document_id": "d-1", "site_id": "s-1"}, {"document_id": None, "site_id": "s-1"}])
    assert src == {
        "record_keys": [DAY_KEY, SESSION_KEY, "reports/2026-10-20/ben_lin/other.json"],
        "documents": [{"document_id": "d-1", "site_id": "s-1"}],
        "report_days": [{"folder": "ben_lin", "report_date": "2026-10-20"}]}


def test_sources_from_chunks_empty():
    assert ask_threads.sources_from_chunks([], None) == {
        "record_keys": [], "documents": [], "report_days": []}


def test_pop_sources_removes_key():
    result = {"answer": "a", "_sources": {"record_keys": ["k"]}}
    assert ask_threads.pop_sources(result) == {"record_keys": ["k"]}
    assert "_sources" not in result
    assert ask_threads.pop_sources(result) is None
    assert ask_threads.pop_sources(None) is None
    assert ask_threads.pop_sources("not a dict") is None


# ---------------------------------------------------------- _rag_answer path

def test_rag_answer_success_carries_sources(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    wire(monkeypatch, [_chunk("c1", SESSION_KEY), _chunk("c2", DAY_KEY),
                       _chunk("c3", None)])
    out = laa._rag_answer({"question": "What did we pour on B2?", "caller_sub": SUB})
    assert out["answer"] == "We poured B2 [1]."
    # The uncited chunk is in the manifest; the NULL key is not.
    assert out["_sources"] == {
        "record_keys": [SESSION_KEY, DAY_KEY], "documents": [],
        "report_days": [{"folder": "ben_lin", "report_date": "2026-10-20"}]}


def test_rag_answer_error_return_has_no_sources(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    wire(monkeypatch, [_chunk("c1", SESSION_KEY)], answer=(None, "model down"))
    out = laa._rag_answer({"question": "What did we pour on B2?", "caller_sub": SUB})
    assert out.get("error") and "_sources" not in out


def test_web_turn_record_cards_in_sources(monkeypatch):
    from tests.unit.ask_web_fakes import block
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    wire(monkeypatch, [_chunk("c1", SESSION_KEY)])
    monkeypatch.setattr(web_answer, "classify",
                        lambda q, chunks, budget: {"kind": "general", "records_answer": False,
                                                   "source": "model"})
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda question, budget, **kw: block(answer="Slabs are 200 mm."))
    out = laa._rag_answer({"question": "How thick should a B2 slab be?", "caller_sub": SUB})
    assert out.get("from_web") is True
    assert out["_sources"]["record_keys"] == [SESSION_KEY]


def test_sync_api_ask_strips_sources(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    wire(monkeypatch, [_chunk("c1", SESSION_KEY)])
    resp = laa.lambda_handler({"question": "What did we pour on B2?", "caller_sub": SUB}, None)
    body = json.loads(resp["body"])
    assert body["answer"] == "We poured B2 [1]."
    assert "_sources" not in body and SESSION_KEY in json.dumps(body["citations"])


def test_sync_api_ask_prompt_only_early_answer_strips_sources(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    wire(monkeypatch, [])
    resp = laa.lambda_handler({"question": "What did we pour on B2?", "caller_sub": SUB,
                               "prompt_only": True}, None)
    assert "_sources" not in json.loads(resp["body"])


# ------------------------------------------------------- orchestrator path

class ScriptChat:
    def __init__(self, *turns):
        self.turns = list(turns)

    def __call__(self, messages, tools, *, tool_choice="required", deadline=None, **kw):
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def final(answer, basis="records"):
    return ChatTurn(tool_calls=[{"id": "f", "name": "final_answer",
                                 "arguments": {"answer": answer, "basis": basis,
                                               "offer_web": False}}])


def calls(*named):
    return ChatTurn(tool_calls=[{"id": f"c{i}", "name": n, "arguments": a}
                                for i, (n, a) in enumerate(named)])


class DocTools(FakeTools):
    """FakeTools plus the documents tool, whose rows become `document` items."""
    TOOLS = FakeTools.TOOLS + [{"name": "search_project_documents",
                                "description": "docs", "parameters": {"type": "object"}}]

    def execute(self, calls, ctx):
        out = super().execute(calls, ctx)
        for env, call in zip(out, calls):
            if call["name"] == "search_project_documents":
                for it in env["items"]:
                    it["kind"] = "document"
                    it["provenance"] = "documents"
        return out


def _rows(call):
    name = call["name"]
    if name == "search_records":
        return [{"id": "c1", "title": "Pour B2", "text": "Poured B2 slab.",
                 "date": "2026-10-20", "site": "Riverside", "source": SESSION_KEY},
                {"id": "c2", "title": "Day", "text": "The day.", "date": "2026-10-20",
                 "site": "Riverside", "source": DAY_KEY}], {}
    if name == "day_overview":
        return [{"id": "t1", "title": "Inspection", "text": "Level 2 checked.",
                 "date": "2026-10-19", "site": "Riverside", "source": OTHER_KEY}], {}
    if name == "search_project_documents":
        return [{"id": "dc1", "title": "Spec", "heading": "Slabs", "text": "200 mm.",
                 "document_id": "d-1", "site_id": "s-1"},
                {"id": "dc2", "title": "Other", "heading": "x", "text": "y",
                 "document_id": "d-2", "site_id": "s-2"}], {}
    return [], {"empty_reason": "no_match"}


@pytest.fixture
def orch_env(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    monkeypatch.delenv("ASK_CONVERSATION_MEMORY", raising=False)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: object())
    monkeypatch.setattr(corroboration_client, "call", lambda *a, **k:
                        corroboration_client.Reply(text=json.dumps({"web": "no"})))


def run(question, chat, tools=None):
    return orch.answer({"question": question, "caller_sub": SUB, "tz": "Pacific/Auckland",
                        "now": "2026-10-21T09:00:00+13:00"},
                       channel="screen", on_state=lambda s: None,
                       tools=tools or DocTools(_rows), chat=chat)


def test_orchestrator_result_carries_shown_and_documents(orch_env):
    chat = ScriptChat(calls(("search_project_documents", {"query": "slab"})),
                      final("We poured B2 [R1]; the spec says 200 mm [D1]."))
    out = run("What did we pour on B2?", chat)
    assert out["answer"].startswith("We poured B2 [1]")
    src = out["_sources"]
    # Every record card the judge was shown, cited or not.
    assert SESSION_KEY in src["record_keys"] and DAY_KEY in src["record_keys"]
    assert src["report_days"] == [{"folder": "ben_lin", "report_date": "2026-10-20"}]
    # Every document shown, cited or not.
    assert {d["document_id"] for d in src["documents"]} == {"d-1", "d-2"}
    assert {"document_id": "d-1", "site_id": "s-1"} in src["documents"]


def test_orchestrator_web_turn_keeps_record_cards(monkeypatch, orch_env):
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda question, budget, **kw: _block(answer="40 mm.", n_sources=1))
    chat = ScriptChat(calls(("web_search", {})), final("Cover is 40 mm [W1].", basis="web"))
    out = run("What concrete cover for B2?", chat)
    assert out.get("from_web") is True
    assert SESSION_KEY in out["_sources"]["record_keys"]


def test_orchestrator_fallback_carries_sources(monkeypatch, orch_env):
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: ("Synth [1].", None))
    out = run("What did we pour on B2?", ScriptChat(ChatTurn(error="deadline", timed_out=True)))
    assert out["answer"] == "Synth [1]."
    assert SESSION_KEY in out["_sources"]["record_keys"]


def test_orchestrator_error_result_has_no_sources(monkeypatch, orch_env):
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: (None, "down"))
    out = run("What did we pour on B2?", ScriptChat(ChatTurn(error="deadline", timed_out=True)))
    assert out.get("error") and "_sources" not in out
