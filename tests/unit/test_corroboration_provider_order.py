"""Gemini calls ask OpenRouter for AI Studio first, Vertex as the fallback.

Spec: docs/superpowers/specs/2026-10-23-gemini-provider-order-design.md

Measured 2026-10-23/24: Vertex on OpenRouter answers 429, OpenRouter retries it
~10 s before falling back, so a draft served by AI Studio after that detour took
13.5-15 s and a verify was aborted at ~11 s; AI Studio asked directly answered
in 3.4-5.1 s. The preference rides on every google/* call this client makes,
and "off" in the env is the rollback.
"""
import io
import json
import logging
import os
import re

import pytest

client = pytest.importorskip("corroboration_client")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


class FakePool:
    def __init__(self, *responses):
        self.queue = list(responses)
        self.bodies = []

    def request(self, method, url, body=None, headers=None, timeout=None):
        self.bodies.append(json.loads(body))
        return self.queue.pop(0)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CORROBORATION_API_KEY", "test-key")
    monkeypatch.delenv("CORROBORATION_PROVIDER_ORDER", raising=False)


def _ok(provider="Google AI Studio"):
    body = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    if provider is not None:
        body["provider"] = provider
    return FakeResponse(200, body)


def _send(monkeypatch, model="google/gemini-3.8-flash", response=None):
    pool = FakePool(response or _ok())
    monkeypatch.setattr(client.urllib3, "PoolManager", lambda *a, **k: pool)
    reply = client.call("q", timeout=5, model=model, caller="test")
    return pool.bodies[0], reply


def test_google_model_asks_for_ai_studio_first_with_fallbacks(monkeypatch):
    body, _ = _send(monkeypatch)
    assert body["provider"] == {"order": ["google-ai-studio", "google-vertex"],
                                "allow_fallbacks": True}


def test_default_model_is_google_and_gets_the_order(monkeypatch):
    pool = FakePool(_ok())
    monkeypatch.setattr(client.urllib3, "PoolManager", lambda *a, **k: pool)
    client.call("q", timeout=5, caller="test")
    assert client.DEFAULT_MODEL.startswith("google/")
    assert pool.bodies[0]["provider"]["order"][0] == "google-ai-studio"


def test_non_google_model_gets_no_preference(monkeypatch):
    body, _ = _send(monkeypatch, model="deepseek/deepseek-v4.1-flash")
    assert "provider" not in body


@pytest.mark.parametrize("value", ["off", "", " , "])
def test_off_or_empty_sends_no_preference(monkeypatch, value):
    monkeypatch.setenv("CORROBORATION_PROVIDER_ORDER", value)
    body, _ = _send(monkeypatch)
    assert "provider" not in body


def test_custom_order_is_trimmed_and_read_per_call(monkeypatch):
    body, _ = _send(monkeypatch)
    assert body["provider"]["order"][0] == "google-ai-studio"
    monkeypatch.setenv("CORROBORATION_PROVIDER_ORDER", " google-vertex , ,google-ai-studio ")
    body, _ = _send(monkeypatch)
    assert body["provider"]["order"] == ["google-vertex", "google-ai-studio"]


def test_reply_says_who_served_it(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _, reply = _send(monkeypatch)
    assert reply.provider == "Google AI Studio"
    line = [r.getMessage() for r in caplog.records if "served_by=" in r.getMessage()]
    assert line == [line[0]] and "served_by=Google AI Studio caller=test" in line[0]
    assert "hi" not in line[0].split("caller=")[1]          # counts only, no text


@pytest.mark.parametrize("raw", [None, 42, {"name": "x"}])
def test_missing_or_odd_provider_is_none(monkeypatch, raw):
    resp = _ok(provider=None)
    if raw is not None:
        payload = json.loads(resp.data)
        payload["provider"] = raw
        resp = FakeResponse(200, payload)
    _, reply = _send(monkeypatch, response=resp)
    assert reply.provider is None


# ---- wiring: a switch you cannot set is not a switch

def _read(*parts):
    return io.open(os.path.join(REPO, *parts), encoding="utf-8").read()


def test_template_parameter_and_both_functions():
    text = _read("src", "template.yaml")
    block = text.split("\n  CorroborationProviderOrder:\n", 1)[1].split("\n\n", 1)[0]
    assert "Default: 'google-ai-studio,google-vertex'" in block
    assert re.search(r"AllowedPattern: '\^\(off\|", block)
    assert text.count("CORROBORATION_PROVIDER_ORDER: !Ref CorroborationProviderOrder") == \
        text.count("CORROBORATION_API_KEY: !Ref CorroborationApiKey") == 2


def test_template_default_matches_the_code():
    text = _read("src", "template.yaml")
    assert f"Default: '{client.DEFAULT_PROVIDER_ORDER}'" in text


def test_allowed_pattern_accepts_the_default_and_off():
    pat = re.compile(r"^(off|[a-z0-9-]+(,[a-z0-9-]+)*)$")
    assert pat.match(client.DEFAULT_PROVIDER_ORDER) and pat.match("off")
    assert not pat.match("") and not pat.match("google-vertex,")


@pytest.mark.parametrize("wf,prefix", [("deploy.yml", "TEST_"), ("deploy-prod.yml", "PROD_")])
def test_workflow_passes_it_only_when_set(wf, prefix):
    text = _read(".github", "workflows", wf)
    var = prefix + "CORROBORATION_PROVIDER_ORDER"
    assert re.search(r'if \[ -n "\$\{\{ vars\.%s \}\}" \]; then\s+PROVIDER_ORDER_PARAM='
                     r'"CorroborationProviderOrder=\$\{\{ vars\.%s \}\}"' % (var, var), text)
    assert re.search(r"^\s+\$PROVIDER_ORDER_PARAM \\$", text, re.MULTILINE)
