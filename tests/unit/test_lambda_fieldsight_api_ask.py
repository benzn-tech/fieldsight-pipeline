"""
Tests for src/lambda_fieldsight_api.py ask_question — Phase 5, Task 4.

Style mirrors tests/unit/test_lambda_ask_agent_rag.py (dummy AWS env vars so
an eager boto3.client('s3')/boto3.client('lambda')/boto3.resource('dynamodb')
at import time never blows up on a missing credential provider; a FakeLambda
double records the invoke() call instead of hitting a real Lambda).

Covers Task 4: ask_question forwards caller_sub (the Cognito sub bridge to
rag-search's get_user_by_sub) on every invoke, and no longer hard-requires
`date` (RAG retrieval is global across the caller's accessible sites) while
still requiring `question` and preserving worker self-scoping.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")


ADMIN_CALLER = {
    "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
    "role": "admin", "display_name": "Ada_Admin", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "c-1",
}

WORKER_CALLER = {
    "sub": "sub-worker-1", "email": "w@x.nz", "name": "Ben Test",
    "role": "worker", "display_name": "Ben_Test", "device_id": "Benl1",
    "sites": ["s-1"], "managed_sites": [], "company_id": "c-1",
}

# Org-provisioned account: absent from the legacy DynamoDB user mapping
# entirely -- no display_name to resolve, no legacy sites/role mapping.
ORG_CALLER = {
    "sub": "sub-ucpk", "email": "", "name": "",
    "role": "viewer", "display_name": "", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "",
}


class FakeLambdaClient:
    """Stand-in for boto3.client('lambda') — records the invoke() call and
    returns a botocore-shaped {"Payload": <stream>} response."""

    def __init__(self, response_payload=None, function_error=None):
        self.response_payload = response_payload if response_payload is not None else {
            "answer": "stub", "citations": [], "model": "stub"
        }
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({
            "FunctionName": FunctionName,
            "InvocationType": InvocationType,
            "Payload": json.loads(Payload),
        })
        resp = {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, **kwargs):
    """Wires the ASK route's own client (`ask_lambda_client`), not the shared
    `lambda_client` corroborate_answer/ask_voice/search_topics still use --
    see test_the_ask_route_does_not_share_its_client_with_corroborate below,
    which pins the split this helper relies on."""
    fake_client = FakeLambdaClient(**kwargs)
    monkeypatch.setattr(fapi, "ask_lambda_client", fake_client)
    return fake_client


def body_of(res):
    return json.loads(res["body"])


def test_payload_includes_caller_sub_admin(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What happened?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-admin-1"


def test_payload_includes_caller_sub_worker(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What happened at my site?"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_ask_without_date_no_longer_400(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "Door inspection on Feb 9?"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_client.calls) == 1
    # date omitted entirely from payload when caller doesn't supply one (soft context only)
    assert "date" not in fake_client.calls[0]["Payload"]


def test_date_still_forwarded_when_supplied(monkeypatch):
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "Q?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert fake_client.calls[0]["Payload"]["date"] == "2026-02-09"


def test_missing_question_still_400(monkeypatch):
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 400
    assert "question" in body_of(res)["error"].lower()
    assert fake_client.calls == []  # never invoked ask-agent


def test_worker_self_scoping_now_via_caller_sub(monkeypatch):
    """UPDATED (BUG-39 WS2): worker self-scoping used to be forced server-side
    here via resolve_user_display_name(); that legacy gate is removed and the
    scoping now happens downstream in rag-search via caller_sub. This proxy
    forwards 'user' as empty soft context (none supplied) plus the caller_sub
    that actually gates access."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "What did I do today?"}, WORKER_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["user"] == ""
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_worker_impersonation_prevention_now_via_caller_sub(monkeypatch):
    """UPDATED (BUG-39 WS2): this proxy no longer rewrites a client-supplied
    'user' field back to the worker's own display_name -- that was the
    legacy pre-gate. Impersonation prevention now lives downstream in
    rag-search, which scopes retrieval by caller_sub (immutable, from the
    Cognito token), not by the client-suppliable 'user' field. This proxy
    forwards both unchanged; caller_sub is what actually gates access."""
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "Q?", "user": "Someone_Else"}, WORKER_CALLER)

    assert fake_client.calls[0]["Payload"]["user"] == "Someone_Else"
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-worker-1"


def test_ask_org_caller_no_dynamo_profile_not_403(monkeypatch):
    """Org-provisioned caller absent from the legacy DynamoDB user mapping
    (role='viewer', no sites, no display_name) must not be blocked by the
    legacy pre-gate -- the RAG ACL is enforced downstream by caller_sub ->
    rag-search (BUG-39 WS2)."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "what happened on site?"}, ORG_CALLER)

    assert res["statusCode"] != 403
    assert res["statusCode"] != 400
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-ucpk"


def test_ask_global_no_user_not_400(monkeypatch):
    """A global Ask (no 'user' in body, from an org account with no legacy
    display_name to resolve) must not 400 -- 'user' is optional soft context
    only; downstream ACL scopes by caller_sub."""
    fake_client = wire(monkeypatch)

    res = fapi.ask_question({"question": "site-wide question"}, ORG_CALLER)

    assert res["statusCode"] != 400
    assert fake_client.calls[0]["Payload"]["caller_sub"] == "sub-ucpk"


def test_history_is_forwarded_cleaned(monkeypatch):
    """Task 4: the screen proxy now forwards a cleaned `history`, exactly as
    the voice proxy already does (same cleaner, same caps, same key names)."""
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "q", "history": [
        {"question": "a?", "answer": "b"}]}, ADMIN_CALLER)

    assert fake_client.calls[0]["Payload"]["history"] == [
        {"question": "a?", "answer": "b"}]


def test_the_wrong_key_names_are_dropped_per_turn_and_the_ask_still_answers(monkeypatch, caplog):
    fake_client = wire(monkeypatch)

    with caplog.at_level("WARNING"):
        res = fapi.ask_question({"question": "q", "history": [
            {"q": "a?", "a": "b"}, {"question": "real?", "answer": "yes"}]},
            ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert fake_client.calls[0]["Payload"]["history"] == [
        {"question": "real?", "answer": "yes"}]
    assert "dropped 1 of 2" in caplog.text
    assert "ask:" in caplog.text


def test_no_usable_history_sends_no_key(monkeypatch):
    """Absent is not `[]` -- see ask_voice, and the agent's history_turns
    count, which would otherwise mean two things."""
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "q", "history": []}, ADMIN_CALLER)

    assert "history" not in fake_client.calls[0]["Payload"]


def test_no_history_key_at_all_sends_no_key(monkeypatch):
    fake_client = wire(monkeypatch)

    fapi.ask_question({"question": "q"}, ADMIN_CALLER)

    assert "history" not in fake_client.calls[0]["Payload"]


def test_history_not_a_list_warns(monkeypatch, caplog):
    fake_client = wire(monkeypatch)

    with caplog.at_level("WARNING"):
        res = fapi.ask_question({"question": "q", "history": "oops"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert "history" not in fake_client.calls[0]["Payload"]
    assert "history was str, not a list" in caplog.text
    assert "ask:" in caplog.text


def test_a_hung_agent_is_described_rather_than_killed(monkeypatch, caplog):
    """Task 8 (spec SS4.8): today the runtime kills ApiFunction (no Config on
    lambda_client -> botocore's default read timeout outlives ApiFunction's
    own Timeout) BEFORE this except runs, so the only trace in prod would be a
    bare `Task timed out`. Force the botocore timeout directly and assert the
    invoke fails inside our own code, where it can be named and turned into a
    504 -- not a 500/502, so the client and any alerting can tell a hung
    agent apart from every other invoke failure."""
    import botocore.exceptions

    def _boom(**kw):
        raise botocore.exceptions.ReadTimeoutError(endpoint_url="lambda")
    monkeypatch.setattr(fapi.ask_lambda_client, "invoke", _boom)

    with caplog.at_level("ERROR"):
        res = fapi.ask_question({"question": "q"}, ADMIN_CALLER)

    assert res["statusCode"] == 504
    assert "ask agent read timeout" in caplog.text.lower()


def test_a_non_timeout_invoke_exception_is_502(monkeypatch, caplog):
    """Any OTHER invoke exception (throttle, network error, etc.) is a
    different failure class from a hung agent and must not be confused with
    it in the log or the status code."""
    def _boom(**kw):
        raise RuntimeError("some other invoke failure")
    monkeypatch.setattr(fapi.ask_lambda_client, "invoke", _boom)

    with caplog.at_level("ERROR"):
        res = fapi.ask_question({"question": "q"}, ADMIN_CALLER)

    assert res["statusCode"] == 502
    assert "ask agent invocation failed" in caplog.text.lower()


def test_the_ask_route_does_not_share_its_client_with_corroborate(monkeypatch):
    """Task 8 review fix / controller ruling: the read_timeout=26s Config must
    apply ONLY to ask_question's own client, not to the shared `lambda_client`
    corroborate_answer/ask_voice/search_topics use. Pins two things:

    (1) the two client objects are distinct, with different configured read
        timeouts -- `ask_lambda_client` is the 26s one, the shared
        `lambda_client` is not;
    (2) corroborate_answer actually calls the SHARED client. If it were ever
        pointed at `ask_lambda_client` instead, this assertion is what would
        catch it -- see the revert-and-red proof in the task report, which
        points corroborate_answer at `ask_lambda_client` alone and confirms
        this exact test goes red.
    """
    assert fapi.lambda_client is not fapi.ask_lambda_client
    assert fapi.ask_lambda_client.meta.config.read_timeout == fapi._LAMBDA_INVOKE_TIMEOUT
    assert fapi.lambda_client.meta.config.read_timeout != fapi._LAMBDA_INVOKE_TIMEOUT

    fake_shared = FakeLambdaClient()
    monkeypatch.setattr(fapi, "lambda_client", fake_shared)

    def _must_not_be_called(**kw):
        raise AssertionError(
            "corroborate_answer must not invoke ask_lambda_client -- that "
            "client's 26s read_timeout is sized for ask_question, not for "
            "corroboration.py's own 27s HARD_STOP_SECONDS budget")
    fake_ask_only = FakeLambdaClient()
    monkeypatch.setattr(fake_ask_only, "invoke", _must_not_be_called)
    monkeypatch.setattr(fapi, "ask_lambda_client", fake_ask_only)

    res = fapi.corroborate_answer({"question": "q", "answer": "a"}, ADMIN_CALLER)

    assert res["statusCode"] == 200
    assert len(fake_shared.calls) == 1
    assert fake_shared.calls[0]["Payload"]["mode"] == "corroborate"


def test_function_error_returns_500_without_stack_trace_leak(monkeypatch):
    """I1: if the Ask Agent lambda itself raised an unhandled exception,
    boto3 reports it via resp['FunctionError'] with a Payload containing
    {errorMessage, errorType, stackTrace}. ApiFunction must never pass that
    payload straight through to the client -- it leaks internals."""
    wire(monkeypatch,
         response_payload={
             "errorMessage": "RuntimeError: dashscope upstream 503",
             "errorType": "RuntimeError",
             "stackTrace": ["  File \"lambda_ask_agent.py\", line 525, in _rag_answer"],
         },
         function_error="Unhandled")

    res = fapi.ask_question({"question": "What happened?", "date": "2026-02-09"}, ADMIN_CALLER)

    assert res["statusCode"] == 500
    body_text = res["body"]
    assert "stackTrace" not in body_text
    assert "lambda_ask_agent.py" not in body_text
