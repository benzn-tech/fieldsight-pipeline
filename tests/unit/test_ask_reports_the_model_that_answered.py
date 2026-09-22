"""A customer-facing Ask answer must NOT name which model wrote it.

This inverts the rule this file used to pin. Before 2026-09-20 the requirement
was the opposite: name the model that ran, because a mismatch (LLM_PROVIDER=qwen
answering, `claude-haiku-4-5-20251001` reported) sent a reader at the wrong
model when tracing a bad answer. That defect is real and the fix (route the
label through `llm_utils.active_model()` rather than a provider-specific
constant) is still correct internally.

The owner's new instruction changes what happens at the boundary: nothing that
reaches a customer -- a Word report, a meeting-minutes document, or an API
response the web app receives -- may name the vendor or model at all, right or
wrong. `_rag_answer` is exactly such an API response (it goes straight into
`ok(...)` in `lambda_handler`), so it must carry no "model" key on any path.
Tracing a bad answer to the model that produced it still works: `llm_utils.
call_llm` logs `qwen call: model=...` / `anthropic call: model=...` for the
same request, so the internal record is the log line, not the response body.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

llm_utils = pytest.importorskip("llm_utils")
agent = pytest.importorskip("lambda_ask_agent", reason="requires boto3")


# ------------------------------------------------------ which model is running
#
# active_model() itself is unchanged: report_generator/meeting_minutes still
# use it to stamp the INTERNAL `_report_metadata.model` / debug record, and
# that provenance must still name the model that actually ran, not a
# provider-specific constant that may not match the active provider.

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


# --------------------------------------- no path may report a model, ever now

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
                        lambda prompt, max_tokens=4096, force_json=False, **kw: answer)


def test_no_results_names_no_model(monkeypatch):
    """This return sits above `call_llm`. It used to carry a model name (`None`),
    which at least admitted no model ran; the customer-facing contract now goes
    further and drops the key entirely, on every path, whether a model ran or
    not."""
    wire(monkeypatch, chunks=[])
    # Not just "the label is absent" -- the model must not run at all. Without
    # this the test would still pass if the branch moved below the call.
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: pytest.fail("a model was called"))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["answer"].startswith("No relevant records")
    assert "model" not in out


def test_a_search_outage_names_no_model(monkeypatch):
    wire(monkeypatch, function_error="Unhandled")
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["error"] == "rag-search unavailable"
    assert "model" not in out


def test_a_model_error_names_no_model(monkeypatch):
    """`call_llm` failed, so no text was produced and there is nothing to attribute."""
    wire(monkeypatch, chunks=[{"chunk_text": "something", "report_date": "2026-08-30"}],
         answer=(None, "upstream timeout"))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out.get("error") == "upstream timeout"
    assert "model" not in out


# ------------------------------------- a model ran, but the customer never sees which one

def test_the_answered_path_still_names_no_model(monkeypatch):
    """Before this change, a model DID run here and the response named it -- that
    was the whole point of the file this test used to live in. The model still
    runs (the answer is still produced by it), but the response handed to
    `ok(...)` -- and from there straight to the web app -- must not say which
    model that was."""
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(llm_utils, "QWEN_MODEL", "qwen3.6-flash")
    monkeypatch.setattr(llm_utils, "CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    wire(monkeypatch, chunks=[{"chunk_text": "something", "report_date": "2026-08-30"}],
         answer=("an answer", None))
    out = agent._rag_answer({"question": "q", "caller_sub": "s"})
    assert out["answer"] == "an answer"
    assert "model" not in out, "a customer-facing Ask response must not name the model"


# ------------------------------------------------------------- the property

def test_no_reporting_site_reads_the_provider_constant_directly():
    """Five sites read `CLAUDE_MODEL` and all five were wrong on a qwen deploy.
    The decision belongs in one function, so it cannot drift back apart."""
    import inspect
    source = inspect.getsource(agent)
    assert "llm_utils.CLAUDE_MODEL" not in source


def test_no_ask_response_names_a_model_at_all():
    """The old rule ("name the model that ran, or None") lived in
    `lambda_ask_agent.py` as literal `"model": ...` / `'model': ...` labels.
    That whole category of label must be gone from the file: a customer-facing
    surface has nothing honest to say here, not even "None" -- the field
    itself does not belong on the response.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / "src/lambda_ask_agent.py").read_text(encoding="utf-8")
    labels = re.findall(r"""['"]model['"]\s*:\s*([^,\n]+)""", source)
    assert labels == [], "lambda_ask_agent.py must not label any response with a model: %r" % labels
