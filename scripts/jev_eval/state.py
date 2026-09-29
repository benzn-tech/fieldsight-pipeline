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
    checked first and protected from it -- but only MULTI-TOKEN terms of 3+
    characters, matched on ASCII word boundaries (fix wave 5): a single-token
    protected term ("Hawkins", "UC", a task named "Line") never shields a
    person alias or a two-token name candidate ("Tom Hawkins", "Caroline
    Smith").
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

  Fix wave 6: even with the stoplist, the generic pass was measured (on the
  real exported data) to mask almost every title-case TOPIC HEADING it saw
  ("Material Procurement", "Scaffolding Safety", "Slab Rebar", "General
  Reflection", "Anything Else", ...) because headings share its
  two-capitalised-word shape. A candidate is now masked only if NEITHER
  token is a "known common word": `_BUILT_IN_COMMON_WORDS` (a fixed module
  constant), OR a lowercase word present in an optional `common_words` set
  the caller supplies to `mask_names`/`build_state` -- the runner builds this
  once per run from the lowercase words in every selected row's own
  allowlisted title/summary text, before any state is built, so the same
  gate applies uniformly to broad, decomposed and control states. This never
  weakens person-alias matching: a KNOWN person alias (from `name_aliases`)
  is always masked even if it is also a common word (the existing
  `_COMMON_WORD_ALIASES` case rules for "Will"/"Mark"/etc. are unchanged) --
  only the generic, alias-free two-token pass is gated this way. Accepted
  residual: if the run's own corpus happens to contain a common word that
  is also a real surname (e.g. the corpus contains "wood" and a genuine name
  "Wood Ward" appears elsewhere in the same run), that name is left
  unmasked -- the corpus gate cannot distinguish the two once "wood" is in
  the run's vocabulary. Same shape of trade as the stoplist below: a little
  under-masking for a lot less over-masking. See also
  `docs/superpowers/specs/2026-09-28-jev-shadow-eval-findings.md` §4.

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
# Fix wave 4, C13: month names are deliberately NOT in `_STOPLIST` (they used
# to be). A month followed by a real name ("May Chen", "June Wilson", "April
# Ng") must be masked -- a month name is not, by itself, evidence the second
# word is not a person. The date case ("May 2026", "June 3") never reaches
# `_TWO_TOKEN_NAME` at all: `_NAME_WORD` requires the token to START WITH A
# LETTER, so a digit-first second token structurally never matches the
# two-capitalised-word shape in the first place -- no digit-specific check is
# needed here. A spelled-out ordinal date ("May Third") is still protected,
# because "Third" is (and remains) in `_ORDINAL_WORDS` below, independently
# of whatever precedes it.
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
    # Fix wave 4, C12: sentence-initial relative-day words -- without these,
    # "Yesterday Sarah Jones" reads "Yesterday Sarah" as a candidate name pair
    # (neither word stoplisted) before "Sarah Jones" is ever tried.
    "Yesterday", "Today", "Tomorrow",
    *_DAYS, *_NUMBER_WORDS, *_ORDINAL_WORDS,
})

# Fix wave 4, C14: alias terms that are ordinary English words are matched
# in their CAPITALISED and ALL-CAPS forms only, whatever casing the user row
# stored (fix wave 5) -- an unqualified case-insensitive
# match on a word this common (e.g. "will", "may", "rose") over-masks
# construction text far more than it protects a real name. Every other alias
# term stays case-insensitive, as before. Compared against an alias term's
# OWN casefold, never the matched text's case.
#
# "ben" is deliberately NOT in this list even though the brief's own draft
# names it: it is the primary account holder's own first name, aliased on
# every export (`export_labels.build_alias_rows`), and under-masking it
# (missing a lowercase "ben" the way this list would produce) is the
# privacy failure this module exists to prevent -- over-masking the common
# word "ben" is comparatively harmless and rare in construction text.
_COMMON_WORD_ALIASES = frozenset({
    "will", "may", "mark", "love", "bill", "grant", "rose", "hope", "joy",
    "faith", "dawn", "rich", "frank", "ray", "sky", "summer", "june", "april",
    "august", "jack", "pat", "sue", "drew", "gene", "lee", "long", "young",
    "white", "black", "brown", "green", "king", "hall", "wood", "stone",
    "field", "park", "bell", "hill", "ward", "page", "lane", "cook", "rob",
    "art", "chance", "sunny", "max",
})

# Fix wave 6: the generic two-token pass, measured against the real exported
# data, was masking title-case TOPIC HEADINGS -- "Material Procurement" (11),
# "Scaffolding Safety" (9), "Slab Rebar" (7), "Concrete Testing", "Device
# Testing", "Route Planning", "Subcontractor Access", "Crane Restrictions",
# "Design Issues", "Electrical Cables", "Weekly Schedule", "Recording
# Device", "General Reflection", "Anything Else" -- while correctly catching
# real names of the same two-capitalised-word shape ("Hector Eggar", "Paul
# Smith", "Liang Min", "Yang Ming"). Controller's ruling: a generic candidate
# is masked only if NEITHER token is a "known common word" -- this built-in
# module constant, OR a word the CALLER supplies via `common_words` (the
# runner computes it once per run from the lowercase words across every
# row's allowlisted title/summary text, so the same rule applies uniformly
# to broad, decomposed and control states). This is unrelated to
# `_COMMON_WORD_ALIASES` above (which governs case rules for known PERSON
# aliases, e.g. "Will"/"Mark") and to `_STOPLIST` (which still gates the
# lead-token scan as before) -- both keep their existing behaviour.
#
# Checked against a token's *lowercase* form, so it matches regardless of
# the casing the generic pass's title-case shape requires.
#
# Accepted residual (measured, not hypothetical): if the per-run corpus
# happens to contain a common word that is ALSO a real surname (e.g. the
# corpus contains "wood" somewhere and a genuine name "Wood Ward" appears
# elsewhere), that name is left unmasked -- the corpus gate cannot tell
# "wood" the common word from "Wood" the surname once its lowercase form
# is in the run's own vocabulary. Same trade as the stoplist (see the
# module docstring and fix wave 4): a little under-masking in exchange for
# a lot less over-masking. See also
# `docs/superpowers/specs/2026-09-28-jev-shadow-eval-findings.md` §4.
_BUILT_IN_COMMON_WORDS = frozenset({
    # From the brief's own measured examples (topic headings that were
    # being over-masked):
    "material", "procurement", "scaffolding", "safety", "slab", "rebar",
    "concrete", "testing", "device", "route", "planning", "subcontractor",
    "access", "crane", "restrictions", "design", "issues", "electrical",
    "cables", "weekly", "schedule", "recording", "general", "reflection",
    "anything", "else",
    # Other common heading/construction words named in the brief.
    "update", "review", "discussion", "meeting", "plan", "issue",
    "progress", "inspection", "delivery", "system", "platform", "client",
    "site", "project", "strategy", "check", "setup", "coordination",
    "options", "daily", "upcoming",
})

# Contact info -- masked in every string, ahead of the name passes.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d(?:[ \-]?\d){6,}")
# The one carve-out: this schema's own ISO `date` fields ("2026-09-20") are
# 8-10 digits and would otherwise be swallowed whole by _PHONE_RE.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Fix wave 4, C16: an ISO date immediately followed by a bare time
# ("2026-09-20 0800") is one continuous digit run under _PHONE_RE's own
# rules (it allows a single embedded space) and must not become "PHONE"
# either.
_ISO_DATE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{3,6}$")
# Fix wave 5, item 2: the ONLY suffixes that make a digit run a money
# amount rather than a phone number.
_MONEY_SUFFIX_RE = re.compile(r"\s*(?:k|m|million)\b", re.IGNORECASE)

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

    def __init__(self, aliases: list | None, common_words: set | None = None) -> None:
        self._person_pairs: list[tuple[str, ...]] = []
        self._protected: set[str] = set()
        # Fix wave 6: lowercase common-word gate for the generic pass (see
        # `_BUILT_IN_COMMON_WORDS`) -- the caller-supplied corpus words are
        # additive to the built-in list, never a replacement for it.
        self._common_words: frozenset = frozenset(
            {w.lower() for w in (common_words or ())}
        ) | _BUILT_IN_COMMON_WORDS

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
        #
        # Fix wave 4, C11: an ASCII (word-character) term used to get a
        # `\b...\b` anchor. Python's `\b` is a transition between \w and
        # non-\w, and Python's default (Unicode) `\w` treats CJK ideographs
        # as word characters too -- so there is NEVER a `\b` boundary between
        # an ASCII letter and an immediately adjacent CJK character, and
        # "Ben说好" (no space) could never match `\bBen\b` at all. ASCII terms
        # now use explicit ASCII-only lookaround assertions instead --
        # `(?<![A-Za-z0-9])term(?![A-Za-z0-9])` -- which only refuses to
        # match when the ADJACENT character is itself an ASCII letter/digit
        # (so "Bench"/"Benefit" still correctly do not match), and happily
        # matches next to a CJK character, punctuation, or string edge. A
        # term containing any CJK character keeps no anchor at all, because
        # `\b` (and this ASCII lookaround) never fires between two adjacent
        # CJK characters either way (see module docstring).
        #
        # Fix wave 4, C14: alias terms that are ordinary English words
        # (`_COMMON_WORD_ALIASES`) match only their capitalised and ALL-CAPS
        # forms (fix wave 5); every other term stays case-insensitive. This
        # is done per-alternative with Python's scoped inline-flag group
        # `(?i:...)`, rather than one `re.IGNORECASE` over the whole pattern,
        # so the two behaviours can coexist inside one compiled regex.
        all_terms = sorted(
            {term for pair in self._person_pairs for term in pair},
            key=len,
            reverse=True,
        )
        if all_terms:
            parts = []
            for term in all_terms:
                escaped = re.escape(term)
                body = escaped if _has_cjk(term) else (
                    r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"
                )
                if term.casefold() in _COMMON_WORD_ALIASES:
                    # Fix wave 5, item 7: the capitalised form and the
                    # ALL-CAPS form, whatever casing the user row stored
                    # ("will"/"Will"/"WILL" all mask "Will" and "WILL") --
                    # never the lowercase common word.
                    lower = term.casefold()
                    forms = {lower[:1].upper() + lower[1:], lower.upper()}
                    alternation = "|".join(re.escape(f) for f in sorted(forms))
                    parts.append(
                        r"(?<![A-Za-z0-9])(?:" + alternation + r")(?![A-Za-z0-9])")
                else:
                    parts.append(f"(?i:{body})")
            self._alias_pattern = re.compile("|".join(parts))
            self._casefold_to_term: dict[str, str] = {}
            for term in all_terms:
                self._casefold_to_term.setdefault(term.casefold(), term)
        else:
            self._alias_pattern = None
            self._casefold_to_term = {}

        # Fix wave 4, C15 / fix wave 5, item 1: protected company/site/task
        # terms are substituted for an opaque sentinel BEFORE both the alias
        # pass and the generic pass, and restored afterwards -- so "SB1108
        # Ellesmere College pour", "Smith Scaffolding Ltd" and "Naylor Love's
        # crew" (with a user surnamed Love) keep their names.
        #
        # Wave 4 matched these terms as unanchored, case-insensitive
        # SUBSTRINGS and let every one of them win over masking -- so a task
        # named "Line" shielded "Caroline", a site named "UC" shielded
        # "Lucy", a single-letter task "A" shielded every "a", and a company
        # named "Hawkins" shielded "Tom Hawkins". Names masked before wave 4
        # left unmasked. The rules now (controller ruling, fix wave 5):
        #   - a protected term is ANCHORED with the same ASCII letter/digit
        #     lookarounds as the alias terms (CJK terms unanchored, as
        #     there), so it never matches inside another word;
        #   - a term shorter than 3 characters protects nothing;
        #   - only MULTI-TOKEN terms are sentinel-protected. A single-token
        #     term never overrides a person alias or a generic two-token
        #     name candidate ("Tom Hawkins" is masked even though "Hawkins"
        #     is a company) -- and since a lone capitalised token is never
        #     masked by either pass unless it IS a person alias, a
        #     single-token protected term has nothing left to protect, so it
        #     is simply not added to the sentinel pattern.
        protected_multi = sorted(
            (term for term in self._protected
             if len(term) >= 3 and len(term.split()) >= 2),
            key=len, reverse=True,
        )
        if protected_multi:
            protected_parts = []
            for term in protected_multi:
                body = re.escape(term) + r"(?:['’]s)?"
                if not _has_cjk(term):
                    body = r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"
                protected_parts.append(body)
            self._protected_pattern = re.compile(
                "(?:" + "|".join(protected_parts) + ")", re.IGNORECASE)
        else:
            self._protected_pattern = None

    def _next_placeholder(self) -> str:
        placeholder = f"PERSON_{self._next_id}"
        self._next_id += 1
        return placeholder

    def _mask_contact_info(self, text: str) -> str:
        text = _EMAIL_RE.sub("EMAIL", text)

        def _replace_phone(match: re.Match) -> str:
            # Fix wave 4, C16: two more digit-run shapes that are not a
            # phone number and must not be swallowed. `full_text`/`start`/
            # `end` let both checks look OUTSIDE the matched span itself
            # (the "$" before it, the "k"/"m"/"million" suffix after it) --
            # neither is part of `_PHONE_RE`'s own match.
            candidate = match.group(0)
            if _ISO_DATE_RE.fullmatch(candidate):
                return candidate
            if _ISO_DATE_TIME_RE.fullmatch(candidate):
                return candidate
            full_text = match.string
            start, end = match.start(), match.end()
            if start > 0 and full_text[start - 1] == "$":
                return candidate
            # Fix wave 5, item 2: only a bare k / m / million suffix marks a
            # money amount -- wave 4 exempted any following WORD that merely
            # started with k or m ("021 555 1234 mate", "... mobile", "...
            # kept ringing"), which left real phone numbers unmasked.
            if _MONEY_SUFFIX_RE.match(full_text, end):
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

    def _apply_generic_pass(self, text: str) -> str:
        """Fix wave 4, C12: scans for the two-capitalised-word shape with an
        explicit position cursor instead of `re.sub` (which only ever
        advances to the END of the current match). The old code, on hitting
        a stoplisted pair like "Friday Sarah", consumed BOTH tokens and
        resumed scanning after "Sarah" -- so "Sarah Jones" was never tried as
        its own pair, and "Friday Sarah Jones confirmed" came out completely
        unmasked. Here, a skip (stoplist OR a still-protected two-word
        candidate) advances the cursor to the end of the FIRST token only,
        so the second token gets a fresh chance to pair with whatever
        follows it -- "Friday Sarah Jones confirmed" -> "Friday PERSON_1
        confirmed"."""
        out = []
        pos = 0
        length = len(text)
        while pos < length:
            match = _TWO_TOKEN_NAME.search(text, pos)
            if not match:
                out.append(text[pos:])
                break
            word1, word2 = match.group(1), match.group(2)
            if (
                word1 in _STOPLIST or word2 in _STOPLIST
                or word1.lower() in self._common_words
                or word2.lower() in self._common_words
            ):
                first_end = match.end(1)
                out.append(text[pos:first_end])
                pos = first_end
                continue
            candidate = match.group(0)
            if candidate in self._protected:
                out.append(text[pos:match.end()])
                pos = match.end()
                continue
            placeholder = self.mapping.get(candidate)
            if placeholder is None:
                placeholder = self._next_placeholder()
                self.mapping[candidate] = placeholder
            out.append(text[pos:match.start()])
            out.append(placeholder)
            pos = match.end()
        return "".join(out)

    def _protect(self, text: str) -> tuple[str, dict]:
        """Fix wave 4, C15: substitute every occurrence of a protected
        company/site/task term for an opaque sentinel BEFORE the alias and
        generic passes run, so neither pass can see (and therefore cannot
        shred) any part of it -- restored verbatim by `_unprotect`."""
        if self._protected_pattern is None:
            return text, {}
        stash: dict[str, str] = {}

        def _sub(match: re.Match) -> str:
            key = f"\x00PROT{len(stash)}\x00"
            stash[key] = match.group(0)
            return key

        return self._protected_pattern.sub(_sub, text), stash

    def _unprotect(self, text: str, stash: dict) -> str:
        for key, original in stash.items():
            text = text.replace(key, original)
        return text

    def mask(self, text: str, apply_generic: bool = True) -> str:
        if not isinstance(text, str) or not text:
            return text

        # Layer 0: contact info -- emails and phone-shaped digit runs, in
        # every string, ahead of the name passes.
        text = self._mask_contact_info(text)

        # Fix wave 4, C15: protect company/site/task terms (any word count,
        # case-insensitive, possessive "'s" allowed) BEFORE either name pass
        # sees the text at all.
        text, protect_stash = self._protect(text)

        # Layer 1: known person alias pairs/groups (wrong_term / right_term,
        # or a whole alias_group, collapse to the same placeholder). Matched
        # case-insensitively (except `_COMMON_WORD_ALIASES` terms, fix wave
        # 4 C14 / wave 5 -- capitalised and ALL-CAPS forms only); ASCII terms
        # anchored so they cannot match inside a longer ASCII word (fix wave
        # 4 C11) -- so a single-token term like "Ben" masks "Ben said" and
        # "Ben's" but leaves "Bench" and "Benefit" alone, AND masks "Ben说好"
        # (no space before the CJK). Longest term first (both in the sort
        # and in the regex alternation, which tries alternatives
        # left-to-right) so "Ben Lin" wins over "Ben" when both are present.
        # "Ben" and "Ben Lin" get the SAME placeholder only if they come from
        # the same alias group (an explicit `alias_group`, or one row's own
        # wrong_term/right_term pair containing both); two separate,
        # ungrouped alias rows -- one for the first name, one for the full
        # name -- get separate placeholders, since nothing ties them
        # together as the same person. Pinned by test.
        if self._alias_pattern is not None:
            text = self._alias_pattern.sub(self._replace_alias, text)

        # Layer 2: generic two-capitalised-word name shape, skipping known
        # company/product alias terms and any pair touching `_STOPLIST`, with
        # a skip advancing only past the first token (fix wave 4, C12) so a
        # stoplisted lead word never swallows the real name that follows it.
        # Skipped entirely for fields in `_NO_GENERIC_PATHS` (task.name).
        if apply_generic:
            text = self._apply_generic_pass(text)

        text = self._unprotect(text, protect_stash)

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


def mask_names(text: str, aliases: list, common_words: set | None = None) -> tuple[str, dict]:
    """Mask person names (and contact info) in `text`, returning
    (masked_text, mapping).

    `common_words` (optional): lowercase words the caller has determined are
    common/ordinary in this run's own text (e.g. every word appearing in the
    titles/summaries of the rows being masked) -- see `_BUILT_IN_COMMON_WORDS`
    for how this is used to stop the generic two-token pass from over-masking
    title-case headings. Additive to the built-in list, never a replacement.

    `mapping` (real name -> PERSON_n) is returned to the caller for this one
    call only; `build_state` keeps its own mapping in memory across the whole
    state and never surfaces it in the output.
    """
    masker = _Masker(aliases, common_words)
    masked = masker.mask(text)
    return masked, dict(masker.mapping)


def build_state(
    set_name: str, features: dict, aliases: list, common_words: set | None = None
) -> dict:
    """Build the small JSON state sent to the third-party (Jev) model.

    `common_words` (optional): see `mask_names`. The runner computes this
    once per run, across every selected set's rows, before any state is
    built, so broad/decomposed/control states all see the same gate.

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
    masker = _Masker(aliases, common_words)
    return _mask_tree(state, masker)


# ---------------------------------------------------------------------------
# Corpus-word helpers for the runner (`scripts/jev_shadow_eval.py`): compute
# the per-run `common_words` set from the ALLOWLISTED text of every row,
# before any state (and therefore any mapping) is built. Reuses the same
# `_BUILDERS` the real state build uses, so the corpus is drawn from exactly
# the fields that would be sent -- never anything outside the allowlist.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z]+")


def build_raw_allowed(set_name: str, features: dict) -> dict:
    """Same field allowlist as `build_state`, but returns the UNMASKED
    allowed fields -- used only to harvest corpus words for `common_words`,
    never sent anywhere."""
    if not isinstance(features, dict):
        raise JevStateError(f"features must be a dict, got {type(features).__name__}")
    builder = _BUILDERS.get(set_name)
    if builder is None:
        raise JevStateError(f"unknown set: {set_name!r}")
    return builder(features)


def extract_words(value: Any) -> set[str]:
    """Walk a (possibly nested) dict/list/str tree and return every ASCII
    alphabetic word found in any string, lowercased."""
    words: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for sub in node.values():
                _walk(sub)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            for w in _WORD_RE.findall(node):
                words.add(w.lower())

    _walk(value)
    return words
