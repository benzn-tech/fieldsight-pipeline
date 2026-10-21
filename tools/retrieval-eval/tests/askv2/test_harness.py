import dataclasses
import io
import json
import logging
import threading
import uuid
from datetime import date

import boto3
import pytest
import urllib3

from askv2 import harness
from askv2.render import EvalAdapterError
from llm_tools_client import ChatTurn

import ask_orchestrator
import ask_tools
import lambda_ask_agent
import lambda_rag_search
from db import connection

U = uuid.UUID("4326dbc7-14a2-4251-8381-9d0067595a85")


@pytest.fixture
def ev():
    return harness.EvalTurns()


def _invoke(fake, payload, name=harness.FAKE_FUNCTION, kind="RequestResponse"):
    return fake.invoke(FunctionName=name, InvocationType=kind, Payload=json.dumps(payload))


def test_fake_invoke_round_trips_typed_rows_through_json(ev, monkeypatch):
    seen = []

    def handler(event, context):
        seen.append(event)
        return {"results": [{"id": "0", "name": "search_records",
                             "rows": [{"report_date": date(2026, 9, 2), "id": U}],
                             "meta": {}}]}
    monkeypatch.setattr(lambda_rag_search, "lambda_handler", handler)
    fake = harness.FakeLambda(ev)
    resp = _invoke(fake, {"mode": "tools", "sub": "s", "calls": [
        {"id": "0", "name": "search_records", "args": {"query": "q"},
         "query_embedding": [0.1] * 4}]})
    assert resp["StatusCode"] == 200
    body = json.loads(resp["Payload"].read())
    assert body["results"][0]["rows"] == [{"report_date": "2026-09-02", "id": str(U)}]
    inv = ev.rec.invokes[0]
    assert inv["calls"] == [{"id": "0", "name": "search_records", "args": {"query": "q"}}]
    assert inv["results"][0]["rows"][0]["report_date"] == "2026-09-02"


@pytest.mark.parametrize("kw,payload", [
    ({"name": "fieldsight-prod-rag-search"}, {"mode": "tools"}),
    ({"kind": "Event"}, {"mode": "tools"}),
    ({}, {"mode": "search"}),
])
def test_fake_invoke_refuses_anything_else(ev, kw, payload):
    with pytest.raises(EvalAdapterError):
        _invoke(harness.FakeLambda(ev), payload, **kw)


def test_patches_route_every_connection_and_client_to_the_eval(ev):
    made = []

    def factory():
        made.append(object())
        conn = type("C", (), {"closed": False, "close": lambda self: closes.append(1)})()
        return conn
    closes = []
    fake = harness.FakeLambda(ev)
    p = harness.install_patches(fake, factory)
    try:
        assert lambda_ask_agent._get_lambda_client() is fake
        assert ask_orchestrator.DEADLINE_S["screen"] == 120.0
        c1 = lambda_rag_search.get_cached_connection()
        assert lambda_rag_search.get_cached_connection() is c1      # same within an invoke
        lambda_rag_search.close_cached_connection()
        assert closes == [1]
        assert lambda_rag_search.get_cached_connection() is not c1  # new after close
        with pytest.raises(EvalAdapterError):
            connection.get_connection()
        other = []
        t = threading.Thread(target=lambda: other.append(lambda_rag_search.get_cached_connection()))
        t.start()
        t.join()
        assert other[0] is not lambda_rag_search.get_cached_connection()
    finally:
        p.restore()
    assert ask_orchestrator.DEADLINE_S["screen"] == 40.0
    assert lambda_rag_search.get_cached_connection is connection.get_cached_connection


def test_url_guard_counts_allowed_hosts_and_refuses_the_rest():
    calls = []
    orig = urllib3.connectionpool.HTTPConnectionPool.urlopen
    urllib3.connectionpool.HTTPConnectionPool.urlopen = lambda self, *a, **k: calls.append(self.host)
    try:
        g = harness.arm_url_guard({"openrouter.ai", "sts.*.amazonaws.com"})
        try:
            urllib3.HTTPSConnectionPool("openrouter.ai").urlopen("POST", "/x")
            urllib3.HTTPSConnectionPool("sts.ap-southeast-2.amazonaws.com").urlopen("POST", "/")
            with pytest.raises(harness.GuardError, match="example.com"):
                urllib3.HTTPSConnectionPool("example.com").urlopen("GET", "/")
        finally:
            g.restore()
    finally:
        urllib3.connectionpool.HTTPConnectionPool.urlopen = orig
    assert calls == ["openrouter.ai", "sts.ap-southeast-2.amazonaws.com"]
    assert g.counts == {"openrouter.ai": 1, "sts.ap-southeast-2.amazonaws.com": 1,
                        "example.com (refused)": 1}


def test_boto_guard_allows_rds_data_only():
    g = harness.arm_boto_guard({"rds-data"})
    try:
        boto3.client("rds-data", region_name="ap-southeast-2", aws_access_key_id="x",
                     aws_secret_access_key="y")
        with pytest.raises(harness.GuardError, match="s3"):
            boto3.client("s3", region_name="ap-southeast-2")
        with pytest.raises(harness.GuardError, match="lambda"):
            boto3.session.Session(region_name="ap-southeast-2").client("lambda")
    finally:
        g.restore()
    boto3.client("s3", region_name="ap-southeast-2", aws_access_key_id="x",
                 aws_secret_access_key="y")


def test_frozen_credentials_are_static():
    s = boto3.session.Session(aws_access_key_id="AKIAEVAL", aws_secret_access_key="sk",
                              region_name="ap-southeast-2")
    frozen = harness.freeze_session(s)
    creds = frozen.get_credentials()
    assert creds.access_key == "AKIAEVAL" and creds.method == "explicit"


def test_log_capture_keeps_routes_and_usage_and_drops_text():
    cap = harness.install_log_capture()
    try:
        log = logging.getLogger()
        log.info("ASK_ROUTE %s", json.dumps({"turn": "t1", "fallback": None, "rounds": 2}))
        log.info("LLM_USAGE provider=openrouter model=m caller=ask_answer prompt_tokens=12 "
                 "completion_tokens=3")
        log.warning("ask orch judge raised: secret question text")
        logging.getLogger("x.y").error("child text")
    finally:
        cap.restore()
    assert cap.routes == {"t1": {"turn": "t1", "fallback": None, "rounds": 2}}
    assert cap.usage == [{"provider": "openrouter", "model": "m", "caller": "ask_answer",
                          "prompt_tokens": "12", "completion_tokens": "3"}]
    assert cap.dropped == 2
    assert all("secret" not in json.dumps(x) for x in (cap.routes, cap.usage))


def test_tools_proxy_returns_envelopes_unchanged_and_builds_a_real_context(ev, monkeypatch):
    out = [{"tool": "search_records", "id": "0", "items": [], "meta": {"error": "upstream"}}]
    monkeypatch.setattr(ask_tools, "execute", lambda calls, ctx: out)
    fake = harness.FakeLambda(ev)
    proxy = harness.ToolsProxy(ev, fake)
    assert proxy.TOOLS is ask_tools.TOOLS and proxy.RefAssigner is ask_tools.RefAssigner
    assert proxy.offered_tools() == ask_tools.offered_tools()
    ctx = proxy.ToolContext(caller_sub="s", today=None, deadline=1.0, lambda_client=object(),
                            function_name="eval-in-process", refs=ask_tools.RefAssigner())
    assert isinstance(ctx, ask_tools.ToolContext) and ctx.lambda_client is fake
    assert dataclasses.replace(ctx, deadline=2.0).lambda_client is fake
    calls = [{"name": "search_records", "args": {"query": "q"}}]
    assert proxy.execute(calls, ctx) is out
    b = ev.rec.batches[0]
    assert b["calls"] == calls and b["envelopes"] == out and b["judge_calls_before"] == 0


def test_chat_wrapper_records_requests_and_turns(ev):
    turn = ChatTurn(tool_calls=[{"id": "f", "name": "final_answer",
                                 "arguments": {"answer": "x [R1]", "basis": "records"}}],
                    usage={"prompt_tokens": 10, "completion_tokens": 2,
                           "completion_tokens_details": {"reasoning_tokens": 1}},
                    model="m")
    msgs = [{"role": "system", "content": "s"}]
    chat = harness.chat_wrapper(ev, lambda m, t, **kw: turn)
    assert chat(msgs, [{"type": "function"}], tool_choice="required", deadline=5.0) is turn
    msgs.append({"role": "user"})
    j = ev.rec.judge[0]
    assert j["messages"] == [{"role": "system", "content": "s"}]
    assert j["tool_choice"] == "required" and j["turn"] is turn
    assert j["usage"] == {"prompt": 10, "completion": 2, "reasoning": 1}
    assert j["model"] == "m" and j["error"] is None


def test_chat_wrapper_records_a_raise(ev):
    def boom(*a, **k):
        raise TimeoutError("x")
    with pytest.raises(TimeoutError):
        harness.chat_wrapper(ev, boom)([], [])
    assert ev.rec.judge[0]["error"] == "raised:TimeoutError"
