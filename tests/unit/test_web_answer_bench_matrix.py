"""Web-answer bench, prompt x model matrix (plan 2026-10-28-web-answer-prompt-matrix).

Fakes only: no SDK, no network. The modules are loaded by path because the
directory name has a hyphen.
"""
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "web-answer-bench")
SRC = os.path.join(REPO, "src")
for _p in (BENCH_DIR, SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


import web_answer                                        # noqa: E402

prompts = _load("prompts")


# ------------------------------------------------------- Task 1: prompt texts

# git show 518900a8:src/web_answer.py, VERIFY_PROMPT ("colleague").
VERIFY_V1 = """A colleague answered the question below from memory. Search the open web
to check it. Keep what the sources confirm, correct what they contradict (citing
the source), and mark anything still unconfirmed as [unverified]. Be brief.

## Question
{question}

## Colleague's answer
{draft}
"""

# git show 518900a8^:src/web_answer.py, WEB_PROMPT.
WEB_V1 = """Search the open web and answer this question, for a reader working
on a New Zealand construction site.

Report only what public sources say. Do not speculate, and do not fill a gap with
general knowledge -- if the sources do not answer the question, say so plainly
rather than guessing.

Name the source of each fact you use. Be brief.

## Question
{question}
"""


def test_shipped_prompts_match_the_constants():
    assert prompts.load("draft_v1") == web_answer.DRAFT_PROMPT
    assert prompts.load("verify_v2") == web_answer.VERIFY_PROMPT


def test_historical_prompts_match_git():
    assert prompts.load("verify_v1") == VERIFY_V1
    assert prompts.load("web_v1") == WEB_V1


def test_candidates_and_placeholders():
    assert prompts.CANDIDATES == {"draft_v1": "draft", "verify_v1": "verify",
                                  "verify_v2": "verify", "web_v1": "single",
                                  "merged_v1": "single"}
    for pid, step in prompts.CANDIDATES.items():
        text = prompts.load(pid)
        assert "{question}" in text
        assert ("{draft}" in text) == (step == "verify"), pid
        # Each one fills with the keys its step supplies and nothing else.
        text.format(question="Q", draft="D")


def test_sha_is_of_the_file_text():
    import hashlib
    assert prompts.sha("draft_v1") == \
        hashlib.sha256(web_answer.DRAFT_PROMPT.encode("utf-8")).hexdigest()


def test_unknown_prompt_id_refused():
    with pytest.raises(KeyError):
        prompts.load("../set.v1")


# ------------------------------------------------------- Task 2: ledger pools

ledger = _load("ledger")


def test_old_format_ledger_counts_as_stage0(tmp_path):
    p = tmp_path / "anthropic_ledger.json"
    p.write_text('{"spent_usd": 1.42, "cap_usd": 45.0}', encoding="utf-8")
    lg = ledger.Ledger(str(p))
    assert lg.spent == pytest.approx(1.42)
    assert lg.pool_spent("stage0") == pytest.approx(1.42)
    assert lg.pool_spent("matrix") == 0.0
    assert lg.room() == pytest.approx(43.58)


def test_matrix_pool_exhaustion_and_persistence(tmp_path):
    p = str(tmp_path / "anthropic_ledger.json")
    lg = ledger.Ledger(p)
    lg.add(1.42)                                  # bare add() is Stage 0's
    lg.add(17.9, pool="matrix")
    assert not lg.pool_exhausted("matrix", ledger.MATRIX_POOL_CAP)
    lg.add(0.2, pool="matrix")
    assert lg.pool_exhausted("matrix", 18)
    again = ledger.Ledger(p)
    assert again.pool_spent("matrix") == pytest.approx(18.1)
    assert again.pool_spent("stage0") == pytest.approx(1.42)
    assert again.spent == pytest.approx(19.52)
    assert again.room() == pytest.approx(45 - 19.52)


def test_total_cap_still_45_across_pools(tmp_path):
    lg = ledger.Ledger(str(tmp_path / "l.json"))
    lg.add(30.0, pool="matrix")
    lg.add(14.99, pool="scored")
    assert not lg.exhausted()
    lg.add(0.01)
    assert lg.exhausted()
    assert (ledger.CAP_USD, ledger.MATRIX_POOL_CAP, ledger.SCORED_RESERVE) == (45.0, 18.0, 23.0)


# ------------------------------------------------ Task 3: OpenRouter bench caller

import json                                              # noqa: E402
from types import SimpleNamespace as NS                  # noqa: E402

import corroboration_client                              # noqa: E402

orb = _load("openrouter_bench")

SECRET_Q = "QSENTINEL-what-is-the-levy"
SECRET_A = "ASENTINEL-the-answer-text"
OR_KEY = "sk-or-SENTINEL-key"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def completion(text=SECRET_A, urls=("https://ex0.govt.nz/p",), choices=True, usage=None):
    data = {"model": "m", "provider": "Google AI Studio",
            "usage": usage or {"prompt_tokens": 300, "completion_tokens": 40, "cost": 0.0012}}
    if choices:
        data["choices"] = [{"finish_reason": "stop", "message": {
            "content": text,
            "annotations": [{"type": "url_citation",
                             "url_citation": {"url": u, "title": "ex.govt.nz"}} for u in urls]}}]
    return NS(status=200, data=json.dumps(data).encode("utf-8"))


class Timeout(Exception):
    pass


class FakeHttp:
    """Outcomes consumed in order: a response, an exception, or (seconds, outcome)."""

    def __init__(self, clock, script=(), default=None):
        self.clock, self.script, self.default = clock, list(script), default
        self.requests = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.requests.append({"method": method, "url": url, "payload": json.loads(body),
                              "headers": headers, "timeout": timeout})
        item = self.script.pop(0) if self.script else self.default()
        if isinstance(item, tuple):
            secs, item = item
            self.clock.t += secs
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def or_key(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", OR_KEY)


def test_payload_deepseek_pinned_reasoning_off():
    p = orb.build_payload("deepseek", "P", web=True, max_tokens=2048)
    assert p["model"] == "deepseek/deepseek-v4.1-flash"
    assert p["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    assert p["reasoning"] == {"enabled": False}
    assert p["plugins"] == corroboration_client.WEB_PLUGIN
    assert p["max_tokens"] == 2048
    assert p["messages"] == [{"role": "user", "content": "P"}]


def test_payload_mimo_and_grader():
    p = orb.build_payload("mimo", "P", web=False, max_tokens=1024)
    assert p["model"] == "xiaomi/mimo-v2.6-pro"
    assert p["reasoning"] == {"enabled": False}
    assert "provider" not in p and "plugins" not in p
    g = orb.build_payload("grader", "P", web=False, max_tokens=256)
    assert g["model"] == "deepseek/deepseek-v4.1-flash"
    assert g["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    with pytest.raises(ValueError):                 # draft only: web plugin unmeasured
        orb.build_payload("mimo", "P", web=True, max_tokens=1024)


def test_payload_gemini_uses_the_shipped_provider_order():
    p = orb.build_payload("gemini", "P", web=False, max_tokens=1024)
    assert p["model"] == "google/gemini-3.8-flash"
    assert p["reasoning"] == {"effort": "low"}
    assert p["provider"] == corroboration_client.provider_preference("google/gemini-3.8-flash")
    assert "plugins" not in p
    assert orb.build_payload("gemini", "P", web=True, max_tokens=1)["plugins"] == \
        corroboration_client.WEB_PLUGIN


def test_ok_with_citations(or_key):
    clock = Clock()
    http = FakeHttp(clock, [(4.0, completion())])
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "ok" and r["searched"] is True
    assert (r["result_count"], r["source_count"], r["calls"]) == (1, 1, 1)
    assert (r["input_tokens"], r["output_tokens"]) == (300, 40)
    assert r["latency_ms"] == 4000 and r["http_status"] == 200
    assert r["text"] == SECRET_A
    assert r["openrouter_cost_usd"] == pytest.approx(0.0012)
    assert http.requests[0]["url"] == corroboration_client.API_URL
    assert http.requests[0]["timeout"] == 14.0
    assert OR_KEY not in json.dumps(r)


def test_draft_without_web_is_ok_unsearched(or_key):
    clock = Clock()
    r = orb.call("mimo", "P", web=False, timeout=12.0,
                 http=FakeHttp(clock, [completion(urls=())]), clock=clock)
    assert r["outcome"] == "ok" and r["searched"] is False


def test_web_text_without_citations_is_no_search(or_key):
    clock = Clock()
    r = orb.call("deepseek", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [completion(urls=())]), clock=clock)
    assert r["outcome"] == "no_search"


def test_200_without_choices_is_failed_no_retry_storm(or_key):
    clock = Clock()
    http = FakeHttp(clock, default=lambda: (1.0, completion(choices=False)))
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "failed" and len(http.requests) == 1
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=20.0, http=http,
                 clock=clock)
    assert r["outcome"] == "failed" and r["calls"] == 2 and len(http.requests) == 3


def test_empty_content_and_bad_json_are_failed(or_key):
    clock = Clock()
    bad = NS(status=200, data=b"<html>")
    for item in (completion(text=""), bad):
        r = orb.call("gemini", "P", web=False, timeout=12.0,
                     http=FakeHttp(clock, [item]), clock=clock)
        assert r["outcome"] == "failed"


def test_timeout_exception_is_timeout(or_key):
    clock = Clock()
    r = orb.call("gemini", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [(14.0, Timeout("read timed out"))]), clock=clock)
    assert r["outcome"] == "timeout"


def test_http_error_status(or_key):
    clock = Clock()
    r = orb.call("gemini", "P", web=True, timeout=14.0,
                 http=FakeHttp(clock, [NS(status=502, data=b"{}")]), clock=clock)
    assert r["outcome"] == "http_error" and r["http_status"] == 502


def test_one_retry_only_with_ten_seconds_left(or_key):
    clock = Clock()
    fail = lambda: (5.0, completion(urls=()))                         # noqa: E731
    http = FakeHttp(clock, default=fail)
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=14.0, http=http,
                 clock=clock)
    assert r["calls"] == 1                         # 9 s left after attempt 1
    http = FakeHttp(clock, [fail(), (3.0, completion())])
    r = orb.call("gemini", "P", web=True, timeout=14.0, retry_left_s=16.0, http=http,
                 clock=clock)
    assert r["calls"] == 2 and r["outcome"] == "ok" and r["retried"] is True
    assert http.requests[1]["timeout"] == pytest.approx(11.0)
    assert r["latency_ms"] == 8000


def test_pinned_429_is_recorded_not_retried(or_key):
    clock = Clock()
    http = FakeHttp(clock, default=lambda: NS(status=429, data=b"{}"))
    r = orb.call("deepseek", "P", web=True, timeout=14.0, retry_left_s=22.0, http=http,
                 clock=clock)
    assert r["outcome"] == "http_error" and r["http_status"] == 429
    assert len(http.requests) == 1


def test_missing_key_fails_without_a_request(monkeypatch):
    monkeypatch.delenv("CORROBORATION_API_KEY", raising=False)
    clock = Clock()
    http = FakeHttp(clock, default=completion)
    r = orb.call("gemini", "P", web=True, timeout=14.0, http=http, clock=clock)
    assert r["outcome"] == "failed" and r["error_code"] == "no_key"
    assert http.requests == []
