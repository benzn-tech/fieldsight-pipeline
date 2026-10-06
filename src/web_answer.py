"""Ask's general-knowledge flow: classify, then (model draft -> web verify).

Spec: docs/superpowers/specs/2026-10-06-ask-knowledge-then-verify-design.md
(supersedes the web-answer route of 2026-09-10-answering-from-the-open-web).

## Trust by claim type

Project facts (what was said or decided on this site) come only from the
records. General knowledge (standards, codes, product data, methods) comes from
the model, checked against the web. When they disagree the answer says so.

## The three entry points, and the one rule they share

  * `classify`        one cheap call after retrieval: kind + "do the records
                      answer it". Replaces the old verdict call.
  * `general_answer`  draft (no web) -> verify (web, question + draft only) ->
                      one retry of verify only if time allows -> else the draft
                      VERBATIM, labelled unverified.
  * `compose`         mixed questions: merge the records answer and the general
                      block with attribution and a conflicts list.

The shared rule: nothing here may turn into "nothing". Measured 2026-10-06 on
the owner's sprinkler question: a lookup that aborted at 11 s left the reader
with "the excerpts do not contain it", twice. And a model asked to FILL THE GAP
after a failed search wrote the most confident wrong number ("2.0 m"), so after
a failed verify the draft is returned as it stands and no further model call is
made.

## What crosses which boundary

  * the SEARCH ENGINE receives the asker's question and the model's own draft --
    never the excerpts
  * the LLM PROVIDER receives the question (draft), question + draft (verify),
    and, for classify/compose, the excerpts / records answer it already sees on
    every /ask
  * `classify` judges the REWRITTEN question (`asked`); everything sent out
    uses the asker's own `question`. That boundary is unchanged: a rewrite is
    derived from history, and a history is a copy taken before a deletion.
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

# Per-call ceilings. Measured: classify 2.4-3.3 s; draft ~3-4 s; verify 8.8-11 s
# (the vendor aborts at ~11 s when it aborts); compose ~3 s. The whole chain is
# further bounded by the caller-supplied budget, which comes out of
# HARD_STOP_SECONDS (API Gateway gives up at 29 s).
CLASSIFY_BUDGET = 8.0
DRAFT_BUDGET = 8.0
VERIFY_BUDGET = 14.0
COMPOSE_BUDGET = 8.0
# A verify retry (~9 s) is attempted only if at least this many seconds remain.
VERIFY_RETRY_MIN_LEFT = 10.0
# Seconds the caller keeps back for compose when it runs after the chain.
COMPOSE_RESERVE = 4.0
HARD_STOP_SECONDS = float(os.environ.get("WEB_ANSWER_HARD_STOP", "27"))

CHEAP_MODEL = os.environ.get("CORROBORATION_CHEAP_MODEL", "google/gemini-3.8-flash")

KINDS = ("project", "general", "mixed")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
# A citation marker in any of its written forms: [1], [1,2], [1; 3], [1-3].
# `_cited` expands each to the set of numbers it names, so the compose guard
# cannot be walked past by [1,2], [1-3] or [1][2].
_MARKER = re.compile(r"\[\s*\d+(?:\s*[-–,;]\s*\d+)*\s*\]")
_RANGE = re.compile(r"(\d+)(?:\s*[-–]\s*(\d+))?")


def _cited(text):
    """Every record number a text's bracketed markers refer to."""
    out = set()
    for m in _MARKER.finditer(text or ""):
        for a, b in _RANGE.findall(m.group(0)):
            lo, hi = int(a), int(b or a)
            out.update(range(lo, min(hi, lo + 100) + 1))
    return out


def enabled():
    """Read at call time, never at import. A flag captured at import survives a
    warm container after the stack has been redeployed with it off."""
    return os.environ.get("ENABLE_WEB_ANSWER", "false").lower() == "true"


CLASSIFY_PROMPT = """Classify this question asked of a construction site's meeting records,
and say whether the excerpts below answer it.

kind:
- "project": about what was said, decided, agreed, requested or done on this
  project or site -- people, dates, quantities, tasks, events.
- "general": about the outside world -- standards, codes, regulations, product
  data, methods -- the kind of thing a reference book would answer.
- "mixed": needs both (e.g. whether what the site did meets a standard).

records_answer: true only if a reader would get what they asked for, for the
project part of the question, from these excerpts alone. Excerpts merely about
the same site or the same day are not an answer. For a "general" question, true
only if the excerpts hold a related discussion worth showing alongside.

Return only JSON, no prose:
{{"kind": "project"|"general"|"mixed", "records_answer": true|false}}

## Question
{question}

## Excerpts
{excerpts}
"""

# Wording from the 2026-10-06 strategy probe (LLM_ONLY).
DRAFT_PROMPT = """Answer this question for a reader working on a New Zealand construction site,
from your own knowledge only. Be brief. Name the standard/clause you rely on. If you
are not sure of a figure, say so plainly instead of guessing.
If the question is about a specific project, site, person, meeting or date that
you cannot know, say plainly that only the project's records could answer it,
and do not invent an answer.

## Question
{question}
"""

# Wording from the 2026-10-06 strategy probe (LLM_THEN_WEB). It carries the
# question and the draft and NOTHING from the records.
# The reply is shown to the reader VERBATIM, so it must read as an answer, not
# as a review of someone else's work (TEST 2026-10-06: "### Review and
# Verification", "**Confirmed.**", "The colleague's answer is accurate...").
# The draft stays in the prompt as INPUT; the output must not mention it.
VERIFY_PROMPT = """Answer the question below for a reader working on a New Zealand construction
site. Below is a draft answer written from memory. Search the open web to check
it, and write the FINAL answer yourself, addressed directly to the reader:
- state the correct content directly, and give the source next to each fact;
- mark anything you could not confirm as [unverified];
- be brief.
Never mention the draft, a colleague, or the checking process, and never use
words such as "confirmed", "corrected", "verified", "review" or "verification".
Do not use headings.

## Question
{question}

## Draft answer
{draft}
"""

COMPOSE_PROMPT = """Merge two answers to one question for a reader working on a New Zealand
construction site.

Rules:
- Facts about THIS project (what was said or decided on site, who, when,
  quantities) may come ONLY from the RECORDS ANSWER.
- General facts (standards, codes, regulations, product data, methods) may come
  ONLY from the GENERAL ANSWER.
- Where the two disagree, list it in "conflicts", one short sentence each, in the
  form "the records say X; <standard or source> says Y". Also state it in the
  answer, marked with a warning sign.
- The RECORDS ANSWER carries inline markers like [1]. Keep every one of them
  exactly as written, next to the fact it supports. NEVER write a [n] marker of
  your own, and never reuse one for a general fact -- name the standard or the
  source in words instead.
- Be brief. No preamble.

Return only JSON, no prose: {{"answer": "...", "conflicts": ["..."]}}

## Question
{question}

## RECORDS ANSWER
{records}

## GENERAL ANSWER
{general}
"""


def _loads(text):
    try:
        return json.loads(_FENCE.sub("", text or "").strip())
    except Exception:                             # noqa: BLE001 - any shape but ours
        return None


def _excerpt_block(chunks, limit=5):
    """What classify judges. Deliberately the same text the grounded answer is
    built from -- a classifier reading headers alone would be judging titles, and
    a topic title says nothing about whether a question was answered."""
    out = []
    for i, c in enumerate(chunks[:limit], start=1):
        header = " . ".join(str(p) for p in (
            c.get("site_name"), c.get("report_date"), c.get("topic_title")) if p)
        out.append("[{n}] {h}\n```\n{t}\n```".format(
            n=i, h=header or "?", t=c.get("chunk_text") or ""))
    return "\n".join(out)


# ------------------------------------------------------------------ classify

def classify(question, chunks, budget):
    """`{"kind", "records_answer", "source"}`. Never raises, never returns None.

    `question` is the question the CLASSIFIER judges (the standalone rewrite when
    conversation memory ran). `source` is "model", "fallback" or "no_records".

    FAIL-SAFE (ruling, 2026-10-06): an unreadable or failed classification is not
    permission to guess narrow. With records in hand it becomes kind="mixed",
    records_answer=True -- both flows run and `compose` attributes each fact to
    the side allowed to supply it, so a project question is never answered from
    the model's general knowledge alone and a general one is never answered from
    meeting notes alone. With no records it becomes kind="general". The cost is
    time (an extra compose call), never an empty answer.
    """
    if not chunks:
        # Nothing was retrieved: retrieval returning nothing IS the verdict, so
        # no model is asked whether an empty set answered anything.
        return {"kind": "general", "records_answer": False, "source": "no_records"}
    fallback = {"kind": "mixed", "records_answer": True, "source": "fallback"}
    reply = client.call(
        CLASSIFY_PROMPT.format(question=question, excerpts=_excerpt_block(chunks)),
        timeout=budget, model=CHEAP_MODEL, max_tokens=512, effort="low",
        caller="web_classify")
    if not reply.ok:
        logger.warning("ask classify failed: %s", reply.error)
        return fallback
    parsed = _loads(reply.text)
    if (not isinstance(parsed, dict) or parsed.get("kind") not in KINDS
            or not isinstance(parsed.get("records_answer"), bool)):
        logger.warning("ask classify: reply was not a classification")
        return fallback
    return {"kind": parsed["kind"], "records_answer": parsed["records_answer"],
            "source": "model"}


# ------------------------------------------------------------ general answer

def _block(*, answer=None, sources=(), status="unverified", kind="general",
           searched=False, timed_out=False, failed=False, refused=None,
           retried=False, ms=None):
    """The `web` block. `_trace` is for the caller's ASK_ROUTE line and is popped
    before the response leaves the lambda."""
    return {"answer": answer, "sources": list(sources), "status": status,
            "kind": kind, "conflicts": [], "searched": searched,
            "timed_out": timed_out, "failed": failed, "refused": refused,
            "_trace": {"retried": retried, "ms": ms or {}}}


def _verify_ok(reply):
    # Prose describing a search is not evidence one happened (measured on a
    # second vendor: 200 OK, no results, a paragraph asserting findings).
    return reply.ok and reply.searched and bool((reply.text or "").strip())


def general_answer(question, budget, *, kind="general", clock=None):
    """Draft from the model's knowledge, verify on the web, never nothing.

    `budget` is the seconds this whole chain may take. Returns a `web` block;
    `answer` is None only when even the draft could not be produced.

      draft   no web. The model says what it knows and where it is unsure.
      verify  web=True; the prompt carries the question and the draft ONLY.
      retry   verify once more, only when `VERIFY_RETRY_MIN_LEFT` seconds remain.
      failed  the draft, VERBATIM, status "unverified". No further model call:
              asking a model to fill the gap after a failed search invented
              "2.0 m" (measured 2026-10-06).

    A question over the admission cap is not sent to the web at all; it still
    gets the draft, with status "too_long".
    """
    # Resolved at call time so a test (or a caller) patching time.monotonic is honoured.
    clock = clock or time.monotonic
    started = clock()
    ms = {}

    def left():
        return budget - (clock() - started)

    refused = question_admission.screen(question)
    if refused == question_admission.EMPTY:
        return _block(kind=kind, failed=True, refused=refused, ms=ms)

    t = clock()
    draft_reply = client.call(
        DRAFT_PROMPT.format(question=question),
        timeout=min(DRAFT_BUDGET, left()), max_tokens=1024, effort="low",
        caller="web_draft")
    ms["draft"] = int((clock() - t) * 1000)
    draft = (draft_reply.text or "").strip() if draft_reply.ok else ""
    if not draft:
        logger.warning("ask general: draft failed: %s", draft_reply.error)
        return _block(kind=kind, failed=True, timed_out=bool(draft_reply.timed_out),
                      ms=ms)

    if refused:
        logger.info("ask general: question not sent to the web -- %s", refused)
        return _block(answer=draft, status="too_long", kind=kind, refused=refused,
                      ms=ms)

    retried = False
    last = None
    for attempt in (1, 2):
        if attempt == 2:
            if left() < VERIFY_RETRY_MIN_LEFT:
                logger.info("ask general: no retry, %.1fs left", left())
                break
            retried = True
        if left() < client.MIN_USEFUL_TIMEOUT:
            break
        t = clock()
        last = client.call(
            VERIFY_PROMPT.format(question=question, draft=draft),
            timeout=min(VERIFY_BUDGET, left()), max_tokens=2048, web=True,
            effort="low", caller="web_verify")
        ms["verify" if attempt == 1 else "verify_retry"] = int((clock() - t) * 1000)
        if _verify_ok(last):
            logger.info("ask general: verified against %d sources",
                        len(last.search_results))
            return _block(answer=last.text.strip(), sources=_sources(last),
                          status="verified", kind=kind, searched=True,
                          retried=retried, ms=ms)
        logger.warning("ask general: verify attempt %d failed: %s (searched=%s)",
                       attempt, getattr(last, "error", None),
                       getattr(last, "searched", None))

    return _block(answer=draft, status="unverified", kind=kind, retried=retried,
                  timed_out=bool(last is not None and last.timed_out),
                  failed=True, ms=ms)


# ------------------------------------------------------------------- compose

def compose(records_answer_text, general_block, question, budget):
    """Merge the records answer and the general block for a mixed question.

    Returns `{"answer", "conflicts", "composed"}`. On any failure `composed` is
    False, `answer` is the records answer UNCHANGED and `conflicts` is empty --
    the caller then shows the general block alongside, as it does for a general
    question with useful records.

    The citations contract (lambda_ask_agent._build_citations): card [i+1] maps
    to inline [n] in the records answer. The general text carries its OWN [n]
    markers pointing at web sources, so they are stripped before the model sees
    it, and the output is rejected unless every marker in it already occurs in
    the records answer. A prompt asking for that is not a guard; this check is.
    """
    fail = {"answer": records_answer_text, "conflicts": [], "composed": False}
    general_text = _MARKER.sub("", (general_block or {}).get("answer") or "").strip()
    if not general_text or not (records_answer_text or "").strip():
        return fail
    if budget < client.MIN_USEFUL_TIMEOUT:
        return fail
    reply = client.call(
        COMPOSE_PROMPT.format(question=question, records=records_answer_text,
                              general=general_text),
        timeout=min(COMPOSE_BUDGET, budget), model=CHEAP_MODEL, max_tokens=2048,
        effort="low", caller="web_compose")
    if not reply.ok:
        logger.warning("ask compose failed: %s", reply.error)
        return fail
    parsed = _loads(reply.text)
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("answer"), str)
            or not parsed["answer"].strip()
            or not isinstance(parsed.get("conflicts", []), list)):
        logger.warning("ask compose: reply was not a composition")
        return fail
    if not _cited(parsed["answer"]) <= _cited(records_answer_text):
        logger.warning("ask compose: invented a citation marker; discarded")
        return fail
    return {"answer": parsed["answer"].strip(),
            "conflicts": [str(c).strip() for c in parsed.get("conflicts", [])
                          if str(c).strip()],
            "composed": True}


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
