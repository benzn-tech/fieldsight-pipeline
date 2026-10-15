"""The multi-part (image / file) call: its own request, its own model chain.

Spec 2026-10-16 (template digitise) section 3. The vision read must not inherit the
text path's habits: the deploy's LLM_FALLBACK_MODELS names text models, and a text
model handed page images answers from the prompt alone and invents items that no
code check can catch. And the request must be the spike's request (ruling R3), so the
gate in Task 0 measured what ships. HTTP is stubbed at urllib3.PoolManager.request,
as in test_model_fallback_chain.py.
"""
import json

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

GEM = "google/gemini-3.8-flash"
TEXT_FALLBACK = "openai/gpt-6-luna-pro"
PARTS = [{"type": "text", "text": "read this"},
         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text='{"sections": []}', model=GEM):
    return _R(200, {"model": model, "choices": [{"message": {"content": text},
                                                  "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


def _err(status, msg="boom"):
    return _R(status, {"error": {"message": msg}})


@pytest.fixture(autouse=True)
def openrouter(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu, "LLM_TEMPERATURE", None)        # prod: unset
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")  # the text path's setting
    monkeypatch.setenv("LLM_FALLBACK_MODELS", TEXT_FALLBACK)  # the TEXT chain
    monkeypatch.delenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("LLM_CHAIN_BUDGET_SECONDS", "840")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def test_the_parts_go_out_as_one_user_message_exactly_as_given(monkeypatch):
    sent = _stub(monkeypatch, [_ok()])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=16000, caller="t")
    assert (text, err) == ('{"sections": []}', None)
    assert sent[0]["model"] == GEM
    assert sent[0]["messages"] == [{"role": "user", "content": PARTS}]


def test_the_request_is_the_spikes_not_the_text_paths(monkeypatch):
    sent = _stub(monkeypatch, [_ok()])
    lu.call_llm_parts(PARTS, model=GEM, max_tokens=16000, caller="t")
    body = sent[0]
    assert body["temperature"] == 0, "temperature 0 even where LLM_TEMPERATURE is unset"
    assert "reasoning" not in body, "LLM_REASONING_EFFORT=high must not ride into a vision read"
    assert body["max_tokens"] == 16000, "no reasoning headroom is added"
    assert body["response_format"] == {"type": "json_object"}


def test_the_text_fallback_chain_is_never_consulted(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)] * lu.MAX_ATTEMPTS)
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and err
    assert {b["model"] for b in sent} == {GEM}


def test_its_own_chain_is_tried_when_one_is_named(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", "vendor/vision-b")
    sent = _stub(monkeypatch, [_err(503)] * lu.MAX_ATTEMPTS + [_ok("from-b", "vendor/vision-b")])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100,
                                  fallback_env="TEMPLATE_DIGITISE_FALLBACK_MODELS", caller="t")
    assert (text, err) == ("from-b", None)
    assert sent[-1]["model"] == "vendor/vision-b"
    assert sent[-1]["messages"][0]["content"] == PARTS


def test_our_own_bad_request_is_not_moved_to_another_model(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_FALLBACK_MODELS", "vendor/vision-b")
    sent = _stub(monkeypatch, [_err(400, "bad part")])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100,
                                  fallback_env="TEMPLATE_DIGITISE_FALLBACK_MODELS", caller="t")
    assert (text, err) == (None, "bad part")
    assert len(sent) == 1


def test_dashscope_cannot_take_parts_and_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_BASE_URL",
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    sent = _stub(monkeypatch, [])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and "OpenRouter" in err
    assert sent == []


def test_the_anthropic_provider_cannot_take_parts(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    sent = _stub(monkeypatch, [])
    text, err = lu.call_llm_parts(PARTS, model=GEM, max_tokens=100, caller="t")
    assert text is None and "OpenRouter" in err and sent == []
