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
   before it left the account.
2. **Masking.** Every string value that reaches the output state is passed
   through `mask_names`, which replaces person names with a `PERSON_n`
   placeholder, consistent within one call to `build_state`. The mapping
   (real name -> placeholder) lives only in memory for the duration of the
   call; it is never written, logged, or returned as part of the state.

Masking has two layers:

- Known corrections from `name_aliases` (kind='person'): both `wrong_term`
  and `right_term` are treated as the same person and masked to the same
  placeholder (the table stores ASR corrections, e.g. wrong "Ben Lynn" ->
  right "Ben Lin" -- both spellings must collapse to one PERSON_n). Task 1
  also feeds the company's user first and last names in this way, some of
  them single tokens (e.g. "Heidi"), so this layer matches on a word
  boundary, case-sensitively -- never a bare substring check -- so that a
  single-token term like "Ben" masks "Ben said" and "Ben's" without also
  matching inside "Bench" or "Benefit".
- A generic two-capitalised-word pass using `_NAME_WORD` (imported, not
  copied, from `corroboration_gate`) for everything prod has not yet
  aliased. Prod currently has zero person aliases, so in practice this
  generic pass carries nearly all of the load. Its known cost (over-masking):
  it will also mask some non-person capitalised two-word strings that happen
  to share the shape of a name (e.g. "Level One"), which is why
  `name_aliases` rows of kind company/product are checked first and
  protected from it -- but an unaliased company/product term with no
  corporate marker will still be masked. That is an accepted false positive,
  not a bug: a masked company name costs the reader a lookup; an unmasked
  person's name is the thing the owner promised would never leave.

  The residual gap runs the other way (under-masking): the generic pass is
  deliberately NOT broadened to catch single-token names (ruling: Task 1
  supplies known first/last names as aliases instead, see above) -- so a
  single first name that is in neither `users` nor `name_aliases` (a
  visitor, a subcontractor mentioned once) passes through unmasked. Closing
  that gap is Task 1's alias coverage, not a regex change here.
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
_TWO_TOKEN_NAME = re.compile(rf"{_NAME_WORD}\s+{_NAME_WORD}")


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
    return _copy_allowed(features, ("title", "summary", "category"))


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
        for alias in aliases or []:
            if not isinstance(alias, dict):
                continue
            kind = alias.get("kind")
            wrong = alias.get("wrong_term")
            right = alias.get("right_term")
            if kind == "person":
                pair = tuple(t for t in (wrong, right) if t)
                if pair:
                    self._person_pairs.append(pair)
            elif kind in ("company", "product"):
                for term in (wrong, right):
                    if term:
                        self._protected.add(term)

        self.mapping: dict[str, str] = {}
        self._next_id = 1

    def _next_placeholder(self) -> str:
        placeholder = f"PERSON_{self._next_id}"
        self._next_id += 1
        return placeholder

    def mask(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text

        # Layer 1: known person alias pairs (wrong_term / right_term collapse
        # to the same placeholder). Matched on a word boundary, case-sensitive
        # regex -- not `str.replace` -- so a single-token term like "Ben"
        # masks "Ben said" and "Ben's" but leaves "Bench" and "Benefit"
        # alone. Longest term first (both in the sort and in the regex
        # alternation, which tries alternatives left-to-right) so "Ben Lin"
        # wins over "Ben" when both are present. "Ben" and "Ben Lin" get the
        # SAME placeholder only if they come from the same alias row (i.e.
        # one row's wrong_term/right_term pair contains both); two separate
        # alias rows -- one for the first name, one for the full name -- get
        # separate placeholders, since nothing ties them together as the same
        # person. Pinned by test.
        all_terms = sorted(
            {term for pair in self._person_pairs for term in pair},
            key=len,
            reverse=True,
        )
        if all_terms:
            alias_pattern = re.compile(
                r"\b(?:" + "|".join(re.escape(term) for term in all_terms) + r")\b"
            )

            def _replace_alias(match: re.Match) -> str:
                term = match.group(0)
                placeholder = self.mapping.get(term)
                if placeholder is None:
                    pair = next(p for p in self._person_pairs if term in p)
                    placeholder = next((self.mapping[t] for t in pair if t in self.mapping), None)
                    if placeholder is None:
                        placeholder = self._next_placeholder()
                    for t in pair:
                        self.mapping[t] = placeholder
                return placeholder

            text = alias_pattern.sub(_replace_alias, text)

        # Layer 2: generic two-capitalised-word name shape, skipping anything
        # protected as a known company/product alias term.
        def _replace(match: re.Match) -> str:
            candidate = match.group(0)
            if candidate in self._protected:
                return candidate
            placeholder = self.mapping.get(candidate)
            if placeholder is None:
                placeholder = self._next_placeholder()
                self.mapping[candidate] = placeholder
            return placeholder

        return _TWO_TOKEN_NAME.sub(_replace, text)


def _mask_tree(value: Any, masker: _Masker) -> Any:
    if isinstance(value, dict):
        return {key: _mask_tree(sub_value, masker) for key, sub_value in value.items()}
    if isinstance(value, list):
        return [_mask_tree(item, masker) for item in value]
    if isinstance(value, str):
        return masker.mask(value)
    return value


def mask_names(text: str, aliases: list) -> tuple[str, dict]:
    """Mask person names in `text`, returning (masked_text, mapping).

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
