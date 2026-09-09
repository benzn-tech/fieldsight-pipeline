"""The client the corroboration steps use, and only they use.

Spec: docs/superpowers/specs/2026-09-08-corroboration-off-anthropic-design.md §2
Supersedes the vendor half of the 2026-08-31 design; its honesty rules stand.

`llm_utils.call_llm` is the repo's shared client and is wrong for this feature in
four specific ways, none of which are its fault -- it was built for a background
pipeline where a slow answer is fine:

1. Its timeout is a module constant. A caller that must finish inside a hard stop
   cannot say so, and 45 seconds of it lands on a proxy that dies at 30.
2. It retries up to four times. Four attempts of a step budgeted at 13 seconds is
   not a retry policy, it is a way of guaranteeing the deadline is missed.
3. It sends no `plugins`, so it cannot run a web search at all.
4. It keeps only the text, so every source and citation would be dropped on the
   floor -- silently, with a plausible-looking answer still coming back. That
   failure would look exactly like "the web had nothing to say".

## Why this vendor

Measured 2026-09-08, one entity and one claim, n=3 per configuration, because
temperature 0 is not deterministic on this endpoint:

    gemini-3.8-flash + plugins:[{"id":"web"}]   10.3 / 11.4 / 11.3 s   6/4/7 sources
    gemini-3.8-flash:online                     12.4 / 13.0 / 12.6 s   over budget
    muse-spark-1.3-contributor + web plugin     16.1 / 16.5 / 18.2 s   ZERO sources

The `:online` suffix is the same model on the same question and was over budget
on every run, so the plugin form is not a style choice.

muse-spark is disqualified for lying rather than for being slow. It returned
HTTP 200, 1200 tokens, no sources, and prose reading "I'll search the web to
verify... Initial results support the claim." Nothing raised. A model's account
of its own tool use is not evidence, which is why `searched` here is derived
from whether results came back and never from what the text says.

## The 200 that means failure

Measured the same day: this model with NO plugin configured returns
`completion_tokens: 0` and an empty content string -- a 200 with nothing in it.
A misconfigured deploy therefore fails as an empty answer rather than as an
error, so empty content is treated here as a failure. It has to be: the caller's
next step would otherwise read "" as findings.

## Reasoning effort

`reasoning: {"effort": ...}` is accepted alongside `plugins`. Measured on the
search prompt: effort low spent 0 reasoning tokens in 9.4 s, effort high spent
711 in 13.6 s, past the search budget. Low is the default for that reason rather
than for cost. The Anthropic client had to special-case models that 400 on an
effort field; nothing measured here does, and re-inventing that guard without a
model that fails would be guessing.
"""
from __future__ import annotations

import json
import logging
import os
import time

import urllib3

logger = logging.getLogger()

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# The search runs on the provider's side and is requested per call, not declared
# as a tool the model may choose. `_search` is the only caller that asks for it;
# a test asserts the cheap steps never do, because a classification step that
# quietly searched would spend the search step's budget a second time.
WEB_PLUGIN = [{"id": "web"}]

DEFAULT_MODEL = os.environ.get("CORROBORATION_MODEL", "google/gemini-3.8-flash")

# Below this there is no time for a request to do anything but time out, and
# spending the caller's remaining budget on a doomed attempt is worse than
# reporting that the budget ran out.
MIN_USEFUL_TIMEOUT = 2.0

_RETRYABLE = {429, 500, 502, 503, 504, 529}


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
    different states, and collapsing them would make `not_found` mean "either the
    web disagreed or our proxy hiccuped" -- which is not a finding a reader can
    act on.

    `searched` is the one a caller must not skip. It says the open web was
    actually consulted, and it is derived from results rather than from prose
    precisely because a model will describe a search it never ran.
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
        # Kept because `corroborate()` reads it. This vendor signals a failed
        # search by returning no annotations rather than by an error object, so
        # `searched` already covers that case; the field stays so a vendor that
        # does distinguish the two has somewhere to say so.
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
    """Read one OpenAI-shaped completion.

    Web results arrive as `annotations` of type `url_citation` on the assistant
    message. An annotation carrying no URL is not a source and is dropped rather
    than counted: `searched` is what the caller trusts, so padding it with
    unusable entries would defeat the guard it exists for.
    """
    choices = data.get("choices") or []
    choice = choices[0] if isinstance(choices, list) and choices else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    annotations = message.get("annotations") or []
    if not isinstance(annotations, list):
        annotations = []

    results = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        cit = ann.get("url_citation") or {}
        url = cit.get("url") if isinstance(cit, dict) else None
        if url:
            results.append(SearchResult(url, cit.get("title")))

    return Reply(text=message.get("content") or "",
                 search_results=results,
                 citations=list(annotations),
                 stop_reason=choice.get("finish_reason"),
                 searched=bool(results))


def call(prompt, *, timeout, model=None, max_tokens=1024, web=False,
         effort="low", system=None, retry_budget=None) -> Reply:
    """One call, bounded by `timeout` seconds, with at most one retry.

    `timeout` is per attempt and is not a suggestion: it is the caller's share of
    a hard stop that belongs to a reader waiting on an answer.

    `web` asks the provider to search. It is a boolean rather than a tool
    definition because the search belongs to the provider, not to the model.

    `retry_budget` is the seconds still available *after* this attempt. A retry
    happens only when the failure was retryable AND that number covers another
    full attempt. The default is no retry at all, because a caller that has not
    thought about its budget must not be allowed to spend it twice.
    """
    api_key = os.environ.get("CORROBORATION_API_KEY")
    if not api_key:
        # Not raising: a missing key must cost the reader the corroboration
        # cards, never the answer they asked for.
        logger.error("corroboration: CORROBORATION_API_KEY not set")
        return Reply(error="CORROBORATION_API_KEY not configured")

    if timeout is None or timeout < MIN_USEFUL_TIMEOUT:
        return Reply(error=f"no time left ({timeout}s)")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model or DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if web:
        payload["plugins"] = WEB_PLUGIN
    if effort:
        payload["reasoning"] = {"effort": effort}

    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
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
            if not (reply.text or "").strip():
                # A 200 carrying nothing. Measured shape: with no plugin
                # configured this model answers `completion_tokens: 0` and an
                # empty string. Passed on as findings that reads as "the web
                # said nothing", which is a claim about the world where the
                # truth is a claim about our configuration.
                reply.error = "empty completion"
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
