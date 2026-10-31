"""call_llm(info=...) reports which model of the chain actually answered, and its usage
(action packs spec §4.5 / §14 S6: the models actually used are recorded after fallbacks;
Task 11). Additive: a caller passing no `info` sees no change. HTTP is stubbed at
urllib3.PoolManager.request as in test_model_fallback_chain.py; no real call is made."""
import json

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

PRIMARY = "deepseek/deepseek-v4.1-flash"
LUNA = "openai/gpt-6-luna-pro"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text, served):
    return _R(200, {"model": served, "choices": [{"message": {"content": text},
                                                   "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 30}})


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu, "QWEN_MODEL", PRIMARY)
    monkeypatch.setattr(lu, "QWEN_MODEL_NONTHINKING", "")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", LUNA)
    monkeypatch.setenv("LLM_CHAIN_BUDGET_SECONDS", "840")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)


def test_info_names_the_fallback_that_answered(monkeypatch):
    _stub(monkeypatch, [_R(503, {"error": {"message": "x"}})] * 2
          + [_ok("{}", "openai/gpt-6-luna-pro-20261001")])
    info = {}
    assert lu.call_llm("p", force_json=True, model=PRIMARY, deadline=60, info=info) == \
        ("{}", None)
    assert info["model"] == LUNA                       # the chain entry that answered
    assert info["served_model"] == "openai/gpt-6-luna-pro-20261001"
    assert info["usage"] == {"prompt_tokens": 120, "completion_tokens": 30}


def test_info_names_the_primary_when_it_answers(monkeypatch):
    _stub(monkeypatch, [_ok("{}", PRIMARY)])
    info = {}
    lu.call_llm("p", force_json=True, model=PRIMARY, deadline=60, info=info)
    assert info["model"] == PRIMARY


def test_info_has_no_model_when_every_model_failed(monkeypatch):
    _stub(monkeypatch, [_R(503, {"error": {"message": "x"}})] * 10)
    info = {}
    text, err = lu.call_llm("p", force_json=True, model=PRIMARY, deadline=60, info=info)
    assert text is None and err
    assert "model" not in info


def test_anthropic_path_names_its_model(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "_call_anthropic", lambda *a, **k: ("{}", None))
    info = {}
    assert lu.call_llm("p", info=info) == ("{}", None)
    assert info["model"] == lu.CLAUDE_MODEL


def test_no_info_is_the_old_call(monkeypatch):
    _stub(monkeypatch, [_ok("{}", PRIMARY)])
    assert lu.call_llm("p", force_json=True, model=PRIMARY) == ("{}", None)
