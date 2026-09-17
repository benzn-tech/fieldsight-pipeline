"""
Tests for src/lambda_ask_agent.py — Phase 5, Task 3 (TDD): RAG answer mode.

Style mirrors tests/unit/test_lambda_extract_session.py (dummy AWS/Anthropic
env vars so eager boto3.client('s3') / llm_utils import never blow up on
a missing credential provider) and tests/unit/test_lambda_embed_report.py
(monkeypatch dashscope_utils.embed / llm_utils.call_llm as shared-module
attributes, since lambda_ask_agent.py calls them as `dashscope_utils.embed(...)`
/ `llm_utils.call_llm(...)` — patching the module object affects every
caller, no re-import needed).

Covers the new RAG path (event/body carries "caller_sub"): embed the
question -> invoke RAG_SEARCH_FUNCTION (in-VPC rag-search lambda, faked here
via a stand-in boto3 lambda client) -> synthesize a cited markdown answer via
llm_utils.call_llm. The pre-existing S3-file path (no caller_sub) is
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
import llm_utils  # noqa: E402  (import after importorskip, same module the handler calls)
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
    "site_slug": "ellesmere",
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
    "site_slug": "rolleston",
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

    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, max_tokens=4096, force_json=False: claude_answer)

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
    monkeypatch.setattr(llm_utils, "call_llm", lambda p, max_tokens=4096, force_json=False: ("unused", None))

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
    def fail_if_called(prompt, max_tokens=4096, force_json=False):
        raise AssertionError("call_llm must not be called when there are no chunks")

    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(llm_utils, "call_llm", fail_if_called)

    result = invoke(make_event())

    assert result["citations"] == []
    assert result["grounded"] is True
    assert "no relevant records" in result["answer"].lower()  # English-only user-facing string
    # This test installs `fail_if_called` above, so it has already proved no
    # model ran -- and then used to assert a model was named. Naming one put a
    # model's name under a sentence no model wrote, and the UI renders it.
    assert result["model"] is None


def test_prompt_contains_numbered_chunks(monkeypatch):
    captured = {}

    def fake_call_llm(prompt, max_tokens=4096, force_json=False):
        captured["prompt"] = prompt
        return "answer", None

    wire(monkeypatch, chunks=[CHUNK_A, CHUNK_B])
    monkeypatch.setattr(llm_utils, "call_llm", fake_call_llm)

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
        "site_slug": "ellesmere",
        "topic_title": "Door Inspection",
        "chunk_type": "report",
        "snippet": CHUNK_A["chunk_text"][:200],
        "time_start": None,
    }
    assert c2["snippet"] == ("x" * 300)[:200]
    assert len(c2["snippet"]) == 200


CHUNK_WINDOW = {
    "id": "c-3",
    "chunk_text": "Worker mentions door latch issue near the loading bay.",
    "chunk_type": "transcript_window",
    "topic_id": "t-1",
    "source_s3_key": "transcripts/Ben/2026-02-09/seg1.json",
    "metadata": {"window_span": "12:42:59–12:45:10"},  # EN DASH separator (chunking.py:134)
    "topic_title": "Door Inspection",
    "topic_summary": "",
    "report_date": "2026-02-09",
    "site_id": "s-1",
    "site_name": "Ellesmere",
    "site_slug": "ellesmere",
    "distance": 0.15,
}


def test_citation_time_start_from_transcript_window(monkeypatch):
    wire(monkeypatch, chunks=[CHUNK_WINDOW],
         claude_answer=("Answer referencing [1].", None))

    result = invoke(make_event())

    assert result["citations"][0]["time_start"] == "12:42:59"


def test_citation_time_start_none_for_topic(monkeypatch):
    topic_chunk = dict(CHUNK_WINDOW, id="c-topic", chunk_type="topic", metadata={})
    wire(monkeypatch, chunks=[topic_chunk],
         claude_answer=("Answer referencing [1].", None))

    result = invoke(make_event())

    assert result["citations"][0]["time_start"] is None


def test_citation_time_start_defensive(monkeypatch):
    chunk_no_metadata = dict(CHUNK_WINDOW, id="c-4", metadata=None)
    chunk_bad_span = dict(CHUNK_WINDOW, id="c-5", metadata={"window_span": "no-dash"})
    wire(monkeypatch, chunks=[chunk_no_metadata, chunk_bad_span],
         claude_answer=("Answer referencing [1] and [2].", None))

    result = invoke(make_event())

    assert result["citations"][0]["time_start"] is None
    assert result["citations"][1]["time_start"] is None


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
    """C2: llm_utils/dashscope_utils must be imported lazily inside
    _rag_answer, not at module top level. deploy-lambda-code.sh zips
    lambda_ask_agent.py + transcript_utils.py + llm_utils.py for prod, but
    dashscope_utils.py is NOT in that bundle -- a top-level import would
    ImportModuleError the whole module (killing the legacy path too) the
    instant this file reaches prod."""
    assert not hasattr(laa, "llm_utils")
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
    # Embedding failed, so the question never reached a model.
    assert result["model"] is None


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
        raise AssertionError("call_llm must not run when rag-search crashed")

    monkeypatch.setattr(llm_utils, "call_llm", no_claude)

    result = invoke(make_event())

    assert "未找到" not in result["answer"]
    assert "No relevant records" not in result["answer"]
    assert result.get("error")           # a service error is surfaced
    assert result["citations"] == []


# ---- reranking rides THIS route, not the search list ----------------------

def _many(n):
    return [dict(CHUNK_B, id=i, chunk_text="c%d" % i,
                 source_s3_key="k%d" % i) for i in range(n)]


def test_reranking_widens_the_fetch_on_the_ask_route(monkeypatch):
    """The widening and the rerank call have to be in the SAME function.

    They were not when this shipped. The payload literal
    `{"sub": ..., "query_embedding": ..., "k": k}` appears in `_rag_search_list`
    too, the edit landed there, and Ask went on fetching 5 chunks and then
    short-circuiting because 5 <= 5. The feature could not run on the path it
    was built for.

    Every test stayed green because they all drove the helper. This one drives
    the ROUTE -- it asserts what rag-search was actually asked for.
    """
    monkeypatch.setattr(laa, "RERANK_ENABLED", True)
    monkeypatch.setattr(laa, "RERANK_CANDIDATES", 32)
    client = wire(monkeypatch, chunks=_many(20))
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda q, docs, n: list(range(len(docs))))
    invoke(make_event())
    assert client.calls[0]["Payload"]["k"] == 32


def test_the_reranker_actually_sees_the_ask_chunks(monkeypatch):
    """Not "was the helper callable" -- was it called, on this route, with the
    chunks rag-search returned."""
    seen = {}
    monkeypatch.setattr(laa, "RERANK_ENABLED", True)
    monkeypatch.setattr(laa, "RERANK_CANDIDATES", 32)
    wire(monkeypatch, chunks=_many(20))
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda q, docs, n: seen.update(n=len(docs), keep=n) or [0, 1, 2, 3, 4])
    invoke(make_event())
    assert seen["n"] == 20 and seen["keep"] == 5


def test_with_the_flag_off_the_route_is_byte_for_byte_what_it_was(monkeypatch):
    monkeypatch.setattr(laa, "RERANK_ENABLED", False)
    client = wire(monkeypatch, chunks=_many(20))
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("reranked while disabled")))
    invoke(make_event())
    assert client.calls[0]["Payload"]["k"] == 5


def test_the_search_list_route_still_honours_its_own_k(monkeypatch):
    """The widening does not belong here. Search asks for 30 by default and a
    caller's k is the caller's; silently fetching 32 instead would make this
    route answer a question nobody asked."""
    client = wire(monkeypatch, chunks=_many(3))
    monkeypatch.setattr(laa, "RERANK_ENABLED", True)
    monkeypatch.setattr(laa, "RERANK_CANDIDATES", 32)
    laa.lambda_handler({"mode": "search", "question": "q", "caller_sub": "sub-1", "k": 7}, None)
    assert client.calls[0]["Payload"]["k"] == 7


# --------------------------------------------------------------------------
# Task 2 (2026-09-17 ask-conversation-memory): pin _rag_answer's behaviour
# with a malformed scope field before the function head moves inside the
# try. Adapted from the task brief to this file's own `wire`/`make_event`
# conventions (no `wired` fixture or `SUB` constant exists here or in the
# other Ask test files) -- see task-2-report.md for detail.
# --------------------------------------------------------------------------

def test_a_malformed_scope_still_answers(monkeypatch):
    """Guards the move: today, q_from/q_to, scope_req and plan must keep
    producing the same result whether computed above or inside the try."""
    wire(monkeypatch, chunks=[])
    out = laa._rag_answer({"question": "what happened?", "caller_sub": "sub-1",
                           "tz": "Pacific/Auckland", "site_id": "not-a-uuid"})
    assert "error" not in out
    assert "invalid" in " ".join(d["reason"] for d in out["applied_scope"]["dropped"])


def test_an_early_try_failure_hits_the_normal_error_handler_not_a_nameerror(monkeypatch):
    """Review finding (controller-mandated fix): applied_scope is assigned
    partway through the try, and the except handler at the bottom of
    _rag_answer reads it. If something raises between `try:` and that real
    assignment -- e.g. the in-try `import query_slots` failing, or here,
    _validate_scope itself raising -- the handler must still return its
    normal graceful error shape, not a NameError that masks the original
    exception and escapes as a raw 500."""
    wire(monkeypatch, chunks=[])

    def boom(body):
        raise RuntimeError("scope validation exploded")

    monkeypatch.setattr(laa, "_validate_scope", boom)

    out = laa._rag_answer({"question": "what happened?", "caller_sub": "sub-1",
                           "tz": "Pacific/Auckland"})

    assert out["error"] == "scope validation exploded"
    assert out["applied_scope"] == {"dropped": []}
    assert out["answer"] == ""
    assert out["model"] is None


# --------------------------------------------------------------------------
# Task 3 (2026-09-17 ask-conversation-memory): _rag_answer rewrites a
# follow-up question into a standalone one before it embeds. Spec sections
# 2, 3.1-3.3, 4.3. `ask_rewrite`, `query_slots` and `metric_slots` are all
# imported INSIDE _rag_answer, so they are not attributes of `laa` -- patch
# the real module objects (they are singletons in sys.modules, so patching
# the module object here reaches the lazy `import X` inside the function).
# --------------------------------------------------------------------------

import ask_rewrite  # noqa: E402
import ask_history  # noqa: E402
import query_slots  # noqa: E402
import metric_slots  # noqa: E402

SUB = "sub-1"

ONE_TURN = [{"question": "what did James say about the ceiling grid?",
            "answer": "James said the grid on level 3 is behind schedule."}]


def test_the_rewritten_text_is_what_gets_embedded(monkeypatch):
    """SS3.1/4.3: the embed call uses `asked`, the rewritten text -- not the
    caller's original wording."""
    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("when is James finishing the grid?", True))

    seen = {}
    real_embed = dashscope_utils.embed
    def spy_embed(texts, dim=None):
        seen["texts"] = texts
        return real_embed(texts, dim=dim)
    monkeypatch.setattr(dashscope_utils, "embed", spy_embed)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": ONE_TURN})
    assert seen["texts"] == ["when is James finishing the grid?"]


def test_the_answering_prompt_gets_the_original_and_no_history(monkeypatch):
    """SS2's whole claim. Assert the ABSENCE explicitly: no history text reaches
    build_rag_prompt, and it is called with the caller's original question."""
    wire(monkeypatch, chunks=[{"chunk_text": "level 3 grid note", "id": "c-1",
                               "topic_id": "t-1", "source_s3_key": "x",
                               "report_date": "2026-09-17"}])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten", True))

    prompts = []
    real_build = laa.build_rag_prompt
    def spy_build(question, chunks, **kw):
        prompts.append((question, kw))
        return real_build(question, chunks, **kw)
    monkeypatch.setattr(laa, "build_rag_prompt", spy_build)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "ceiling grid?",
                                  "answer": "level 3 is behind"}]})

    asked_with, kwargs = prompts[0]
    assert asked_with == "when is he finishing it?"
    assert "history" not in kwargs
    assert "level 3 is behind" not in str(kwargs)


def test_a_body_without_history_sends_the_payload_it_sends_today(monkeypatch):
    """SS3.1: absent and empty history must retrieve identically, and with no
    history the payload is unchanged from before this feature."""
    fake1 = wire(monkeypatch, chunks=[])
    laa._rag_answer({"question": "what happened?", "caller_sub": SUB})
    before = dict(fake1.calls[0]["Payload"])

    fake2 = wire(monkeypatch, chunks=[])
    laa._rag_answer({"question": "what happened?", "caller_sub": SUB, "history": []})
    after = dict(fake2.calls[0]["Payload"])

    assert after == before, "absent and empty must retrieve identically"


def test_an_original_that_names_a_date_is_not_recomputed(monkeypatch):
    """SS4.3: when the ORIGINAL question already resolves a range, the rewrite
    must not cause a second, different resolution from `asked`."""
    wire(monkeypatch, chunks=[])
    ranges = []
    real_time_range = query_slots.time_range
    def spy_time_range(q, t):
        ranges.append(q)
        return real_time_range(q, t)
    monkeypatch.setattr(query_slots, "time_range", spy_time_range)
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten with no date", True))

    laa._rag_answer({"question": "what happened yesterday?", "caller_sub": SUB,
                     "tz": "Pacific/Auckland", "history": ONE_TURN})

    assert ranges == ["what happened yesterday?"], "the original resolved; do not re-ask"


def test_a_rewritten_date_word_does_not_open_the_metric_route(monkeypatch):
    """Added by review (#17): a rewrite that INTRODUCES a date word into a
    question that had none must not flip the request onto the metric route --
    metric_slots.detect must never be called for this question."""
    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("how many photos yesterday?", True))

    calls = []
    real_detect = metric_slots.detect
    def spy_detect(q):
        calls.append(q)
        return real_detect(q)
    monkeypatch.setattr(metric_slots, "detect", spy_detect)

    laa._rag_answer({"question": "how many photos did I take", "caller_sub": SUB,
                     "tz": "Pacific/Auckland", "history": ONE_TURN})

    assert calls == [], "metric_slots.detect must not run for an undated original"


def test_a_rewritten_date_word_leaves_the_scope_decision_unchanged(monkeypatch):
    """Added by review (#18): same scenario as #17 -- plan["body_date_sent"] /
    the `scoped` decision (read here via applied_scope) must be identical to
    the no-rewrite run, so the web-fallback decision does not move."""
    fake_no_rewrite = wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: (q, False))
    out_no_rewrite = laa._rag_answer(
        {"question": "how many photos did I take", "caller_sub": SUB,
         "tz": "Pacific/Auckland"})

    fake_rewrite = wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("how many photos yesterday?", True))
    out_rewrite = laa._rag_answer(
        {"question": "how many photos did I take", "caller_sub": SUB,
         "tz": "Pacific/Auckland", "history": ONE_TURN})

    assert out_rewrite["applied_scope"] == out_no_rewrite["applied_scope"]


def test_the_web_answer_branch_receives_the_original_question(monkeypatch):
    """Added by review (#20): the web-answer branch is fed the ORIGINAL
    question, never `asked` -- `asked` may quote a record deleted since the
    previous turn (spec SS2.1/SS4.3)."""
    import web_answer
    wire(monkeypatch, chunks=[{"chunk_text": "note", "id": "c-1", "topic_id": "t-1",
                               "source_s3_key": "x", "report_date": "2026-09-17"}])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten question", True))

    seen = []
    monkeypatch.setattr(web_answer, "answer",
                        lambda question, chunks, **kw: seen.append(question) or None)

    laa._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": ONE_TURN})

    assert seen and seen[0] == "when is he finishing it?"


def test_a_rewritten_metric_route_answer_still_carries_asked(monkeypatch):
    """Controller review finding: `return _metric_answer(...)` never carried
    `asked`, and a rewrite can precede it -- the rewrite runs whenever there
    is history and budget, independently of whether the ORIGINAL question
    already names a date (the metric gate's only requirement). Without this,
    a rewritten metric-route answer would silently show no "Searched for:
    ..." -- the invisible-rewrite failure spec SS3.3 forbids."""
    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("how many photos did James take yesterday?", True))

    out = laa._rag_answer(
        {"question": "how many photos did I take yesterday?", "caller_sub": SUB,
         "tz": "Pacific/Auckland", "history": ONE_TURN})

    assert out["computed"] is True, "sanity: the metric route was actually taken"
    assert out["asked"] == "how many photos did James take yesterday?"


def test_a_no_rewrite_metric_route_answer_has_asked_none(monkeypatch):
    wire(monkeypatch, chunks=[])
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: (q, False))

    out = laa._rag_answer(
        {"question": "how many photos did I take yesterday?", "caller_sub": SUB,
         "tz": "Pacific/Auckland", "history": ONE_TURN})

    assert out["computed"] is True, "sanity: the metric route was actually taken"
    assert out["asked"] is None


# --------------------------------------------------------------------------
# Task 6: the distance gate skips the verdict call when retrieval obviously
# cannot answer, without weakening question_admission's guard (spec SS4.4).
# `web_answer._verdict` is monkeypatched (not `web_answer.answer`) so the real
# `answer()` body runs -- including its own `enabled()` check -- and the gate
# computed in `_rag_answer` is what decides whether `_verdict` is reached.
# --------------------------------------------------------------------------

def _gate_chunk(distance=None, topic_title="Door Inspection", chunk_text="note"):
    c = {"chunk_text": chunk_text, "id": "c-1", "topic_id": "t-1",
         "source_s3_key": "x", "report_date": "2026-09-17",
         "topic_title": topic_title}
    if distance is not None:
        c["distance"] = distance
    return c


@pytest.fixture
def enable_web_answer(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")


def _spy_verdict(monkeypatch):
    import web_answer
    called = []
    monkeypatch.setattr(web_answer, "_verdict",
                        lambda *a, **kw: (called.append(1), ({"answered": True}, None))[1])
    return called


def test_a_far_nearest_distance_skips_the_verdict(monkeypatch, enable_web_answer):
    wire(monkeypatch, chunks=[_gate_chunk(distance=0.61)])
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what happened on site", "caller_sub": SUB})

    assert called == [], "nearest distance 0.61 with no lexical match must skip the verdict"


def test_a_near_distance_still_asks_the_verdict(monkeypatch, enable_web_answer):
    wire(monkeypatch, chunks=[_gate_chunk(distance=0.40)])
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what happened on site", "caller_sub": SUB})

    assert called == [1], "nearest distance 0.40 is within the gate; the verdict must run"


def test_an_absent_distance_does_not_gate(monkeypatch, enable_web_answer):
    """A chunk with no `distance` key must NOT count as far -- absent != far."""
    wire(monkeypatch, chunks=[_gate_chunk(distance=None)])
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what happened on site", "caller_sub": SUB})

    assert called == [1], "a missing distance must not gate the verdict"


def test_a_widened_basis_does_not_gate(monkeypatch, enable_web_answer):
    """basis.widened means the distances are against a day the user did not
    ask about -- the gate must not fire, or an answer could report a widened
    date while the same request is sent to the open web (spec SS4.4)."""
    fake_client = wire(monkeypatch, chunks=[_gate_chunk(distance=0.61)])
    fake_client.response_payload["basis"] = {"widened": True}
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what happened on site", "caller_sub": SUB})

    assert called == [1], "a widened basis must not gate the verdict"


def test_a_lexical_chunk_beats_the_distance(monkeypatch, enable_web_answer):
    """The lexical escape hatch is carried over: a title that literally names
    what was asked must not be declared unanswerable on distance alone."""
    wire(monkeypatch, chunks=[_gate_chunk(distance=0.61, topic_title="Scaffold Inspection")])
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what does the scaffold report say", "caller_sub": SUB})

    assert called == [1], "a lexical title match must not gate the verdict"


def test_the_lexical_match_is_title_only_not_chunk_text(monkeypatch, enable_web_answer):
    """Pins the title-only rule (mirrors _aggregate_topics, :845-849): the
    retrieved chunk text is semantically near the query almost by definition,
    so matching against chunk_text would make the lexical arm true for nearly
    everything and the gate would never fire."""
    wire(monkeypatch, chunks=[_gate_chunk(
        distance=0.61, topic_title="Door Inspection",
        chunk_text="The scaffold was checked and signed off.")])
    called = _spy_verdict(monkeypatch)

    laa._rag_answer({"question": "what does the scaffold report say", "caller_sub": SUB})

    assert called == [], "a term present only in chunk_text must not defeat the gate"


def test_ask_history_is_the_same_module_both_lambdas_import():
    """The move (Task 3): lambda_fieldsight_api and lambda_ask_agent both
    import the cleaner from `ask_history`, not from each other, and get
    identical behaviour."""
    import lambda_fieldsight_api as fapi
    assert fapi._clean_voice_history is ask_history._clean_voice_history
    assert fapi.MAX_VOICE_HISTORY_TURNS == ask_history.MAX_VOICE_HISTORY_TURNS
    assert fapi.MAX_VOICE_HISTORY_CHARS == ask_history.MAX_VOICE_HISTORY_CHARS

    raw = [{"question": "q", "answer": "a"}, {"question": "bad"}]
    assert fapi._clean_voice_history(raw) == ask_history._clean_voice_history(raw)
