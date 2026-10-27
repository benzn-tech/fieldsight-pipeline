"""One Claude + web_search verify call, as the bench measures it.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.0, §4.3.

The client is passed in (the official `anthropic` SDK client built with
`max_retries=0`, or a test fake), so this module never imports the SDK.

Rules that matter here, each from a measured or documented trap:
  * `searched` comes from result blocks, never from prose. A
    `web_search_tool_result` whose `content` is an error object
    (`max_uses_exceeded`, `too_many_requests`, ...) arrives with HTTP 200 and is
    a failed search, not an exception.
  * `pause_turn`: re-send the user message plus the assistant content once, with
    the time that is left; no extra "continue" turn. A second pause fails.
  * Exceptions are classified by shape (`status_code`, class name) so the SDK is
    not needed to read them: 400 = the config is rejected, not slow.
"""
from __future__ import annotations

import time

MAX_TOKENS = 4096
MAX_USES = 3
MIN_CONTINUE_S = 2.0
USER_LOCATION = {"type": "approximate", "country": "NZ", "timezone": "Pacific/Auckland"}


def build_request(model, tool_version, prompt):
    """kwargs for `client.messages.create`, without `timeout`. No `thinking`
    field: `{type: disabled}` is a 400 on Sonnet 5.5."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": "low"},
        "tools": [{"type": tool_version, "name": "web_search", "max_uses": MAX_USES,
                   "user_location": dict(USER_LOCATION)}],
        "messages": [{"role": "user", "content": prompt}],
    }


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def parse(response):
    """The numbers (and the text) of one response."""
    text, results, error_code, urls = [], 0, None, set()
    for block in _get(response, "content", None) or []:
        kind = _get(block, "type")
        if kind == "text":
            text.append(_get(block, "text", "") or "")
        elif kind == "web_search_tool_result":
            content = _get(block, "content")
            if isinstance(content, (list, tuple)):
                for r in content:
                    if _get(r, "type") == "web_search_result":
                        results += 1
                        if _get(r, "url"):
                            urls.add(_get(r, "url"))
            else:
                error_code = _get(content, "error_code") or "unknown_error"
    u = _get(response, "usage")
    stu = _get(u, "server_tool_use")
    return {
        "text": "".join(text).strip(),
        "searched": results > 0,
        "result_count": results,
        "source_count": len(urls),
        "error_code": error_code,
        "stop_reason": _get(response, "stop_reason"),
        "input_tokens": int(_get(u, "input_tokens", 0) or 0),
        "output_tokens": int(_get(u, "output_tokens", 0) or 0),
        "search_requests": int(_get(stu, "web_search_requests", 0) or 0),
    }


def _classify(exc):
    status = getattr(exc, "status_code", None)
    if status == 400:
        return "rejected", status
    if "Timeout" in type(exc).__name__:
        return "timeout", status
    return "http_error", status


def verify(client, *, model, tool_version, prompt, budget_s=14.0, clock=time.monotonic):
    """One verify: a call, at most one `pause_turn` continuation. Never raises."""
    request = build_request(model, tool_version, prompt)
    started = clock()
    total = {"input_tokens": 0, "output_tokens": 0, "search_requests": 0}
    out = {"text": "", "searched": False, "result_count": 0, "source_count": 0,
           "error_code": None, "stop_reason": None, "continuation": False,
           "http_status": None, "outcome": "failed"}
    calls = 0
    while True:
        left = budget_s - (clock() - started)
        if calls and left < MIN_CONTINUE_S:
            out["outcome"] = "timeout"
            break
        calls += 1
        try:
            resp = client.messages.create(timeout=left, **request)
        except Exception as exc:                          # noqa: BLE001 - classified
            out["outcome"], out["http_status"] = _classify(exc)
            break
        out["http_status"] = 200
        p = parse(resp)
        for k in total:
            total[k] += p[k]
        out.update({k: p[k] for k in ("text", "stop_reason", "error_code")})
        out["result_count"] += p["result_count"]
        out["source_count"] = max(out["source_count"], p["source_count"])
        out["searched"] = out["searched"] or p["searched"]
        if p["stop_reason"] == "pause_turn":
            if calls >= 2:
                out["outcome"] = "paused_twice"
                break
            out["continuation"] = True
            request = dict(request, messages=[
                request["messages"][0],
                {"role": "assistant", "content": _get(resp, "content")}])
            continue
        if p["stop_reason"] in ("refusal", "max_tokens"):
            out["outcome"] = p["stop_reason"]
        elif out["searched"] and out["text"]:
            out["outcome"] = "ok"
        else:
            out["outcome"] = "failed"
        break
    out.update(total)
    out["calls"] = calls
    out["latency_ms"] = int(round((clock() - started) * 1000))
    return out
