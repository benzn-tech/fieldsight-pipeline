"""Named-target resolution for the action agent (spec 2026-11-05 §3.2.2; plan PR-3 Task 3.2).

The planner lists the items an instruction names (`Plan.targets`: a task number or a short
phrase). Gather looks each one up and this module decides, in code, which item it is:

* A target with a `task_no` resolves by the number alone. A numbered target NEVER falls
  through to text matching: "#12" that no row carries is `none`, not a look-alike.
* A phrase target scores every candidate item by containment: the share of the phrase's
  distinct tokens found in the item text's tokens. Both sides are normalised with the
  near-duplicate code's rules (`todo_collapse.normalise`, then `carry_forward._key`);
  Latin text splits into words, every CJK character is its own token, and a short fixed
  English stop-word list is dropped. (SequenceMatcher over whole texts, as
  `near_duplicate` uses, can never match a short phrase against a full item text.)

Outcome over the candidates scoring >= TARGET_MATCH_MIN:
    none       no candidate;
    resolved   one candidate, or a best candidate with no other within AMBIGUITY_MARGIN;
    ambiguous  two or more candidates within AMBIGUITY_MARGIN of the best (all of them
               are listed, best first).

The item's status is deliberately ignored: a named item that is already done must resolve
to itself (the writer then says "already done"), never to a newer open look-alike.

Pure: no I/O, no logging. Item text never leaves this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import carry_forward
import todo_collapse
from evidence_match import is_cjk

TARGET_MATCH_MIN = 0.8
AMBIGUITY_MARGIN = 0.15
_EPS = 1e-9                         # 1.0 - 0.85 is 0.15000000000000002 in floats

STOP_WORDS = frozenset({"the", "a", "an", "of", "to", "for", "on", "in", "at", "and", "or",
                        "item", "task"})

RESOLVED, AMBIGUOUS, NONE = "resolved", "ambiguous", "none"


@dataclass
class Resolution:
    kind: str                                   # resolved | ambiguous | none
    stable_id: str | None = None
    candidates: list = field(default_factory=list)


def tokens(text) -> list:
    """Normalised tokens in order: Latin/digit words, single CJK characters; stop words
    dropped. Punctuation (ASCII or full-width) separates."""
    norm = carry_forward._key(todo_collapse.normalise(text))
    out, buf = [], []

    def flush():
        if buf:
            word = "".join(buf)
            buf.clear()
            if word not in STOP_WORDS:
                out.append(word)

    for ch in norm:
        if not ch.isalnum():
            flush()
        elif is_cjk(ch):
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return out


def containment(phrase, text) -> float:
    """The share of the phrase's distinct tokens found in the text's tokens; 0.0 for a
    phrase with no tokens."""
    want = set(tokens(phrase))
    if not want:
        return 0.0
    return len(want & set(tokens(text))) / len(want)


def _task_no(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value.strip())
    return None


def resolve(target: dict, items: dict) -> Resolution:
    """`target` = a Plan.targets entry; `items` = {stable_id: items row} (any status)."""
    target = target if isinstance(target, dict) else {}
    no = _task_no(target.get("task_no"))
    if no is not None:
        hits = sorted(sid for sid, it in items.items()
                      if isinstance(it, dict) and _task_no(it.get("task_no")) == no)
        if len(hits) == 1:
            return Resolution(RESOLVED, hits[0], hits)
        return Resolution(AMBIGUOUS, None, hits) if hits else Resolution(NONE)
    phrase = target.get("phrase")
    if not isinstance(phrase, str) or not tokens(phrase):
        return Resolution(NONE)
    scored = []
    for sid, it in items.items():
        if not isinstance(it, dict):
            continue
        s = containment(phrase, it.get("text"))
        if s >= TARGET_MATCH_MIN:
            scored.append((s, str(sid)))
    if not scored:
        return Resolution(NONE)
    scored.sort(key=lambda p: (-p[0], p[1]))
    best = scored[0][0]
    close = [sid for s, sid in scored if best - s <= AMBIGUITY_MARGIN + _EPS]
    if len(close) == 1:
        return Resolution(RESOLVED, close[0], close)
    return Resolution(AMBIGUOUS, None, close)
