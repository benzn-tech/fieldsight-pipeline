"""Python port of voice_llm/restate.mjs (spec 2026-10-15 section 7).

The guard cases live in tests/fixtures/restate_guard_cases.json and are loaded
by BOTH this file and voice_llm/restate.test.mjs, so the two implementations
cannot drift apart without a test going red. No test here makes a network call.
"""
import io
import json
import pathlib
import re
import socket
import urllib.error

import pytest

import voice_restate as vr

ROOT = pathlib.Path(__file__).resolve().parents[2]
CASES = json.loads((ROOT / "tests/fixtures/restate_guard_cases.json").read_text(encoding="utf-8"))

Q = "When does the Unit 11 backfill have to be finished"
GOOD = "you're asking when the Unit 11 backfill has to be finished"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_guard_middle_matches_the_shared_fixture(case):
    got = vr.guard_middle(case["question"], case["middle"])
    assert got["ok"] is case["ok"]
    if case["ok"]:
        assert got["middle"] == case.get("expected_middle", got["middle"])
    else:
        assert got["reason"] == case["reason"]


def test_fixture_covers_every_reason_and_both_outcomes():
    assert len(CASES) >= 14
    assert {c.get("reason") for c in CASES} >= {"guard_prefix", "guard_words", "guard_number", "guard_name", None}


def test_guard_rejects_non_strings():
    assert vr.guard_middle(Q, None)["reason"] == "guard_prefix"
    assert vr.guard_middle(Q, 42)["reason"] == "guard_prefix"


def test_assemble_for_both_intents():
    assert vr.assemble(GOOD, False) == "Got it, %s. Checking your site records now." % GOOD
    assert vr.assemble(GOOD, True) == "Got it, %s. Searching online now." % GOOD


def test_correction_and_rejected_model_match_the_node_rules():
    for s in ["No, I meant level three", "no. the other one", "I meant the pour", "Nope, Friday", "i mean the other site"]:
        assert vr.is_correction(s) is True, s
    for s in ["Normal question about the pour", "what is the status", "Nobody was on site", "north wall progress"]:
        assert vr.is_correction(s) is False, s
    for m in ["google/gemini-3-flash", "x/y-thinking", "deepseek/deepseek-r1"]:
        assert vr.model_rejected(m) is True, m
    assert vr.model_rejected("meta-llama/llama-3.1-8b-instruct") is False


def test_restate_system_equals_the_node_string():
    src = (ROOT / "voice_llm/restate.mjs").read_text(encoding="utf-8")
    block = re.search(r"RESTATE_SYSTEM\s*=\s*\[(.*?)\]\s*\.join", src, re.S).group(1)
    parts = [json.loads(p) for p in re.findall(r'"(?:[^"\\]|\\.)*"', block)]
    assert len(parts) == 5
    assert vr.RESTATE_SYSTEM == " ".join(parts)


# ----------------------------------------------------------------- restate()

class FakeResp:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def completion(text):
    return {"choices": [{"message": {"content": text}}]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ASK_VOICE_RESTATE", "true")
    monkeypatch.setenv("QWEN_API_KEY", "k-test")
    monkeypatch.setenv("QWEN_BASE_URL", "http://llm.test/v1/")
    monkeypatch.delenv("VOICE_RESTATE_MODEL", raising=False)


def patch_urlopen(monkeypatch, result):
    sent = {}

    def fake(req, timeout=None):
        sent["req"], sent["timeout"] = req, timeout
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(vr.urllib.request, "urlopen", fake)
    return sent


def test_success_assembles_and_sends_exactly_the_contract(env, monkeypatch):
    sent = patch_urlopen(monkeypatch, FakeResp(completion('  "%s."  ' % GOOD)))
    r = vr.restate(Q, web_intent=False)
    assert r["text"] == "Got it, %s. Checking your site records now." % GOOD
    assert r["reason"] is None and isinstance(r["ms"], int)
    req = sent["req"]
    assert req.full_url == "http://llm.test/v1/chat/completions"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer k-test"
    assert sent["timeout"] == 1.2
    body = json.loads(req.data)
    assert set(body) == {"model", "messages", "max_tokens", "temperature"}
    assert body["model"] == "meta-llama/llama-3.1-8b-instruct"
    assert body["max_tokens"] == 40 and body["temperature"] == 0
    assert body["messages"] == [{"role": "system", "content": vr.RESTATE_SYSTEM},
                                {"role": "user", "content": Q}]


def test_web_intent_changes_only_the_tail(env, monkeypatch):
    patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    assert vr.restate(Q, web_intent=True)["text"].endswith("Searching online now.")


def test_model_is_read_at_call_time(env, monkeypatch):
    sent = patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    monkeypatch.setenv("VOICE_RESTATE_MODEL", "vendor/other-model")
    vr.restate(Q, web_intent=False)
    assert json.loads(sent["req"].data)["model"] == "vendor/other-model"


def test_timeout_returns_a_reason_not_an_exception(env, monkeypatch):
    patch_urlopen(monkeypatch, socket.timeout("slow"))
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "timeout"


def test_urlerror_wrapping_a_timeout_is_a_timeout(env, monkeypatch):
    patch_urlopen(monkeypatch, urllib.error.URLError(socket.timeout("slow")))
    assert vr.restate(Q, web_intent=False)["reason"] == "timeout"


def test_non_200_is_http(env, monkeypatch):
    patch_urlopen(monkeypatch, FakeResp({}, status=500))
    assert vr.restate(Q, web_intent=False)["reason"] == "http"
    patch_urlopen(monkeypatch, urllib.error.HTTPError("http://x", 429, "busy", {}, io.BytesIO(b"")))
    assert vr.restate(Q, web_intent=False)["reason"] == "http"


def test_garbage_body_is_an_error_not_a_raise(env, monkeypatch):
    patch_urlopen(monkeypatch, FakeResp(b"<html>"))
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "error"
    patch_urlopen(monkeypatch, FakeResp({"choices": []}))
    assert vr.restate(Q, web_intent=False)["reason"] == "error"


def test_guard_miss_reports_the_guard_reason(env, monkeypatch):
    patch_urlopen(monkeypatch, FakeResp(completion("you're asking about Hanlon")))
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "guard_name"


def test_flag_off_makes_no_call(env, monkeypatch):
    sent = patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    monkeypatch.setenv("ASK_VOICE_RESTATE", "false")
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "off" and "req" not in sent


def test_correction_makes_no_call(env, monkeypatch):
    sent = patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    r = vr.restate("No, I meant level three", web_intent=False)
    assert r["text"] is None and r["reason"] == "correction" and "req" not in sent


def test_rejected_model_makes_no_call(env, monkeypatch):
    sent = patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    monkeypatch.setenv("VOICE_RESTATE_MODEL", "deepseek/deepseek-r1")
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "model_rejected" and "req" not in sent


@pytest.mark.parametrize("missing", ["QWEN_API_KEY", "QWEN_BASE_URL"])
def test_missing_key_or_base_url_is_no_key(env, monkeypatch, missing):
    sent = patch_urlopen(monkeypatch, FakeResp(completion(GOOD)))
    monkeypatch.delenv(missing)
    r = vr.restate(Q, web_intent=False)
    assert r["text"] is None and r["reason"] == "no_key" and "req" not in sent
