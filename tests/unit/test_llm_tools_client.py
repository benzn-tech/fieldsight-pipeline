"""The tool-calling chat client for the Ask judge (phase 2).

Spec: docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md §5.

Nothing here reaches a network. The HTTP layer is mocked the same way
`test_corroboration_client.py` mocks it -- the client builds its pool through
`corroboration_client.http_session()`, which calls `urllib3.PoolManager()` at
call time, so patching that one attribute covers both clients.
"""
import json
import logging

import pytest

import corroboration_client
import llm_tools_client as tools_client
import llm_utils


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = (payload if isinstance(payload, bytes)
                     else json.dumps(payload).encode("utf-8"))


class FakePool:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "timeout": timeout,
                           "headers": headers, "body": json.loads(body)})
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", "test-key")
    for name in ("ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS",
                 "CORROBORATION_CHEAP_MODEL", "ASK_JUDGE_PROVIDER",
                 "ASK_JUDGE_REASONING"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_auto_only(monkeypatch):
    # The auto-only memory is per container; no test may inherit another's.
    monkeypatch.setattr(tools_client, "_AUTO_ONLY", set())


def _install(monkeypatch, pool):
    monkeypatch.setattr(corroboration_client.urllib3, "PoolManager",
                        lambda *a, **k: pool)
    return pool


def _tool_call(cid, name, arguments):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _body(content=None, tool_calls=None, model="google/gemini-3.8-flash",
          usage=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"model": model,
            "choices": [{"message": message,
                         "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30}}


TOOLS = [
    {"type": "function",
     "function": {"name": "search_records",
                  "description": "Search the site records.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}}},
    {"type": "function",
     "function": {"name": "final_answer",
                  "description": "Answer the reader.",
                  "parameters": {"type": "object",
                                 "properties": {"answer": {"type": "string"}},
                                 "required": ["answer"]}}},
]

MESSAGES = [{"role": "system", "content": "You are the judge."},
            {"role": "user", "content": "What did we do yesterday?"}]


def _ok_tool_reply():
    return FakeResponse(200, _body(tool_calls=[
        _tool_call("call_1", "search_records", '{"query": "yesterday"}')]))


# ------------------------------------------------------------------ request body

def test_the_request_carries_messages_tools_and_tool_choice(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    call = pool.calls[0]
    assert call["url"] == corroboration_client.API_URL
    assert call["headers"]["Authorization"] == "Bearer test-key"
    body = call["body"]
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert body["tool_choice"] == "required"


def test_max_tokens_is_an_answer_budget_plus_reasoning_headroom(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok_tool_reply(), _ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert pool.calls[0]["body"]["max_tokens"] == \
        2048 + llm_utils.REASONING_HEADROOM_TOKENS
    tools_client.chat(MESSAGES, TOOLS, max_tokens=500)
    assert pool.calls[1]["body"]["max_tokens"] == \
        500 + llm_utils.REASONING_HEADROOM_TOKENS


def test_no_plugins_field_is_sent(monkeypatch):
    """Web search is our own tool here, not the provider's plugin."""
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert "plugins" not in pool.calls[0]["body"]


def test_model_comes_from_ask_judge_model(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", "judge/model-a")
    monkeypatch.setenv("CORROBORATION_CHEAP_MODEL", "cheap/model-b")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert pool.calls[0]["body"]["model"] == "judge/model-a"


def test_model_falls_back_to_the_cheap_corroboration_model(monkeypatch):
    monkeypatch.setenv("CORROBORATION_CHEAP_MODEL", "cheap/model-b")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert pool.calls[0]["body"]["model"] == "cheap/model-b"


def test_an_explicit_model_wins(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", "judge/model-a")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS, model="explicit/model")
    assert pool.calls[0]["body"]["model"] == "explicit/model"


def test_a_missing_key_is_an_error_and_no_call(monkeypatch):
    monkeypatch.delenv("CORROBORATION_API_KEY", raising=False)
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.error and pool.calls == []


def test_the_client_does_not_carry_its_own_key_handling():
    """The key and session come from corroboration_client, by import."""
    import inspect
    code = inspect.getsource(tools_client).split('"""', 2)[-1]
    assert "CORROBORATION_API_KEY" not in code
    assert "PoolManager" not in code
    assert "openrouter.ai" not in code


# ----------------------------------------------------------------------- parsing

def test_tool_call_arguments_are_parsed_into_dicts(monkeypatch):
    _install(monkeypatch, FakePool(FakeResponse(200, _body(
        tool_calls=[_tool_call("call_1", "search_records",
                               '{"query": "concrete pour"}'),
                    _tool_call("call_2", "final_answer",
                               '{"answer": "Poured level 2."}')],
        usage={"prompt_tokens": 10, "completion_tokens": 5}))))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.tool_calls == [
        {"id": "call_1", "name": "search_records",
         "arguments": {"query": "concrete pour"}},
        {"id": "call_2", "name": "final_answer",
         "arguments": {"answer": "Poured level 2."}},
    ]
    assert turn.bad_args == []
    assert turn.text == ""
    assert turn.error is None and turn.timed_out is False
    assert turn.model == "google/gemini-3.8-flash"
    assert turn.usage["prompt_tokens"] == 10


def test_unparseable_arguments_land_in_bad_args(monkeypatch):
    _install(monkeypatch, FakePool(FakeResponse(200, _body(tool_calls=[
        _tool_call("call_ok", "search_records", '{"query": "x"}'),
        _tool_call("call_bad", "final_answer", '{"answer": "unterminated'),
        _tool_call("call_list", "final_answer", '["not", "an", "object"]'),
    ]))))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert [c["id"] for c in turn.tool_calls] == ["call_ok", "call_bad", "call_list"]
    assert turn.tool_calls[1]["arguments"] == {}
    assert turn.tool_calls[2]["arguments"] == {}
    assert turn.bad_args == ["call_bad", "call_list"]
    assert turn.error is None


def test_dict_arguments_are_accepted_as_is(monkeypatch):
    _install(monkeypatch, FakePool(FakeResponse(200, _body(tool_calls=[
        _tool_call("call_1", "search_records", {"query": "x"})]))))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.tool_calls[0]["arguments"] == {"query": "x"}
    assert turn.bad_args == []


def test_text_only_content_gives_no_tool_calls(monkeypatch):
    _install(monkeypatch, FakePool(FakeResponse(200, _body(
        content="We poured the level 2 slab."))))
    turn = tools_client.chat(MESSAGES, TOOLS, tool_choice="auto")
    assert turn.tool_calls == []
    assert turn.text == "We poured the level 2 slab."
    assert turn.error is None


def test_a_200_with_neither_tool_calls_nor_text_is_an_error(monkeypatch):
    """Measured shape on this endpoint (corroboration_client docstring): a 200
    carrying nothing. Passed on as a turn, the loop would read it as an answer."""
    _install(monkeypatch, FakePool(FakeResponse(200, _body(content=""))))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.error == "empty completion"


def test_tool_calls_with_no_text_are_not_an_error(monkeypatch):
    _install(monkeypatch, FakePool(_ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.error is None and turn.text == ""


# --------------------------------------------------------------------- fallbacks

def _http_error(status, message):
    return FakeResponse(status, {"error": {"message": message}})


def test_a_tool_choice_400_retries_once_with_auto(monkeypatch):
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "tool_choice 'required' is not supported"),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert [c["body"]["tool_choice"] for c in pool.calls] == ["required", "auto"]
    assert pool.calls[0]["body"]["model"] == pool.calls[1]["body"]["model"]
    assert turn.error is None and turn.tool_calls


def test_the_tool_choice_retry_happens_only_once(monkeypatch):
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "tool_choice 'required' is not supported"),
        _http_error(400, "tool_choice still unhappy"),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert len(pool.calls) == 2
    assert turn.error and "400" in turn.error


def test_a_400_not_about_tool_choice_is_not_retried(monkeypatch):
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "messages: invalid role"), _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert len(pool.calls) == 1
    assert turn.error and "400" in turn.error


def test_a_forced_function_choice_is_sent_as_is_first(monkeypatch):
    forced = {"type": "function", "function": {"name": "final_answer"}}
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS, tool_choice=forced)
    assert pool.calls[0]["body"]["tool_choice"] == forced
    assert len(pool.calls) == 1


def test_a_rejected_forced_choice_retries_auto_with_only_that_tool(monkeypatch):
    """muse-spark accepts only "auto" (TEST 2026-10-16). The forced last turn
    stays forced in effect: the one tool offered is the one it named."""
    forced = {"type": "function", "function": {"name": "final_answer"}}
    tools = TOOLS
    assert len(tools) > 1 and "final_answer" in [t["function"]["name"] for t in tools]
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "tool_choice rejected"), _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, tools, tool_choice=forced)
    assert [c["body"]["tool_choice"] for c in pool.calls] == [forced, "auto"]
    assert [t["function"]["name"] for t in pool.calls[1]["body"]["tools"]] ==         ["final_answer"]
    assert turn.error is None


def test_the_vendor_reason_inside_metadata_raw_counts(monkeypatch):
    """OpenRouter wraps the provider's 400 as "Provider returned error" and puts
    the reason in error.metadata.raw (live muse reply, 2026-10-16)."""
    raw = json.dumps({"error": {"message": "only `\"auto\"` is supported for "
                                "`tool_choice`.", "param": "tool_choice"}})
    pool = _install(monkeypatch, FakePool(
        FakeResponse(400, {"error": {"message": "Provider returned error",
                                     "code": 400, "metadata": {"raw": raw}}}),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert [c["body"]["tool_choice"] for c in pool.calls] == ["required", "auto"]
    assert turn.error is None


def test_the_fallback_chain_is_tried_in_order_on_5xx(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", "primary/m")
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "fb/one, fb/two")
    pool = _install(monkeypatch, FakePool(
        _http_error(502, "bad gateway"),
        _http_error(503, "unavailable"),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert [c["body"]["model"] for c in pool.calls] == \
        ["primary/m", "fb/one", "fb/two"]
    assert turn.error is None and turn.tool_calls


def test_the_fallback_chain_is_tried_on_timeout(monkeypatch):
    import urllib3
    monkeypatch.setenv("ASK_JUDGE_MODEL", "primary/m")
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "fb/one")
    pool = _install(monkeypatch, FakePool(
        urllib3.exceptions.ReadTimeoutError(None, None, "read timed out"),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert [c["body"]["model"] for c in pool.calls] == ["primary/m", "fb/one"]
    assert turn.error is None


def test_the_last_timeout_reports_timed_out(monkeypatch):
    import urllib3
    pool = _install(monkeypatch, FakePool(
        urllib3.exceptions.ReadTimeoutError(None, None, "read timed out")))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert len(pool.calls) == 1
    assert turn.timed_out is True and turn.error


def test_no_fallbacks_when_unset(monkeypatch):
    pool = _install(monkeypatch, FakePool(_http_error(502, "bad gateway"),
                                          _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert len(pool.calls) == 1
    assert turn.error and "502" in turn.error


def test_a_4xx_does_not_advance_the_chain(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "fb/one")
    pool = _install(monkeypatch, FakePool(_http_error(401, "no auth"),
                                          _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert len(pool.calls) == 1
    assert turn.error


def test_the_fallback_chain_stops_at_the_deadline(monkeypatch):
    """The chain is tried within the deadline, not past it."""
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "fb/one,fb/two")
    clock = {"now": 100.0}
    monkeypatch.setattr(tools_client, "_now", lambda: clock["now"])

    class SlowPool(FakePool):
        def request(self, *a, **k):
            clock["now"] += 9.5          # each attempt burns 9.5 s
            return super().request(*a, **k)

    pool = _install(monkeypatch, SlowPool(_http_error(502, "x"),
                                          _http_error(502, "y"),
                                          _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS, deadline=110.0)
    assert len(pool.calls) == 1
    assert turn.timed_out is True and turn.error


def test_the_attempt_timeout_never_exceeds_the_time_left(monkeypatch):
    monkeypatch.setattr(tools_client, "_now", lambda: 100.0)
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS, deadline=107.0)
    assert pool.calls[0]["timeout"] == pytest.approx(7.0)


# ---------------------------------------------------------------------- deadline

def test_under_one_second_left_returns_timed_out_without_a_call(monkeypatch):
    monkeypatch.setattr(tools_client, "_now", lambda: 100.0)
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS, deadline=100.5)
    assert turn.timed_out is True
    assert turn.tool_calls == [] and turn.error
    assert pool.calls == []


def test_a_passed_deadline_returns_timed_out_without_a_call(monkeypatch):
    monkeypatch.setattr(tools_client, "_now", lambda: 100.0)
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS, deadline=90.0)
    assert turn.timed_out is True and pool.calls == []


# ----------------------------------------------------------------------- logging

SENTINEL = "ZEBRA-SENTINEL-7731"


def test_the_log_names_caller_model_tools_usage_and_ms_but_no_text(
        monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    messages = [{"role": "user", "content": f"question {SENTINEL}"}]
    _install(monkeypatch, FakePool(FakeResponse(200, _body(
        content=f"thinking {SENTINEL}",
        tool_calls=[_tool_call("call_1", "search_records",
                               json.dumps({"query": SENTINEL}))],
        usage={"prompt_tokens": 321, "completion_tokens": 45}))))
    tools_client.chat(messages, TOOLS, caller="ask_judge_test")
    text = caplog.text
    assert SENTINEL not in text
    assert "ask_judge_test" in text
    assert "google/gemini-3.8-flash" in text
    assert "search_records" in text
    assert "321" in text
    assert "latency_ms=" in text or "ms=" in text


def test_failures_do_not_log_message_text(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    messages = [{"role": "user", "content": f"question {SENTINEL}"}]
    _install(monkeypatch, FakePool(
        _http_error(400, "tool_choice nope"),
        _http_error(500, "boom")))
    tools_client.chat(messages, TOOLS)
    assert SENTINEL not in caplog.text


def test_an_auto_only_model_is_remembered_for_the_next_turn(monkeypatch):
    monkeypatch.setattr(tools_client, "_AUTO_ONLY", set())
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "only auto is supported for tool_choice"),
        _ok_tool_reply(), _ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    tools_client.chat(MESSAGES, TOOLS)
    assert [c["body"]["tool_choice"] for c in pool.calls] == ["required", "auto", "auto"]


# ------------------------------------------- provider pin and reasoning control

def test_payload_has_no_provider_or_reasoning_when_unset(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    body = pool.calls[0]["body"]
    assert set(body) == {"model", "messages", "tools", "tool_choice", "max_tokens"}


def test_empty_env_values_send_nothing(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", " , ")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    body = pool.calls[0]["body"]
    assert "provider" not in body and "reasoning" not in body


def test_provider_pin_rides_on_the_primary(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8, other/tag")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert pool.calls[0]["body"]["provider"] == {
        "only": ["coreweave/fp8", "other/tag"], "allow_fallbacks": False}


@pytest.mark.parametrize("value,expected", [
    ("off", {"enabled": False}),
    ("OFF", {"enabled": False}),
    ("low", {"effort": "low"}),
    ("medium", {"effort": "medium"}),
    (" high ", {"effort": "high"}),
])
def test_reasoning_control_on_the_primary(monkeypatch, value, expected):
    monkeypatch.setenv("ASK_JUDGE_REASONING", value)
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    assert pool.calls[0]["body"]["reasoning"] == expected


def test_unknown_reasoning_value_warns_and_sends_nothing(monkeypatch, caplog):
    monkeypatch.setenv("ASK_JUDGE_REASONING", "turbo")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    with caplog.at_level(logging.WARNING):
        tools_client.chat(MESSAGES, TOOLS)
    assert "reasoning" not in pool.calls[0]["body"]
    assert any("ASK_JUDGE_REASONING" in r.getMessage() for r in caplog.records)


def test_env_is_read_at_call_time(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok_tool_reply(), _ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS)
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    tools_client.chat(MESSAGES, TOOLS)
    assert "provider" not in pool.calls[0]["body"]
    assert pool.calls[1]["body"]["provider"]["only"] == ["coreweave/fp8"]
    assert pool.calls[1]["body"]["reasoning"] == {"enabled": False}


def test_fallback_models_get_no_provider_or_reasoning(monkeypatch):
    """A fallback on another vendor must not inherit a CoreWeave-only pin."""
    monkeypatch.setenv("ASK_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "fb/one,fb/two")
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    pool = _install(monkeypatch, FakePool(_http_error(502, "x"),
                                          _http_error(503, "y"),
                                          _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.error is None
    bodies = [c["body"] for c in pool.calls]
    assert [b["model"] for b in bodies] == [
        "deepseek/deepseek-v4.1-flash", "fb/one", "fb/two"]
    assert bodies[0]["provider"] == {"only": ["coreweave/fp8"],
                                     "allow_fallbacks": False}
    assert bodies[0]["reasoning"] == {"enabled": False}
    for b in bodies[1:]:
        assert "provider" not in b and "reasoning" not in b


def test_an_explicit_other_model_gets_no_pin(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    pool = _install(monkeypatch, FakePool(_ok_tool_reply()))
    tools_client.chat(MESSAGES, TOOLS, model="other/model")
    body = pool.calls[0]["body"]
    assert "provider" not in body and "reasoning" not in body


def test_pin_survives_the_tool_choice_downgrade_retry(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    pool = _install(monkeypatch, FakePool(
        _http_error(400, "tool_choice required is not supported"),
        _ok_tool_reply()))
    turn = tools_client.chat(MESSAGES, TOOLS)
    assert turn.error is None
    assert [c["body"]["tool_choice"] for c in pool.calls] == ["required", "auto"]
    for c in pool.calls:
        assert c["body"]["provider"]["only"] == ["coreweave/fp8"]
        assert c["body"]["reasoning"] == {"enabled": False}
