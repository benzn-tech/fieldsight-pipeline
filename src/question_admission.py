"""May this question be sent to a search engine, whole?

`corroboration_gate` answers a narrower question -- may this NAME be sent -- by
breaking an answer into entities and screening each one. That works when the
thing to look up is already named.

It failed on the most ordinary question there is. Asked *"Which New Zealand
standard covers timber design?"*, entity extraction returned NOTHING on three
runs out of four, because the question contains no standard number: **the number
is the thing the asker does not know, which is why they are asking.** A design
that only searches for names it was handed cannot serve the case where the name
is the answer.

So the question goes out whole, and this module decides whether it may. Same
protection, admission instead of dissection.

## What it refuses, and why each signal is bounded

Detecting "a person's name" in free text is unbounded, and this file will not
pretend otherwise. Three signals that are not:

**Commercial terms.** Reused verbatim from the gate: prices, claims, variations,
contracts. Commercially sensitive regardless of who is named, and the rule is
already load-bearing next door.

**Names from THIS account's own records.** The retrieved excerpts are the
customer's world in text form. A capitalised name in the question that also
appears in those excerpts is theirs -- a colleague, a site, a supplier they are
dealing with. This is the signal the gate cannot use and this module can,
because here the records are in hand.

**Site names.** Carried on every chunk as a field, so no inference is needed.

## What it deliberately does NOT do

It does not refuse every capitalised word. "New Zealand", "Building Code" and
"WorkSafe" are two capitalised words each and are the entire point of the
questions this feature exists for. A rule that refused them would refuse the
feature.

That is the trade, stated plainly: a person whose name never appears in the
retrieved excerpts is not detected. The signal is "is this the customer's own
world", not "is this a human being".

PURE PYTHON, NO MODEL. The gate's docstring explains why, and it applies here
with more force: an LLM asked "is this safe to send?" says yes under pressure
from a plausible-sounding question, and the pressure here arrives as the user's
own words.
"""
from __future__ import annotations

import re

from corroboration_gate import _COMMERCIAL, _NAME_WORD

# A name-shaped run ANYWHERE in the sentence, not anchored like the gate's --
# the gate screens a string that IS an entity; this scans a sentence that may
# contain one.
_NAME_RUN = re.compile(_NAME_WORD + r"(?:\s+" + _NAME_WORD + r"){0,2}")

# Sentence-initial capitals are grammar, not names. "Which" and "What" would
# otherwise match the name shape on every question ever asked.
_OPENERS = frozenset("""
which what when where who why how is are was were does do did can could should
would will the a an our my this that these those if in on at for from by with
""".split())

MAX_QUESTION_CHARS = 300


def _runs(question):
    """Capitalised runs worth testing, with grammar filtered out."""
    out = []
    for m in _NAME_RUN.finditer(question or ""):
        run = m.group(0).strip()
        first = run.split()[0].lower()
        if first in _OPENERS and m.start() == 0:
            # Drop only the leading grammatical capital; what follows it may
            # still be a name ("Which Neil signed it").
            rest = run.split(None, 1)
            if len(rest) == 1:
                continue
            run = rest[1]
        if run:
            out.append(run)
    return out


def _appears_in(needle, haystack):
    """Whole-word, case-insensitive. Substring matching would refuse `Ben` for
    every question containing `bench` or `benchmark`."""
    return re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", haystack,
                     re.IGNORECASE) is not None


# `NZ` is a currency code in the gate's commercial pattern and a country in
# every question this feature exists for -- "what is the NZ standard for
# scaffolding" was refused as a price. The gate screens NAMES, where a bare
# currency code is a strong signal; a sentence is not a name, so here it counts
# only when a number is beside it.
_CURRENCY_CODE = re.compile(r"(?i)^(nzd?|aud?|usd|gst)$")
_NEAR_A_NUMBER = re.compile(r"\d")


def _is_a_false_currency(hit, question):
    token = hit.group(0)
    if not _CURRENCY_CODE.match(token.strip()):
        return False
    around = question[max(0, hit.start() - 12):hit.end() + 12]
    return not _NEAR_A_NUMBER.search(around)


def screen(question, chunks):
    """Return None when the question may be sent, or a reason when it may not.

    The reason is returned rather than swallowed, for the same rule the gate
    states: a refusal nobody can see cannot be measured, and this repository has
    shipped guards that were inert for months behind a silence that looked
    exactly like "nothing to do".
    """
    q = (question or "").strip()
    if not q:
        return "empty question"
    if len(q) > MAX_QUESTION_CHARS:
        # A long question is a paragraph of context, and a paragraph of this
        # customer's context is the thing that must not leave.
        return "longer than %d characters" % MAX_QUESTION_CHARS

    hit = _COMMERCIAL.search(q)
    if hit and not _is_a_false_currency(hit, q):
        return "carries a commercial term (%s)" % hit.group(0)

    corpus = "\n".join((c.get("chunk_text") or "") for c in (chunks or []))
    sites = [str(c.get("site_name") or "") for c in (chunks or []) if c.get("site_name")]

    for site in sites:
        if site and _appears_in(site, q):
            return "names a site from this account (%s)" % site

    for run in _runs(q):
        if _appears_in(run, corpus):
            return "names something from this account's own records (%s)" % run

    return None
