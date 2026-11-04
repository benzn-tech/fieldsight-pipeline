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
- **Claude ids never go to OpenRouter** (owner rule 2026-11-04). A chain entry that
  is `claude-*` or `anthropic/*` is sent to the Anthropic Messages API with
  `ANTHROPIC_API_KEY` (unset = an error, no OpenRouter fallback), converted to and
  from the OpenAI shape callers use; `anthropic/claude-opus-5.5` is sent as
  `claude-opus-5-5`. Other entries keep going to OpenRouter. A 529 counts as a 5xx.
- **Logging** carries caller, model, tool names, token usage and ms. Never
  message, argument or answer text: those are customer conversation data.
"""
from __future__ import annotations

import json
import logging
import os
import re
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


# ---- Claude models: api.anthropic.com only, never OpenRouter (owner rule 2026-11-04) ----

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# complete_json on Anthropic: no response_format json_object there, and an assistant
# prefill is a 400 on every 4.6+ model. A system instruction asks; the callers' own
# parsers (fence / brace slice) still read what comes back, as they do for OpenRouter.
JSON_SYSTEM = ("Reply with exactly one JSON object and nothing else: no prose before or "
               "after it, no code fence.")

# Anthropic tool_use ids must match ^[a-zA-Z0-9_-]+$. The same mapping is applied to
# the assistant's tool_use id and the tool message's tool_call_id, so pairs still match.
_TOOL_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")

# A tool call written into visible text instead of a tool_use block: the call never
# runs and nothing errors (memory: anthropic-tooluse-silent-shapes). Markup markers...
_TEXT_TOOL_MARKUP = re.compile(
    r"<\s*/?\s*(?:antml:)?(?:function_calls|invoke|tool_use|tool_call)\b", re.I)

_CLAUDE_MAJOR = re.compile(r"^claude-[a-z]+-(\d+)")


def is_claude(model_id):
    """A Claude model id: bare `claude-*` or OpenRouter-style `anthropic/*`."""
    if not isinstance(model_id, str):
        return False
    mid = model_id.strip().lower()
    return mid.startswith("claude-") or mid.startswith("anthropic/")


def native_claude_id(model_id):
    """The Anthropic API id for a Claude id: `anthropic/claude-opus-5.5` ->
    `claude-opus-5-5`; a bare id passes through (lower-cased, dots to dashes)."""
    mid = model_id.strip()
    if "/" in mid:
        mid = mid.split("/", 1)[1]
    mid = mid.split(":", 1)[0]              # OpenRouter variants such as ":beta"
    return mid.replace(".", "-").lower()


def _anthropic_key():
    """Read per call, so a test or an env change takes effect without a reload."""
    return os.environ.get("ANTHROPIC_API_KEY") or ""


def _thinks_by_default(native):
    """Claude 5.x and later think when `thinking` is omitted (and Opus 5.5 cannot turn
    it off). Forced tool use (`any` / `tool`) is rejected with thinking on, and on Opus
    5.5 / Sonnet 5.5 / Fable 5.1 outright, so those models are only ever sent `auto`."""
    m = _CLAUDE_MAJOR.match(native or "")
    return bool(m) and int(m.group(1)) >= 5


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and p.get("type") == "text"
                         and isinstance(p.get("text"), str))
    return ""


def _tool_id(cid):
    return _TOOL_ID_BAD.sub("_", str(cid or "")) or "call"


def _to_anthropic_messages(messages):
    """OpenAI-shape messages -> (system text, Anthropic messages).

    System messages go to the top-level `system`. An assistant turn becomes text (when
    there is any) plus one `tool_use` block per tool call, its JSON-string arguments
    parsed into `input`. Each `tool` message becomes a `tool_result` block, and
    consecutive same-role turns are merged so the N results after one assistant turn
    arrive in ONE user message (results first, any following user text after them).
    No thinking blocks are replayed: ChatTurn carries none, and a history without
    them is the documented no-replay shape."""
    system, out = [], []

    def push(role, blocks):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text = _text_of(m.get("content"))
        if role == "system":
            if text.strip():
                system.append(text)
        elif role == "user":
            if text.strip():
                push("user", [{"type": "text", "text": text}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": text}] if text.strip() else []
            for call in m.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                blocks.append({"type": "tool_use", "id": _tool_id(call.get("id")),
                               "name": fn.get("name") or "unknown",
                               "input": args if isinstance(args, dict) else {}})
            push("assistant", blocks)
        elif role == "tool":
            push("user", [{"type": "tool_result",
                           "tool_use_id": _tool_id(m.get("tool_call_id")),
                           "content": text}])
    return "\n\n".join(system), out


def _to_anthropic_tools(tools):
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        spec = {"name": fn["name"],
                "input_schema": fn.get("parameters") or {"type": "object",
                                                         "properties": {}}}
        if fn.get("description"):
            spec["description"] = fn["description"]
        out.append(spec)
    return out


def _anthropic_choice(choice, tools, native):
    """OpenAI tool_choice -> (Anthropic tool_choice, the OpenAI tools to offer).

    "auto" -> auto, "none" -> none. "required" -> `any` and a forced function ->
    `{"type": "tool"}` only on a model that does not think by default; on 5.x they
    become `auto`, a forced function offering ONLY that tool (as `_auto_only` does)."""
    if choice == "none":
        return {"type": "none"}, tools
    if _thinks_by_default(native):
        if isinstance(choice, dict):
            _, tools = _auto_only(choice, tools)
        return {"type": "auto"}, tools
    if choice == "required":
        return {"type": "any"}, tools
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}, tools
    return {"type": "auto"}, tools


def _anthropic_payload(native, messages, tools, choice, max_tokens, json_mode=False):
    """The Messages API body. Never carries OpenRouter's `provider` / `reasoning` /
    `response_format`, nor `thinking` (5.x default adaptive; disabling it makes Opus
    write tool calls into text) nor `output_config.effort` (Haiku 4.5 400s on it; the
    model's own default applies). `max_tokens` keeps the reasoning headroom: thinking
    tokens count against it, as reasoning does on OpenRouter."""
    system, msgs = _to_anthropic_messages(messages)
    if json_mode:
        system = (system + "\n\n" + JSON_SYSTEM) if system else JSON_SYSTEM
    payload = {"model": native,
               "max_tokens": max_tokens + llm_utils.REASONING_HEADROOM_TOKENS,
               "messages": msgs}
    if system:
        payload["system"] = system
    if tools:
        wire_choice, offered = _anthropic_choice(choice, tools, native)
        a_tools = _to_anthropic_tools(offered)
        if a_tools:
            payload["tools"] = a_tools
            payload["tool_choice"] = wire_choice
    return payload


def _anthropic_headers(key):
    return {"Content-Type": "application/json", "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION}


def _anthropic_usage(raw):
    """Anthropic usage -> the OpenAI keys the rest of the code reads. `prompt_tokens`
    is the whole prompt (input + cache read + cache write), as OpenAI counts it."""
    if not isinstance(raw, dict):
        return {}

    def num(k):
        v = raw.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    inp, out = num("input_tokens"), num("output_tokens")
    read, write = num("cache_read_input_tokens"), num("cache_creation_input_tokens")
    usage = {"prompt_tokens": (None if inp is None else inp + (read or 0) + (write or 0)),
             "completion_tokens": out}
    if read is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": read}
    if write is not None:
        usage["cache_creation_input_tokens"] = write
    return usage


def _tool_call_in_text(text, offered_names):
    if _TEXT_TOOL_MARKUP.search(text):
        return True
    for name in offered_names:
        n = re.escape(name)
        if (re.search(r'"name"\s*:\s*"' + n + '"', text)
                or re.search(r"\b" + n + r"\s*\(\s*\{", text)):
            return True
    return False


def _parse_anthropic(data, model, offered_names=()):
    """Read one Messages API response into a ChatTurn (thinking blocks ignored)."""
    blocks = data.get("content") or []
    if not isinstance(blocks, list):
        blocks = []
    texts, tool_calls, bad_args = [], [], []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif b.get("type") == "tool_use":
            cid = b.get("id") or ""
            args = b.get("input")
            if not isinstance(args, dict):
                args = {}
                bad_args.append(cid)
            tool_calls.append({"id": cid, "name": b.get("name") or "", "arguments": args})
    turn = ChatTurn(tool_calls=tool_calls, text="".join(texts),
                    usage=_anthropic_usage(data.get("usage")),
                    model=data.get("model") or model, bad_args=bad_args)
    stop = data.get("stop_reason")
    if stop == "refusal":
        turn.error = "refusal"
    elif stop == "max_tokens" and not tool_calls and not turn.text.strip():
        turn.error = "max_tokens before any text"
    elif (offered_names and not tool_calls and turn.text.strip()
          and _tool_call_in_text(turn.text, offered_names)):
        # Succeeding here would hand the loop an "answer" whose tool never ran.
        turn.error = "tool call written as text"
    return turn


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


def _log(turn, caller, elapsed, n_tools_offered, provider="openrouter"):
    # Telemetry must never cost the caller its turn.
    try:
        usage = turn.usage or {}
        completion_detail = usage.get("completion_tokens_details") or {}
        prompt_detail = usage.get("prompt_tokens_details") or {}
        log_usage(provider, turn.model, caller, elapsed,
                  prompt_tokens=usage.get("prompt_tokens"),
                  completion_tokens=usage.get("completion_tokens"),
                  reasoning_tokens=completion_detail.get("reasoning_tokens"),
                  cache_read_tokens=prompt_detail.get("cached_tokens"),
                  cache_write_tokens=usage.get("cache_creation_input_tokens"))
        # Names and counts only. Arguments and text are conversation data.
        logger.info("LLM_TOOLS provider=%s caller=%s model=%s tools_offered=%d "
                    "tool_calls=%s n_bad_args=%d text_chars=%d ms=%d",
                    provider, caller, turn.model, n_tools_offered,
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

    A Claude chain entry (`claude-*` / `anthropic/*`) is sent to api.anthropic.com
    with ANTHROPIC_API_KEY, never to OpenRouter; with that key unset the call ends
    with an error on that entry (no OpenRouter fallback, no next entry). Every other
    entry goes to OpenRouter as before. The fallback rules are the same on both.
    """
    env_primary = _primary_model()
    chain = _model_chain(model or env_primary)
    if not any(is_claude(m) for m in chain) and not corroboration_client.api_key():
        logger.error("llm_tools: API key not set caller=%s", caller)
        return ChatTurn(error="API key not configured")

    if deadline is not None and deadline - _now() < MIN_TIME_LEFT:
        return ChatTurn(error="no time left", timed_out=True)

    # Pin and reasoning belong to the configured judge model: never to a
    # fallback, nor to a model a caller names in its place. OpenRouter only.
    primary_extras = {}
    if chain[0] == env_primary:
        pin, reasoning = _provider_pin(), _reasoning()
        if pin:
            primary_extras["provider"] = pin
        if reasoning:
            primary_extras["reasoning"] = reasoning
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
        claude = is_claude(current)
        if claude:
            akey = _anthropic_key()
            if not akey:
                logger.error("llm_tools: ANTHROPIC_API_KEY not set caller=%s model=%s",
                             caller, current)
                return ChatTurn(error="ANTHROPIC_API_KEY not configured", model=current)
            url, headers = ANTHROPIC_URL, _anthropic_headers(akey)
            payload = _anthropic_payload(native_claude_id(current), messages, tools,
                                         choice, max_tokens)
        else:
            key = corroboration_client.api_key()
            if not key:
                logger.error("llm_tools: API key not set caller=%s", caller)
                return ChatTurn(error="API key not configured", model=current)
            url = corroboration_client.API_URL
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer " + key}
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
            resp = http.request("POST", url, body=json.dumps(payload), headers=headers,
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
            if claude:
                names = [(t.get("function") or {}).get("name") for t in tools or []
                         if isinstance(t, dict)]
                turn = _parse_anthropic(data, native_claude_id(current),
                                        [n for n in names if n])
            else:
                turn = _parse(data, current)
            if turn.error is None and not turn.tool_calls and not turn.text.strip():
                # A 200 with nothing in it is a failure, not an empty answer.
                turn.error = "empty completion"
            _log(turn, caller, _now() - started, len(tools or []),
                 provider="anthropic" if claude else "openrouter")
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
    walks its own chain and checks each answer's family itself.

    A Claude entry goes to api.anthropic.com exactly as in `chat` (ANTHROPIC_API_KEY,
    unset = an error, never OpenRouter), with a JSON-only system instruction in place
    of `response_format`."""
    env_primary = _primary_model()
    chain = _model_chain(model or env_primary) if judge_routing else [model or env_primary]
    if not any(is_claude(m) for m in chain) and not corroboration_client.api_key():
        logger.error("llm_tools: API key not set caller=%s", caller)
        return ChatTurn(error="API key not configured")
    if deadline is not None and deadline - _now() < MIN_TIME_LEFT:
        return ChatTurn(error="no time left", timed_out=True)
    primary_extras = {}
    if judge_routing and chain[0] == env_primary:
        pin, reasoning = _provider_pin(), _reasoning()
        if pin:
            primary_extras["provider"] = pin
        if reasoning:
            primary_extras["reasoning"] = reasoning
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
        claude = is_claude(current)
        if claude:
            akey = _anthropic_key()
            if not akey:
                logger.error("llm_tools json: ANTHROPIC_API_KEY not set caller=%s model=%s",
                             caller, current)
                return ChatTurn(error="ANTHROPIC_API_KEY not configured", model=current,
                                requested_model="")
            url, headers = ANTHROPIC_URL, _anthropic_headers(akey)
            payload = _anthropic_payload(native_claude_id(current),
                                         [{"role": "user", "content": prompt}],
                                         None, None, max_tokens, json_mode=True)
        else:
            key = corroboration_client.api_key()
            if not key:
                logger.error("llm_tools: API key not set caller=%s", caller)
                return ChatTurn(error="API key not configured", model=current,
                                requested_model="")
            url = corroboration_client.API_URL
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
            payload = {"model": current,
                       "messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object"},
                       "max_tokens": max_tokens + llm_utils.REASONING_HEADROOM_TOKENS}
            if idx == 0:
                payload.update(primary_extras)
        attempt_start = _now()
        try:
            resp = http.request("POST", url, body=json.dumps(payload), headers=headers,
                                timeout=timeout)
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
            if claude:
                turn = _parse_anthropic(data, native_claude_id(current))
            else:
                turn = _parse(data, current)
            turn.requested_model = current
            if turn.error is None and not turn.text.strip():
                turn.error = "empty completion"
            _log(turn, caller, _now() - started, 0,
                 provider="anthropic" if claude else "openrouter")
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
