"""State builder for the Jev shadow evaluation (Track A).

The owner allowed exported human-labelled decisions to leave the country ONLY
as structured event JSON, never transcript text, with person names masked.
This module is the boundary that enforces both of those as privacy guards,
not formatting choices:

1. **Allowlist.** Each `set` (`programme_match`, `threads`, `work_class`) has
   a fixed, small set of fields it may send. Anything not on the allowlist is
   dropped. A key that *looks* like it carries a transcript fragment
   (`quote`, `turns`, `window`, `transcript`, `evidence`, `text_window`)
   raises instead of being silently dropped, even when nested arbitrarily
   deep in `features` -- Task 1's own shape test bans the same keys on the
   way in, so this is defense in depth against a shape neither side reviewed
   before it left the account. `work_class` sends ONLY `title` and
   `category` (owner decision, 2026-09-28) -- NOT `summary`: work_class
   positives are the recorder's private conversations (health, family), and
   `summary` is exactly where that private content lives. The label page may
   still show the summary to the owner locally; it never reaches this state.
2. **Masking.** Every string value that reaches the output state is passed
   through `mask_names`, which replaces person names with a `PERSON_n`
   placeholder, consistent within one call to `build_state`, and also masks
   email addresses (`EMAIL`) and phone-shaped digit runs (`PHONE`). The
   mapping (real name -> placeholder) lives only in memory for the duration
   of the call; it is never written, logged, or returned as part of the
   state.

Masking has these layers, in order:

- **Contact info.** Email addresses -> `EMAIL`; digit runs of 7 or more
  (allowing embedded single spaces/dashes and a leading `+`) -> `PHONE`,
  applied to every string. The one carved-out exception is a bare ISO
  `YYYY-MM-DD` date (this schema's `date` fields, e.g. `"2026-09-20"`, are
  exactly this shape and are 8-10 digits once the dashes are counted) --
  without the exception every `date` field in every set would be replaced
  with `PHONE`, which is not a privacy leak fixed, just data destroyed.
- Known corrections from `name_aliases` (kind='person'): both `wrong_term`
  and `right_term` are treated as the same person and masked to the same
  placeholder (the table stores ASR corrections, e.g. wrong "Ben Lynn" ->
  right "Ben Lin" -- both spellings must collapse to one PERSON_n). Task 1
  also feeds the company's user first name, last name AND full name in this
  way -- all three collapsing to the SAME placeholder, via an `alias_group`
  key shared across the three rows (a 2-term wrong_term/right_term row with
  no `alias_group` still works exactly as before, so this is additive, not
  a breaking change to the row shape). Matching is case-insensitive (so
  "ben and lin" masks) and on a word boundary for terms made of ASCII word
  characters -- never a bare substring check -- so that a single-token term
  like "Ben" masks "Ben said" and "Ben's" without also matching inside
  "Bench" or "Benefit". A term containing any CJK character is matched
  WITHOUT a word-boundary anchor: a word boundary is a transition between a
  word character and a non-word character, and CJK ideographs are all word
  characters, so there is never a boundary between two adjacent CJK
  characters -- an anchored match on "林本" cannot match "林本说明天去医院"
  at all, because the character after "本" is also "说" and is also a word
  character. The placeholder mapping is keyed on the alias's own canonical
  casing, never on whatever case the text happened to use.
- A generic two-capitalised-word pass using `_NAME_WORD` (imported, not
  copied, from `corroboration_gate`) for everything prod has not yet
  aliased. Prod currently has zero person aliases, so in practice this
  generic pass carries nearly all of the load. Its known cost (over-masking)
  is bounded two ways:
  - `name_aliases` rows of kind company/product (and, as of this fix, every
    exported company name, every exported site name and every programme
    task name of those sites -- see `export_labels.build_alias_rows`) are
    checked first and protected from it.
  - `_STOPLIST` (a module constant): a two-word match is skipped if EITHER
    word is a common sentence-start or construction word (see the constant
    itself for the exact list). This trades a small amount of under-masking
    (a real person named, say, "Roof" would slip through) for a large
    reduction in over-masking of ordinary construction phrases ("Roof
    Framing", "Level Two") that share the two-capitalised-word shape with a
    person's name but are not one.
  - The generic pass NEVER runs on `programme_match`'s `task.name` field at
    all (person aliases still apply to it) -- programme/task names are
    exactly the signal the baseline sees raw and Jev must not be handicapped
    against, and this field is short enough that the generic pass's
    non-overlapping match behaviour was masking the entire field
    (`"Roof Framing Inspection"` -> `"PERSON_1 Inspection"`) rather than a
    plausible name inside it.

  Even with the stoplist, an unaliased company/product term with no
  corporate marker and no stoplist word can still be masked. That is an
  accepted false positive, not a bug: a masked company name costs the
  reader a lookup; an unmasked person's name is the thing the owner
  promised would never leave.

  The residual gap runs the other way (under-masking): the generic pass is
  deliberately NOT broadened to catch single-token names (ruling: Task 1
  supplies known first/last names as aliases instead, see above) -- so a
  single first name that is in neither `users` nor `name_aliases` (a
  visitor, a subcontractor mentioned once) passes through unmasked. Closing
  that gap is Task 1's alias coverage, not a regex change here. A second,
  narrower residual: a non-person capitalised two-word phrase that happens
  to share a word with the stoplist AND still reads as ambiguous (e.g. a
  literal person surnamed "Roof") is not something either layer can
  distinguish from the construction term it exists to protect.
"""
from __future__ import annotations

import re
from typing import Any

from corroboration_gate import _NAME_WORD

# Keys that mean "this carries raw transcript text", checked at every depth of
# `features`, regardless of whether the key would otherwise be allowlisted.
_TRANSCRIPT_LIKE_KEYS = {"quote", "turns", "window", "transcript", "evidence", "text_window"}

# Exactly two capitalised words -- the brief's own scope for the generic pass.
# Not copied from corroboration_gate: only the name-word shape is reused.
# Captures each word separately so the stoplist can be checked per-token.
_TWO_TOKEN_NAME = re.compile(rf"({_NAME_WORD})\s+({_NAME_WORD})")

# Sentence-start and construction words that are NOT skipped from masking by
# themselves (a lone "Roof" is still ambiguous), but DO stop the generic
# two-token pass from firing when adjacent to another capitalised word --
# "Roof Framing", "Level Two", "The Scaffold" read as names by shape alone,
# and are the dominant source of over-masking on real site text. Trading a
# little under-masking (a person literally named e.g. "Roof") for a lot less
# over-masking of ordinary construction phrases; see the module docstring.
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_NUMBER_WORDS = (
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen", "Twenty",
)
_ORDINAL_WORDS = (
    "First", "Second", "Third", "Fourth", "Fifth",
    "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
)
_STOPLIST = frozenset({
    "The", "This", "That", "These", "Those", "A", "An",
    "Level", "Floor", "Site", "Ground", "Roof", "Stage", "Block", "Room",
    "Unit", "Area", "Zone", "North", "South", "East", "West", "Main", "New",
    "Old", "Phase", "Building", "Wall", "Door", "Window", "Stair",
    *_DAYS, *_MONTHS, *_NUMBER_WORDS, *_ORDINAL_WORDS,
})

# Contact info -- masked in every string, ahead of the name passes.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d(?:[ \-]?\d){6,}")
# The one carve-out: this schema's own ISO `date` fields ("2026-09-20") are
# 8-10 digits and would otherwise be swallowed whole by _PHONE_RE.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Fields where the generic two-token pass must never run (see docstring):
# dotted paths within the built `state` tree, matched exactly.
_NO_GENERIC_PATHS = frozenset({"task.name"})

_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
)


def _has_cjk(text: str) -> bool:
    return any(
        any(start <= ord(ch) <= end for start, end in _CJK_RANGES)
        for ch in text
    )


class JevStateError(ValueError):
    """Raised when `features` cannot be turned into a safe Jev state."""


def _raise_if_transcript_like(value: Any, path: str = "") -> None:
    """Walk the whole features tree and raise on any transcript-like key.

    This runs over the entire structure, not just the allowlisted parts --
    dropping a transcript-like key because it wasn't allowlisted anyway would
    hide the same leak this guard exists to catch.
    """
    if isinstance(value, dict):
        for key, sub_value in value.items():
            key_str = str(key).lower()
            if key_str in _TRANSCRIPT_LIKE_KEYS:
                where = f"{path}.{key}" if path else str(key)
                raise JevStateError(f"transcript-like key {key!r} found in features at {where!r}")
            _raise_if_transcript_like(sub_value, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _raise_if_transcript_like(item, f"{path}[{index}]")


def _copy_allowed(source: dict, keys: tuple) -> dict:
    return {key: source[key] for key in keys if key in source}


def _build_programme_match(features: dict) -> dict:
    state: dict = {}

    observation = features.get("observation")
    if isinstance(observation, dict):
        obs_out = _copy_allowed(observation, ("title", "summary", "date"))
        action_items = observation.get("action_items")
        if isinstance(action_items, list):
            items_out = [
                {"text": item["text"]}
                for item in action_items
                if isinstance(item, dict) and "text" in item
            ]
            if items_out:
                obs_out["action_items"] = items_out
        if obs_out:
            state["observation"] = obs_out

    task = features.get("task")
    if isinstance(task, dict):
        task_out = _copy_allowed(task, ("name", "status", "progress_pct", "start", "end"))
        if task_out:
            state["task"] = task_out

    return state


def _build_threads(features: dict) -> dict:
    state: dict = {}
    for side in ("earlier", "later"):
        side_value = features.get(side)
        if isinstance(side_value, dict):
            side_out = _copy_allowed(side_value, ("title", "summary", "date"))
            if side_out:
                state[side] = side_out
    if "gap_days" in features:
        state["gap_days"] = features["gap_days"]
    return state


def _build_work_class(features: dict) -> dict:
    # Owner decision 2026-09-28: title + category ONLY, no summary --
    # work_class positives are the recorder's private conversations (health,
    # family), and summary is exactly where that content lives.
    return _copy_allowed(features, ("title", "category"))


_BUILDERS = {
    "programme_match": _build_programme_match,
    "threads": _build_threads,
    "work_class": _build_work_class,
}


class _Masker:
    """Holds the one PERSON_n mapping for a single build_state/mask_names call."""

    def __init__(self, aliases: list | None) -> None:
        self._person_pairs: list[tuple[str, ...]] = []
        self._protected: set[str] = set()

        # Person aliases group by `alias_group` when present (so first name,
        # last name and full name of one user collapse to the SAME
        # placeholder even though they arrive as separate rows); a row with
        # no `alias_group` behaves exactly as before -- its own wrong/right
        # pair is one group of its own.
        groups: dict[Any, list[str]] = {}
        group_order: list = []
        for alias in aliases or []:
            if not isinstance(alias, dict):
                continue
            kind = alias.get("kind")
            wrong = alias.get("wrong_term")
            right = alias.get("right_term")
            if kind == "person":
                terms = [t for t in (wrong, right) if t]
                group = alias.get("alias_group")
                if group is None:
                    if terms:
                        self._person_pairs.append(tuple(terms))
                else:
                    bucket = groups.setdefault(group, [])
                    for term in terms:
                        if term not in bucket:
                            bucket.append(term)
                    if group not in group_order:
                        group_order.append(group)
            elif kind in ("company", "product"):
                for term in (wrong, right):
                    if term:
                        self._protected.add(term)
        for group in group_order:
            self._person_pairs.append(tuple(groups[group]))

        self.mapping: dict[str, str] = {}
        self._next_id = 1

        # Compiled once per _Masker instance (the alias pairs are fixed at
        # construction time and never mutated afterwards), reused across
        # every call to mask() instead of being recompiled per string.
        # ASCII (word-character) terms get a `\b...\b` anchor; a term
        # containing any CJK character gets none, because `\b` never fires
        # between two adjacent CJK characters (see module docstring) --
        # each alternative in the alternation carries its own anchoring
        # rather than one `\b` wrapping the whole group.
        all_terms = sorted(
            {term for pair in self._person_pairs for term in pair},
            key=len,
            reverse=True,
        )
        if all_terms:
            parts = [
                re.escape(term) if _has_cjk(term)
                else r"\b" + re.escape(term) + r"\b"
                for term in all_terms
            ]
            self._alias_pattern = re.compile("|".join(parts), re.IGNORECASE)
            self._casefold_to_term: dict[str, str] = {}
            for term in all_terms:
                self._casefold_to_term.setdefault(term.casefold(), term)
        else:
            self._alias_pattern = None
            self._casefold_to_term = {}

    def _next_placeholder(self) -> str:
        placeholder = f"PERSON_{self._next_id}"
        self._next_id += 1
        return placeholder

    def _mask_contact_info(self, text: str) -> str:
        text = _EMAIL_RE.sub("EMAIL", text)

        def _replace_phone(match: re.Match) -> str:
            candidate = match.group(0)
            if _ISO_DATE_RE.fullmatch(candidate):
                return candidate
            return "PHONE"

        return _PHONE_RE.sub(_replace_phone, text)

    def _replace_alias(self, match: re.Match) -> str:
        # Matching is case-insensitive; the placeholder mapping is always
        # keyed on the alias's own canonical casing, not whatever case the
        # text happened to use ("ben and lin" still resolves to the "Ben"/
        # "Ben Lin" alias terms).
        matched_text = match.group(0)
        term = self._casefold_to_term.get(matched_text.casefold(), matched_text)
        placeholder = self.mapping.get(term)
        if placeholder is None:
            pair = next(p for p in self._person_pairs if term in p)
            placeholder = next((self.mapping[t] for t in pair if t in self.mapping), None)
            if placeholder is None:
                placeholder = self._next_placeholder()
            for t in pair:
                self.mapping[t] = placeholder
        return placeholder

    def _replace_generic(self, match: re.Match) -> str:
        candidate = match.group(0)
        if candidate in self._protected:
            return candidate
        if match.group(1) in _STOPLIST or match.group(2) in _STOPLIST:
            return candidate
        placeholder = self.mapping.get(candidate)
        if placeholder is None:
            placeholder = self._next_placeholder()
            self.mapping[candidate] = placeholder
        return placeholder

    def mask(self, text: str, apply_generic: bool = True) -> str:
        if not isinstance(text, str) or not text:
            return text

        # Layer 0: contact info -- emails and phone-shaped digit runs, in
        # every string, ahead of the name passes.
        text = self._mask_contact_info(text)

        # Layer 1: known person alias pairs/groups (wrong_term / right_term,
        # or a whole alias_group, collapse to the same placeholder). Matched
        # case-insensitively; ASCII terms on a word boundary -- not
        # `str.replace` -- so a single-token term like "Ben" masks "Ben
        # said" and "Ben's" but leaves "Bench" and "Benefit" alone. Longest
        # term first (both in the sort and in the regex alternation, which
        # tries alternatives left-to-right) so "Ben Lin" wins over "Ben" when
        # both are present. "Ben" and "Ben Lin" get the SAME placeholder only
        # if they come from the same alias group (an explicit `alias_group`,
        # or one row's own wrong_term/right_term pair containing both); two
        # separate, ungrouped alias rows -- one for the first name, one for
        # the full name -- get separate placeholders, since nothing ties them
        # together as the same person. Pinned by test.
        if self._alias_pattern is not None:
            text = self._alias_pattern.sub(self._replace_alias, text)

        # Layer 2: generic two-capitalised-word name shape, skipping known
        # company/product alias terms and any pair touching `_STOPLIST`.
        # Skipped entirely for fields in `_NO_GENERIC_PATHS` (task.name).
        if apply_generic:
            text = _TWO_TOKEN_NAME.sub(self._replace_generic, text)

        return text


def _mask_tree(value: Any, masker: _Masker, path: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: _mask_tree(sub_value, masker, f"{path}.{key}" if path else str(key))
            for key, sub_value in value.items()
        }
    if isinstance(value, list):
        return [_mask_tree(item, masker, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, str):
        apply_generic = path not in _NO_GENERIC_PATHS
        return masker.mask(value, apply_generic=apply_generic)
    return value


def mask_names(text: str, aliases: list) -> tuple[str, dict]:
    """Mask person names (and contact info) in `text`, returning
    (masked_text, mapping).

    `mapping` (real name -> PERSON_n) is returned to the caller for this one
    call only; `build_state` keeps its own mapping in memory across the whole
    state and never surfaces it in the output.
    """
    masker = _Masker(aliases)
    masked = masker.mask(text)
    return masked, dict(masker.mapping)


def build_state(set_name: str, features: dict, aliases: list) -> dict:
    """Build the small JSON state sent to the third-party (Jev) model.

    Raises `JevStateError` if `features` contains a transcript-like key
    anywhere, or if `set_name` is not one of the known sets.
    """
    if not isinstance(features, dict):
        raise JevStateError(f"features must be a dict, got {type(features).__name__}")

    builder = _BUILDERS.get(set_name)
    if builder is None:
        raise JevStateError(f"unknown set: {set_name!r}")

    _raise_if_transcript_like(features)

    state = builder(features)
    masker = _Masker(aliases)
    return _mask_tree(state, masker)
