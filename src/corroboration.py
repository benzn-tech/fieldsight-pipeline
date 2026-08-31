"""What the open web says, kept apart from what the recording says.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.2, §5.3

Four steps behind a hard stop, orchestrated here so the honesty rules live in one
readable place rather than being spread across three prompts:

    1. extraction  4 s   entities + the claim the answer makes about each
    2. gate      <10 ms  corroboration_gate -- pure Python, no model
    3. search     12 s   one call, Anthropic + the web_search tool
    4. reconcile   6 s   a state per entity

## The two things this module refuses to do

**It never reports a state it did not get.** A model that returns a state
outside the enum has told us nothing, and the tempting repairs are all
dishonest: `corroborated` invents agreement, `not_found` invents a search that
came back empty, `no_checkable_claim` invents a judgement about the answer. The
entity is dropped and the reason is recorded, so the count of unusable states is
visible instead of being laundered into a card.

**It never salvages a timed-out search.** Step 3 is one call covering every
surviving entity, so there is no per-entity progress to keep. When it misses its
slice the result is zero corroborations and `timed_out: true` -- not a shorter
list, which would read as "we checked these and found nothing".

`truncated` and `timed_out` stay separate for the same reason. One is the cap
biting, which is deterministic and knowable before any network call; the other
is a deadline missed. A reader who sees three cards deserves to know which.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import corroboration_client as client
import corroboration_gate as gate

logger = logging.getLogger()

# ApiFunction dies at 30 s, and an agent still working at 35 s is answering a
# proxy that is already gone. The stop is internal and earlier than the target
# so the caller always gets a shaped body rather than a gateway error.
HARD_STOP_SECONDS = float(os.environ.get("CORROBORATION_HARD_STOP", "24"))

EXTRACT_BUDGET = 4.0
SEARCH_BUDGET = 12.0
RECONCILE_BUDGET = 6.0

# Steps 1 and 4 are classification, not reasoning, and they are the two that pay
# for the search step's twelve seconds.
CHEAP_MODEL = os.environ.get("CORROBORATION_CHEAP_MODEL", "claude-haiku-4-5")

STATES = frozenset({"corroborated", "conflicts", "not_found", "no_checkable_claim"})

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def enabled() -> bool:
    """Read at call time, never at import.

    A flag captured at import is a flag whose value is whatever the container
    started with, and this repository has shipped switches that looked wired and
    only ever returned their default.
    """
    return os.environ.get("ENABLE_EXTERNAL_CORROBORATION", "false").lower() == "true"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(text):
    """Parse a model's JSON, tolerating the code fence it sometimes adds.

    Returns None rather than raising: a step that cannot read its own model's
    output must cost the reader the cards, never the answer.
    """
    if not text:
        return None
    stripped = _FENCE.sub("", text).strip()
    try:
        return json.loads(stripped)
    except Exception:                             # noqa: BLE001 - any shape can arrive
        start, end = stripped.find("["), stripped.rfind("]")
        if start == -1 or end <= start:
            start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(stripped[start:end + 1])
        except Exception:                         # noqa: BLE001
            return None


# Measured against 40 real prod sessions before this paragraph existed: 22 strings
# would have left the account, and only three of them were worth looking up.
# The rest were the customer's own project codes (`UCPK`, `Raven`), our own
# product name, and ubiquitous brands nobody needs corroborated -- `Microsoft`,
# `Wi-Fi`, `SIM card`, `iPad`, `UberEats`. Those are not a privacy failure: no
# person, price or commercial term got through. They are a *usefulness* failure
# with a privacy cost attached, and the two point the same way. Noise entities
# spend the three-entity budget -- the cap bit on 3 of the 11 sessions that had
# any entity at all -- so the useless ones were crowding out the useful ones,
# and every one of them was also a string leaving the account for nothing.
#
# `no_checkable_claim` cannot fix this: it runs in step 4, after the search has
# already happened and the budget has already been spent.
EXTRACT_PROMPT = """You are given a question a construction-site worker asked about their own recorded meetings, and the answer our system gave from those recordings.

List the named external entities the ANSWER refers to, and for each one, the claim the answer makes about it.

An entity is a company, a published standard, a product, a material, a regulator, an authority, or a public role. Do NOT list people, sites, addresses, projects, or anything that only exists inside this customer's own records.

Leave out an entity, even a real one, when looking it up could not tell the reader anything they need:

- project codes, site names, job numbers and internal shorthand, even when they look like a company name
- the software the conversation was recorded or written with, including this product
- ubiquitous consumer or technology brands mentioned in passing -- a phone, a food delivery app, an operating system, a radio station, a coffee chain
- generic technology named as a thing rather than a product: wifi, a sim card, a spreadsheet, email

Keep an entity when the answer asserts something substantive about it that a reader would want checked: a contractor, a supplier, a manufacturer, a published standard, a regulator, a specified material or building product.

The claim must be something the ANSWER states about the entity, in the answer's own terms. Never build the claim out of the question: the question describes this customer's job, and a claim taken from it is a claim about their job rather than about the entity. If the question asks "who supplied the plasterboard" and the answer says a firm is a large materials manufacturer, the claim is that it is a large materials manufacturer -- not that it supplied the plasterboard.

If the answer merely names the entity without asserting anything about it, set claim to null.

Return ONLY a JSON array, no prose:
[{{"entity": "...", "kind": "company|standard|product|material|public_role|regulator|authority", "claim": "..." | null}}]

An empty array is a correct answer, and the common one -- most answers name no external entity worth looking up.

QUESTION:
{question}

ANSWER:
{answer}
"""

SEARCH_PROMPT = """Search the open web for each of the following entities and report \
what public sources say about the specific claim listed beside it.

Report only what the sources say. Do not speculate, and do not fill a gap with general \
knowledge -- if the sources do not address a claim, say so plainly for that entity.

{entities}

For each entity, write a short paragraph beginning with the entity name.
"""

# Two questions in a fixed order, not four states in a list.
#
# The list form was measured: 48 runs over 16 labelled cases. It never once
# called a false claim "corroborated" -- but on claims about the customer's own
# project it answered "not_found" 6 times out of 12, because the search findings
# sat right there saying the sources did not address it, and a model reading
# those findings answers the question they suggest. "We searched and found
# nothing" about a sentence the web could never have held is a quiet slur on
# something that was only ever internal.
#
# So checkability is asked FIRST and separately, and the findings are not to be
# consulted until it has been answered.
RECONCILE_PROMPT = """For each entity below, answer two questions in order.

QUESTION 1 -- is this claim about the ENTITY, or about this customer's own job?

A claim about the entity is one a stranger could look up: what a company does,
where it operates, what a standard covers, what a product is. A claim about the
customer's own job merely mentions the entity and asserts something only their own
records could settle -- what was delivered, what was said in a meeting, what was
scheduled, who is visiting, what sits on a shelf.

If it is about the customer's own job, the state is "no_checkable_claim" and you are
done with that entity. Do not read the search findings for it. In particular do not
let findings that say "the sources do not address this" talk you into "not_found":
nothing was ever there to find, and reporting a search that failed would suggest the
web declined to back up a sentence the web never had.

QUESTION 2 -- only for claims about the entity itself. Compare the claim with what
the search found:

- "corroborated": a source agrees with the claim
- "conflicts": a source disagrees with the claim
- "not_found": the search ran and nothing usable came back either way

Confirming that a company merely exists is NOT corroboration of a claim about it.

Return ONLY a JSON array, no prose. The summary says WHY in one or two sentences,
and must never be empty for "corroborated" or "conflicts":
[{{"entity": "...", "state": "...", "summary": "..."}}]

CLAIMS:
{claims}

WHAT THE SEARCH FOUND:
{findings}
"""


def _extract(question, answer, budget):
    reply = client.call(
        EXTRACT_PROMPT.format(question=question, answer=answer),
        timeout=budget, model=CHEAP_MODEL, max_tokens=1024, effort=None)
    if not reply.ok:
        return None, reply.error
    parsed = _loads(reply.text)
    if not isinstance(parsed, list):
        return None, "extraction did not return a list"
    return parsed, None


def _search(allowed, budget):
    lines = "\n".join(
        f"- {a['entity']} ({a['kind']}): {a.get('claim') or 'no specific claim'}"
        for a in allowed)
    return client.call(SEARCH_PROMPT.format(entities=lines),
                       timeout=budget, max_tokens=2048,
                       tools=[client.WEB_SEARCH_TOOL], effort="low")


def _reconcile(allowed, findings, budget):
    claims = "\n".join(
        f"- {a['entity']}: {a.get('claim') or '(the answer asserts nothing about it)'}"
        for a in allowed)
    reply = client.call(
        RECONCILE_PROMPT.format(claims=claims, findings=findings or "(nothing found)"),
        timeout=budget, model=CHEAP_MODEL, max_tokens=1024, effort=None)
    if not reply.ok:
        return None, reply.error
    parsed = _loads(reply.text)
    if not isinstance(parsed, list):
        return None, "reconcile did not return a list"
    return parsed, None


def _sources(reply, limit=4):
    seen, out = set(), []
    for r in reply.search_results:
        if not r.url or r.url in seen:
            continue
        seen.add(r.url)
        out.append({"title": r.title, "url": r.url, "published": r.page_age})
        if len(out) >= limit:
            break
    return out


def corroborate(question, answer, *, clock=time.monotonic) -> dict:
    """Run the four steps and return the §5.1 body.

    Never raises. Every failure mode -- flag off, no entities, gate refused
    everything, a step timed out, a model returned nonsense -- degrades to a body
    with zero corroborations and a truthful pair of flags.
    """
    started = clock()
    empty = {"corroborations": [], "dropped": [], "truncated": False,
             "timed_out": False}

    if not question or not answer:
        return empty

    def left():
        return HARD_STOP_SECONDS - (clock() - started)

    # --- 1. what does the answer claim, and about whom -----------------------
    entities, err = _extract(question, answer, min(EXTRACT_BUDGET, left()))
    if err:
        logger.warning("corroboration: extraction failed: %s", err)
        return dict(empty, timed_out=True)
    if not entities:
        return empty

    # --- 2. what of that may leave the account ------------------------------
    result = gate.screen(entities, max_entities=gate.MAX_ENTITIES)
    dropped = [{"entity": r.entity, "reason": r.reason} for r in result.rejected]
    if not result.allowed:
        # Refusals are loud. A gate whose rejections are invisible cannot be
        # measured, and the count of passes is worth a line too.
        logger.info("corroboration: gate allowed 0 of %d entities", len(entities))
        return dict(empty, dropped=dropped)
    logger.info("corroboration: gate allowed %d, refused %d, truncated=%s",
                len(result.allowed), len(result.rejected), result.truncated)

    base = {"corroborations": [], "dropped": dropped,
            "truncated": result.truncated, "timed_out": False}

    # --- 3. one search covering all of them ---------------------------------
    if left() < client.MIN_USEFUL_TIMEOUT:
        return dict(base, timed_out=True)
    search = _search(result.allowed, min(SEARCH_BUDGET, left()))
    if not search.ok:
        # One call, so there is no partial progress to keep. A shorter list here
        # would read as "we checked these and found nothing".
        logger.warning("corroboration: search failed: %s", search.error)
        return dict(base, timed_out=True)
    if search.search_error:
        logger.warning("corroboration: search tool error: %s", search.search_error)

    # --- 4. a state per entity ----------------------------------------------
    if left() < client.MIN_USEFUL_TIMEOUT:
        return dict(base, timed_out=True)
    verdicts, err = _reconcile(result.allowed, search.text,
                               min(RECONCILE_BUDGET, left()))
    if err:
        logger.warning("corroboration: reconcile failed: %s", err)
        return dict(base, timed_out=True)

    by_entity = {}
    for v in verdicts or []:
        if isinstance(v, dict) and isinstance(v.get("entity"), str):
            by_entity[v["entity"].strip().casefold()] = v

    sources = _sources(search)
    retrieved_at = _now_iso()
    cards = []
    for a in result.allowed:
        v = by_entity.get(a["entity"].casefold())
        state = (v or {}).get("state")
        summary = ((v or {}).get("summary") or "").strip()
        # A card that says the web agrees, or disagrees, and does not say how is
        # an assertion the reader cannot check and cannot dismiss. Measured on
        # the real API: haiku returned `conflicts` with an empty summary on a
        # claim that was in fact correct. The two other states survive an empty
        # summary -- "nothing usable came back" and "nothing to check" are
        # complete statements on their own.
        if state in ("corroborated", "conflicts") and not summary:
            dropped.append({"entity": a["entity"], "reason": "a verdict with no reason"})
            continue
        if state not in STATES:
            # No verdict, or one outside the enum. Every repair available here
            # would invent a finding, so the entity is dropped and counted.
            dropped.append({"entity": a["entity"], "reason": "no usable state"})
            continue
        cards.append({
            "entity": a["entity"],
            "kind": a["kind"],
            "state": state,
            "claim": a.get("claim"),
            "summary": summary,
            # A card whose state is a finding about the web needs the sources
            # that finding came from. `no_checkable_claim` is a judgement about
            # our own answer and cites nothing.
            "sources": sources if state != "no_checkable_claim" else [],
            "retrieved_at": retrieved_at,
        })

    return {"corroborations": cards, "dropped": dropped,
            "truncated": result.truncated, "timed_out": False}
