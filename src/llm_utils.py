"""
llm_utils.py — Unified LLM client with runtime provider dispatch.

Replaces the four duplicated call_claude implementations (claude_utils.py,
lambda_report_generator.py, lambda_meeting_minutes.py, lambda_ask_agent.py).
Dispatches on LLM_PROVIDER: 'anthropic' (Claude Messages API, verbatim
behaviour) or 'qwen' (DashScope OpenAI-compatible /chat/completions). Adds the
exponential-backoff retry claude_utils.py never had, mirroring
dashscope_utils.py (MAX_ATTEMPTS=4, backoff on 429/5xx).

Model selection is per-Lambda via env: CLAUDE_MODEL for the anthropic path,
QWEN_MODEL for the qwen path. Never reads both.

Environment Variables:
    LLM_PROVIDER   - 'anthropic' (default) | 'qwen'
    ANTHROPIC_API_KEY / CLAUDE_MODEL - anthropic path
    QWEN_API_KEY (falls back to DASHSCOPE_API_KEY) / QWEN_BASE_URL / QWEN_MODEL - qwen path
    QWEN_ENABLE_THINKING - 'true' runs qwen in thinking mode (skips response_format) - qwen path
                           (per-function default; call_llm(enable_thinking=) overrides per call)
"""
import json
import logging
import os
import re
import time

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

def _optional_float(name):
    """A knob that is UNSET must stay unsent, not become 0.0. Sending a default
    would change every caller in this repo silently."""
    raw = os.environ.get(name, "").strip()
    # `unset` is the sentinel the deploy has to use, not a typo: SAM CLI REJECTS an
    # empty --parameter-overrides value ("LlmTemperature= is not a valid format"), so
    # "no temperature" cannot be spelled as the empty string once it has to travel
    # through a workflow. It failed the prod deploy on 2026-08-12 and only prod, because
    # test passes a real number. Treated here rather than warned about, so the ordinary
    # production configuration does not log a warning on every cold start.
    if not raw or raw.lower() in ("unset", "none", "default"):
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number -- ignoring", name, raw)
        return None


# Sampling temperature, applied to whichever provider is in use. Never set
# before 2026-08-12, so every call has taken the provider default (DashScope
# documents 0.7 for the non-thinking Qwen path).
#
# It does NOT make extraction reproducible: a preregistered 2x2 (10 calls per
# cell, one fixed session) found the action count still ranged 1-9 at
# temperature=0 against 1-10 at the default, coverage difference inside the
# noise. What it did do, far too large to be noise, is take the share of action
# items carrying a `responsible` from 79% to 92% -- and that is the field a
# misheard name has already cost something on.
LLM_TEMPERATURE = _optional_float("LLM_TEMPERATURE")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-max")
# When true, the qwen path runs in thinking mode (enable_thinking) for higher
# answer quality on batch tasks, and does NOT force response_format — DashScope
# guidance is that thinking + json_object can yield non-strict JSON, so we let
# the model output freely and rely on extract_json(). Default false keeps the
# fast/cheap non-thinking path for latency-bound callers (ask-agent).
QWEN_ENABLE_THINKING = os.environ.get("QWEN_ENABLE_THINKING", "false").lower() == "true"

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
# 150s so the HTTP client loses the race against the Lambda's own Timeout and
# we get a catchable urllib3 error instead of a runtime hard-kill.
# ReportGeneratorFunction and MeetingMinutesFunction override this to 180 via
# LLM_HTTP_TIMEOUT (see template.yaml) because their Lambda Timeout is 300s,
# not 180s like extract_session/matcher/ask-agent.
HTTP_TIMEOUT = float(os.environ.get("LLM_HTTP_TIMEOUT", "150"))


def api_key_configured():
    """True if the active provider's key is set (used for fail-fast checks)."""
    if LLM_PROVIDER == "qwen":
        return bool(QWEN_API_KEY)
    return bool(ANTHROPIC_API_KEY)


def call_llm(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
    """Return (text, None) on success or (None, error_string) on failure.

    enable_thinking (qwen path only; the anthropic path ignores it):
      None  - use the QWEN_ENABLE_THINKING env default (every pre-existing
              caller keeps its exact behaviour).
      True  - force thinking mode for THIS call.
      False - force the fast non-thinking path for THIS call.
    The per-call override exists because one Lambda can need both modes:
    lambda_extract_session runs a fast live pass during recording and a
    thinking-mode final pass once the session closes.
    """
    if LLM_PROVIDER == "qwen":
        return _call_qwen(prompt, max_tokens, force_json, enable_thinking)
    return _call_anthropic(prompt, max_tokens)


def _post_with_retry(url, body, headers):
    """Single POST with exponential backoff on 429/5xx. Returns (resp, error)."""
    http = urllib3.PoolManager()
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request(
                "POST", url, body=body, headers=headers, timeout=HTTP_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 - network errors are retryable
            last_error = str(e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return None, last_error
        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            last_error = f"HTTP {resp.status}"
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        return resp, None
    return None, last_error


def _call_anthropic(prompt, max_tokens):
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return None, "ANTHROPIC_API_KEY not configured"
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if LLM_TEMPERATURE is not None:
        payload["temperature"] = LLM_TEMPERATURE
    body = json.dumps(payload)
    resp, err = _post_with_retry(
        "https://api.anthropic.com/v1/messages",
        body,
        {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    if resp is None:
        logger.error(f"Claude API call failed: {err}")
        return None, err
    data = json.loads(resp.data.decode("utf-8"))
    if resp.status == 200:
        blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(blocks), None
    msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
    logger.error(f"Claude API error: {msg}")
    return None, msg


def _call_qwen(prompt, max_tokens, force_json, enable_thinking=None):
    if not QWEN_API_KEY:
        logger.error("QWEN_API_KEY / DASHSCOPE_API_KEY not set")
        return None, "QWEN_API_KEY not configured"
    payload = {"model": QWEN_MODEL, "messages": [{"role": "user", "content": prompt}]}
    if LLM_TEMPERATURE is not None:
        payload["temperature"] = LLM_TEMPERATURE
    # Per-call override wins; None falls back to the function's env default.
    thinking = QWEN_ENABLE_THINKING if enable_thinking is None else bool(enable_thinking)
    if thinking:
        # Thinking mode: highest quality for batch tasks. Do NOT force
        # response_format even when force_json (thinking + json_object risks
        # non-strict JSON); the prompt already instructs JSON and extract_json()
        # parses it. No max_tokens cap so the answer isn't truncated after the
        # (separate reasoning_content) chain of thought.
        payload["enable_thinking"] = True
    else:
        # Non-thinking. DashScope's Qwen3 models DEFAULT to thinking when
        # enable_thinking is OMITTED, so QWEN_ENABLE_THINKING=false is INERT
        # unless we send the flag explicitly False — otherwise a "non-thinking"
        # caller silently burns reasoning latency (measured on the summary task:
        # qwen3.7-max 38s omitted vs 4s explicit-False; qwen3.6-flash 19s vs 3s).
        payload["enable_thinking"] = False
        if force_json:
            # DashScope: do NOT send max_tokens with response_format (truncation risk).
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["max_tokens"] = max_tokens
    resp, err = _post_with_retry(
        f"{QWEN_BASE_URL}/chat/completions",
        json.dumps(payload),
        {"Content-Type": "application/json", "Authorization": f"Bearer {QWEN_API_KEY}"},
    )
    if resp is None:
        logger.error(f"Qwen API call failed: {err}")
        return None, err
    data = json.loads(resp.data.decode("utf-8"))
    if resp.status == 200:
        try:
            return data["choices"][0]["message"]["content"], None
        except (KeyError, IndexError):
            logger.error(f"Qwen unexpected response shape: {str(data)[:500]}")
            return None, "unexpected Qwen response shape"
    err_obj = data.get("error") or {}
    msg = err_obj.get("message", f"HTTP {resp.status}")
    # The CODE, not just the message. These read almost identically in a log
    # line -- "Requests rate limit exceeded" vs "Free allocated quota
    # exceeded" -- but they are opposite problems: the first is a burst that
    # recovers on its own and wants backoff, the second is an account that
    # has stopped working and wants a human. A 2026-08-04 outage was
    # diagnosed as quota exhaustion and was in fact a per-minute TPM limit
    # that had already self-recovered, because only the message was logged.
    code = err_obj.get("code")
    logger.error("Qwen API error: status=%s code=%s message=%s",
                 resp.status, code or "-", msg)
    return None, msg


def extract_json(raw_text):
    """Three-tier fallback: fenced ```json``` block, whole string, brace slice."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(raw_text.strip())
    except json.JSONDecodeError:
        pass
    first_brace = raw_text.find("{")
    last_brace = raw_text.rfind("}")
    if first_brace != -1 and last_brace != -1:
        try:
            return json.loads(raw_text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    logger.error(f"Failed to extract JSON from LLM response: {raw_text[:500]}")
    return None
