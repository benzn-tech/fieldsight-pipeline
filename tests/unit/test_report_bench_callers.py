"""Report bench, Task 6: callers with the prod T retry mirrored (fakes only)."""
import importlib.util
import json
import logging
import os
import sys
from types import SimpleNamespace as NS

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


callers = _load("rb_callers")
SECRET_P = "PSENTINEL-prompt-text"
SECRET_A = "ASENTINEL-answer-text"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


class FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.bodies.append(json.loads(body))
        self.url, self.timeout = url, timeout
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return NS(status=r.get("status", 200), data=json.dumps(r["data"]).encode("utf-8"))


def _or(text, finish="stop", provider="CoreWeave"):
    return {"data": {"choices": [{"message": {"content": text}, "finish_reason": finish}],
                     "provider": provider,
                     "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                               "completion_tokens_details": {"reasoning_tokens": 30}}}}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "k-test")


def test_openrouter_t_length_retries_once():
    http = FakeHttp([_or("", finish="length"), _or(SECRET_A)])
    r = callers.call("muse", "T", SECRET_P, 8000, http=http, clock=Clock())
    assert len(r["attempts"]) == 2
    assert http.bodies[1]["reasoning"] == {"effort": "low"}
    assert http.bodies[1]["max_tokens"] == 12000 + 8000
    assert r["outcome"] == "ok" and r["text"] == SECRET_A
    assert r["attempts"][0]["outcome"] == "truncated" and r["attempts"][1]["retry"] is True
    assert http.url == "https://openrouter.ai/api/v1/chat/completions"
    assert http.timeout == 540.0


def test_openrouter_x_length_no_retry():
    http = FakeHttp([_or("{", finish="length")])
    r = callers.call("muse", "X", SECRET_P, 9000, http=http, clock=Clock())
    assert len(r["attempts"]) == 1 and r["outcome"] == "truncated"


def test_openrouter_empty_200():
    http = FakeHttp([_or("")])
    r = callers.call("muse", "T", SECRET_P, 8000, http=http, clock=Clock())
    assert r["outcome"] == "empty" and len(r["attempts"]) == 1


def test_openrouter_records_provider_and_reasoning():
    http = FakeHttp([_or(SECRET_A, provider="Google AI Studio")])
    r = callers.call("gemini", "X", SECRET_P, 9000, http=http, clock=Clock())
    a = r["attempts"][0]
    assert a["provider"] == "Google AI Studio" and a["reasoning_tokens"] == 30
    assert a["input_tokens"] == 100 and r["usd_upper"] is None


def test_openrouter_timeout():
    http = FakeHttp([TimeoutError("timed out")])
    r = callers.call("deepseek", "X", SECRET_P, 9000, http=http, clock=Clock())
    assert r["outcome"] == "timeout"


def test_openrouter_no_key(monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    r = callers.call("muse", "X", SECRET_P, 9000, http=FakeHttp([]), clock=Clock())
    assert r["attempts"][0]["error_code"] == "no_key"


class FakeStream:
    def __init__(self, reply):
        self.reply = reply

    def __enter__(self):
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.reply


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = self

    def stream(self, **kw):
        self.requests.append(kw)
        return FakeStream(self.replies.pop(0))


def _msg(text, stop="end_turn"):
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=text)],
              stop_reason=stop, usage=NS(input_tokens=1000, output_tokens=200))


class APIStatusError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code, self.message = status, message


def test_claude_t_max_tokens_retries():
    c = FakeClient([_msg("", stop="max_tokens"), _msg(SECRET_A)])
    r = callers.call("sonnet", "T", SECRET_P, 8000, client=c, clock=Clock())
    assert len(r["attempts"]) == 2
    assert c.requests[1]["max_tokens"] == 12000 + 8000
    assert c.requests[1]["output_config"]["effort"] == "low"
    assert r["outcome"] == "ok"
    assert r["usd"] == pytest.approx(2 * (1000 * 2.0 + 200 * 10.0) / 1e6)
    assert c.requests[0]["timeout"] == 540.0


def test_claude_refusal_and_text_join():
    c = FakeClient([_msg("", stop="refusal")])
    assert callers.call("haiku", "X", '"topics"', 9000, client=c,
                        clock=Clock())["outcome"] == "refusal"
    c = FakeClient([_msg(SECRET_A)])
    assert callers.call("haiku", "X", '"topics"', 9000, client=c,
                        clock=Clock())["text"] == SECRET_A


def test_claude_schema_rejected():
    c = FakeClient([APIStatusError(400, "output_config.format: unsupported schema keyword")])
    r = callers.call("sonnet", "X", '"topics"', 9000, client=c, clock=Clock())
    assert r["outcome"] == "schema_rejected" and r["attempts"][0]["http_status"] == 400


def test_claude_other_400_is_http_error():
    c = FakeClient([APIStatusError(400, "max_tokens too large")])
    r = callers.call("sonnet", "X", '"topics"', 9000, client=c, clock=Clock())
    assert r["outcome"] == "http_error"


def test_no_text_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    callers.call("muse", "T", SECRET_P, 8000, http=FakeHttp([_or(SECRET_A)]), clock=Clock())
    callers.call("sonnet", "T", SECRET_P, 8000, client=FakeClient([_msg(SECRET_A)]),
                 clock=Clock())
    for rec in caplog.records:
        assert SECRET_P not in rec.getMessage() and SECRET_A not in rec.getMessage()
