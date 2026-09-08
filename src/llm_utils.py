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
    if not raw:
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
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-flash")
# The model to use when a call runs WITHOUT thinking. Empty means "same as
# QWEN_MODEL", which is what every deploy did before this existed.
#
# It exists because a model can be safe in one mode and not the other, and
# nothing else in this system can express that. The model is a CloudFormation
# stack parameter; thinking is a per-function env var that one caller
# (lambda_extract_session's live pass) overrides per call. So a per-function
# model parameter cannot separate the two modes inside a single function, and
# no test can go red on the pairing -- the two values never meet until here.
#
# Measured 2026-09-07, five runs per configuration, on one site sentence
# carrying an explicit date ("delayed to Friday"): qwen3.7-max and
# qwen3.6-flash put Friday in the `due` field 4/5 with thinking off;
# qwen3.8-flash managed 0/5 with thinking off -- in BOTH output modes, so it is
# not response_format -- and 4/5 with thinking on. Five of the eight qwen call
# sites in this repo run non-thinking deliberately, for latency.
# docs/superpowers/specs/2026-09-07-qwen38-flash-thinking-dependency.md
QWEN_MODEL_NONTHINKING = os.environ.get("QWEN_MODEL_NONTHINKING", "").strip()
# Sentinel rather than empty string: an override that resolves to nothing is
# rendered `"QwenModelNonThinking=" \` and fails the WHOLE deploy the day the
# repo variable is cleared, which `test_no_override_line_can_emit_an_empty_value`
# exists to prevent. An explicit word survives the round trip.
QWEN_MODEL_INHERIT = "inherit"
# When true, the qwen path runs in thinking mode (enable_thinking) for higher
# answer quality on batch tasks, and does NOT force response_format — DashScope
# guidance is that thinking + json_object can yield non-strict JSON, so we let
# the model output freely and rely on extract_json(). Default false keeps the
# fast/cheap non-thinking path for latency-bound callers (ask-agent).
QWEN_ENABLE_THINKING = os.environ.get("QWEN_ENABLE_THINKING", "false").lower() == "true"

# How hard a reasoning-capable vendor should think, when the vendor expresses that
# as a level rather than a boolean.
#
# DashScope has `enable_thinking`, which is on or off. OpenAI-compatible vendors
# (OpenRouter, and the models behind it) take `reasoning: {"effort": ...}`, where
# the levels are priced and latency-differentiated. Those are not the same knob and
# must not be conflated: `effort` sent to DashScope is an unknown field, and an
# unknown field is either rejected loudly or DROPPED SILENTLY -- and if dropped, a
# caller that asked for `low` on the latency path quietly pays for full reasoning.
#
# Empty means "say nothing about effort", which is what every deploy did before this
# existed, so an unset value changes no request.
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "").strip().lower()
VALID_EFFORTS = ("low", "medium", "high")
# Added to the caller's max_tokens on reasoning endpoints, because reasoning
# tokens are completion tokens and are emitted BEFORE the answer. Sized from
# measurement rather than taste: the same prompt used 516 reasoning tokens at
# effort=low, 743 at high, and 1671 in JSON mode -- so a 4k default with no
# headroom is fine until the day a long transcript makes the model think, which
# is exactly the day it matters.
REASONING_HEADROOM_TOKENS = int(os.environ.get("LLM_REASONING_HEADROOM", "8000"))

# The most output any caller may ask for. One number, here, because three
# lambdas were each carrying their own 16000 and a vendor change has to move
# all of them or none. Sized to the SMALLEST completion cap among the models
# this deploy can reach -- gemini-3.8-flash caps at 65,536, muse-spark-1.3 at
# 943,718, qwen3.8-flash at 131,072 -- and REASONING_HEADROOM_TOKENS is added
# on top before it goes on the wire, so 32000 + 8000 clears the lowest.
# Measured peak on the widest real prod report was 3,386 tokens; this is not
# sized to be tight, it is sized so the answer is never the thing that stops.
ANSWER_TOKEN_CEILING = int(os.environ.get("LLM_ANSWER_TOKEN_CEILING", "32000"))

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
# 150s so the HTTP client loses the race against the Lambda's own Timeout and
# we get a catchable urllib3 error instead of a runtime hard-kill.
# ReportGeneratorFunction and MeetingMinutesFunction override this to 180 via
# LLM_HTTP_TIMEOUT (see template.yaml) because their Lambda Timeout is 300s,
# not 180s like extract_session/matcher/ask-agent.
HTTP_TIMEOUT = float(os.environ.get("LLM_HTTP_TIMEOUT", "150"))


def qwen_model_for(thinking):
    """Which qwen model a call in this mode actually reaches.

    One function can need both modes, so this is a property of the CALL, never
    of the deploy. Inheritance resolves HERE and not at import: a snapshot taken
    when the module loaded stops following `QWEN_MODEL` the moment anything
    changes it, and the two would then disagree with no way to see it. Anything reporting a model name to a reader has to ask the
    same question the caller asked, or it names a model that did not write the
    answer -- which this repo has already shipped once.
    """
    if thinking or not QWEN_MODEL_NONTHINKING:
        return QWEN_MODEL
    # Case-folded on COMPARISON only. The value crosses a repo variable, a
    # shell, a CLI override and CloudFormation and none of them normalise it,
    # so `Inherit` would otherwise reach DashScope as a literal model name and
    # fail every non-thinking call in six functions at runtime. Folding the
    # value itself instead would quietly lowercase a real model name, and not
    # every vendor's are lowercase.
    if QWEN_MODEL_NONTHINKING.lower() == QWEN_MODEL_INHERIT:
        return QWEN_MODEL
    return QWEN_MODEL_NONTHINKING


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


def active_model(enable_thinking=None):
    """The model this deploy actually calls, for reporting to a caller.

    `CLAUDE_MODEL` is the Anthropic branch's model and is set on every function
    regardless of provider, so reading it directly answers a different question
    from the one a caller is asking. On prod today `LLM_PROVIDER=qwen`, and every
    Ask answer was labelled `claude-haiku-4-5-20251001` under the reader's nose
    while `qwen3.6-flash` wrote it.

    Returns None when the provider is one this function does not know about --
    a wrong name is worse than no name, because the reader cannot tell it is
    wrong.
    """
    if LLM_PROVIDER == "qwen":
        return qwen_model_for(enable_thinking if enable_thinking is not None
                              else QWEN_ENABLE_THINKING)
    if LLM_PROVIDER == "anthropic":
        return CLAUDE_MODEL
    return None


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


def _is_dashscope(base_url):
    """Whether the configured chat endpoint is DashScope.

    `enable_thinking` is a DashScope extension. It is absent from every other
    vendor's parameter list, and an unknown field is either rejected outright
    or -- worse -- silently dropped, which would turn "no thinking" into
    thinking on the latency-bound Ask path with nothing to show for it.

    Dispatching on the endpoint rather than adding a third LLM_PROVIDER value
    is deliberate: a new provider branch would duplicate the retry, JSON-mode
    and error-code handling that already work here.
    """
    return "aliyuncs.com" in (base_url or "")


def _call_qwen(prompt, max_tokens, force_json, enable_thinking=None):
    if not QWEN_API_KEY:
        logger.error("QWEN_API_KEY / DASHSCOPE_API_KEY not set")
        return None, "QWEN_API_KEY not configured"
    # Per-call override wins; None falls back to the function's env default.
    thinking = QWEN_ENABLE_THINKING if enable_thinking is None else bool(enable_thinking)
    # Resolved BEFORE the model, because the model depends on it.
    payload = {"model": qwen_model_for(thinking),
               "messages": [{"role": "user", "content": prompt}]}
    if LLM_TEMPERATURE is not None:
        payload["temperature"] = LLM_TEMPERATURE

    if not _is_dashscope(QWEN_BASE_URL):
        # OpenAI-compatible vendors (OpenRouter today). Reasoning is expressed
        # with `reasoning`; `enable_thinking` must never be sent.
        #
        # The DashScope branch below also DROPS max_tokens and skips
        # response_format whenever thinking is on. That coupling is a DashScope
        # workaround -- thinking + json_object there risks non-strict JSON --
        # and it is deliberately NOT ported: an unbounded completion on a
        # per-token vendor is a cost incident waiting to happen, and structured
        # outputs are supported here.
        # `effort` when the deploy states one, the boolean otherwise. A value this
        # vendor does not know would be worse than saying nothing, so anything
        # outside the known set falls back rather than travelling.
        #
        # A caller that passed enable_thinking=False asked for the FAST path for
        # THIS call, and that beats the deploy-wide effort. It has to: one Lambda
        # can need both modes, which is the entire reason the parameter exists.
        # lambda_extract_session runs a live pass on every 30-second chunk and a
        # thinking pass when the session closes -- one env value cannot serve both,
        # and until this branch existed the env silently won. Measured on the
        # deployed live pass with LLM_REASONING_EFFORT=high: 119.1s and 8,753
        # reasoning tokens, on a path throttled to run every 90s. The "fast" pass
        # had become slower than its own trigger interval (BUG-43's shape) while
        # its log line still read thinking=False.
        #
        # enable_thinking=True does NOT override: the caller is saying THINK, and
        # how hard is the deploy's call, so the env effort still names the level.
        if enable_thinking is False:
            payload["reasoning"] = {"effort": "low"}
        elif LLM_REASONING_EFFORT in VALID_EFFORTS:
            payload["reasoning"] = {"effort": LLM_REASONING_EFFORT}
        else:
            if LLM_REASONING_EFFORT:
                logger.warning("ignoring unknown LLM_REASONING_EFFORT=%r (want one of %s)",
                               LLM_REASONING_EFFORT, ", ".join(VALID_EFFORTS))
            # The boolean mapped onto the vendor's vocabulary, NOT
            # `{"enabled": False}`. Measured against
            # meta/muse-spark-1.3-contributor: that field is a hard 400 --
            # "Reasoning is mandatory for this endpoint and cannot be disabled"
            # -- so a deploy that merely forgot to set an effort would fail
            # every call rather than fall back. `low` is the closest thing the
            # endpoint offers to off, and it is measurably cheaper and faster
            # than the default (516 reasoning tokens / 6.8s vs 606 / 8.0s on the
            # same prompt).
            payload["reasoning"] = {"effort": "high" if thinking else "low"}
        # Reasoning tokens are COMPLETION tokens: they come out of max_tokens
        # before the answer does. A caller asking for 4096 "for the answer" gets
        # an empty answer if the model spends 4096 thinking -- measured, at
        # max_tokens=1200 this model produced 1197 reasoning tokens and
        # content=''. So the caller's number stays the ANSWER budget and the
        # thinking budget is added on top.
        payload["max_tokens"] = max_tokens + REASONING_HEADROOM_TOKENS
        if force_json:
            payload["response_format"] = {"type": "json_object"}
    elif thinking:
        # Thinking mode: highest quality for batch tasks. Do NOT force
        # response_format even when force_json (thinking + json_object risks
        # non-strict JSON); the prompt already instructs JSON and extract_json()
        # parses it. No max_tokens cap so the answer isn't truncated after the
        # (separate reasoning_content) chain of thought.
        payload["enable_thinking"] = True
    else:
        # Non-thinking. DashScope's Qwen3 models DEFAULT to thinking when
        # enable_thinking is OMITTED, so QWEN_ENABLE_THINKING=false is INERT
        # unless we send the flag explicitly False -- otherwise a "non-thinking"
        # caller silently burns reasoning latency (measured on the summary task:
        # qwen3.7-max 38s omitted vs 4s explicit-False; qwen3.6-flash 19s vs 3s).
        payload["enable_thinking"] = False
        if force_json:
            # DashScope: do NOT send max_tokens with response_format (truncation risk).
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["max_tokens"] = max_tokens

    # One line per call, on the way OUT, naming what actually goes on the wire.
    #
    # Which model served a call was unanswerable from anywhere: the env says
    # what a deploy CAN reach, and one function reaches two. The rolling-summary
    # artifact records turn_count and updated_at and no model, so after a bump
    # nobody can say whether a bad answer came from the new model or the old --
    # and a silent regression is exactly the failure this pairing exists to
    # prevent. A guard that passes still has to leave a line, or "it ran
    # correctly" and "it never ran" look the same.
    # `effort` is in here because its absence is what hid the defect above: the
    # line said thinking=False on a call that was reasoning at high, and nothing
    # anywhere printed what actually went on the wire.
    logger.info("qwen call: model=%s thinking=%s effort=%s json=%s",
                payload["model"], thinking,
                (payload.get("reasoning") or {}).get("effort", "-"),
                "response_format" in payload)
    started = time.monotonic()
    resp, err = _post_with_retry(
        f"{QWEN_BASE_URL}/chat/completions",
        json.dumps(payload),
        {"Content-Type": "application/json", "Authorization": f"Bearer {QWEN_API_KEY}"},
    )
    elapsed = time.monotonic() - started
    if resp is None:
        logger.error(f"Qwen API call failed: {err}")
        return None, err
    data = json.loads(resp.data.decode("utf-8"))
    if resp.status == 200:
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError):
            logger.error(f"Qwen unexpected response shape: {str(data)[:500]}")
            return None, "unexpected Qwen response shape"
        usage = data.get("usage") or {}
        detail = usage.get("completion_tokens_details") or {}
        # HTTP 200 WITH NO ANSWER IS A FAILURE, and on a reasoning model it is
        # the likeliest one. The tokens are spent inside `reasoning`, `content`
        # comes back '' or None, and every status field says success -- so a
        # caller that trusts the 200 writes an empty summary for a real day and
        # nothing anywhere says why. Measured on
        # meta/muse-spark-1.3-contributor: max_tokens=1200 -> 1197 reasoning
        # tokens, content='', finish_reason='length', HTTP 200.
        #
        # Returned as an error so the caller's existing failure path runs: a
        # day with no summary and a loud log beats a day with a blank one. The
        # error string carries finish_reason because "the model said nothing"
        # and "the model was cut off mid-thought" want different fixes.
        if not (content or "").strip():
            reason = choice.get("finish_reason")
            logger.error(
                "Qwen returned an empty answer: finish_reason=%s completion_tokens=%s "
                "reasoning_tokens=%s max_tokens=%s model=%s -- the budget was spent "
                "thinking, raise max_tokens or lower the reasoning effort",
                reason, usage.get("completion_tokens"),
                detail.get("reasoning_tokens"), payload.get("max_tokens"),
                data.get("model"))
            return None, f"empty answer from model (finish_reason={reason})"
        # Throughput is a deploy decision -- muse, gemini and qwen differ by
        # multiples on the same prompt -- and it was not answerable from any
        # log: duration lived in the Lambda REPORT line, which counts S3 and
        # Aurora too, and the token counts were never written down at all. A
        # call that SUCCEEDS has to leave its numbers behind, or the next model
        # comparison is guesswork again.
        completion = usage.get("completion_tokens")
        logger.info(
            "qwen done: model=%s %.1fs prompt=%s completion=%s reasoning=%s finish=%s%s",
            data.get("model"), elapsed, usage.get("prompt_tokens"), completion,
            detail.get("reasoning_tokens"), choice.get("finish_reason"),
            (" %.1f tok/s" % (completion / elapsed)) if completion and elapsed > 0 else "")
        return content, None
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


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(obj):
    r"""Scrub C0 control characters out of every string in a parsed response.

    2026-09-08, prod. An extraction came back with

        "time_range": "13:37 \x0e2\x0813:37"

    -- SHIFT OUT and BACKSPACE where the en dash belongs. Both `parse_time_range`
    implementations correctly refused it and returned None, so the topic owned no
    time window and NOTHING bound to it. No exception, no warning: the guard held
    and the feature stopped working, which is the most expensive failure shape
    there is.

    Rare, and total when it lands. Across 123 prod extraction artifacts and 281
    topics, exactly 2 were unparseable -- and both were in the SAME session, so
    that session lost photo binding on every topic it had.

    Scrubbed HERE, not in the parsers, because there are TWO of those
    (photo_binding and chunking) and two copies of one rule is how one of them
    gets fixed and the other does not -- the same reason `elide_middle` was
    factored out. This is the choke point every LLM caller already passes
    through, so no control character reaches any consumer.

    TAB, LF and CR are deliberately kept: summaries legitimately contain them.
    """
    if isinstance(obj, str):
        return _CONTROL_CHARS.sub("", obj)
    if isinstance(obj, list):
        return [_strip_control_chars(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _strip_control_chars(v) for k, v in obj.items()}
    return obj


def extract_json(raw_text):
    """Three-tier fallback: fenced ```json``` block, whole string, brace slice.

    Every tier returns through `_strip_control_chars`: a scrub wired into only
    the first would pass a naive test and still leak on real output, which
    usually arrives fenced or with prose around it.
    """
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if match:
        try:
            return _strip_control_chars(json.loads(match.group(1)))
        except json.JSONDecodeError:
            pass
    try:
        return _strip_control_chars(json.loads(raw_text.strip()))
    except json.JSONDecodeError:
        pass
    first_brace = raw_text.find("{")
    last_brace = raw_text.rfind("}")
    if first_brace != -1 and last_brace != -1:
        try:
            return _strip_control_chars(
                json.loads(raw_text[first_brace:last_brace + 1]))
        except json.JSONDecodeError:
            pass
    logger.error(f"Failed to extract JSON from LLM response: {raw_text[:500]}")
    return None
