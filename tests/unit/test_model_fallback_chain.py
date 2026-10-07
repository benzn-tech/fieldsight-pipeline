"""The shared client falls back to the next model when the deploy's model is down.

Spec: docs/superpowers/specs/2026-10-07-model-fallback-and-recovery-design.md
(D1-D3). The HTTP layer is stubbed at urllib3.PoolManager.request, as in
test_llm_utils.py; no real call is made.
"""
import json
import logging

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

MURE = "meta/muse-spark-1.3-contributor"
LUNA = "openai/gpt-6-luna-pro"
MIMO = "xiaomi/mimo-v2.6-pro"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text="{}", model="x"):
    return _R(200, {"model": model, "choices": [{"message": {"content": text},
                                                  "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


def _err(status, msg="boom"):
    return _R(status, {"error": {"message": msg}})


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu, "QWEN_MODEL", MURE)
    monkeypatch.setattr(lu, "QWEN_MODEL_NONTHINKING", "")
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")
    monkeypatch.setattr(lu, "REASONING_HEADROOM_TOKENS", 8000)
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def test_503_on_primary_luna_answers(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _ok("from-luna")])
    text, err = lu.call_llm("p", max_tokens=1000, force_json=True)
    assert (text, err) == ("from-luna", None)
    assert [b["model"] for b in sent] == [MURE, LUNA]
    # luna keeps today's payload exactly
    assert sent[1]["reasoning"] == {"effort": "high"}
    assert sent[1]["max_tokens"] == 9000
    assert sent[1]["response_format"] == {"type": "json_object"}


def test_luna_429_mimo_answers_with_reasoning_off_and_no_headroom(monkeypatch):
    sent = _stub(monkeypatch, [_err(502), _err(429), _ok("from-mimo")])
    text, err = lu.call_llm("p", max_tokens=1000, force_json=True)
    assert (text, err) == ("from-mimo", None)
    assert [b["model"] for b in sent] == [MURE, LUNA, MIMO]
    assert sent[2]["reasoning"] == {"enabled": False}
    assert sent[2]["max_tokens"] == 1000
    assert sent[2]["response_format"] == {"type": "json_object"}


def test_mimo_without_force_json_sends_no_response_format(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _err(503), _ok("t")])
    lu.call_llm("p", max_tokens=500)
    assert "response_format" not in sent[2]


def test_all_fail_surfaces_the_original_error(monkeypatch):
    # the last model keeps its retry ladder (4 attempts); queue enough 503s
    sent = _stub(monkeypatch, [_err(503, "primary down"), _err(429, "luna busy")]
                 + [_err(500, "mimo down")] * 4)
    text, err = lu.call_llm("p", max_tokens=100)
    assert text is None and err == "primary down"
    assert [b["model"] for b in sent][:3] == [MURE, LUNA, MIMO]


def test_400_does_not_fall_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(400, "bad request")])
    text, err = lu.call_llm("p", max_tokens=100)
    assert (text, err) == (None, "bad request")
    assert len(sent) == 1


def test_plain_404_does_not_fall_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(404, "no such model")])
    assert lu.call_llm("p")[0] is None
    assert len(sent) == 1


def test_empty_content_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_ok(""), _ok("filled")])
    assert lu.call_llm("p") == ("filled", None)
    assert len(sent) == 2


def test_finish_length_empty_falls_back(monkeypatch):
    empty = _R(200, {"choices": [{"message": {"content": None},
                                  "finish_reason": "length"}], "usage": {}})
    sent = _stub(monkeypatch, [empty, _ok("filled")])
    assert lu.call_llm("p") == ("filled", None)
    assert len(sent) == 2


def test_timeout_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [Exception("Read timed out"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2


def test_provider_error_404_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(404, "Provider returned error"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2


def test_deadline_exhausted_means_no_next_model(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)])
    t = {"now": 100.0}
    monkeypatch.setattr(lu.time, "monotonic", lambda: t["now"])
    orig = lu.urllib3.PoolManager.request

    def slow(self, *a, **k):
        t["now"] += 9.5     # the primary burns the budget
        return orig(self, *a, **k)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", slow)
    text, err = lu.call_llm("p", deadline=10)
    assert text is None
    assert len(sent) == 1


def test_deadline_with_time_left_still_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _ok("ok")])
    assert lu.call_llm("p", deadline=20) == ("ok", None)
    assert len(sent) == 2


def test_empty_env_is_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "")
    sent = _stub(monkeypatch, [_err(503)] * 4)
    text, err = lu.call_llm("p")
    assert text is None and err == "boom"
    assert len(sent) == 4 and {b["model"] for b in sent} == {MURE}


def test_primary_already_luna_is_not_called_twice(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_MODEL", LUNA)
    sent = _stub(monkeypatch, [_err(503), _ok("from-mimo")])
    assert lu.call_llm("p") == ("from-mimo", None)
    assert [b["model"] for b in sent] == [LUNA, MIMO]


def test_per_call_model_override_is_the_primary(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _ok("ok")])
    lu.call_llm("p", model="google/gemini-3.8-flash")
    assert [b["model"] for b in sent] == ["google/gemini-3.8-flash", LUNA]


def test_one_log_line_per_switch_and_no_prompt(monkeypatch, caplog):
    _stub(monkeypatch, [_err(503, "down"), _err(429, "busy"), _ok("ok")])
    with caplog.at_level(logging.INFO):
        lu.call_llm("SECRET PROMPT TEXT", caller="extraction")
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("LLM_FALLBACK ")]
    assert len(lines) == 2
    first = json.loads(lines[0].split(" ", 1)[1])
    assert first == {"caller": "extraction", "from": MURE, "to": LUNA,
                     "reason": "down"}
    assert json.loads(lines[1].split(" ", 1)[1])["to"] == MIMO
    assert not any("SECRET PROMPT" in r.getMessage() for r in caplog.records)


def test_dashscope_never_falls_back(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_BASE_URL",
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    sent = _stub(monkeypatch, [_err(400, "x")])
    lu.call_llm("p")
    assert len(sent) == 1


def test_provider_returned_error_on_any_status_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(400, "Provider returned error"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2
