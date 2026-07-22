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

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-plus")

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


def call_llm(prompt, max_tokens=4096, force_json=False):
    """Return (text, None) on success or (None, error_string) on failure."""
    if LLM_PROVIDER == "qwen":
        return _call_qwen(prompt, max_tokens, force_json)
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
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
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


def _call_qwen(prompt, max_tokens, force_json):
    if not QWEN_API_KEY:
        logger.error("QWEN_API_KEY / DASHSCOPE_API_KEY not set")
        return None, "QWEN_API_KEY not configured"
    payload = {"model": QWEN_MODEL, "messages": [{"role": "user", "content": prompt}]}
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
    msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
    logger.error(f"Qwen API error: {msg}")
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
