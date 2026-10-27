"""One logical bench run: a call and, for T only, prod's one retry (spec §2.3, §3).

OpenRouter: POST `<QWEN_BASE_URL>/chat/completions` with `QWEN_API_KEY` (the
same env var and URL shape `llm_utils._call_qwen` uses), urllib3 pool from
`corroboration_client.http_session`, timeout 540 s, no retry ladder, no
fallback chain (the bench measures one model).

Claude: `client.messages.stream(**request)` -> `get_final_message()`; the
client is the official SDK built by the runner with `max_retries=0`, or a test
fake, so this module never imports the SDK. Exceptions are classified by shape
(web bench `claude_verify._classify` / `_get`).

T retry (lambda_session_report.py:1010-1024): on `finish_reason == "length"` /
`stop_reason == "max_tokens"` one more call at effort low with 12 000 answer
tokens; both attempts are recorded, the run's outcome is the retry's.

Never raises; never logs prompt or answer text.
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_ledger  # noqa: E402
import rb_requests  # noqa: E402

import corroboration_client as cc  # noqa: E402

_CV = rb_common.load_web("claude_verify")
_ORB = rb_common.load_web("openrouter_bench")
_get = _CV._get

OUTCOMES = ("ok", "empty", "truncated", "refusal", "http_error", "timeout",
            "schema_rejected")


def openrouter_url():
    return rb_common.IMPORT_ENV["QWEN_BASE_URL"].rstrip("/") + "/chat/completions"


def openrouter_key():
    return os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))


def est_input_tokens(prompt: str) -> int:
    """Upper estimate for $ before a call (CJK one token per character)."""
    return rb_common.est_tokens(prompt)


def usd_upper(key, prompt, max_tokens):
    if rb_requests.MODELS[key]["vendor"] != "anthropic":
        return None
    return rb_ledger.call_cost(rb_requests.MODELS[key]["model"], est_input_tokens(prompt),
                               max_tokens)


def _blank(key, prompt, req):
    return {"finish": None, "outcome": "http_error", "latency_ms": 0, "input_tokens": 0,
            "output_tokens": 0, "reasoning_tokens": None, "provider": None,
            "http_status": None, "error_code": None,
            "usd_upper": usd_upper(key, prompt, req["max_tokens"]), "usd": None,
            "openrouter_cost_usd": None, "effort": None, "max_tokens": req["max_tokens"],
            "format_sent": bool((req.get("output_config") or {}).get("format"))}


def _openrouter_attempt(key, req, prompt, http, clock):
    a = _blank(key, prompt, req)
    a["effort"] = (req.get("reasoning") or {}).get("effort")
    k = openrouter_key()
    if not k:
        a["error_code"] = "no_key"
        return a, ""
    started = clock()
    try:
        resp = http.request("POST", openrouter_url(), body=json.dumps(req),
                            headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + k},
                            timeout=rb_requests.TIMEOUT_S)
    except Exception as exc:                              # noqa: BLE001 - classified
        a["latency_ms"] = int(round((clock() - started) * 1000))
        a["error_code"] = type(exc).__name__
        a["outcome"] = "timeout" if _ORB._is_timeout(exc) else "http_error"
        return a, ""
    a["latency_ms"] = int(round((clock() - started) * 1000))
    a["http_status"] = resp.status
    if resp.status != 200:
        return a, ""
    try:
        data = json.loads(resp.data.decode("utf-8"))
        assert isinstance(data, dict)
    except Exception:                                     # noqa: BLE001
        a["error_code"] = "bad_json"
        return a, ""
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = choice.get("message") or {}
    text = (msg.get("content") or "") if isinstance(msg, dict) else ""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") or {}
    a.update(finish=choice.get("finish_reason"), provider=data.get("provider"),
             input_tokens=int(usage.get("prompt_tokens") or 0),
             output_tokens=int(usage.get("completion_tokens") or 0),
             reasoning_tokens=details.get("reasoning_tokens") if isinstance(details, dict)
             else None,
             openrouter_cost_usd=usage.get("cost"))
    if a["finish"] == "length":
        a["outcome"] = "truncated"
    elif not text.strip():
        a["outcome"] = "empty"
    else:
        a["outcome"] = "ok"
    return a, text


def _schema_message(exc):
    msg = str(getattr(exc, "message", "") or exc).lower()
    return "format" in msg or "schema" in msg


def _claude_attempt(key, req, prompt, client, clock):
    a = _blank(key, prompt, req)
    a["effort"] = req["output_config"]["effort"]
    a["provider"] = "anthropic"
    started = clock()
    try:
        with client.messages.stream(timeout=rb_requests.TIMEOUT_S, **req) as stream:
            msg = stream.get_final_message()
    except Exception as exc:                              # noqa: BLE001 - classified
        a["latency_ms"] = int(round((clock() - started) * 1000))
        outcome, status = _CV._classify(exc)
        a["http_status"], a["error_code"] = status, type(exc).__name__
        if outcome == "rejected" and a["format_sent"] and _schema_message(exc):
            a["outcome"] = "schema_rejected"
        else:
            a["outcome"] = "timeout" if outcome == "timeout" else "http_error"
        return a, ""
    a["latency_ms"] = int(round((clock() - started) * 1000))
    a["http_status"] = 200
    text = "".join(_get(b, "text", "") or "" for b in (_get(msg, "content") or [])
                   if _get(b, "type") == "text")
    u = _get(msg, "usage")
    a["input_tokens"] = int(_get(u, "input_tokens", 0) or 0)
    a["output_tokens"] = int(_get(u, "output_tokens", 0) or 0)
    a["usd"] = round(rb_ledger.call_cost(req["model"], a["input_tokens"],
                                         a["output_tokens"]), 6)
    stop = _get(msg, "stop_reason")
    a["finish"] = stop
    if stop == "max_tokens":
        a["outcome"] = "truncated"
    elif stop == "refusal":
        a["outcome"] = "refusal"
    elif not text.strip():
        a["outcome"] = "empty"
    else:
        a["outcome"] = "ok"
    return a, text


def call(key, task, prompt, budget, *, http=None, client=None, clock=time.monotonic,
         with_format=True):
    vendor = rb_requests.MODELS[key]["vendor"]
    attempts, text = [], ""
    for retry in (False, True):
        b = rb_requests.budget("T", 0, retry=True) if retry else budget
        req = rb_requests.request_for(key, task, prompt, b, retry=retry,
                                      with_format=with_format)
        if vendor == "anthropic":
            a, text = _claude_attempt(key, req, prompt, client, clock)
        else:
            a, text = _openrouter_attempt(key, req, prompt, http or cc.http_session(), clock)
        a["retry"] = retry
        attempts.append(a)
        if not (task == "T" and a["outcome"] == "truncated" and not retry):
            break
    last = attempts[-1]
    return {"attempts": attempts, "outcome": last["outcome"], "text": text,
            "usd": round(sum(x["usd"] or 0.0 for x in attempts), 6),
            "usd_upper": (sum(x["usd_upper"] or 0.0 for x in attempts)
                          if vendor == "anthropic" else None)}
