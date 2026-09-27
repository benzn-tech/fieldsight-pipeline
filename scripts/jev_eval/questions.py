"""Question sets for the Jev shadow evaluation (Track A, Task 4).

Wire format ruling (2026-09-27): the question schema each arm below emits is
the one documented at `docs.typesafe.ai/api.md` (`type` / `instructions` /
`criteria`), fetched and verified raw with `curl` -- NOT the `prompt` /
`options` shape used incidentally as fixture data in
`tests/unit/test_systemone_client.py`. Those fixtures only ever assert
pass-through (`body["questions"] == questions`) and assert nothing about the
real contract; they will be aligned to this shape separately, not here.

Per that shape:
- Every question is `{"type": ..., "instructions": <question text>}`.
- `noul` questions never carry `criteria` here: each brief sentence is the
  whole question, so the optional true/false rubric adds nothing.
- `choice` questions carry `criteria` as `{option: one-line rubric, ...}`
  (required by the wire format) -- the option keys are exactly the ones the
  task brief lists; the rubric text for each was written here (the brief did
  not supply it) to be plain, observable and mutually exclusive.
- `score` is not used anywhere in this module: the brief's three decisions
  only need `noul` and `choice`.

`QUESTION_SETS[set_name]` has five keys per the controller's ruling:
- `"broad"`: the single-question set (passed straight to `systemone_client.ask`).
- `"decomposed"`: the multi-question set of observable sub-facts.
- `"composite"`: `fn(answers) -> float` -- the code-side v0 composite over
  the *decomposed* answers, clipped to [0, 1]. Raises `JevQuestionsError` on
  any missing sub-answer rather than defaulting it to 0 (Task 2's `ask()`
  already treats an empty `answers` map as a hard failure; a partial map that
  is missing exactly the key a composite needs must fail the same way, not
  quietly read as "no").
- `"control"`: `fn(state, donors, key) -> state`. Never mutates its `state`
  argument. `donors` is a list of already-built states for the SAME
  `set_name` from OTHER sites (Task 6's job to filter); `key` is the row id,
  used only to make the donor choice deterministic across reruns. Raises
  `JevQuestionsError` if no donor in `donors` is usable, rather than
  returning `state` unchanged -- an unchanged control would silently pass
  the Task 7 "control differs from state" check.
- `"broad_score"`: `fn(answers) -> float` -- the arm's single P(label=yes)
  read straight off the *broad* question's answer, so Task 6/7 never have to
  re-derive it from a raw `ask()` result. Label semantics (controller ruling):
  programme_match yes = "observation is about this task"; threads yes =
  "later topic follows up the earlier one"; work_class yes = "topic is
  NON-work", so its `broad_score` is `P(non_work)` read off the broad
  question's choice `probabilities`.

`question_hash(set_name, arm)` hashes the canonical JSON of one arm's
question set (`"broad"` or `"decomposed"`) so Task 6 can stamp every result
row with exactly which question text produced it, per the eval plan.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


class JevQuestionsError(ValueError):
    """Raised when a composite/broad_score is missing a required sub-answer,
    or when a control function has no usable donor to swap in."""


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _noul(answers: dict, name: str) -> float:
    """Read `answers[name]["noul"]`, raising -- never defaulting to 0 -- if
    the sub-answer is missing or malformed (present but not a dict, so it
    cannot carry a "noul" key at all). A missing/malformed key here means the
    endpoint did not answer a question the composite depends on; silently
    treating that as "no" would quietly turn an API gap into a label."""
    try:
        return answers[name]["noul"]
    except (KeyError, TypeError) as exc:
        raise JevQuestionsError(f"missing or malformed sub-answer: {name!r}") from exc


def _stable_index(key, n: int) -> int:
    """Deterministic index into a pool of size `n`, derived from `key` (the
    row id) so a rerun over the same donors picks the same donor."""
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return int(digest, 16) % n


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def _words(text: str) -> set:
    return set(_WORD_RE.findall(text.lower()))


def _normalise(text: str) -> str:
    """Casefold, strip punctuation, and collapse whitespace, so two donor
    texts that only differ in case/punctuation/spacing are recognised as the
    same text -- used to reject a donor that would produce a no-op control
    (e.g. two sites both naming a task "Site establishment.")."""
    text = _PUNCT_RE.sub("", text.casefold())
    return _WHITESPACE_RE.sub(" ", text).strip()


def _assert_changed(original, new, description: str) -> None:
    """Cheap post-condition: raise if a control ended up producing the exact
    same value it started with. Defense in depth behind the donor-exclusion
    filters above -- an unchanged control would silently pass the Task 7
    "control differs from state" check."""
    if original == new:
        raise JevQuestionsError(
            f"control produced no change in {description}; refusing a no-op control")


# ---------------------------------------------------------------------------
# programme_match
# ---------------------------------------------------------------------------

_PROGRAMME_MATCH_BROAD = {
    "match": {
        "type": "noul",
        "instructions": "This site observation is about the scheduled task.",
    },
}

_PROGRAMME_MATCH_DECOMPOSED = {
    "same_work_item": {
        "type": "noul",
        "instructions": (
            "The observation and the task describe the same physical work "
            "item (e.g. the same doors, the same slab, the same duct run)."
        ),
    },
    "task_named": {
        "type": "noul",
        "instructions": (
            "The observation mentions the task by name or an unambiguous "
            "synonym."
        ),
    },
    "same_trade": {
        "type": "noul",
        "instructions": "The observation and the task belong to the same trade.",
    },
    "status_claimed": {
        "type": "choice",
        "instructions": "What state does the observation say this work is in?",
        "criteria": {
            "none": "The observation does not say what state the work is in.",
            "in_progress": (
                "The observation says the work is currently underway but "
                "not yet finished."
            ),
            "completed": "The observation says the work is fully finished.",
            "blocked": (
                "The observation says the work cannot proceed because "
                "something is stopping it."
            ),
            "delayed": (
                "The observation says the work is behind schedule but not "
                "blocked."
            ),
        },
    },
    "progress_stated": {
        "type": "noul",
        "instructions": (
            "The observation states a percentage complete or an explicit "
            "completion."
        ),
    },
}


def _composite_programme_match(answers: dict) -> float:
    same_work_item = _noul(answers, "same_work_item")
    task_named = _noul(answers, "task_named")
    same_trade = _noul(answers, "same_trade")
    value = 0.5 * same_work_item + 0.3 * task_named + 0.2 * same_trade
    return _clip01(value)


def _broad_score_programme_match(answers: dict) -> float:
    return _clip01(_noul(answers, "match"))


def _control_programme_match(state: dict, donors: list, key) -> dict:
    """Replace only `task.name` with a donor's `task.name`, preferring a
    donor whose task name shares a word with the original (the "same trade
    word if possible" rule), falling back to any donor with a usable name.

    Donors whose task name normalises to the same text as the original are
    excluded before either pool is built: generic task names ("Site
    establishment") recur across programmes, and picking one of those would
    produce a no-op control that silently passes the Task 7 control check."""
    original_name = None
    task = state.get("task")
    if isinstance(task, dict):
        original_name = task.get("name")
    original_norm = _normalise(original_name) if original_name else None

    candidates = [
        donor for donor in donors
        if isinstance(donor, dict)
        and isinstance(donor.get("task"), dict)
        and donor["task"].get("name")
        and (original_norm is None or _normalise(donor["task"]["name"]) != original_norm)
    ]
    if not candidates:
        raise JevQuestionsError(
            "programme_match control: no donor has a usable task.name "
            "distinct from the original")

    preferred = []
    if original_name:
        original_words = _words(original_name)
        preferred = [
            donor for donor in candidates
            if _words(donor["task"]["name"]) & original_words
        ]

    pool = preferred or candidates
    donor = pool[_stable_index(key, len(pool))]

    new_state = copy.deepcopy(state)
    new_state.setdefault("task", {})
    new_state["task"]["name"] = donor["task"]["name"]
    _assert_changed(original_name, new_state["task"]["name"], "task.name")
    return new_state


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------

_THREADS_BROAD = {
    "same_subject": {
        "type": "noul",
        "instructions": (
            "The later topic is a restatement or follow-up of the earlier "
            "topic."
        ),
    },
}

_THREADS_DECOMPOSED = {
    "same_subject_noun": {
        "type": "noul",
        "instructions": (
            "The earlier and later topics share the same distinctive "
            "subject noun."
        ),
    },
    "same_location": {
        "type": "noul",
        "instructions": (
            "The earlier and later topics refer to the same location."
        ),
    },
    "continuation": {
        "type": "noul",
        "instructions": (
            "The later text refers to a commitment, deadline or state the "
            "earlier one set up."
        ),
    },
    "generic_only": {
        "type": "noul",
        "instructions": (
            "The overlap between the earlier and later topics is only "
            "generic process words like installation, documentation, floor."
        ),
    },
}


def _composite_threads(answers: dict) -> float:
    same_subject_noun = _noul(answers, "same_subject_noun")
    continuation = _noul(answers, "continuation")
    same_location = _noul(answers, "same_location")
    generic_only = _noul(answers, "generic_only")
    value = (
        0.5 * same_subject_noun
        + 0.3 * continuation
        + 0.2 * same_location
        - 0.4 * generic_only
    )
    return _clip01(value)


def _broad_score_threads(answers: dict) -> float:
    return _clip01(_noul(answers, "same_subject"))


def _control_threads(state: dict, donors: list, key) -> dict:
    """Replace only `earlier` with a donor's `earlier`, preferring the donor
    whose `gap_days` is closest to the row's.

    Donors whose `earlier.title` normalises to the same text as the
    original's `earlier.title` are excluded before either pool is built --
    the same no-op-control risk as programme_match's task name (generic
    topic titles recur, e.g. "Site walk")."""
    original_earlier = state.get("earlier")
    original_title = None
    if isinstance(original_earlier, dict):
        original_title = original_earlier.get("title")
    original_title_norm = _normalise(original_title) if original_title else None

    def _usable(donor):
        if not (isinstance(donor, dict) and isinstance(donor.get("earlier"), dict)
                and donor["earlier"]):
            return False
        if original_title_norm is None:
            return True
        donor_title = donor["earlier"].get("title")
        if not donor_title:
            return True
        return _normalise(donor_title) != original_title_norm

    candidates = [donor for donor in donors if _usable(donor)]
    if not candidates:
        raise JevQuestionsError(
            "threads control: no donor has a usable 'earlier' topic "
            "distinct from the original")

    original_gap = state.get("gap_days")
    preferred = []
    if isinstance(original_gap, (int, float)):
        with_gap = [
            donor for donor in candidates
            if isinstance(donor.get("gap_days"), (int, float))
        ]
        if with_gap:
            best_diff = min(abs(donor["gap_days"] - original_gap) for donor in with_gap)
            preferred = [
                donor for donor in with_gap
                if abs(donor["gap_days"] - original_gap) == best_diff
            ]

    pool = preferred or candidates
    donor = pool[_stable_index(key, len(pool))]

    new_state = copy.deepcopy(state)
    new_state["earlier"] = copy.deepcopy(donor["earlier"])
    _assert_changed(original_earlier, new_state["earlier"], "earlier")
    return new_state


# ---------------------------------------------------------------------------
# work_class
# ---------------------------------------------------------------------------

_WORK_CLASS_BROAD = {
    "work_class": {
        "type": "choice",
        "instructions": "Is this conversation about work or non-work?",
        "criteria": {
            "work": (
                "The conversation is about construction work, a site, a "
                "programme, a subcontractor or the business of running the "
                "job."
            ),
            "non_work": (
                "The conversation is about something other than the job: "
                "the speaker's private life, family, health, unrelated "
                "business, or testing the recording device."
            ),
        },
    },
}

_WORK_CLASS_DECOMPOSED = {
    "about_the_site": {
        "type": "noul",
        "instructions": (
            "The conversation is about construction work, a site, a "
            "programme or a subcontractor."
        ),
    },
    "personal": {
        "type": "noul",
        "instructions": (
            "The conversation is about the recorder's private life, "
            "family, health or unrelated business."
        ),
    },
    "recording_test": {
        "type": "noul",
        "instructions": (
            "The conversation is about testing the recording device or app "
            "itself."
        ),
    },
}


def _composite_work_class(answers: dict) -> float:
    personal = _noul(answers, "personal")
    recording_test = _noul(answers, "recording_test")
    about_the_site = _noul(answers, "about_the_site")
    value = max(personal, recording_test) * (1 - about_the_site)
    return _clip01(value)


def _broad_score_work_class(answers: dict) -> float:
    try:
        probabilities = answers["work_class"]["probabilities"]
        return _clip01(probabilities["non_work"])
    except (KeyError, TypeError) as exc:
        raise JevQuestionsError(
            "missing or malformed required broad answer: 'work_class' -> "
            "probabilities['non_work']") from exc


def _control_work_class(state: dict, donors: list, key) -> dict:
    """Ignores `donors` and `key` (per the brief): replaces `title` and
    `summary` with a neutral sentence. `category` is left untouched -- the
    brief names only title and summary as the swap target, and category is
    a coarse label rather than the free-text content the control needs to
    neutralise."""
    original_title = state.get("title")
    original_summary = state.get("summary")
    new_state = copy.deepcopy(state)
    new_state["title"] = "General discussion."
    new_state["summary"] = "General discussion."
    _assert_changed(
        (original_title, original_summary),
        (new_state["title"], new_state["summary"]),
        "title/summary")
    return new_state


# ---------------------------------------------------------------------------
# public registry
# ---------------------------------------------------------------------------

QUESTION_SETS = {
    "programme_match": {
        "broad": _PROGRAMME_MATCH_BROAD,
        "decomposed": _PROGRAMME_MATCH_DECOMPOSED,
        "composite": _composite_programme_match,
        "control": _control_programme_match,
        "broad_score": _broad_score_programme_match,
    },
    "threads": {
        "broad": _THREADS_BROAD,
        "decomposed": _THREADS_DECOMPOSED,
        "composite": _composite_threads,
        "control": _control_threads,
        "broad_score": _broad_score_threads,
    },
    "work_class": {
        "broad": _WORK_CLASS_BROAD,
        "decomposed": _WORK_CLASS_DECOMPOSED,
        "composite": _composite_work_class,
        "control": _control_work_class,
        "broad_score": _broad_score_work_class,
    },
}


def question_hash(set_name: str, arm: str) -> str:
    """sha256 hex digest of the canonical JSON of `QUESTION_SETS[set_name][arm]`
    (`arm` is `"broad"` or `"decomposed"`), so a results row can be stamped
    with exactly which question text produced it."""
    if arm not in ("broad", "decomposed"):
        raise JevQuestionsError(
            f"question_hash only supports 'broad' or 'decomposed' arms, got {arm!r}")
    try:
        questions = QUESTION_SETS[set_name][arm]
    except KeyError as exc:
        raise JevQuestionsError(
            f"unknown set/arm: {set_name!r}/{arm!r}") from exc
    canonical = json.dumps(questions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
