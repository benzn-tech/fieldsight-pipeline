"""Answer from the open web when the records cannot.

Spec: docs/superpowers/specs/2026-09-10-answering-from-the-open-web-design.md
Plan: docs/superpowers/plans/2026-09-11-answering-from-the-open-web.md

This is the SMALLEST end-to-end version of that plan: one route, no concurrency,
no separate door for an empty corpus, no UI beyond a block the client may render.
It exists so the feature can be judged from use rather than from a document --
five review rounds produced a design and no working path, and a design nobody has
felt is a design nobody can correct.

## Why the gate runs BEFORE the grounded answer

The obvious shape is "answer from records, notice it declined, then look it up".
It does not fit. Measured, worst case, from the HTTP request:

    retrieval 1.36 + synthesis 11.9 + gate 3.23 + search 11.4 + compose 5.31
      = 33.2s, against API Gateway's 29s

Running the gate first turns it into a router, and both branches fit:

    records cannot answer   1.36 + 3.23 + 11.40 + 5.31 = 21.30s
    records can answer      1.36 + 3.23 + 11.90         = 16.49s

The cost is 3.23s on every question, including the majority the corpus answers.
That is the price of the small version; the plan's concurrent shape removes it
and is the next step, not this one.

## What crosses which boundary

Two boundaries, and they are not the same one (see corroboration_gate's own
docstring, corrected 2026-09-11):

  * the SEARCH ENGINE receives gate-screened entity NAMES and nothing else --
    no claim field, no question text, no excerpt text
  * the LLM PROVIDER receives the question and the excerpts, which it already
    receives on every `/ask`

The claim field is deliberately absent from what this module builds. `screen()`
inspects the entity string and forwards `claim` verbatim into the search-enabled
call; that is safe only while claims come from an ANSWER. Subjects here come from
a QUESTION, so a claim built from one would put the user's own words one model
hop from a search engine -- the threat the gate exists for, through the one
channel it never looks at.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import corroboration_client as client
import corroboration_gate as gate

logger = logging.getLogger()

# Sized from the measurements in the module docstring, each over the worst case
# actually observed rather than over a median -- and they have to SUM under the
# stop with the floor to spare, or the last step is refused for having no time
# left and the reader is told it timed out when the truth is arithmetic.
#
#     gate      MAX 6.80s (four questions; one question alone said 3.23)
#     search    MAX 11.40s (three entities; two entities alone said 8.66)
#     compose   MAX  5.31s (n=8)
#
# 7 + 12 + 6 = 25, plus the 2s floor, exactly fills 27. Every one of those
# maxima came from a sample this repo has already been burned for trusting, so
# they are ceilings to re-measure rather than facts to build on.
GATE_BUDGET = 7.0
SEARCH_BUDGET = 12.0
COMPOSE_BUDGET = 6.0
HARD_STOP_SECONDS = float(os.environ.get("WEB_ANSWER_HARD_STOP", "27"))

CHEAP_MODEL = os.environ.get("CORROBORATION_CHEAP_MODEL", "google/gemini-3.8-flash")

# At most this many subjects reach a search engine. The corroboration gate caps
# entities at three for the same reason and this path is no more entitled.
MAX_SUBJECTS = 3

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def enabled():
    """Read at call time, never at import. A flag captured at import survives a
    warm container after the stack has been redeployed with it off."""
    return os.environ.get("ENABLE_WEB_ANSWER", "false").lower() == "true"


GATE_PROMPT = """Two questions about the excerpts below, answered together in one JSON object.

1. Do the excerpts answer the question? `answered` is true only if a reader would
   get what they asked for from these excerpts alone.
2. If they do NOT, what external subjects would have to be looked up to answer it?
   A subject is a company, a published standard, a product, a material, a
   regulator or an authority. NOT people, sites, addresses, project codes, or
   anything that only exists inside this customer's own records. Take them from
   the QUESTION, not from the excerpts.

Return only JSON, no prose:
{{"answered": true|false,
  "subjects": [{{"entity": "...", "kind": "company|standard|product|material|regulator|authority"}}]}}

`subjects` is empty when `answered` is true, and may be empty when it is false --
a question naming nothing external cannot be looked up.

## Question
{question}

## Excerpts
{excerpts}
"""

SEARCH_PROMPT = """Search the open web for each subject below and report what public
sources say about it, in the context of New Zealand construction.

Report only what the sources say. Do not speculate, and do not fill a gap with
general knowledge -- if the sources do not cover a subject, say so plainly for
that subject.

{subjects}

For each subject, write a short paragraph beginning with the subject name.
"""

COMPOSE_PROMPT = """A user asked a question that their own recorded meetings do not
answer. Below is what public web sources say about the subjects of that question.

Answer the question from these findings only. Report what the sources say. Do not
speculate and do not fill a gap with general knowledge -- if the findings do not
answer the question, say so plainly.

Name the source of each fact you use. Be brief: the reader is on a construction site.

## Question
{question}

## What public sources say
{findings}
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


def _screened_subjects(raw):
    """Gate-screened names, and NOTHING else.

    `screen()` wants `{entity, kind, claim}` and forwards `claim` untouched into
    the search call. Every subject is given `claim: None` here so there is
    nothing to forward -- see the module docstring for why that is the whole
    point rather than a detail.
    """
    candidates = []
    for item in (raw or [])[:MAX_SUBJECTS * 2]:
        if not isinstance(item, dict):
            continue
        entity = item.get("entity")
        if isinstance(entity, str) and entity.strip():
            candidates.append({"entity": entity.strip(),
                               "kind": item.get("kind"),
                               "claim": None})
    if not candidates:
        return [], []
    result = gate.screen(candidates, max_entities=MAX_SUBJECTS)
    dropped = [{"entity": r.entity, "reason": r.reason} for r in result.rejected]
    return result.allowed, dropped


def answer(question, chunks, *, clock=time.monotonic):
    """Return a web-answer block, or None to leave the grounded path alone.

    Never raises. Every failure -- flag off, gate said the records answer it, no
    nameable subject, a step that broke, a deadline missed -- returns either None
    or a body whose flags say which, because "we found nothing" and "we did not
    look" are different sentences and only one of them is about the world.
    """
    if not enabled() or not question or not chunks:
        return None

    started = clock()

    def left():
        return HARD_STOP_SECONDS - (clock() - started)

    verdict, err = _gate(question, chunks, min(GATE_BUDGET, left()))
    if err or verdict is None:
        logger.warning("web answer: gate failed: %s", err)
        return None
    if verdict.get("answered") is True:
        # The records answered it. Nothing to look up, and saying so out loud
        # keeps the "we never fired" case distinguishable from "we broke".
        logger.info("web answer: records answered it; no lookup")
        return None

    subjects, dropped = _screened_subjects(verdict.get("subjects"))
    if not subjects:
        logger.info("web answer: nothing nameable to look up (dropped=%d)",
                    len(dropped))
        return {"answer": None, "sources": [], "subjects": [],
                "dropped": dropped, "searched": False, "timed_out": False,
                "failed": False}

    names = [a["entity"] for a in subjects]
    logger.info("web answer: looking up %s", names)

    if left() < client.MIN_USEFUL_TIMEOUT:
        return _spent(names, dropped, timed_out=True)
    found = _search(names, min(SEARCH_BUDGET, left()))
    if not found.ok:
        logger.warning("web answer: search failed: %s (timed_out=%s)",
                       found.error, found.timed_out)
        return _spent(names, dropped, timed_out=bool(found.timed_out),
                      failed=not found.timed_out)
    if not found.searched:
        # Prose describing a search is not evidence one happened. Measured on a
        # second vendor: 200 OK, no results, and a paragraph asserting findings.
        logger.warning("web answer: no web results came back")
        return _spent(names, dropped)

    if left() < client.MIN_USEFUL_TIMEOUT:
        return _spent(names, dropped, timed_out=True, sources=_sources(found))
    text, err = _compose(question, found.text, min(COMPOSE_BUDGET, left()))
    if err or not text:
        logger.warning("web answer: compose failed: %s", err)
        return _spent(names, dropped, failed=True, sources=_sources(found))

    return {"answer": text, "sources": _sources(found), "subjects": names,
            "dropped": dropped, "searched": True, "timed_out": False,
            "failed": False}


def _spent(names, dropped, *, timed_out=False, failed=False, sources=None):
    """A body that says what happened, never an empty one that reads as
    'the web had nothing to say'."""
    return {"answer": None, "sources": sources or [], "subjects": names,
            "dropped": dropped, "searched": False,
            "timed_out": timed_out, "failed": failed}


def _gate(question, chunks, budget):
    reply = client.call(
        GATE_PROMPT.format(question=question, excerpts=_excerpt_block(chunks)),
        timeout=budget, model=CHEAP_MODEL, max_tokens=1024, effort="low")
    if not reply.ok:
        return None, reply.error
    parsed = _loads(reply.text)
    if not isinstance(parsed, dict) or "answered" not in parsed:
        # Fail closed: an unreadable verdict is not permission to search.
        return None, "gate did not return a verdict"
    return parsed, None


def _search(names, budget):
    lines = "\n".join("- %s" % n for n in names)
    return client.call(SEARCH_PROMPT.format(subjects=lines),
                       timeout=budget, max_tokens=2048, web=True, effort="low")


def _compose(question, findings, budget):
    reply = client.call(
        COMPOSE_PROMPT.format(question=question, findings=findings),
        timeout=budget, model=CHEAP_MODEL, max_tokens=1024, effort="low")
    if not reply.ok:
        return None, reply.error
    return (reply.text or "").strip() or None, None


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
