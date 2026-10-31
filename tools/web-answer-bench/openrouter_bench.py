"""One OpenRouter call per model profile, as the matrix measures it.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.4, §5.1.

`corroboration_client.call` cannot be reused as is: it sends `reasoning
{effort}` and the google provider order to every model, and DeepSeek needs a
pinned endpoint with reasoning off (`provider_preference` is None for non-google
models). So the payload is built here per profile, and the rest is borrowed: the
key, the HTTP pool, the URL, the web plugin and the response parser (`searched`
= at least one url citation, as `corroboration_client._parse` defines it).

Returns the keys `claude_verify.verify` returns, so one record shape covers both
vendors. Never raises; nothing here logs prompt or answer text.
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import corroboration_client as cc                      # noqa: E402


def _web_ledger():
    """tools/web-answer-bench/ledger.py under the name the report bench loads it
    by (rb_common.load_web), so one interpreter holds one copy."""
    import importlib.util
    mod = sys.modules.get("web_ledger")
    if mod is None:
        spec = importlib.util.spec_from_file_location("web_ledger",
                                                      os.path.join(HERE, "ledger.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["web_ledger"] = mod
        spec.loader.exec_module(mod)
    return mod


_L = _web_ledger()
# Sent with every request, outside build_payload: the recorded params and the
# prompt sha stay what they were; the response then carries `usage.cost`.
USAGE_INCLUDE = {"include": True}

DEEPSEEK = "deepseek/deepseek-v4.1-flash"
COREWEAVE_PIN = {"only": ["coreweave/fp8"], "allow_fallbacks": False}

# `web`: may this profile be sent the web plugin. mimo is draft only (its plugin
# behaviour is unmeasured) and, since 2026-10-31, in no cell; the grader never
# searches. `retry_429`: a 429 from a pinned endpoint is recorded, not retried
# (spec §4.4).
PROFILES = {
    "gemini": {"model": "google/gemini-3.8-flash", "reasoning": {"effort": "low"},
               "provider": "shipped", "web": True, "retry_429": True},
    "deepseek": {"model": DEEPSEEK, "reasoning": {"enabled": False},
                 "provider": COREWEAVE_PIN, "web": True, "retry_429": False},
    # muse refuses `reasoning {enabled: false}` (llm_utils.py:527-535): effort low,
    # as for gemini. No json forcing: web answers are text (spec 2026-10-31 Part A).
    "muse": {"model": "meta/muse-spark-1.3-contributor", "reasoning": {"effort": "low"},
             "provider": None, "web": True, "retry_429": True},
    # reasoning off as llm_utils.py:483-488 sends it.
    "mimo": {"model": "xiaomi/mimo-v2.6-pro", "reasoning": {"enabled": False},
             "provider": None, "web": False, "retry_429": True},
    "grader": {"model": DEEPSEEK, "reasoning": {"enabled": False},
               "provider": COREWEAVE_PIN, "web": False, "retry_429": False},
}

# web_answer.VERIFY_RETRY_MIN_LEFT: a retry only while this much chain time is left.
RETRY_MIN_LEFT_S = 10.0


def build_payload(profile, prompt, *, web, max_tokens):
    prof = PROFILES[profile]
    if web and not prof["web"]:
        raise ValueError(f"profile {profile!r} is not sent the web plugin")
    payload = {"model": prof["model"], "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}],
               "reasoning": dict(prof["reasoning"])}
    provider = prof["provider"]
    if provider == "shipped":
        # Read per call, as the shipped client does.
        provider = cc.provider_preference(prof["model"])
    if provider:
        payload["provider"] = json.loads(json.dumps(provider))
    if web:
        payload["plugins"] = cc.WEB_PLUGIN
    return payload


def _is_timeout(exc):
    try:
        import urllib3
        if isinstance(exc, urllib3.exceptions.TimeoutError):
            return True
    except Exception:                                     # noqa: BLE001
        pass
    return "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower()


def _attempt(http, body, headers, timeout, web, model=None, upper_tokens=(0, 0)):
    """One request -> (partial result, outcome). `openrouter_cost_usd` is the
    response's `usage.cost`, else the pinned upper price of its tokens; a
    timeout (billed, usage never seen) is booked at `upper_tokens`."""
    r = {"http_status": None, "error_code": None, "text": "", "searched": False,
         "result_urls": [], "result_titles": [], "stop_reason": None,
         "input_tokens": 0, "output_tokens": 0, "openrouter_cost_usd": None,
         "served_by": None, "cost_estimated": False}
    try:
        resp = http.request("POST", cc.API_URL, body=body, headers=headers, timeout=timeout)
    except Exception as exc:                              # noqa: BLE001 - classified
        r["error_code"] = type(exc).__name__
        if _is_timeout(exc):
            if model in _L.OR_PRICES_2026_10_31:
                r["openrouter_cost_usd"] = _L.or_upper(model, *upper_tokens) + (
                    _L.WEB_PLUGIN_USD_PER_CALL_2026_10_31 if web else 0.0)
                r["cost_estimated"] = True
            return r, "timeout"
        return r, "http_error"
    r["http_status"] = resp.status
    if resp.status != 200:
        return r, "http_error"
    try:
        data = json.loads(resp.data.decode("utf-8"))
    except Exception:                                     # noqa: BLE001 - html, truncated
        r["error_code"] = "bad_json"
        return r, "failed"
    if not isinstance(data, dict):
        r["error_code"] = "bad_json"
        return r, "failed"
    reply = cc._parse(data)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)) and model in _L.OR_PRICES_2026_10_31:
        # No usage.cost: charged at the pinned upper price, never $0. Without
        # token counts either, at the call's upper token estimate.
        cost = _L.or_cost(data, model, *((in_tok, out_tok) if in_tok or out_tok
                                         else upper_tokens))
        if web:
            cost += _L.WEB_PLUGIN_USD_PER_CALL_2026_10_31   # not in the token price
        r["cost_estimated"] = True
    seen = []
    for s in reply.search_results:
        if s.url not in seen:
            seen.append(s.url)
            r["result_titles"].append(s.title)
    r.update(text=(reply.text or "").strip(), searched=reply.searched, result_urls=seen,
             stop_reason=reply.stop_reason, served_by=reply.provider,
             input_tokens=in_tok, output_tokens=out_tok, openrouter_cost_usd=cost)
    r["result_count"] = len(reply.search_results)
    if not r["text"]:
        # A 200 carrying nothing (no choices, empty content) is a failure, never
        # an empty answer.
        r["error_code"] = "empty_completion"
        return r, "failed"
    if web and not reply.searched:
        return r, "no_search"
    return r, "ok"


def call(profile, prompt, *, web, timeout, max_tokens=1024, retry_left_s=None, http=None,
         clock=time.monotonic, key=None):
    """One step: a request, at most one retry. `retry_left_s` is the chain time
    left when the step starts; None = no retry (draft, single call, grader).
    `key`: the bench-only key from --openrouter-key-file; None = the env's."""
    out = {"text": "", "searched": False, "result_count": 0, "source_count": 0,
           "result_urls": [], "result_titles": [], "outcome": "failed", "latency_ms": 0,
           "calls": 0, "input_tokens": 0, "output_tokens": 0, "http_status": None,
           "error_code": None, "stop_reason": None, "retried": False,
           "openrouter_cost_usd": None, "served_by": None, "profile": profile,
           "model": PROFILES[profile]["model"], "cost_estimated": False}
    key = key or cc.api_key()
    if not key:
        out["error_code"] = "no_key"
        return out
    payload = build_payload(profile, prompt, web=web, max_tokens=max_tokens)
    payload["usage"] = dict(USAGE_INCLUDE)
    body = json.dumps(payload)
    # Upper tokens for a call whose usage never arrives: one per prompt char.
    upper_tokens = (len(prompt), max_tokens)
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    http = http or cc.http_session()
    started = clock()
    attempt_timeout = timeout
    cost = 0.0
    while True:
        if attempt_timeout is None or attempt_timeout < cc.MIN_USEFUL_TIMEOUT:
            out["outcome"] = "timeout"
            break
        out["calls"] += 1
        r, outcome = _attempt(http, body, headers, attempt_timeout, web,
                              model=PROFILES[profile]["model"], upper_tokens=upper_tokens)
        out["input_tokens"] += r.pop("input_tokens")
        out["output_tokens"] += r.pop("output_tokens")
        c = r.pop("openrouter_cost_usd")
        out["cost_estimated"] = out["cost_estimated"] or r.pop("cost_estimated")
        if isinstance(c, (int, float)):
            cost += c
            out["openrouter_cost_usd"] = round(cost, 8)
        out.update(r)
        out["outcome"] = outcome
        if outcome == "ok" or out["calls"] >= 2 or retry_left_s is None:
            break
        if out["http_status"] == 429 and not PROFILES[profile]["retry_429"]:
            break
        left = retry_left_s - (clock() - started)
        if left < RETRY_MIN_LEFT_S:
            break
        out["retried"] = True
        attempt_timeout = min(timeout, left)
    out["source_count"] = len(out["result_urls"])
    out["latency_ms"] = int(round((clock() - started) * 1000))
    return out
