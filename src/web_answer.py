"""Answer from the open web when the records cannot.

Spec: docs/superpowers/specs/2026-09-10-answering-from-the-open-web-design.md
Plan: docs/superpowers/plans/2026-09-11-answering-from-the-open-web.md

This is the SMALLEST end-to-end version of that plan: one route, no concurrency,
no separate door for an empty corpus, no UI beyond a block the client may render.
It exists so the feature can be judged from use rather than from a document --
five review rounds produced a design and no working path, and a design nobody has
felt is a design nobody can correct.

## The breakdown this replaced, and why

The first build broke the question into gate-screened entity NAMES, searched
those, and composed an answer from the findings -- three model calls, so nothing
but names would reach a search engine.

It failed on the most ordinary question there is. Asked *"Which New Zealand
standard covers timber design?"*, subject extraction returned NOTHING on three
runs out of four: the question contains no standard number, because **the number
is the thing the asker does not know, which is why they are asking.**

So the question goes out whole, and `question_admission` decides whether it may
-- same protection, admission instead of dissection. Two calls instead of four.

## Why the verdict runs BEFORE the grounded answer

Answering from records first and looking up second does not fit. Measured worst
case from the HTTP request: retrieval 1.36 + synthesis 11.9 + verdict 3.2 +
lookup 11.4 = 27.9s against API Gateway's 29s, before anything goes wrong.
Asking first turns the verdict into a router and both branches fit.

The cost is the verdict call on every question, including the majority the
corpus answers. The plan's concurrent shape removes it; this is the small
version.

## What crosses which boundary

  * the SEARCH ENGINE receives the QUESTION, once `question_admission` has
    passed it -- never the excerpts, never a paragraph of context
  * the LLM PROVIDER receives the question and the excerpts, which it already
    receives on every `/ask`

"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import corroboration_client as client
import question_admission

logger = logging.getLogger()

# Measured against the deployed vendor: the verdict is a classification
# (2.4-3.3s over four questions, 6.8s once) and the lookup is a search plus an
# answer (10.3-11.4s). 8 + 14 = 22 plus the 2s floor fits the 27s stop, which
# fits API Gateway's 29s.
VERDICT_BUDGET = 8.0
WEB_BUDGET = 14.0
HARD_STOP_SECONDS = float(os.environ.get("WEB_ANSWER_HARD_STOP", "27"))

CHEAP_MODEL = os.environ.get("CORROBORATION_CHEAP_MODEL", "google/gemini-3.8-flash")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)



def enabled():
    """Read at call time, never at import. A flag captured at import survives a
    warm container after the stack has been redeployed with it off."""
    return os.environ.get("ENABLE_WEB_ANSWER", "false").lower() == "true"


VERDICT_PROMPT = """Do the excerpts below answer the question?

Answer true only if a reader would get what they asked for from these excerpts
alone. Excerpts merely about the same site or the same day are not an answer.

Return only JSON, no prose: {{"answered": true|false}}

## Question
{question}

## Excerpts
{excerpts}
"""

WEB_PROMPT = """Search the open web and answer this question, for a reader working
on a New Zealand construction site.

Report only what public sources say. Do not speculate, and do not fill a gap with
general knowledge -- if the sources do not answer the question, say so plainly
rather than guessing.

Name the source of each fact you use. Be brief.

## Question
{question}
"""


def _loads(text):
    try:
        return json.loads(_FENCE.sub("", text or "").strip())
    except Exception:                             # noqa: BLE001 - any shape but ours
        return None


def _excerpt_block(chunks, limit=5):
    """What the gate judges. Deliberately the same text the grounded answer is
    built from -- a gate reading headers alone would be judging titles, and a
    topic title says nothing about whether a question was answered."""
    out = []
    for i, c in enumerate(chunks[:limit], start=1):
        header = " . ".join(str(p) for p in (
            c.get("site_name"), c.get("report_date"), c.get("topic_title")) if p)
        out.append("[{n}] {h}\n```\n{t}\n```".format(
            n=i, h=header or "?", t=c.get("chunk_text") or ""))
    return "\n".join(out)


def answer(question, chunks, *, clock=time.monotonic):
    """A web-answer block, or None to leave the grounded path alone.

    `chunks` may be empty: retrieval returning nothing IS the verdict, so that
    case skips straight to the lookup rather than asking a model whether an
    empty set answered anything. That is the case the first build missed
    entirely -- it returned a fixed "no relevant records" string above this hook
    and never reached it.

    Never raises. Every failure returns either None or a body whose flags say
    which -- "we found nothing", "we may not ask" and "we did not look" are
    three different sentences and only one of them is about the world.
    """
    if not enabled() or not question:
        return None

    started = clock()

    def left():
        return HARD_STOP_SECONDS - (clock() - started)

    if chunks:
        verdict, err = _verdict(question, chunks, min(VERDICT_BUDGET, left()))
        if err or verdict is None:
            # Fail closed. An unreadable verdict is not permission to search.
            logger.warning("web answer: verdict failed: %s", err)
            return None
        if verdict.get("answered") is True:
            logger.info("web answer: the records answer it; no lookup")
            return None
    else:
        logger.info("web answer: nothing retrieved; the records cannot answer it")

    refused = question_admission.screen(question, chunks)
    if refused:
        # Loud, and returned rather than swallowed: a refusal nobody can see
        # cannot be measured.
        logger.info("web answer: question not sent -- %s", refused)
        return _spent(refused=refused)

    if left() < client.MIN_USEFUL_TIMEOUT:
        return _spent(timed_out=True)

    found = _ask_the_web(question, min(WEB_BUDGET, left()))
    if not found.ok:
        logger.warning("web answer: lookup failed: %s (timed_out=%s)",
                       found.error, found.timed_out)
        return _spent(timed_out=bool(found.timed_out),
                      failed=not found.timed_out)
    if not found.searched:
        # Prose describing a search is not evidence one happened. Measured on a
        # second vendor: 200 OK, no results, a paragraph asserting findings.
        logger.warning("web answer: no web results came back")
        return _spent()

    logger.info("web answer: answered from %d sources", len(found.search_results))
    return {"answer": (found.text or "").strip(),
            "sources": _sources(found),
            "searched": True, "timed_out": False, "failed": False,
            "refused": None}


def _spent(*, timed_out=False, failed=False, refused=None):
    """A body that says what happened, never an empty one that reads as
    'the web had nothing to say'. Four states, because they lead four different
    places: we may not ask, we did not look, we ran out of time, it broke."""
    return {"answer": None, "sources": [], "searched": False,
            "timed_out": timed_out, "failed": failed, "refused": refused}


def _verdict(question, chunks, budget):
    reply = client.call(
        VERDICT_PROMPT.format(question=question, excerpts=_excerpt_block(chunks)),
        timeout=budget, model=CHEAP_MODEL, max_tokens=512, effort="low")
    if not reply.ok:
        return None, reply.error
    parsed = _loads(reply.text)
    if not isinstance(parsed, dict) or "answered" not in parsed:
        return None, "verdict was not a verdict"
    return parsed, None


def _ask_the_web(question, budget):
    """One call: the question, the web plugin, an answer. The provider composes
    its own queries from the question, which is why `question_admission` runs
    before this and not after."""
    return client.call(WEB_PROMPT.format(question=question),
                       timeout=budget, max_tokens=2048, web=True, effort="low")


def _sources(reply, limit=4):
    seen, out = set(), []
    for r in reply.search_results:
        if not r.url or r.url in seen:
            continue
        seen.add(r.url)
        out.append({"url": r.url, "title": r.title,
                    "domain": _domain(r.url, r.title)})
        if len(out) >= limit:
            break
    return out


_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                       r"(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*"
                       r"\.[a-z]{2,}$")


def _domain(url, title):
    """Who published this. The vendor returns Google grounding redirects, so
    parsing the URL makes every source read `vertexaisearch.cloud.google.com`;
    the host is in `title` instead. Trusted only when the title IS a hostname,
    so a vendor returning real URLs still resolves correctly."""
    candidate = (title or "").strip().lower()
    if _HOSTNAME.match(candidate):
        return candidate[4:] if candidate.startswith("www.") else candidate
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", (url or "").strip(), re.I)
    host = m.group(1).split("@")[-1].split(":")[0].lower() if m else ""
    if host.startswith("www."):
        host = host[4:]
    return host or None
