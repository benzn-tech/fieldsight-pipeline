"""`enable_thinking` is a DashScope extension and must never leave for another vendor.

Chat and embeddings were pinned to one credential on seven functions
(`QWEN_API_KEY: !Ref DashScopeApiKey`) while `QwenBaseUrl` is a single shared
parameter. Pointing that base URL at another vendor without first separating
the credential would have sent the DashScope key to that vendor and 401'd all
seven in one deploy. The template now carries `QwenChatApiKey`; this file
covers the other half -- the request body.

The failure being prevented is the QUIET one. An unknown field is either
rejected (loud, findable) or dropped (silent), and if it is dropped then "no
thinking" becomes thinking on the Ask path, which is the one place the choice
was made for latency. `meta/muse-spark-1.3-contributor`'s advertised
`supported_parameters` do not include `enable_thinking`.
"""
import importlib
import json

import pytest

DASHSCOPE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _restore_the_module():
    """Put `llm_utils` back the way this file found it.

    `monkeypatch.setenv` is undone at teardown, but a module RELOADED under
    those variables is not -- its module-level config keeps whatever it read.
    Alphabetically this file sorts before `test_llm_utils.py`, so without this
    the whole suite ran with the endpoint left pointing elsewhere and six of
    that file's tests failed on a `KeyError: 'enable_thinking'`. The failing
    tests had nothing to do with the change; they were reading a module this
    file had rewritten.

    This repo has been bitten by import-time env reads before, badly enough
    that test FILENAME ORDER became an invisible contract. Reload back on the
    way out rather than leaving the next file to discover it.
    """
    yield
    import importlib
    import llm_utils
    importlib.reload(llm_utils)


def _load(monkeypatch, base_url, thinking_env="false"):
    """Re-import with a chosen endpoint: the module reads its config at import."""
    monkeypatch.setenv("QWEN_BASE_URL", base_url)
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("QWEN_ENABLE_THINKING", thinking_env)
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    import llm_utils
    return importlib.reload(llm_utils)


def _capture(mod, monkeypatch):
    sent = {}

    class _Resp:
        status = 200
        data = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _post(url, body, headers):
        sent["url"] = url
        sent["body"] = json.loads(body)
        return _Resp(), None

    monkeypatch.setattr(mod, "_post_with_retry", _post)
    return sent


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------

@pytest.mark.parametrize("thinking", [True, False])
def test_enable_thinking_never_leaves_for_another_vendor(monkeypatch, thinking):
    mod = _load(monkeypatch, OPENROUTER)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, enable_thinking=thinking)
    assert "enable_thinking" not in sent["body"]
    assert sent["body"]["reasoning"] == {"enabled": thinking}


@pytest.mark.parametrize("thinking", [True, False])
def test_dashscope_still_gets_its_own_field_and_not_reasoning(monkeypatch, thinking):
    """The existing vendor's behaviour is unchanged -- including the explicit
    False, which is load-bearing: omitting it makes Qwen3 default to thinking
    and a 'non-thinking' caller silently pays for reasoning."""
    mod = _load(monkeypatch, DASHSCOPE)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, enable_thinking=thinking)
    assert sent["body"]["enable_thinking"] is thinking
    assert "reasoning" not in sent["body"]


def test_the_other_vendor_always_gets_a_token_ceiling(monkeypatch):
    """DashScope drops max_tokens whenever thinking is on, to avoid truncating
    after the reasoning chain. Porting that to a per-token vendor is an
    unbounded completion, i.e. a cost incident. Not ported."""
    mod = _load(monkeypatch, OPENROUTER)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=1234, enable_thinking=True)
    assert sent["body"]["max_tokens"] == 1234


def test_dashscope_keeps_dropping_max_tokens_while_thinking(monkeypatch):
    mod = _load(monkeypatch, DASHSCOPE)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=1234, enable_thinking=True)
    assert "max_tokens" not in sent["body"]


def test_json_mode_survives_thinking_on_the_other_vendor(monkeypatch):
    """`structured_outputs` is supported there, so the DashScope workaround of
    skipping response_format under thinking does not apply."""
    mod = _load(monkeypatch, OPENROUTER)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, force_json=True, enable_thinking=True)
    assert sent["body"]["response_format"] == {"type": "json_object"}


def test_dashscope_still_skips_json_mode_under_thinking(monkeypatch):
    mod = _load(monkeypatch, DASHSCOPE)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=100, force_json=True, enable_thinking=True)
    assert "response_format" not in sent["body"]


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", True),
    ("https://openrouter.ai/api/v1", False),
    ("https://api.openai.com/v1", False),
    ("", False),
    (None, False),
])
def test_the_endpoint_predicate(monkeypatch, url, expected):
    """Unset or empty must NOT be treated as DashScope. The module default is
    the DashScope URL, so an empty override means someone cleared it on
    purpose, and guessing 'DashScope' there would send the one field that can
    silently change behaviour elsewhere."""
    mod = _load(monkeypatch, DASHSCOPE)
    assert mod._is_dashscope(url) is expected


def test_the_url_actually_used_is_the_configured_one(monkeypatch):
    mod = _load(monkeypatch, OPENROUTER)
    sent = _capture(mod, monkeypatch)
    mod.call_llm("hi", max_tokens=10)
    assert sent["url"] == OPENROUTER + "/chat/completions"
