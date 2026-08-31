"""The route, the bridge, and the mode branch.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.1

Style mirrors tests/unit/test_lambda_fieldsight_api_ask_voice.py: dummy AWS env
so the eager boto3 clients import, and a FakeLambdaClient that records the
invoke instead of reaching AWS.

The point of this file is the two ways wiring goes wrong in this repository.
A route that exists but is never dispatched, and a feature whose flag is read
somewhere the request never reaches, both look identical from outside: a well-
formed empty answer, no error, no log line saying why.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3")
agent = pytest.importorskip("lambda_ask_agent", reason="requires boto3")

CALLER = {"sub": "sub-1", "email": "w@x.nz", "name": "Ben", "role": "worker",
          "display_name": "Ben_Test", "sites": ["s-1"], "managed_sites": [],
          "company_id": "c-1"}

OK_BODY = {"corroborations": [], "dropped": [], "truncated": False,
           "timed_out": False}


class FakeLambdaClient:
    def __init__(self, payload=None, function_error=None):
        self.payload = OK_BODY if payload is None else payload
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName,
                           "Payload": json.loads(Payload)})
        resp = {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, **kw):
    fake = FakeLambdaClient(**kw)
    monkeypatch.setattr(fapi, "lambda_client", fake)
    return fake


def body_of(res):
    return json.loads(res["body"])


# ----------------------------------------------------------------- the route exists

def test_the_router_dispatches_the_new_path(monkeypatch):
    """A handler nobody routes to is the most common shape of dead code here.
    This goes through lambda_handler, not the function, for that reason."""
    fake = wire(monkeypatch)
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: CALLER)
    res = fapi.lambda_handler({
        "httpMethod": "POST", "path": "/api/ask/corroborate",
        "body": json.dumps({"question": "who built it", "answer": "Naylor Love did"}),
    }, None)
    assert res["statusCode"] == 200
    assert len(fake.calls) == 1, "the router did not reach the bridge"


def test_a_get_on_the_route_is_a_404_not_a_corroboration(monkeypatch):
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: CALLER)
    res = fapi.lambda_handler({"httpMethod": "GET", "path": "/api/ask/corroborate"}, None)
    assert res["statusCode"] == 404


# ------------------------------------------------------------------- what is sent

def test_the_agent_is_told_which_mode_and_who_is_asking(monkeypatch):
    fake = wire(monkeypatch)
    fapi.corroborate_answer({"question": "q", "answer": "a"}, CALLER)
    sent = fake.calls[0]["Payload"]
    assert sent["mode"] == "corroborate"
    assert sent["caller_sub"] == "sub-1"
    assert sent["question"] == "q" and sent["answer"] == "a"


@pytest.mark.parametrize("body", [{}, {"question": "q"}, {"answer": "a"},
                                  {"question": "  ", "answer": "a"}])
def test_a_missing_half_is_rejected_before_any_invoke(monkeypatch, body):
    fake = wire(monkeypatch)
    res = fapi.corroborate_answer(body, CALLER)
    assert res["statusCode"] == 400
    assert fake.calls == []


def test_a_transcript_pasted_as_an_answer_is_refused(monkeypatch):
    """An answer long enough to be a transcript is a transcript, and this is the
    one route in the system that aims its input at a third party. Refusing here
    is cheaper than trusting the gate to catch every sentence downstream."""
    fake = wire(monkeypatch)
    res = fapi.corroborate_answer(
        {"question": "q", "answer": "x" * (fapi.MAX_CORROBORATE_CHARS + 1)}, CALLER)
    assert res["statusCode"] == 400
    assert fake.calls == []


def test_a_function_error_is_not_passed_through(monkeypatch):
    """An unhandled exception in the agent comes back as a 200 with
    FunctionError set and a stack trace in the payload. ask_question has this
    guard; a new route that invokes the same lambda needs it for the same
    reason, and copying the invoke without it is the easy mistake."""
    wire(monkeypatch, function_error="Unhandled",
         payload={"errorMessage": "boom", "stackTrace": ["src/secret.py line 12"]})
    res = fapi.corroborate_answer({"question": "q", "answer": "a"}, CALLER)
    assert res["statusCode"] == 500
    assert "stackTrace" not in res["body"] and "secret.py" not in res["body"]


def test_an_invoke_failure_is_a_500_and_not_a_raise(monkeypatch):
    class Boom:
        def invoke(self, **kw):
            raise RuntimeError("no such function")
    monkeypatch.setattr(fapi, "lambda_client", Boom())
    assert fapi.corroborate_answer({"question": "q", "answer": "a"}, CALLER)["statusCode"] == 500


# ------------------------------------------------------- the agent-side mode branch

def test_the_agent_branches_on_mode_before_the_rag_path(monkeypatch):
    """Corroboration reads neither the recordings nor the RAG index. Sitting it
    inside the caller_sub/RAG_SEARCH_FUNCTION branch would make it inert for a
    reason unrelated to what it does -- and inert here means an empty body, not
    an error."""
    monkeypatch.delenv("RAG_SEARCH_FUNCTION", raising=False)
    seen = {}

    def fake(body):
        seen.update(body)
        return OK_BODY

    monkeypatch.setattr(agent, "_corroborate", fake)
    res = agent.lambda_handler({"question": "q", "answer": "a",
                                "mode": "corroborate", "caller_sub": "sub-1"}, None)
    assert res["statusCode"] == 200
    assert seen.get("answer") == "a", "the corroborate branch never ran"


def test_the_flag_being_off_yields_an_empty_body_not_an_error(monkeypatch):
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "false")
    assert agent._corroborate({"question": "q", "answer": "a"}) == OK_BODY


def test_a_broken_corroboration_module_does_not_take_ask_down(monkeypatch):
    """This lambda serves /api/ask on every request. An import error inside a
    feature that is off by default must cost the cards, not the answers."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "corroboration":
            raise ImportError("no module named corroboration")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert agent._corroborate({"question": "q", "answer": "a"}) == OK_BODY


def test_an_exception_inside_the_steps_is_caught(monkeypatch):
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "true")
    import corroboration
    monkeypatch.setattr(corroboration, "corroborate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert agent._corroborate({"question": "q", "answer": "a"}) == OK_BODY


# -------------------------------------------------------------- no /ask regression

def test_a_plain_ask_still_takes_the_rag_path(monkeypatch):
    """B10: the mode branch must be additive. A request with no `mode` reaches
    the same path it always did."""
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    called = {}
    monkeypatch.setattr(agent, "_rag_answer", lambda b: called.setdefault("rag", b) or {})
    monkeypatch.setattr(agent, "_corroborate",
                        lambda b: pytest.fail("corroborate stole a plain ask"))
    agent.lambda_handler({"question": "q", "caller_sub": "sub-1"}, None)
    assert "rag" in called


def test_ask_search_mode_is_untouched(monkeypatch):
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "rag-fn")
    called = {}
    monkeypatch.setattr(agent, "_rag_search_list", lambda b: called.setdefault("s", b) or {})
    monkeypatch.setattr(agent, "_corroborate",
                        lambda b: pytest.fail("corroborate stole a search"))
    agent.lambda_handler({"question": "q", "caller_sub": "sub-1", "mode": "search"}, None)
    assert "s" in called


def test_the_existing_ask_route_is_still_dispatched(monkeypatch):
    monkeypatch.setattr(fapi, "get_caller_identity", lambda e: CALLER)
    called = {}
    monkeypatch.setattr(fapi, "ask_question", lambda b, c: called.setdefault("ask", b) or fapi.ok({}))
    fapi.lambda_handler({"httpMethod": "POST", "path": "/api/ask",
                         "body": json.dumps({"question": "q"})}, None)
    assert "ask" in called


# ------------------------------------------------------------------ the flag is wired

def test_the_template_declares_the_flag_where_the_code_reads_it():
    """Three segments make a switch: a template parameter, an env var on the
    function, and a read in the code. Missing the middle one leaves the code
    reading its own default forever, with no error anywhere -- this repository
    has shipped exactly that.
    """
    import pathlib, re
    tpl = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"
    text = tpl.read_text(encoding="utf-8")
    assert "EnableExternalCorroboration:" in text, "no parameter"
    assert "ENABLE_EXTERNAL_CORROBORATION: !Ref EnableExternalCorroboration" in text
    # and it must sit on the function that actually runs the steps -- a flag on
    # ApiFunction would be read by nothing, which is the same as absent
    block = re.search(r"^  AskAgentFunction:\n(.*?)(?=^  \w+:\n)", text, re.S | re.M)
    assert block and "ENABLE_EXTERNAL_CORROBORATION" in block.group(1), \
        "the flag is not on AskAgentFunction"


def test_the_flag_defaults_to_off_in_the_template():
    """The answer to whether the pilot contracts permit an external lookup is a
    commercial decision. A default of 'true' would make it a deploy-time one."""
    import pathlib, re
    tpl = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"
    text = tpl.read_text(encoding="utf-8")
    block = re.search(r"^  EnableExternalCorroboration:\n((?:    .*\n|\n)+)",
                      text, re.MULTILINE)
    assert block and "Default: 'false'" in block.group(1)
