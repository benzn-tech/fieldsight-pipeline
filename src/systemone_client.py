"""Client for the Jev (TypeSafe System One) Decisions endpoint.

Track A of the event-graph programme: an offline shadow evaluation of Jev
against today's LLM gates. This module is the only piece of Track A a later
track (C) will reuse in production, so it is written in the style of the
repo's other hand-rolled provider clients (`llm_utils._call_qwen`,
`corroboration_client.call`) rather than as a one-off script: urllib only,
no SDK, one retry on a retryable status, and a 200 with nothing usable in it
is treated as a failure rather than passed on to a caller as an answer.

## The endpoint

Owner decision 2026-09-24: use the OpenRouter route. It is a dedicated
**Decisions** endpoint, not chat completions -- an OpenAI-compatible SDK
cannot call it. The body is the same `model` / `state` / `questions` shape as
TypeSafe's own API, and every *question* is evaluated in parallel and in
isolation within one request. Three answer shapes come back, keyed by
question name:

    noul:   {"noul": 0.0-1.0}
    choice: {"choice": <option>, "probabilities": {...}, "confidence": c}
    score:  {"score": s, "probabilities": {...}}

Re-verified 2026-09-27 against `openrouter.ai/docs/guides/community/jev` and
`docs.typesafe.ai/api.md` (both reachable): endpoint URL, the three answer
shapes above, and the alpha/32k-context framing are confirmed from the public
docs. The usage object's field names were NOT consistently confirmed -- the
OpenRouter page names `usage.cost` and mentions `prompt_tokens` without a full
schema, while the TypeSafe direct-API fragment names `input_tokens` /
`output_tokens` instead. Both are read defensively below (see `_usage_tokens`)
so neither route's usage block is silently dropped.

The OpenRouter listing caps the whole request at 32k tokens (TypeSafe direct
says 64k); the pre-send size check below is deliberately sized for the
smaller, 32k number regardless of which `DECISIONS_URL` is configured, so a
future route switch cannot silently raise the risk of a 413 or a
provider-side truncation -- exactly BUG-15's shape (a limit that stopped
matching what it was guarding).

## What this module refuses to do

- Never logs `state` or `questions` -- that is customer/site text. Only
  counts, tokens, latency and identifiers reach `llm_usage.log_usage` or any
  log line here.
- Never falls back to another env var for the API key: `DECISIONS_API_KEY`
  only, per the controller's 2026-09-24 ruling. On OpenRouter this happens to
  be the same secret `OPENROUTER_API_KEY` already funds (`corroboration_client`
  uses the analogous `CORROBORATION_API_KEY`), but a silent fallback here
  would let a missing `DECISIONS_API_KEY` quietly spend a DIFFERENT budget
  instead of failing loudly.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

from llm_usage import log_usage

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"

# The OpenRouter route's model id carries the `~typesafe/` provider prefix;
# TypeSafe's own API does not. Each route's own default, not one default
# reused for both, because sending the wrong one would be a 404, not a
# graceful fallback.
_DEFAULT_MODEL_OPENROUTER = "~typesafe/jev-latest"
_DEFAULT_MODEL_TYPESAFE = "jev-latest"

# One retry, on a retryable status, honouring Retry-After when the vendor
# sends one -- mirrors `llm_utils._call_qwen` / `_post_with_retry` rather than
# reinventing a backoff ladder for a client this small.
MAX_ATTEMPTS = 2
DEFAULT_BACKOFF_SECONDS = 1.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

# Conservative pre-send guard. The OpenRouter listing caps the whole request
# (state + questions) at 32k tokens; 20k leaves headroom for the envelope
# (question schemas, JSON structure) the 4-chars/token estimate does not
# itself account for. A 413 or a silent provider-side truncation is exactly
# the BUG-15 shape this refuses to risk.
MAX_ESTIMATED_TOKENS = 20_000
CHARS_PER_TOKEN = 4


def _normalise_answer(raw: dict) -> dict:
    """Map the endpoint's per-question answer onto the interface this module
    promises callers: `{"noul": p}` / `{"choice": x, "probabilities": {...},
    "confidence": c}` / `{"score": s, "probabilities": {...}}`.

    The wire shape carries a `"type"` discriminator (confirmed against
    `docs.typesafe.ai/api.md`); dropping it here keeps every caller of
    `ask()` from having to know the endpoint's own vocabulary for its three
    answer kinds.
    """
    kind = raw.get("type")
    if kind == "noul":
        return {"noul": raw.get("noul")}
    if kind == "choice":
        return {"choice": raw.get("choice"),
                "probabilities": raw.get("probabilities") or {},
                "confidence": raw.get("confidence")}
    if kind == "score":
        return {"score": raw.get("score"),
                "probabilities": raw.get("probabilities") or {}}
    # An answer kind this client does not recognise yet. Passed through
    # rather than dropped -- silently discarding a real answer would be
    # worse than handing the caller an unfamiliar shape it can at least see.
    return raw


class SystemOneError(Exception):
    """Raised for every failure this client recognises as a failure --
    including a 200 whose `answers` map is empty, per the `llm_utils`
    empty-answer precedent (`llm_utils.py:555-564`): a caller that reads an
    empty dict as "no findings" is reading our configuration, not the
    model's judgment."""


def _is_typesafe_direct(url: str) -> bool:
    return "api.typesafe.ai" in url


def _default_model(url: str) -> str:
    return _DEFAULT_MODEL_TYPESAFE if _is_typesafe_direct(url) else _DEFAULT_MODEL_OPENROUTER


def _provider_label(url: str) -> str:
    return "typesafe" if _is_typesafe_direct(url) else "openrouter-decisions"


def _estimate_tokens(state, questions) -> int:
    """4 chars/token over the serialised request body -- conservative and
    cheap, not a real tokenizer. Sized to fail closed: a serialisation this
    coarse is more likely to over-count than under-count real requests, and
    over-counting here only costs a refused call, never a truncated one."""
    serialised = json.dumps({"state": state, "questions": questions},
                             ensure_ascii=False, default=str)
    return len(serialised) // CHARS_PER_TOKEN


def _usage_tokens(usage: dict):
    """Read prompt/completion tokens defensively.

    Not confirmed which field-name convention this endpoint actually sends:
    public docs disagree (`prompt_tokens`/`completion_tokens` vs
    `input_tokens`/`output_tokens`) and this module has not yet been run
    against the live endpoint. Trying both means neither route's usage block
    silently reads as absent."""
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    return prompt_tokens, completion_tokens


def _post(url: str, body: bytes, headers: dict, timeout: float):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def ask(state, questions: dict, *, model=None, timeout=None, caller=None) -> dict:
    """Evaluate `questions` against `state` in one Decisions call.

    `state` is the record under evaluation (a string or a JSON-serialisable
    dict) and `questions` is the map of named question definitions the
    endpoint evaluates in parallel and in isolation. Returns
    `{"answers": {...}, "usage": {...}, "latency_ms": int}`.

    Raises `SystemOneError` for: a missing API key, an oversize request
    (refused before any network call), a non-2xx response after the retry
    budget is spent, or a 200 whose `answers` map is empty.
    """
    api_key = os.environ.get("DECISIONS_API_KEY")
    if not api_key:
        raise SystemOneError("DECISIONS_API_KEY not configured")

    estimated_tokens = _estimate_tokens(state, questions)
    if estimated_tokens > MAX_ESTIMATED_TOKENS:
        raise SystemOneError(
            f"state + questions estimated at {estimated_tokens} tokens, "
            f"over the {MAX_ESTIMATED_TOKENS}-token pre-send limit -- refusing "
            "before sending rather than risking a 413 or a provider-side "
            "truncation")

    # Read per call, not cached at import time: `DECISIONS_URL` is a
    # deliberately switchable knob (the endpoint is alpha) and a module-level
    # constant would freeze whichever value was set when this module first
    # imported, in this process, for the rest of the process's life.
    url = os.environ.get("DECISIONS_URL", DEFAULT_OPENROUTER_URL)
    resolved_model = model or _default_model(url)
    caller = caller or "unknown"

    payload = {"model": resolved_model, "state": state, "questions": questions}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    }
    http_timeout = timeout if timeout is not None else float(
        os.environ.get("DECISIONS_HTTP_TIMEOUT", "10"))

    started = time.time()
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with _post(url, body, headers, http_timeout) as resp:
                status = resp.status
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                data = json.loads(e.read().decode("utf-8"))
            except Exception:  # noqa: BLE001 - error body may not be JSON
                data = {}
            if status == 413:
                raise SystemOneError(
                    f"HTTP 413 payload too large (estimated {estimated_tokens} "
                    "tokens)") from e
            if status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = DEFAULT_BACKOFF_SECONDS
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                last_error = f"HTTP {status}"
                logger.warning("systemone: retryable %s, backing off %.1fs",
                               last_error, wait)
                if wait > 0:
                    time.sleep(wait)
                continue
            detail = (data or {}).get("error")
            if isinstance(detail, dict):
                detail = detail.get("message")
            raise SystemOneError(
                f"HTTP {status}" + (f": {detail}" if detail else "")) from e
        except urllib.error.URLError as e:
            last_error = str(e.reason)
            if attempt < MAX_ATTEMPTS - 1:
                logger.warning("systemone: request failed (%s), retrying", last_error)
                time.sleep(DEFAULT_BACKOFF_SECONDS)
                continue
            raise SystemOneError(f"request failed: {last_error}") from e

        # 2xx reached this point.
        raw_answers = data.get("answers") or {}
        if not raw_answers:
            # Same posture as `llm_utils`'s empty-answer precedent
            # (llm_utils.py:555-564): a 200 with nothing usable in it is a
            # failure, not an answer with zero findings.
            raise SystemOneError(
                "empty answers map from Decisions endpoint (HTTP 200)")
        answers = {name: _normalise_answer(raw) if isinstance(raw, dict) else raw
                   for name, raw in raw_answers.items()}

        elapsed = time.time() - started
        usage = data.get("usage") or {}
        prompt_tokens, completion_tokens = _usage_tokens(usage)
        try:
            log_usage(
                _provider_label(url), data.get("model") or resolved_model,
                caller, elapsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        except Exception:  # noqa: BLE001 - telemetry must never break the call
            logger.warning("LLM_USAGE logging failed (systemone)", exc_info=True)

        return {
            "answers": answers,
            "usage": usage,
            "latency_ms": int(elapsed * 1000),
        }

    # Every attempt hit a retryable status and the budget ran out.
    raise SystemOneError(last_error or "no response from Decisions endpoint")
