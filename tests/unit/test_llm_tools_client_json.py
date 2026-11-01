"""llm_tools_client.complete_json: the Ask judge's routing (provider pin, reasoning,
ASK_JUDGE_FALLBACKS) for a tool-less JSON answer (action packs Task 11 fix round 1: the
writer IS the Ask judge). The HTTP layer is mocked as in test_llm_tools_client.py."""
import json

import pytest

import corroboration_client
import llm_tools_client as tc
import llm_utils

JUDGE = "deepseek/deepseek-v4.1-flash"
FB = "openai/gpt-6-luna-pro"


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakePool:
    def __init__(self, *responses):
        self.queue, self.calls = list(responses), []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.calls.append({"timeout": timeout, "body": json.loads(body)})
        nxt = self.queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _ok(text='{"a": 1}', served=JUDGE + "-20261001"):
    return FakeResponse(200, {"model": served,
                              "choices": [{"message": {"content": text},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", "k")
    monkeypatch.setenv("ASK_JUDGE_MODEL", JUDGE)
    monkeypatch.setenv("ASK_JUDGE_PROVIDER", "coreweave/fp8")
    monkeypatch.setenv("ASK_JUDGE_REASONING", "off")
    monkeypatch.delenv("ASK_JUDGE_FALLBACKS", raising=False)


def _install(monkeypatch, pool):
    monkeypatch.setattr(corroboration_client.urllib3, "PoolManager", lambda *a, **k: pool)
    return pool


def test_the_judge_model_carries_pin_reasoning_and_json_mode(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok()))
    turn = tc.complete_json("prompt", model=JUDGE, max_tokens=500, deadline=None,
                            caller="action_write")
    body = pool.calls[0]["body"]
    assert body["model"] == JUDGE
    assert body["provider"] == llm_utils.parse_provider("coreweave/fp8")
    assert body["reasoning"] == {"enabled": False}
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "prompt"}]
    assert "tools" not in body and "tool_choice" not in body
    assert body["max_tokens"] == 500 + llm_utils.REASONING_HEADROOM_TOKENS
    assert turn.error is None and turn.text == '{"a": 1}'
    assert turn.requested_model == JUDGE and turn.model == JUDGE + "-20261001"
    assert turn.usage == {"prompt_tokens": 100, "completion_tokens": 20}


def test_falls_back_along_ask_judge_fallbacks_without_the_pin(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", FB)
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {"error": {"message": "x"}}),
                                          _ok(served=FB)))
    turn = tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=None, caller="t")
    assert [c["body"]["model"] for c in pool.calls] == [JUDGE, FB]
    assert "provider" not in pool.calls[1]["body"] and "reasoning" not in pool.calls[1]["body"]
    assert turn.error is None and turn.requested_model == FB


def test_unset_fallbacks_mean_none(monkeypatch):
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {"error": {"message": "x"}})))
    turn = tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=None, caller="t")
    assert len(pool.calls) == 1 and turn.error


def test_a_4xx_is_ours_and_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", FB)
    pool = _install(monkeypatch, FakePool(FakeResponse(400, {"error": {"message": "bad"}})))
    turn = tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=None, caller="t")
    assert len(pool.calls) == 1 and turn.error.startswith("HTTP 400")


def test_another_model_gets_no_judge_pin(monkeypatch):
    pool = _install(monkeypatch, FakePool(_ok()))
    tc.complete_json("p", model="x/other", max_tokens=500, deadline=None, caller="t")
    assert "provider" not in pool.calls[0]["body"]


def test_deadline_bounds_the_attempt_and_no_time_means_no_call(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(tc, "_now", lambda: now[0])
    pool = _install(monkeypatch, FakePool(_ok()))
    tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=112.0, caller="t")
    assert pool.calls[0]["timeout"] == pytest.approx(12.0)
    turn = tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=100.5, caller="t")
    assert turn.timed_out and len(pool.calls) == 1


def test_an_empty_200_is_a_failure(monkeypatch):
    _install(monkeypatch, FakePool(_ok(text="")))
    turn = tc.complete_json("p", model=JUDGE, max_tokens=500, deadline=None, caller="t")
    assert turn.error == "empty completion"


def test_plain_routing_tries_only_the_model_asked_for_and_never_pins(monkeypatch):
    """The action-pack verifier (Task 12) is not the judge: no judge pin or reasoning even
    on the judge's model id, and no ASK_JUDGE_FALLBACKS (it walks its own chain)."""
    monkeypatch.setenv("ASK_JUDGE_FALLBACKS", FB)
    pool = _install(monkeypatch, FakePool(FakeResponse(503, {"error": {"message": "x"}})))
    turn = tc.complete_json("p", model=JUDGE, max_tokens=50, deadline=None, caller="v",
                            judge_routing=False)
    assert [c["body"]["model"] for c in pool.calls] == [JUDGE]
    assert "provider" not in pool.calls[0]["body"]
    assert "reasoning" not in pool.calls[0]["body"]
    assert turn.error and turn.requested_model == ""

    pool = _install(monkeypatch, FakePool(_ok(served="g")))
    turn = tc.complete_json("p", model="google/gemini-3.8-flash", max_tokens=50,
                            deadline=None, caller="v", judge_routing=False)
    assert turn.error is None and turn.requested_model == "google/gemini-3.8-flash"
