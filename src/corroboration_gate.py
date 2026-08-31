"""What may leave the account on its way to a search engine.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §4

Only ENTITIES go out. Never conversation text. Construction meeting content is
commercially confidential -- a claim dispute, a subcontractor's pricing, a defect
allocation -- and sending any of it to a third-party search engine is a contractual
problem regardless of how good the answer would be.

PURE PYTHON, NO I/O, NO MODEL. This is the whole point of the module existing
separately. An LLM asked "is this safe to send?" says yes under pressure from a
plausible-sounding question, and the pressure here arrives as a user's own words. The
decision is code, it is unit-tested, and it runs AFTER extraction regardless of what
extraction returned.

## The two limits this gate does not pretend to hold (spec §4.1)

**Its input is model-assigned.** Extraction returns `[{entity, kind, claim}]` and the
kind allowlist filters on `kind` -- a label only as good as haiku's classification. A
person's name labelled `company`, a firm literally named "Ben Smith Contracting", and a
project codename shaped like a company all pass the label check. So every entity also
goes through **string-shape checks that do not consult the label at all**. The residual
risk is accepted, not denied.

What the gate does hold against, and the reason it is worth having: `"we're being sued
by Naylor Love"` yields the query `"Naylor Love"`. That leaks interest in a company and
not the dispute. Saying which attack fails is more useful than claiming the gate is
airtight.

**Only the search step is covered here.** Reconcile (spec step 4) sends the answer to
the LLM provider, and the answer is conversation-derived. That is a different statement
from "only entities leave", defensible because the same provider already saw the
transcript during `/ask` -- but no *search engine* ever receives it, and this module is
the thing that makes that true.

## Why a rejection is returned rather than dropped

Every refusal comes back with its reason. A gate whose rejections are invisible cannot
be measured, and this repository has shipped several guards that were inert for months
behind a silence that looked exactly like "nothing to do" (memory:
guard-caught-it-is-not-it-works). The caller logs `rejected`; the count of passes is
worth a line too.
"""
from __future__ import annotations

import re
import unicodedata

# The kinds that may be looked up at all. Anything else -- a person, a site, a document,
# whatever new label extraction invents next month -- is refused by default. An unknown
# label is a refusal and not a pass: a permissive default here is the failure that costs
# a contract, and a missing corroboration costs a card.
ALLOWED_KINDS = frozenset({"company", "standard", "product", "material",
                           "public_role", "regulator", "authority"})

# An entity is a name. Anything long enough to be a sentence is a sentence, whatever the
# label says, and a smuggled transcript fragment is exactly what this number is for.
MAX_ENTITY_CHARS = 60

# Commercial content, in any language, anywhere in the string. These are the words that
# turn "a company name" into "our dealings with that company".
_COMMERCIAL = re.compile(
    r"(?i)(\$|£|€|¥|\bnzd?\b|\baud?\b|\busd\b|\bgst\b"
    r"|\bprice[sd]?\b|\bcost(s|ed|ing)?\b|\bquote[sd]?\b|\bquotation\b"
    r"|\bclaim(s|ed|ing)?\b|\bvariation\b|\bcontract\b|\binvoice\b|\bpayment\b"
    r"|\bdefect(s|ive)?\b|\bdispute[sd]?\b|\blitigation\b|\bsu(e|ed|ing)\b"
    r"|\bdelay(s|ed)?\b|\bpenalt(y|ies)\b|\bdamages\b"
    r"|价格|报价|费用|索赔|合同|合約|发票|發票|付款|缺陷|争议|爭議|罚款|罰款|延误|延誤)")

# A bare standard number may leave: `NZS 3604`, `AS/NZS 1170`, `ISO 9001`. A clause
# narrowed to the defect under dispute may not: `NZS 3604 clause 8.2.3` names which part
# of the code the argument is about, which is the argument.
_CLAUSE = re.compile(
    r"(?i)(\bclause\b|\bsection\b|\bpart\b|\btable\b|\bfig(ure)?\b|\bamend(ment)?\b"
    r"|第\s*\d|条款|條款"
    r"|\d+\s*[.:]\s*\d+\s*[.:]\s*\d)")           # 8.2.3 — two separators deep

# Digits are how a quantity or a claim value rides along inside something labelled a
# company. A standard is the one kind whose whole identity IS a number, so it is the one
# kind allowed to carry them.
_HAS_DIGIT = re.compile(r"\d")

# Latin person-name shape: two or three capitalised words and nothing else. "John Smith"
# is refused; "Naylor Love" would be too, which is the cost -- see _looks_like_a_person.
# A name word may carry an internal capital, and the first two attempts at this both
# missed one. `[a-z'’-]` cannot match the P in `Jean-Paul`; adding a hyphen branch still
# could not match the B in `O'Brien`, because the apostrophe had already been eaten as a
# lowercase character. Both walked straight through a check written to stop exactly them.
# The separator is optional here and the capital is not.
_NAME_WORD = r"[A-Z][a-z'’]{1,20}(?:[-'’]?[A-Z][a-z'’]{1,20})*"
_PERSON_SHAPE = re.compile(rf"^{_NAME_WORD}(?:\s+{_NAME_WORD}){{1,2}}$")

# What tells a firm from a person when both are two capitalised words. Not a complete
# list of the world's company suffixes and not meant to be: it only has to catch the
# forms a New Zealand construction meeting produces.
_CORPORATE = re.compile(
    r"(?i)\b(ltd|limited|llc|inc|plc|pty|group|holdings?|construction|constructions"
    r"|contracting|contractors?|builders?|building|engineering|engineers?|architects?"
    r"|constructors?|joinery|roofing|scaffolding|electrical|plumbing|concrete|steel"
    r"|glass|interiors?|carpentry|painters?|flooring|cladding|civil|projects?|developments?"
    r"|services?|solutions?|systems?|supplies|supply|trust|partners?|associates?"
    r"|worksafe|council|authority|commission|ministry|department|agency|board"
    r"|institute|standards|university|college|school|hospital|society|association"
    r"|zealand|australia|australasia|pacific|international|national)\b")

# A title is a role, not a person, and the role nouns are what make the query useful
# ("Naylor Love" CEO). Kept explicit rather than inferred.
_PUBLIC_ROLE = re.compile(
    r"(?i)^(ceo|cfo|coo|cto|managing director|director|chair(man|person)?|president"
    r"|founder|owner|general manager|gm|head of [a-z ]{2,20})$")

MAX_ENTITIES = 3


class Rejected:
    """One refusal and why. A tuple would have been shorter and unreadable at the call
    site, which is where these end up in a log line."""

    __slots__ = ("entity", "kind", "reason")

    def __init__(self, entity, kind, reason):
        self.entity, self.kind, self.reason = entity, kind, reason

    def __repr__(self):                                   # for log lines and assertions
        return f"Rejected({self.entity!r}, kind={self.kind!r}, reason={self.reason!r})"

    def __eq__(self, other):
        return (isinstance(other, Rejected) and self.entity == other.entity
                and self.kind == other.kind and self.reason == other.reason)


class GateResult:
    """`allowed` may leave. `truncated` means the cap bit, not that anything failed.

    `truncated` and a timeout are different facts and are never merged: this one is
    deterministic and knowable here, the other belongs to a step that has not run yet.
    """

    __slots__ = ("allowed", "rejected", "truncated")

    def __init__(self, allowed, rejected, truncated):
        self.allowed, self.rejected, self.truncated = allowed, rejected, truncated

    def __repr__(self):
        return (f"GateResult(allowed={self.allowed!r}, truncated={self.truncated!r}, "
                f"rejected={self.rejected!r})")


def _normalise(value) -> str:
    """NFKC + collapsed whitespace, or "" for anything that is not a string.

    NFKC and not NFC, which the first version used: NFC leaves full-width Latin alone, so
    `Naylor Love ｃｌａｉｍ` kept its commercial term and walked straight past the
    denylist. A search engine reads the two forms as the same word; the gate has to as
    well. Found by the normalisation test, not by review.

    Extraction is a model and may return a number, a null, or a nested object. None of
    those are entities; coercing them to `str()` would turn `{'entity': ...}` into a
    plausible-looking string and let it through.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _looks_like_a_person(entity: str) -> bool:
    """Two or three capitalised Latin words with no corporate marker.

    This refuses some real firms -- "Naylor Love" is exactly this shape -- and that is
    the direction the error has to fall. A refused company costs one card that would
    have said something the reader could have looked up themselves. A person's name sent
    to a search engine is the thing the customer was promised would not happen.

    It says nothing about CJK names: capitalisation does not exist there, and a rule
    invented for the shape of 张伟 would refuse most Chinese company names with it. That
    gap is real, stated, and covered by the length cap and the commercial-term check
    rather than pretended away.
    """
    if not _PERSON_SHAPE.match(entity):
        return False
    return not _CORPORATE.search(entity)


def screen_entity(entity, kind) -> str | None:
    """The reason this entity may not leave, or None if it may.

    Order matters only for which reason gets reported; every check is independent and
    all of them run against the same normalised string.
    """
    text = _normalise(entity)
    if not text:
        return "not a non-empty string"
    if len(text) > MAX_ENTITY_CHARS:
        return f"longer than {MAX_ENTITY_CHARS} chars — a sentence, not a name"

    kind_label = kind if isinstance(kind, str) else ""
    if kind_label.strip().lower() not in ALLOWED_KINDS:
        return f"kind {kind_label!r} is not in the allowlist"

    # --- from here down, nothing consults the label ---------------------------------
    if _COMMERCIAL.search(text):
        return "carries commercial terms"
    if _CLAUSE.search(text):
        return "narrowed to a clause, not a bare standard"

    is_standard = kind_label.strip().lower() == "standard"
    if _HAS_DIGIT.search(text) and not is_standard:
        return "carries digits and is not a standard"

    if _looks_like_a_person(text):
        return "shaped like a person's name"

    if kind_label.strip().lower() == "public_role" and not _PUBLIC_ROLE.match(text):
        return "not a recognised public role"

    return None


def screen(entities, max_entities: int = MAX_ENTITIES) -> GateResult:
    """Filter extraction's entities down to what may reach a search engine.

    `entities` is whatever extraction returned — a list of dicts is the contract, and
    anything else is treated as no entities rather than raising, because a malformed
    extraction must degrade to "no corroboration" and never to a 500 on the answer the
    reader is waiting for.

    Duplicates are collapsed case-insensitively before the cap, so three spellings of
    one company cannot spend the whole budget.
    """
    allowed: list[dict] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()

    if not isinstance(entities, list):
        return GateResult([], [], False)

    for item in entities:
        if not isinstance(item, dict):
            rejected.append(Rejected(item, None, "not an object"))
            continue
        entity, kind = item.get("entity"), item.get("kind")
        reason = screen_entity(entity, kind)
        if reason:
            rejected.append(Rejected(entity, kind, reason))
            continue
        text = _normalise(entity)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        allowed.append({"entity": text, "kind": kind.strip().lower(),
                        "claim": item.get("claim")})

    truncated = len(allowed) > max_entities
    return GateResult(allowed[:max_entities], rejected, truncated)
