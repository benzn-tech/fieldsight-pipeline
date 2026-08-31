"""The Anthropic client the corroboration steps use, and only they use.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.4

`llm_utils.call_llm` is the repo's shared client and is wrong for this feature in
four specific ways, none of which are its fault -- it was built for a background
pipeline where a slow answer is fine:

1. Its timeout is a module constant. A caller that must finish inside a 24-second
   hard stop cannot say so, and 45 seconds of it lands on a proxy that dies at 30.
2. It retries up to four times. Four attempts of a step budgeted at 12 seconds is
   not a retry policy, it is a way of guaranteeing the deadline is missed.
3. It sends no `tools`, so it cannot run a web search at all.
4. Its Anthropic branch keeps only `type == "text"` blocks. Even if a search ran,
   every result and every citation would be dropped on the floor -- silently, with
   a plausible-looking answer still coming back. That failure would look exactly
   like "the web had nothing to say".

And on TEST `LLM_PROVIDER=qwen`, so the shared client would route to DashScope
there and Anthropic in prod. A feature whose whole output is "what the open web
says" cannot be exercised against a different model in the environment where it
gets tested. This client is Anthropic on every stack; `ANTHROPIC_API_KEY` is
already on `AskAgentFunction` regardless of `LlmProvider`.

## Two model-behaviour choices that are load-bearing, not preferences

**Thinking stays on, and effort is what gets turned down.** Claude Opus 5 thinks
by default, and the obvious way to protect a 12-second budget -- send
`thinking: {"type": "disabled"}` -- is the one change that can break the search
step outright: with thinking disabled the model sometimes writes a tool call into
its visible text instead of emitting a `server_tool_use` block. The turn succeeds,
the search never runs, nothing raises, and the caller sees `not_found`. Low effort
buys most of the same latency back without that failure mode.

**`max_tokens` covers thinking as well as the answer.** A ceiling sized for the
prose alone truncates mid-sentence once the model thinks first.
"""
from __future__ import annotations

import json
import logging
import os
import time

import urllib3

logger = logging.getLogger()

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Overridable so a latency regression can be answered without a deploy. The
# default is haiku because every step measured faster on it than the answer could
# afford elsewhere (see WEB_SEARCH_TOOL), and because TEST and prod must run the
# same model or the tests say nothing about what ships.
DEFAULT_MODEL = os.environ.get("CORROBORATION_MODEL", "claude-haiku-4-5")

# The basic search tool, chosen by measurement rather than by capability.
#
# The dynamic-filtering variant (`web_search_20260209`) is the better tool: it
# filters results before they reach the model's context. It also requires Opus
# 4.6+ / Sonnet 4.6+, and on the real API that pairing takes **17 seconds** for a
# single entity -- against a 12-second budget inside a 24-second hard stop. Three
# runs, three timeouts. `max_uses: 1` did not help (19.9 s), because what costs
# the time is the model, not the number of searches: the same model with no tool
# at all still takes 9.3 s.
#
# Haiku with this basic tool answers the same prompt in **4.2-4.7 s** with ten
# results. That is the whole reason for the downgrade. The cost is real and
# stated: raw results reach the model's context instead of a filtered set, which
# is affordable at one to three entities and would not be at thirty.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}

# Below this there is no time for a request to do anything but time out, and
# spending the caller's remaining budget on a doomed attempt is worse than
# reporting that the budget ran out.
MIN_USEFUL_TIMEOUT = 2.0

_RETRYABLE = {429, 500, 502, 503, 504, 529}

# `output_config.effort` is a 400 on Haiku 4.5, and the steps that use haiku are
# the cheap ones where a caller is most likely to leave the default in place.
# Dropped here rather than left to each call site, because the failure has no
# local symptom: the request is well-formed, the model is real, and the only
# evidence is a 400 in prod.
_NO_EFFORT_PREFIXES = ("claude-haiku",)


def _supports_effort(model: str) -> bool:
    return not str(model or "").startswith(_NO_EFFORT_PREFIXES)


class SearchResult:
    """One page the search step saw. `url` is what a card cites."""

    __slots__ = ("url", "title", "page_age")

    def __init__(self, url, title, page_age=None):
        self.url, self.title, self.page_age = url, title, page_age

    def __repr__(self):
        return f"SearchResult({self.url!r}, {self.title!r})"

    def __eq__(self, other):
        return (isinstance(other, SearchResult) and self.url == other.url
                and self.title == other.title and self.page_age == other.page_age)


class Reply:
    """What came back, in the shape the corroboration steps need.

    `error` being set and `text` being empty are different facts and both are
    reported. A step that timed out and a step whose model found nothing produce
    different states in §5.2, and collapsing them would make `not_found` mean
    "either the web disagreed or our proxy hiccuped" -- which is not a finding a
    reader can act on.
    """

    __slots__ = ("text", "search_results", "citations", "stop_reason",
                 "error", "elapsed", "searched", "search_error")

    def __init__(self, text="", search_results=None, citations=None,
                 stop_reason=None, error=None, elapsed=0.0, searched=False,
                 search_error=None):
        self.text = text
        self.search_results = search_results or []
        self.citations = citations or []
        self.stop_reason = stop_reason
        self.error = error
        self.elapsed = elapsed
        self.searched = searched
        # The search itself failed while the request succeeded. Kept apart from
        # `error` because the caller may still have a usable answer, and apart
        # from "no results" because that one is a claim about the world.
        self.search_error = search_error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self):
        return (f"Reply(ok={self.ok}, searched={self.searched}, "
                f"results={len(self.search_results)}, error={self.error!r}, "
                f"search_error={self.search_error!r}, "
                f"elapsed={self.elapsed:.2f}s)")


def _parse(data) -> Reply:
    """Pull text, search results and citations out of one response body.

    A `web_search_tool_result` block carries either a list of results or a single
    error object, and the two are told apart by shape rather than by a status
    code. Treating the error object as a result list is how a search failure
    becomes "the web returned nothing about this company", which reads to a user
    as a finding rather than a fault.
    """
    text_parts, results, citations = [], [], []
    searched = False
    search_error = None

    for block in data.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")

        if btype == "text":
            text_parts.append(block.get("text") or "")
            for cite in block.get("citations") or []:
                if isinstance(cite, dict):
                    citations.append(cite)

        elif btype == "server_tool_use":
            if block.get("name") == "web_search":
                searched = True

        elif btype == "web_search_tool_result":
            searched = True
            content = block.get("content")
            if isinstance(content, dict):
                # The error shape: {"type": "web_search_tool_result_error",
                #                   "error_code": "max_uses_exceeded"}. It is a
                # dict where a success is a list, and that is the only signal --
                # the HTTP status is 200 either way.
                search_error = content.get("error_code") or "web_search_failed"
                logger.warning("corroboration: web_search returned an error: %s",
                               search_error)
                continue
            for item in content or []:
                if isinstance(item, dict) and item.get("type") == "web_search_result":
                    results.append(SearchResult(item.get("url"),
                                                item.get("title"),
                                                item.get("page_age")))

    return Reply(text="\n".join(p for p in text_parts if p),
                 search_results=results,
                 citations=citations,
                 stop_reason=data.get("stop_reason"),
                 searched=searched,
                 search_error=search_error)


def call(prompt, *, timeout, model=None, max_tokens=1024, tools=None,
         effort="low", system=None, retry_budget=None) -> Reply:
    """One Anthropic call, bounded by `timeout` seconds, with at most one retry.

    `timeout` is per attempt and is not a suggestion: it is the caller's share of
    a hard stop that belongs to a reader waiting on an answer.

    `retry_budget` is the seconds still available *after* this attempt. A retry
    happens only when the failure was retryable AND that number covers another
    full attempt. The default is no retry at all, because a caller that has not
    thought about its budget must not be allowed to spend it twice.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Not raising: a missing key must cost the reader the corroboration
        # cards, never the answer they asked for.
        logger.error("corroboration: ANTHROPIC_API_KEY not set")
        return Reply(error="ANTHROPIC_API_KEY not configured")

    if timeout is None or timeout < MIN_USEFUL_TIMEOUT:
        return Reply(error=f"no time left ({timeout}s)")

    chosen_model = model or DEFAULT_MODEL
    payload = {
        "model": chosen_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    if effort and _supports_effort(chosen_model):
        payload["output_config"] = {"effort": effort}

    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    http = urllib3.PoolManager()
    attempts_left = 2 if (retry_budget or 0) >= timeout + MIN_USEFUL_TIMEOUT else 1
    started = time.time()
    last_error = None

    for attempt in range(attempts_left):
        attempt_start = time.time()
        try:
            resp = http.request("POST", API_URL, body=body, headers=headers,
                                timeout=timeout)
        except Exception as e:                    # noqa: BLE001 - all of it is a miss
            last_error = f"{type(e).__name__}: {e}"
            logger.warning("corroboration: attempt %d failed: %s",
                           attempt + 1, last_error)
            continue

        elapsed = time.time() - attempt_start
        if resp.status == 200:
            reply = _parse(json.loads(resp.data.decode("utf-8")))
            reply.elapsed = time.time() - started
            if reply.stop_reason == "refusal":
                # A 200 whose content is empty. Reported rather than read as
                # "nothing found", which it is not.
                reply.error = "refused"
            return reply

        if resp.status in _RETRYABLE and attempt + 1 < attempts_left:
            last_error = f"HTTP {resp.status}"
            logger.warning("corroboration: retryable %s after %.2fs",
                           last_error, elapsed)
            continue

        try:
            detail = json.loads(resp.data.decode("utf-8"))
            last_error = detail.get("error", {}).get("message") or f"HTTP {resp.status}"
        except Exception:                         # noqa: BLE001 - body may be html
            last_error = f"HTTP {resp.status}"
        break

    return Reply(error=last_error or "no response", elapsed=time.time() - started)
