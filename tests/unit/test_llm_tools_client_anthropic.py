"""llm_tools_client: Claude ids go to api.anthropic.com, never OpenRouter (owner rule
2026-11-04). The HTTP layer is mocked as in test_llm_tools_client.py: the client builds
its pool through corroboration_client.http_session(), so one PoolManager patch catches
both vendors' requests."""
import json

import pytest

import action_pack_verify
import corroboration_client
import llm_tools_client as tc
import llm_utils

OPUS = "claude-opus-5-5"
OR_MODEL = "deepseek/deepseek-v4.1-flash"
HEADROOM = llm_utils.REASONING_HEADROOM_TOKENS


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakePool:
    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout,
                           "body": json.loads(body)})
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", "or-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an-key")
    for name in ("ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS", "CORROBORATION_CHEAP_MODEL",
                 "ASK_JUDGE_PROVIDER", "ASK_JUDGE_REASONING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tc, "_AUTO_ONLY", set())


def _install(monkeypatch, pool):
    monkeypatch.setattr(corroboration_client.urllib3, "PoolManager", lambda *a, **k: pool)
    return pool


def _no_claude_to_openrouter(pool):
    for c in pool.calls:
        if c["url"] == corroboration_client.API_URL:
            assert not tc.is_claude(c["body"]["model"])


def _msg(content, stop="end_turn", model=OPUS, usage=None):
    return FakeResponse(200, {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop,
        "usage": usage or {"input_tokens": 100, "output_tokens": 30}})


def _tool_use(cid, name, inp):
    return {"type": "tool_use", "id": cid, "name": name, "input": inp}


def _or_ok(text='{"a": 1}', model=OR_MODEL):
    return FakeResponse(200, {"model": model,
                              "choices": [{"message": {"content": text},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 2}})


def _err(status, message="x", etype="api_error"):
    return FakeResponse(status, {"type": "error",
                                 "error": {"type": etype, "message": message}})


TOOLS = [
    {"type": "function",
     "function": {"name": "search_records", "description": "Search the site records.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}}},
    {"type": "function",
     "function": {"name": "final_answer", "description": "Answer the reader.",
                  "parameters": {"type": "object",
                                 "properties": {"answer": {"type": "string"}},
                                 "required": ["answer"]}}},
]

# The shape ask_orchestrator._messages builds: one synthetic assistant tool round, a
# `tool` message per call, then the forced-final user nudge.
HISTORY = [
    {"role": "system", "content": "You are the judge."},
    {"role": "user", "content": "What did we do yesterday?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_0", "type": "function",
         "function": {"name": "search_records", "arguments": '{"query": "yesterday"}'}},
        {"id": "call.1", "type": "function",
         "function": {"name": "search_records", "arguments": "not json"}}]},
    {"role": "tool", "tool_call_id": "call_0", "content": "record A"},
    {"role": "tool", "tool_call_id": "call.1", "content": "record B"},
    {"role": "user", "content": "Answer now."},
]


# ------------------------------------------------------------------------ routing

def test_a_claude_id_goes_to_anthropic_with_its_own_key(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg([{"type": "text", "text": "{}"}])))
    turn = tc.complete_json("p", model=OPUS, caller="action_write")
    call = pool.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "an-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in call["headers"]
    assert turn.error is None and turn.text == "{}"


def test_a_non_claude_id_still_goes_to_openrouter(monkeypatch):
    pool = _install(monkeypatch, FakePool(_or_ok()))
    tc.complete_json("p", model=OR_MODEL)
    call = pool.calls[0]
    assert call["url"] == corroboration_client.API_URL
    assert call["headers"]["Authorization"] == "Bearer or-key"
    assert "x-api-key" not in call["headers"]


def test_chat_routes_a_claude_judge_to_anthropic(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OPUS)
    pool = _install(monkeypatch, FakePool(_msg(
        [_tool_use("toolu_1", "search_records", {"query": "q"})], stop="tool_use")))
    tc.chat(HISTORY[:2], TOOLS)
    assert pool.calls[0]["url"] == tc.ANTHROPIC_URL
    _no_claude_to_openrouter(pool)


@pytest.mark.parametrize("given,native", [
    ("anthropic/claude-opus-5.5", "claude-opus-5-5"),
    ("anthropic/claude-sonnet-4.6", "claude-sonnet-4-6"),
    ("anthropic/claude-haiku-4.5:beta", "claude-haiku-4-5"),
    ("claude-opus-5-5", "claude-opus-5-5"),
    ("Claude-Opus-5-5", "claude-opus-5-5"),
])
def test_id_mapping(given, native):
    assert tc.is_claude(given)
    assert tc.native_claude_id(given) == native


@pytest.mark.parametrize("mid", ["deepseek/deepseek-v4.1-flash", "google/gemini-3.8-flash",
                                 "openai/gpt-6-luna-pro", "meta/muse-spark", "", None])
def test_non_claude_ids_are_not_claude(mid):
    assert not tc.is_claude(mid)


def test_an_openrouter_style_id_is_sent_native_and_reported_as_requested(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg([{"type": "text", "text": "{}"}])))
    turn = tc.complete_json("p", model="anthropic/claude-opus-5.5")
    assert pool.calls[0]["url"] == tc.ANTHROPIC_URL
    assert pool.calls[0]["body"]["model"] == "claude-opus-5-5"
    assert turn.requested_model == "anthropic/claude-opus-5.5"
    assert turn.model == OPUS


# ------------------------------------------------------------------ request shape

def test_history_converts_to_anthropic_blocks(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg(
        [_tool_use("toolu_9", "final_answer", {"answer": "A"})], stop="tool_use")))
    tc.chat(HISTORY, TOOLS, model=OPUS)
    body = pool.calls[0]["body"]
    assert body["system"] == "You are the judge."
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == [{"type": "text", "text": "What did we do yesterday?"}]
    assert msgs[1]["content"] == [
        {"type": "tool_use", "id": "call_0", "name": "search_records",
         "input": {"query": "yesterday"}},
        {"type": "tool_use", "id": "call_1", "name": "search_records", "input": {}}]
    assert msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_0", "content": "record A"},
        {"type": "tool_result", "tool_use_id": "call_1", "content": "record B"},
        {"type": "text", "text": "Answer now."}]
    assert body["tools"] == [
        {"name": "search_records", "description": "Search the site records.",
         "input_schema": TOOLS[0]["function"]["parameters"]},
        {"name": "final_answer", "description": "Answer the reader.",
         "input_schema": TOOLS[1]["function"]["parameters"]}]
    assert body["max_tokens"] == 2048 + HEADROOM


def test_no_openrouter_only_fields_reach_anthropic(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OPUS)
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    pool = _install(monkeypatch, FakePool(
        _msg([_tool_use("t", "final_answer", {"answer": "A"})], stop="tool_use"),
        _msg([{"type": "text", "text": "{}"}])))
    tc.chat(HISTORY[:2], TOOLS)
    tc.complete_json("p")
    for call in pool.calls:
        body = call["body"]
        for k in ("provider", "reasoning", "response_format", "plugins", "thinking",
                  "temperature", "output_config"):
            assert k not in body
        assert all(m["role"] in ("user", "assistant") for m in body["messages"])


def test_required_becomes_auto_on_a_model_that_thinks_by_default(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg(
        [_tool_use("t", "search_records", {"query": "q"})], stop="tool_use")))
    tc.chat(HISTORY[:2], TOOLS, model=OPUS, tool_choice="required")
    assert pool.calls[0]["body"]["tool_choice"] == {"type": "auto"}
    assert len(pool.calls[0]["body"]["tools"]) == 2


def test_a_forced_function_offers_only_that_tool_under_auto_on_5x(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg(
        [_tool_use("t", "final_answer", {"answer": "A"})], stop="tool_use")))
    forced = {"type": "function", "function": {"name": "final_answer"}}
    tc.chat(HISTORY, TOOLS, model=OPUS, tool_choice=forced)
    body = pool.calls[0]["body"]
    assert body["tool_choice"] == {"type": "auto"}
    assert [t["name"] for t in body["tools"]] == ["final_answer"]


def test_forced_choices_map_natively_on_a_model_without_default_thinking(monkeypatch):
    pool = _install(monkeypatch, FakePool(
        _msg([_tool_use("t", "search_records", {"query": "q"})], stop="tool_use"),
        _msg([_tool_use("t", "final_answer", {"answer": "A"})], stop="tool_use"),
        _msg([{"type": "text", "text": "hi"}])))
    haiku = "claude-haiku-4-5"
    tc.chat(HISTORY[:2], TOOLS, model=haiku, tool_choice="required")
    tc.chat(HISTORY[:2], TOOLS, model=haiku,
            tool_choice={"type": "function", "function": {"name": "final_answer"}})
    tc.chat(HISTORY[:2], TOOLS, model=haiku, tool_choice="auto")
    assert pool.calls[0]["body"]["tool_choice"] == {"type": "any"}
    assert pool.calls[1]["body"]["tool_choice"] == {"type": "tool", "name": "final_answer"}
    assert pool.calls[2]["body"]["tool_choice"] == {"type": "auto"}


def test_complete_json_asks_for_json_in_system_and_sends_no_tools(monkeypatch):
    pool = _install(monkeypatch, FakePool(_msg([{"type": "text", "text": '{"a": 1}'}])))
    turn = tc.complete_json("the prompt", model=OPUS, max_tokens=500)
    body = pool.calls[0]["body"]
    assert body["system"] == tc.JSON_SYSTEM
    assert body["messages"] == [{"role": "user",
                                 "content": [{"type": "text", "text": "the prompt"}]}]
    assert "tools" not in body and "tool_choice" not in body
    assert body["max_tokens"] == 500 + HEADROOM
    assert turn.text == '{"a": 1}' and turn.error is None


# ------------------------------------------------------------------ response shape

def test_tool_use_blocks_come_back_as_tool_calls(monkeypatch):
    _install(monkeypatch, FakePool(_msg([
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Looking."},
        _tool_use("toolu_1", "search_records", {"query": "concrete"}),
        {"type": "tool_use", "id": "toolu_2", "name": "search_records", "input": "bad"},
    ], stop="tool_use")))
    turn = tc.chat(HISTORY[:2], TOOLS, model=OPUS)
    assert turn.error is None
    assert turn.text == "Looking."
    assert turn.tool_calls == [
        {"id": "toolu_1", "name": "search_records", "arguments": {"query": "concrete"}},
        {"id": "toolu_2", "name": "search_records", "arguments": {}}]
    assert turn.bad_args == ["toolu_2"]
    assert turn.model == OPUS


def test_a_returned_turn_round_trips_into_the_next_request(monkeypatch):
    """Anthropic tool_use -> ChatTurn -> OpenAI history -> Anthropic tool_use again."""
    pool = _install(monkeypatch, FakePool(
        _msg([_tool_use("toolu_1", "search_records", {"query": "q"})], stop="tool_use"),
        _msg([_tool_use("toolu_2", "final_answer", {"answer": "A"})], stop="tool_use")))
    first = tc.chat(HISTORY[:2], TOOLS, model=OPUS)
    call = first.tool_calls[0]
    history = HISTORY[:2] + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call["id"], "type": "function",
             "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}]},
        {"role": "tool", "tool_call_id": call["id"], "content": "result"}]
    tc.chat(history, TOOLS, model=OPUS)
    msgs = pool.calls[1]["body"]["messages"]
    assert msgs[1] == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "search_records",
         "input": {"query": "q"}}]}
    assert msgs[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"}]}


def test_usage_maps_to_openai_keys_and_logs_as_anthropic(monkeypatch):
    seen = []
    monkeypatch.setattr(tc, "log_usage", lambda *a, **k: seen.append((a, k)))
    _install(monkeypatch, FakePool(_msg([{"type": "text", "text": "{}"}], usage={
        "input_tokens": 100, "output_tokens": 30, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5})))
    turn = tc.complete_json("p", model=OPUS, caller="action_verify")
    assert turn.usage["prompt_tokens"] == 125
    assert turn.usage["completion_tokens"] == 30
    (args, kwargs), = seen
    assert args[0] == "anthropic" and args[1] == OPUS and args[2] == "action_verify"
    assert kwargs["prompt_tokens"] == 125 and kwargs["completion_tokens"] == 30
    assert kwargs["cache_read_tokens"] == 20 and kwargs["cache_write_tokens"] == 5


def test_openrouter_calls_still_log_as_openrouter(monkeypatch):
    seen = []
    monkeypatch.setattr(tc, "log_usage", lambda *a, **k: seen.append(a[0]))
    _install(monkeypatch, FakePool(_or_ok()))
    tc.complete_json("p", model=OR_MODEL)
    assert seen == ["openrouter"]


@pytest.mark.parametrize("text", [
    '<function_calls><invoke name="search_records"><parameter name="query">q',
    'I will call {"name": "search_records", "arguments": {"query": "q"}}',
    'final_answer({"answer": "A"})',
])
def test_a_tool_call_written_as_text_is_an_error(monkeypatch, text):
    _install(monkeypatch, FakePool(_msg([{"type": "text", "text": text}])))
    turn = tc.chat(HISTORY[:2], TOOLS, model=OPUS)
    assert turn.error == "tool call written as text"
    assert turn.tool_calls == []


def test_plain_text_with_tools_offered_is_not_flagged(monkeypatch):
    _install(monkeypatch, FakePool(_msg([{"type": "text",
                                          "text": "The final answer is the slab pour."}])))
    turn = tc.chat(HISTORY[:2], TOOLS, model=OPUS)
    assert turn.error is None


def test_a_refusal_is_an_error(monkeypatch):
    _install(monkeypatch, FakePool(_msg([], stop="refusal")))
    assert tc.complete_json("p", model=OPUS).error == "refusal"


def test_max_tokens_spent_thinking_is_an_error(monkeypatch):
    _install(monkeypatch, FakePool(_msg([{"type": "thinking", "thinking": ""}],
                                        stop="max_tokens")))
    assert tc.complete_json("p", model=OPUS).error == "max_tokens before any text"


# --------------------------------------------------------------- keys and fallback

def test_a_missing_anthropic_key_is_an_error_and_never_openrouter(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    pool = _install(monkeypatch, FakePool(_or_ok(), _or_ok()))
    turn = tc.complete_json("p", model=OPUS)
    assert turn.error == "ANTHROPIC_API_KEY not configured"
    chat_turn = tc.chat(HISTORY[:2], TOOLS, model=OPUS)
    assert chat_turn.error == "ANTHROPIC_API_KEY not configured"
    assert pool.calls == []


def test_a_missing_anthropic_key_on_a_fallback_ends_the_chain(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ASK_JUDGE_MODEL", OR_MODEL)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", OPUS + ",google/gemini-3.8-flash")
    pool = _install(monkeypatch, FakePool(_err(502), _or_ok()))
    turn = tc.complete_json("p")
    assert turn.error == "ANTHROPIC_API_KEY not configured"
    assert len(pool.calls) == 1
    _no_claude_to_openrouter(pool)


def test_an_all_claude_chain_needs_no_openrouter_key(monkeypatch):
    monkeypatch.delenv("CORROBORATION_API_KEY")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    pool = _install(monkeypatch, FakePool(_msg([{"type": "text", "text": "{}"}])))
    turn = tc.complete_json("p", model=OPUS)
    assert turn.error is None and pool.calls[0]["url"] == tc.ANTHROPIC_URL


def test_a_529_falls_back_to_the_next_entry_on_openrouter(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OPUS)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", OR_MODEL)
    pool = _install(monkeypatch, FakePool(_err(529, "Overloaded", "overloaded_error"),
                                          _or_ok()))
    turn = tc.complete_json("p")
    assert [c["url"] for c in pool.calls] == [tc.ANTHROPIC_URL, corroboration_client.API_URL]
    assert pool.calls[1]["body"]["model"] == OR_MODEL
    assert turn.error is None and turn.requested_model == OR_MODEL


def test_a_529_in_chat_falls_back_too(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OPUS)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", OR_MODEL)
    pool = _install(monkeypatch, FakePool(
        _err(529, "Overloaded", "overloaded_error"),
        FakeResponse(200, {"model": OR_MODEL, "choices": [{"message": {
            "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {
                "name": "final_answer", "arguments": '{"answer": "A"}'}}]}}]})))
    turn = tc.chat(HISTORY[:2], TOOLS)
    assert turn.error is None and turn.tool_calls[0]["name"] == "final_answer"
    assert pool.calls[1]["url"] == corroboration_client.API_URL


def test_an_openrouter_5xx_falls_back_to_a_claude_entry_on_anthropic(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OR_MODEL)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", "anthropic/claude-opus-5.5")
    pool = _install(monkeypatch, FakePool(_err(503),
                                          _msg([{"type": "text", "text": "{}"}])))
    turn = tc.complete_json("p")
    assert pool.calls[1]["url"] == tc.ANTHROPIC_URL
    assert pool.calls[1]["body"]["model"] == "claude-opus-5-5"
    assert turn.requested_model == "anthropic/claude-opus-5.5"
    _no_claude_to_openrouter(pool)


def test_an_anthropic_400_does_not_advance_the_chain(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_MODEL", OPUS)
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", OR_MODEL)
    pool = _install(monkeypatch, FakePool(_err(400, "bad", "invalid_request_error"),
                                          _or_ok()))
    turn = tc.complete_json("p")
    assert turn.error == "HTTP 400: bad" and len(pool.calls) == 1


def test_the_attempt_timeout_rules_apply_to_anthropic(monkeypatch):
    monkeypatch.setattr(tc, "_now", lambda: 100.0)
    pool = _install(monkeypatch, FakePool(_msg([{"type": "text", "text": "{}"}])))
    tc.complete_json("p", model=OPUS, deadline=105.0, attempt_timeout=20)
    assert pool.calls[0]["timeout"] == pytest.approx(5.0)


# ---------------------------------------------------------------- verifier family

@pytest.mark.parametrize("mid", ["claude-opus-5-5", "anthropic/claude-opus-5.5",
                                 "claude-haiku-4-5"])
def test_verifier_family_of_claude_ids_is_anthropic(mid):
    assert action_pack_verify.family(mid) == "anthropic"
