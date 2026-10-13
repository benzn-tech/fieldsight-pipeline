"""The voice endpoint streams with the request Python would have sent.
That only holds if there is ONE builder; two copies drift (the repo has paid
for a forked request shape before -- vendor-swap fossils).

The expected bodies below are LITERALS captured from `_call_qwen` before the
builder was extracted. Comparing the builder to what `_call_qwen` posts would be
a tautology once `_call_qwen` calls the builder; the literals are what pin the
refactor to the behaviour it replaced."""
import importlib
import json

import pytest

OR = "https://openrouter.ai/api/v1"
DS = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
P = "hello prompt"
U = [{"role": "user", "content": P}]
JSON_MODE = {"type": "json_object"}

# (id, base_url, env, (max_tokens, force_json, enable_thinking, model), body)
CASES = [
    ("or_text_off", OR, {}, (512, False, False, "google/x"),
     {"model": "google/x", "messages": U, "reasoning": {"effort": "low"}, "max_tokens": 8512}),
    ("or_json_off", OR, {}, (512, True, False, "google/x"),
     {"model": "google/x", "messages": U, "reasoning": {"effort": "low"}, "max_tokens": 8512,
      "response_format": JSON_MODE}),
    ("or_text_on", OR, {}, (512, False, True, "google/x"),
     {"model": "google/x", "messages": U, "reasoning": {"effort": "high"}, "max_tokens": 8512}),
    ("or_json_none_default_model", OR, {}, (512, True, None, None),
     {"model": "qwen3.8-flash", "messages": U, "reasoning": {"effort": "low"}, "max_tokens": 8512,
      "response_format": JSON_MODE}),
    ("or_text_none_default_model", OR, {}, (512, False, None, None),
     {"model": "qwen3.8-flash", "messages": U, "reasoning": {"effort": "low"}, "max_tokens": 8512}),
    ("or_effort_medium_none", OR, {"LLM_REASONING_EFFORT": "medium"}, (512, True, None, "google/x"),
     {"model": "google/x", "messages": U, "reasoning": {"effort": "medium"}, "max_tokens": 8512,
      "response_format": JSON_MODE}),
    ("or_effort_high_but_call_says_off", OR, {"LLM_REASONING_EFFORT": "high"}, (512, True, False, "google/x"),
     {"model": "google/x", "messages": U, "reasoning": {"effort": "low"}, "max_tokens": 8512,
      "response_format": JSON_MODE}),
    ("or_temperature", OR, {"LLM_TEMPERATURE": "0"}, (512, False, True, "google/x"),
     {"model": "google/x", "messages": U, "temperature": 0.0, "reasoning": {"effort": "high"},
      "max_tokens": 8512}),
    ("or_mimo_json", OR, {}, (512, True, True, "xiaomi/mimo-v2"),
     {"model": "xiaomi/mimo-v2", "messages": U, "reasoning": {"enabled": False}, "max_tokens": 512,
      "response_format": JSON_MODE}),
    ("or_mimo_text", OR, {}, (512, False, None, "xiaomi/mimo-v2"),
     {"model": "xiaomi/mimo-v2", "messages": U, "reasoning": {"enabled": False}, "max_tokens": 512}),
    ("ds_text_off", DS, {}, (512, False, False, "qwen-x"),
     {"model": "qwen-x", "messages": U, "enable_thinking": False, "max_tokens": 512}),
    ("ds_json_off", DS, {}, (512, True, False, "qwen-x"),
     {"model": "qwen-x", "messages": U, "enable_thinking": False, "response_format": JSON_MODE}),
    ("ds_text_on", DS, {}, (512, False, True, "qwen-x"),
     {"model": "qwen-x", "messages": U, "enable_thinking": True}),
    ("ds_json_on_default_model", DS, {}, (512, True, True, None),
     {"model": "qwen3.8-flash", "messages": U, "enable_thinking": True}),
]

_ENV = ("LLM_TEMPERATURE", "LLM_REASONING_EFFORT", "LLM_FALLBACK_MODELS", "QWEN_MODEL",
        "QWEN_MODEL_NONTHINKING", "QWEN_ENABLE_THINKING", "LLM_REASONING_HEADROOM")


@pytest.fixture
def load(monkeypatch):
    """Reload llm_utils under a chosen env: it reads its config at import."""
    def _load(base_url, env):
        for k in _ENV:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("QWEN_API_KEY", "k")
        monkeypatch.setenv("QWEN_BASE_URL", base_url)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        import llm_utils
        return importlib.reload(llm_utils)
    yield _load
    # monkeypatch undoes the env but not a module reloaded under it.
    monkeypatch.undo()
    import llm_utils
    importlib.reload(llm_utils)


def _posted(lu, monkeypatch, prompt, args):
    """The body `_call_qwen` really sends, captured at the HTTP seam."""
    sent = {}

    def fake_post(url, body, headers, deadline=None, clock=None, **kw):
        sent["url"], sent["body"] = url, body
        return None, "stop after capture"

    monkeypatch.setattr(lu, "_post_with_retry", fake_post)
    monkeypatch.setattr(lu, "model_chain", lambda m: [m])
    max_tokens, force_json, thinking, model = args
    lu._call_qwen(prompt, max_tokens, force_json, enable_thinking=thinking, model=model)
    assert sent["url"] == f"{lu.QWEN_BASE_URL}/chat/completions"
    return json.loads(sent["body"])


@pytest.mark.parametrize("base,env,args,body", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_call_qwen_still_posts_the_captured_body(load, monkeypatch, base, env, args, body):
    lu = load(base, env)
    assert _posted(lu, monkeypatch, P, args) == body


@pytest.mark.parametrize("base,env,args,body", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_builder_returns_the_captured_body(load, base, env, args, body):
    lu = load(base, env)
    max_tokens, force_json, thinking, model = args
    assert lu.build_qwen_payload(P, max_tokens, force_json, enable_thinking=thinking, model=model) == body


def test_builder_is_what_call_qwen_posts(load, monkeypatch):
    lu = load(OR, {})
    built = lu.build_qwen_payload(P, 512, False, enable_thinking=False, model="google/x")
    assert _posted(lu, monkeypatch, P, (512, False, False, "google/x")) == built


def test_builder_sends_one_user_message(load):
    lu = load(OR, {})
    p = lu.build_qwen_payload("the whole prompt", 256, False)
    assert p["messages"] == [{"role": "user", "content": "the whole prompt"}]
