"""Tests for the Jev shadow-eval state builder (Track A, Task 3).

`build_state` turns one exported row's `features` dict into the small JSON
`state` that would be sent to the third-party ("Jev") decision model. The
owner allowed data to leave the country ONLY as structured event JSON, with
person names masked -- never transcript text. So the two things this file
exists to prove are:

1. The field allowlist actually drops everything not on it, and a key that
   *looks* like a transcript fragment -- anywhere in the nested features --
   raises rather than being silently dropped.
2. Every string that reaches the output state has person names masked,
   consistently, using PERSON_n placeholders that are never exposed as a
   stable identity (just a same-state consistency), while known
   company/product alias terms survive untouched.
"""
import json

import pytest

from scripts.jev_eval.state import build_state, mask_names

PERSON_ALIASES = [
    {"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"},
]
COMPANY_ALIASES = [
    {"wrong_term": "Naylor Love", "right_term": "Naylor Love", "kind": "company"},
]


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

def test_programme_match_allowlist_keeps_only_listed_fields():
    features = {
        "observation": {
            "title": "Slab pour delayed",
            "summary": "Pour pushed to Thursday.",
            "date": "2026-09-20",
            "internal_note": "drop me",
            "action_items": [{"text": "Reschedule crane"}, {"other": "drop me"}],
        },
        "task": {
            "name": "Level 2 slab pour",
            "status": "in_progress",
            "progress_pct": 40,
            "secret_cost_estimate": 99999,
        },
        "extra_top_level": "drop me too",
    }
    state = build_state("programme_match", features, [])
    assert state == {
        "observation": {
            "title": "Slab pour delayed",
            "summary": "Pour pushed to Thursday.",
            "date": "2026-09-20",
            "action_items": [{"text": "Reschedule crane"}],
        },
        "task": {
            "name": "Level 2 slab pour",
            "status": "in_progress",
            "progress_pct": 40,
        },
    }


def test_programme_match_absent_optional_fields_are_omitted():
    # Task 1 cannot supply task.start/end -- absence must not appear as null.
    features = {"task": {"name": "Foundations"}}
    state = build_state("programme_match", features, [])
    assert state == {"task": {"name": "Foundations"}}
    assert "start" not in state["task"]
    assert "end" not in state["task"]


def test_threads_allowlist():
    features = {
        "earlier": {"title": "Deck framing", "summary": "Started framing.", "date": "2026-09-01", "junk": 1},
        "later": {"title": "Deck complete", "summary": "Framing signed off.", "date": "2026-09-10"},
        "gap_days": 9,
        "unrelated": "drop me",
    }
    state = build_state("threads", features, [])
    assert state == {
        "earlier": {"title": "Deck framing", "summary": "Started framing.", "date": "2026-09-01"},
        "later": {"title": "Deck complete", "summary": "Framing signed off.", "date": "2026-09-10"},
        "gap_days": 9,
    }


def test_work_class_allowlist():
    features = {"title": "Fix leaking valve", "summary": "Valve on level 3 leaking.", "category": "plumbing", "cost": 500}
    state = build_state("work_class", features, [])
    assert state == {"title": "Fix leaking valve", "summary": "Valve on level 3 leaking.", "category": "plumbing"}


# ---------------------------------------------------------------------------
# Transcript-like keys raise, even nested
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", ["transcript", "quote", "turns", "window", "evidence", "text_window"])
def test_transcript_like_key_raises_at_top_level(bad_key):
    features = {"title": "x", "summary": "y", "category": "z", bad_key: "should never leave"}
    with pytest.raises(Exception):
        build_state("work_class", features, [])


def test_transcript_like_key_raises_when_nested():
    features = {
        "observation": {
            "title": "x",
            "summary": "y",
            "date": "2026-09-20",
            "action_items": [{"text": "ok", "quote": "the raw words said here"}],
        },
        "task": {"name": "n", "status": "open", "progress_pct": 0},
    }
    with pytest.raises(Exception):
        build_state("programme_match", features, [])


def test_transcript_like_key_raises_even_though_not_allowlisted():
    # A key that would be dropped anyway (not on the allowlist) must still raise --
    # dropping it silently is exactly the bug this guards against.
    features = {"title": "x", "summary": "y", "category": "z", "meta": {"transcript": "raw text"}}
    with pytest.raises(Exception):
        build_state("work_class", features, [])


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------

def test_mask_names_replaces_capitalised_two_token_name():
    text, mapping = mask_names("Ben Lin said the slab is late", [])
    assert text == "PERSON_1 said the slab is late"
    assert mapping == {"Ben Lin": "PERSON_1"}


def test_mask_names_uses_person_alias_pair_for_both_wrong_and_right_term():
    text, mapping = mask_names("Ben Lynn said it, then Ben Lin confirmed it.", PERSON_ALIASES)
    assert text == "PERSON_1 said it, then PERSON_1 confirmed it."
    assert mapping["Ben Lynn"] == "PERSON_1"
    assert mapping["Ben Lin"] == "PERSON_1"


def test_mask_names_leaves_company_alias_untouched():
    text, _mapping = mask_names("Naylor Love is doing the framing.", COMPANY_ALIASES)
    assert "Naylor Love" in text
    assert "PERSON_" not in text


def test_mask_names_same_name_twice_gets_same_placeholder():
    text, mapping = mask_names("Ben Lin called Ben Lin back.", [])
    assert text == "PERSON_1 called PERSON_1 back."
    assert mapping == {"Ben Lin": "PERSON_1"}


def test_mask_names_two_different_names_get_different_placeholders():
    text, mapping = mask_names("Ben Lin spoke with Sarah Jones.", [])
    assert text == "PERSON_1 spoke with PERSON_2."
    assert mapping == {"Ben Lin": "PERSON_1", "Sarah Jones": "PERSON_2"}


def test_build_state_masks_names_consistently_across_the_whole_state():
    features = {
        "observation": {
            "title": "Ben Lin flagged an issue",
            "summary": "Ben Lin said the slab is late; Naylor Love is on site.",
            "date": "2026-09-20",
        },
        "task": {"name": "Level 2 slab pour", "status": "in_progress", "progress_pct": 40},
    }
    state = build_state("programme_match", features, COMPANY_ALIASES)
    assert state["observation"]["title"] == "PERSON_1 flagged an issue"
    assert "PERSON_1 said the slab is late" in state["observation"]["summary"]
    assert "Naylor Love" in state["observation"]["summary"]


def test_build_state_mapping_never_appears_in_output():
    features = {"title": "Ben Lin raised a defect", "summary": "s", "category": "c"}
    state = build_state("work_class", features, [])
    dumped = json.dumps(state)
    assert "mapping" not in dumped
    assert "Ben Lin" not in dumped


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

def test_typical_state_serialises_under_4k_chars():
    features = {
        "observation": {
            "title": "Slab pour delayed due to weather",
            "summary": "Heavy rain overnight pushed the level 2 slab pour from Wednesday to "
                       "Thursday. Ben Lin will confirm the new crane booking with Naylor Love "
                       "once the site has dried out.",
            "date": "2026-09-20",
            "action_items": [
                {"text": "Reschedule crane booking"},
                {"text": "Check drainage on the western edge"},
            ],
        },
        "task": {
            "name": "Level 2 slab pour",
            "status": "in_progress",
            "progress_pct": 40,
            "start": "2026-09-18",
            "end": "2026-09-25",
        },
    }
    state = build_state("programme_match", features, COMPANY_ALIASES)
    assert len(json.dumps(state)) < 4000
