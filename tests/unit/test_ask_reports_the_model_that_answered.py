"""The name under an answer must be the model that wrote it.

Found on live prod: `LLM_PROVIDER=qwen`, so every Ask answer was written by
`qwen3.6-flash` -- and every response reported `claude-haiku-4-5-20251001`,
which the UI renders under the answer. Three of the five reporting sites were
worse than wrong: they sat on paths where **no model ran at all**, including the
no-results short-circuit, which returns above the `call_llm` below it.

The rule these tests pin: **name a model only where a model produced the text,
and name the one that ran.** It is the same rule the metric route already
follows by returning `model: None` -- a number computed by SQL has no model to
name, and neither does a system message.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

llm_utils = pytest.importorskip("llm_utils")
agent = pytest.importorskip("lambda_ask_agent", reason="requires boto3")


# ------------------------------------------------------ which model is running

def test_active_model_follows_the_provider(monkeypatch):
    """Reading `CLAUDE_MODEL` directly answers a different question from the one
    the caller asked. It is set on every function regardless of provider."""
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_MODEL", "qwen3.6-flash")
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    assert llm_utils.active_model() == "qwen3.6-flash"

    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "anthropic")
    assert llm_utils.active_model() == "claude-haiku-4-5-20251001"


def test_an_unknown_provider_names_nothing(monkeypatch):
    """A wrong name is worse than no name: the reader cannot tell it is wrong."""
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "something-new")
    assert llm_utils.active_model() is None


# --------------------------------------------- no model ran, so nothing is named

class FakeLambdaClient:
    """Mirrors tests/unit/test_lambda_ask_agent_rag.py."""

    def __init__(self, payload, function_error=None):
        self.payload = payload
        self.function_error = function_error

    def invoke(self, FunctionName, InvocationType, Payload):
        import io as _io
        import json as _json
        resp = {"Payload": _io.BytesIO(_json.dumps(self.payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, *, chunks=None, function_error=None, answer=("an answer", None)):
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    monkeypatch.setattr(agent, "_get_lambda_client",
                        lambda: FakeLambdaClient({"chunks": chunks or []}, function_error))
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda prompt, max_tokens=4096, force_json=False: answer)


def test_no_results_names_no_model(monkeypatch):
    """This return sits above `call_llm`. It used to carry a model name, so the
    UI printed one under a sentence no model had written."""
    wire(monkeypatch, chunks=[])
    # Not just "the label is absent" -- the model must not run at all. Without
    # this the test would still pass if the branch moved below the call.
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: pytest.fail("a model was called"))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["answer"].startswith("No relevant records")
    assert out["model"] is None


def test_a_search_outage_names_no_model(monkeypatch):
    wire(monkeypatch, function_error="Unhandled")
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["error"] == "rag-search unavailable"
    assert out["model"] is None


def test_a_model_error_names_no_model(monkeypatch):
    """`call_llm` failed, so no text was produced and there is nothing to attribute."""
    wire(monkeypatch, chunks=[{"chunk_text": "something", "report_date": "2026-08-30"}],
         answer=(None, "upstream timeout"))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out.get("error") == "upstream timeout"
    assert out["model"] is None


# ------------------------------------------- a model ran, so the right one is named

def test_the_answered_path_names_the_model_that_ran(monkeypatch):
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_MODEL", "qwen3.6-flash")
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    wire(monkeypatch, chunks=[{"chunk_text": "something", "report_date": "2026-08-30"}],
         answer=("an answer", None))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["answer"] == "an answer"
    assert out["model"] == "qwen3.6-flash",         "the label named a provider this deploy is not using"


# ------------------------------------------------------------- the property

def test_no_reporting_site_reads_the_provider_constant_directly():
    """Five sites read `CLAUDE_MODEL` and all five were wrong on a qwen deploy.
    The decision belongs in one function, so it cannot drift back apart."""
    import inspect
    source = inspect.getsource(agent)
    assert "llm_utils.CLAUDE_MODEL" not in source
