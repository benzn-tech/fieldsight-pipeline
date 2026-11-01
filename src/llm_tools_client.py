"""Tool-calling chat for the Ask judge (phase 2 of Ask orchestration v2).

Spec: docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md §5.

Neither existing client carries a messages array or tools: `llm_utils.call_llm`
takes one prompt string and `corroboration_client.call` takes system + user.
This one sends OpenRouter chat completions with OpenAI-shape `tools[]` and
`tool_choice`, and hands back the tool calls as data. Nothing calls it yet; the
phase 3 judge loop will.

What it reuses, by import and never by copy: `corroboration_client`'s endpoint
URL, its API key lookup and its HTTP session factory. A second place that knew
which env var holds the key would be a second place to get it wrong.

Rules that shape the code below:

- **No `plugins`.** Web search is our own tool in the judge loop, not the
  provider's plugin; a plugin here would search on every turn and bill for it.
- **Malformed arguments are data, not exceptions.** A tool call whose
  `arguments` is not a JSON object is returned with `arguments={}` and its id in
  `bad_args`, so the loop can feed the failure back to the model (§6.4 ends the
  loop after two in a row).
- **`tool_choice` portability.** Some endpoints reject `"required"`, and some
  (muse-spark, TEST 2026-10-16) accept only `"auto"`. A 400 that names
  `tool_choice` -- in the message or in OpenRouter's `metadata.raw` -- is
  retried once, same model, with `"auto"`. A forced `{"type": "function", ...}`
  is sent as is first; when it is rejected the retry offers ONLY the named tool
  under `"auto"`, so the forced final answer stays the only tool there is.
- **Fallbacks** (`ASK_JUDGE_FALLBACKS`, comma-separated) are tried in order on a
  5xx, a 429 or a transport failure (timeout included), and only while the
  deadline leaves time for another attempt. A 429 is the pinned endpoint's
  capacity, which the pin forbids OpenRouter to route around. Any other 4xx is
  our request's or the account's (402) and another model would fail the same way.
- **Provider pin and reasoning** (`ASK_JUDGE_PROVIDER`, `ASK_JUDGE_REASONING`)
  ride on the primary judge model only, never on a fallback: a fallback on
  another vendor would fail under an endpoint pin that names someone else.
  Unset means no `provider` / `reasoning` key, the payload as before.
- **Deadline** is an absolute `time.monotonic()` value, the convention the Ask
  agent already uses. Under one second left means no call at all.
- **Logging** carries caller, model, tool names, token usage and ms. Never
  message, argument or answer text: those are customer conversation data.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import urllib3

import corroboration_client
import llm_utils
from llm_usage import log_usage

logger = logging.getLogger()

DEFAULT_MODEL = "google/gemini-3.8-flash"

# Per-attempt ceiling when the caller gives no deadline, and the cap on any
# attempt when it does. A judge turn is one step of a loop inside a 45 s
# budget; one attempt allowed to eat all of it would leave nothing for the
# fallback chain this client exists to walk.
HTTP_TIMEOUT_CAP = float(os.environ.get("ASK_JUDGE_HTTP_TIMEOUT", "30"))

# Below this there is no time for a request to do anything but time out.
MIN_TIME_LEFT = 1.0

# Indirection so a test can drive the clock without patching time.monotonic
# for the whole process (pytest itself reads it).
_now = time.monotonic


@dataclass
class ChatTurn:
    """One assistant turn.

    `error` and `timed_out` are separate facts: a deadline and a vendor failure
    lead a reader to different places, so `timed_out` is structural and never
    inferred from the wording of `error`.
    """
    tool_calls: list = field(default_factory=list)
    text: str = ""
    usage: dict = field(default_factory=dict)
    model: str = ""
    error: str | None = None
    timed_out: bool = False
    bad_args: list = field(default_factory=list)
    # complete_json only: the chain entry that answered (`model` is the vendor's id).
    requested_model: str = ""


def _primary_model():
    """Read per call, so an env change or a test takes effect without a reload."""
    return (os.environ.get("ASK_JUDGE_MODEL")
            or os.environ.get("CORROBORATION_CHEAP_MODEL")
            or DEFAULT_MODEL)


def _provider_pin():
    """`{"only": [...], "allow_fallbacks": False}` from `ASK_JUDGE_PROVIDER`
    (comma-separated OpenRouter endpoint tags, e.g. "coreweave/fp8"), or None.
    The parser is shared with the template digitise read (llm_utils)."""
    return llm_utils.parse_provider(os.environ.get("ASK_JUDGE_PROVIDER"))


def _reasoning():
    """OpenRouter `reasoning` from `ASK_JUDGE_REASONING`: off, low, medium or
    high; anything else (after a warning) and unset mean no key at all.

    No model is passed, on purpose: the judge's `off` reaches the wire as
    `{"enabled": false}` whatever the judge model is, as it always has."""
    return llm_utils.parse_reasoning(os.environ.get("ASK_JUDGE_REASONING"),
                                     name="llm_tools: ASK_JUDGE_REASONING")


def _model_chain(primary):
    """primary, then each `ASK_JUDGE_FALLBACKS` entry once, never the primary again.

    Not `llm_utils.model_chain`: that one reads `LLM_FALLBACK_MODELS` with a
    non-empty default. Unset here means no fallback at all.
    """
    chain = [primary]
    raw = os.environ.get("ASK_JUDGE_FALLBACKS") or ""
    for m in (s.strip() for s in raw.split(",")):
        if m and m not in chain:
            chain.append(m)
    return chain


def _parse(data, model):
    """Read one OpenAI-shaped completion into a ChatTurn."""
    choices = data.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    content = message.get("content")
    text = content if isinstance(content, str) else ""

    tool_calls, bad_args = [], []
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raw_calls = []
    for raw in raw_calls:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") or {}
        if not isinstance(fn, dict):
            fn = {}
        cid = raw.get("id") or ""
        args = fn.get("arguments")
        if isinstance(args, dict):
            parsed = args
        else:
            try:
                parsed = json.loads(args) if isinstance(args, str) else None
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                parsed = {}
                bad_args.append(cid)
        tool_calls.append({"id": cid, "name": fn.get("name") or "",
                           "arguments": parsed})

    usage = data.get("usage")
    return ChatTurn(tool_calls=tool_calls, text=text,
                    usage=usage if isinstance(usage, dict) else {},
                    model=data.get("model") or model, bad_args=bad_args)


def _error_detail(resp):
    """(status, vendor message or ""). The message goes on the turn, never the log."""
    try:
        detail = json.loads(resp.data.decode("utf-8"))
        err = detail.get("error") if isinstance(detail, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        message = message if isinstance(message, str) else ""
        # OpenRouter wraps a provider 400 as "Provider returned error" and puts
        # the provider's own reason in metadata.raw.
        meta = err.get("metadata") if isinstance(err, dict) else None
        raw = meta.get("raw") if isinstance(meta, dict) else None
        if isinstance(raw, str) and raw:
            message = (message + ": " + raw[:500]) if message else raw[:500]
        return message
    except Exception:                         # noqa: BLE001 - body may be html
        return ""


def _log(turn, caller, elapsed, n_tools_offered):
    # Telemetry must never cost the caller its turn.
    try:
        usage = turn.usage or {}
        completion_detail = usage.get("completion_tokens_details") or {}
        prompt_detail = usage.get("prompt_tokens_details") or {}
        log_usage("openrouter", turn.model, caller, elapsed,
                  prompt_tokens=usage.get("prompt_tokens"),
                  completion_tokens=usage.get("completion_tokens"),
                  reasoning_tokens=completion_detail.get("reasoning_tokens"),
                  cache_read_tokens=prompt_detail.get("cached_tokens"),
                  cache_write_tokens=None)
        # Names and counts only. Arguments and text are conversation data.
        logger.info("LLM_TOOLS caller=%s model=%s tools_offered=%d "
                    "tool_calls=%s n_bad_args=%d text_chars=%d ms=%d",
                    caller, turn.model, n_tools_offered,
                    ",".join(c["name"] for c in turn.tool_calls) or "-",
                    len(turn.bad_args), len(turn.text), int(elapsed * 1000))
    except Exception:                         # noqa: BLE001
        logger.warning("LLM_TOOLS logging failed", exc_info=True)


# Models that rejected a non-"auto" tool_choice in this container. Each round
# would otherwise pay a ~0.6 s 400 before the retry (muse, TEST 2026-10-16).
_AUTO_ONLY = set()


def _auto_only(choice, tools):
    """("auto", tools) for an endpoint that takes only "auto": a forced choice
    keeps only its named tool, so the forced final answer stays forced."""
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        tools = [t for t in tools or []
                 if (t.get("function") or {}).get("name") == name] or tools
    return "auto", tools


def chat(messages, tools, *, model=None, tool_choice="required",
         max_tokens=2048, deadline=None, caller="ask_judge") -> ChatTurn:
    """One assistant turn with tools offered. Never raises for a vendor failure.

    `max_tokens` is an ANSWER budget; `llm_utils.REASONING_HEADROOM_TOKENS` is
    added on the wire because reasoning tokens are completion tokens and are
    spent before the answer.
    """
    key = corroboration_client.api_key()
    if not key:
        logger.error("llm_tools: API key not set caller=%s", caller)
        return ChatTurn(error="API key not configured")

    if deadline is not None and deadline - _now() < MIN_TIME_LEFT:
        return ChatTurn(error="no time left", timed_out=True)

    env_primary = _primary_model()
    chain = _model_chain(model or env_primary)
    # Pin and reasoning belong to the configured judge model: never to a
    # fallback, nor to a model a caller names in its place.
    primary_extras = {}
    if chain[0] == env_primary:
        pin, reasoning = _provider_pin(), _reasoning()
        if pin:
            primary_extras["provider"] = pin
        if reasoning:
            primary_extras["reasoning"] = reasoning
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    http = corroboration_client.http_session()
    started = _now()
    choice = tool_choice
    downgraded = False
    last_error, last_timed_out = None, False
    idx = 0

    while idx < len(chain):
        current = chain[idx]
        timeout = HTTP_TIMEOUT_CAP
        if deadline is not None:
            left = deadline - _now()
            if left < MIN_TIME_LEFT:
                logger.warning("llm_tools: deadline reached before model %d/%d "
                               "caller=%s", idx + 1, len(chain), caller)
                return ChatTurn(error=f"deadline reached ({last_error})",
                                timed_out=True, model=current)
            timeout = min(timeout, left)

        if current in _AUTO_ONLY and choice != "auto":
            choice, tools = _auto_only(choice, tools)
            downgraded = True
        payload = {
            "model": current,
            "messages": messages,
            "tools": tools,
            "tool_choice": choice,
            "max_tokens": max_tokens + llm_utils.REASONING_HEADROOM_TOKENS,
        }
        if idx == 0:
            payload.update(primary_extras)
        attempt_start = _now()
        try:
            resp = http.request("POST", corroboration_client.API_URL,
                                body=json.dumps(payload), headers=headers,
                                timeout=timeout)
        except Exception as e:                # noqa: BLE001 - all of it is a miss
            last_error = f"{type(e).__name__} on {current}"
            last_timed_out = isinstance(e, urllib3.exceptions.TimeoutError)
            logger.warning("llm_tools: %s caller=%s model=%s after %.2fs",
                           type(e).__name__, caller, current,
                           _now() - attempt_start)
            idx += 1
            continue

        if resp.status == 200:
            try:
                data = json.loads(resp.data.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except Exception:                 # noqa: BLE001
                logger.warning("llm_tools: unreadable 200 caller=%s model=%s",
                               caller, current)
                return ChatTurn(error="unreadable completion", model=current)
            turn = _parse(data, current)
            if not turn.tool_calls and not turn.text.strip():
                # A 200 with nothing in it is a failure, not an empty answer.
                turn.error = "empty completion"
            _log(turn, caller, _now() - started, len(tools or []))
            return turn

        message = _error_detail(resp)
        last_error = (f"HTTP {resp.status}: {message}" if message
                      else f"HTTP {resp.status}")
        last_timed_out = False
        logger.warning("llm_tools: HTTP %d caller=%s model=%s after %.2fs",
                       resp.status, caller, current, _now() - attempt_start)

        if (resp.status == 400 and not downgraded and "tool_choice" in message
                and (choice == "required" or isinstance(choice, dict))):
            _AUTO_ONLY.add(current)
            choice, tools = _auto_only(choice, tools)
            downgraded = True
            continue                          # same model, once
        if 500 <= resp.status < 600 or resp.status == 429:
            idx += 1
            continue
        break

    return ChatTurn(error=last_error or "no response", timed_out=last_timed_out,
                    model=chain[min(idx, len(chain) - 1)])


def complete_json(prompt, *, model=None, max_tokens=2048, deadline=None,
                  caller="ask_judge", attempt_timeout=None, judge_routing=True) -> ChatTurn:
    """One tool-less JSON answer routed exactly like the Ask judge: `model` (default the
    judge model) then ASK_JUDGE_FALLBACKS (unset = none), the provider pin and reasoning on
    the configured judge model only, a fallback on a 5xx, a 429 or a transport failure,
    never on another 4xx. `response_format` json_object; no tools. Never raises for a
    vendor failure. `deadline` is absolute on `_now()`; `attempt_timeout` (default
    HTTP_TIMEOUT_CAP) caps one attempt. `turn.requested_model` is the chain entry that
    answered, `turn.model` the vendor's served id. Added for the action agent (action
    packs Task 11); `chat` is unchanged.

    `judge_routing=False` (the action-pack verifier, Task 12): only `model`, no
    ASK_JUDGE_FALLBACKS and never the judge's provider pin or reasoning -- the verifier
    walks its own chain and checks each answer's family itself."""
    key = corroboration_client.api_key()
    if not key:
        logger.error("llm_tools: API key not set caller=%s", caller)
        return ChatTurn(error="API key not configured")
    if deadline is not None and deadline - _now() < MIN_TIME_LEFT:
        return ChatTurn(error="no time left", timed_out=True)
    env_primary = _primary_model()
    chain = _model_chain(model or env_primary) if judge_routing else [model or env_primary]
    primary_extras = {}
    if judge_routing and chain[0] == env_primary:
        pin, reasoning = _provider_pin(), _reasoning()
        if pin:
            primary_extras["provider"] = pin
        if reasoning:
            primary_extras["reasoning"] = reasoning
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    http = corroboration_client.http_session()
    cap = HTTP_TIMEOUT_CAP if attempt_timeout is None else float(attempt_timeout)
    started = _now()
    last_error, last_timed_out = None, False
    for idx, current in enumerate(chain):
        timeout = cap
        if deadline is not None:
            left = deadline - _now()
            if left < MIN_TIME_LEFT:
                return ChatTurn(error=f"deadline reached ({last_error})", timed_out=True,
                                model=current, requested_model=current)
            timeout = min(timeout, left)
        payload = {"model": current,
                   "messages": [{"role": "user", "content": prompt}],
                   "response_format": {"type": "json_object"},
                   "max_tokens": max_tokens + llm_utils.REASONING_HEADROOM_TOKENS}
        if idx == 0:
            payload.update(primary_extras)
        attempt_start = _now()
        try:
            resp = http.request("POST", corroboration_client.API_URL,
                                body=json.dumps(payload), headers=headers, timeout=timeout)
        except Exception as e:                # noqa: BLE001 - all of it is a miss
            last_error = f"{type(e).__name__} on {current}"
            last_timed_out = isinstance(e, urllib3.exceptions.TimeoutError)
            logger.warning("llm_tools json: %s caller=%s model=%s after %.2fs",
                           type(e).__name__, caller, current, _now() - attempt_start)
            continue
        if resp.status == 200:
            try:
                data = json.loads(resp.data.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not an object")
            except Exception:                 # noqa: BLE001
                return ChatTurn(error="unreadable completion", model=current,
                                requested_model=current)
            turn = _parse(data, current)
            turn.requested_model = current
            if not turn.text.strip():
                turn.error = "empty completion"
            _log(turn, caller, _now() - started, 0)
            return turn
        message = _error_detail(resp)
        last_error = f"HTTP {resp.status}: {message}" if message else f"HTTP {resp.status}"
        last_timed_out = False
        logger.warning("llm_tools json: HTTP %d caller=%s model=%s after %.2fs",
                       resp.status, caller, current, _now() - attempt_start)
        if 500 <= resp.status < 600 or resp.status == 429:
            continue
        break
    return ChatTurn(error=last_error or "no response", timed_out=last_timed_out,
                    model=chain[-1], requested_model="")
