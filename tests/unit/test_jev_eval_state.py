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
    # Owner decision 2026-09-28: work_class sends ONLY title + category, no
    # summary -- work_class positives are the recorder's private
    # conversations (health, family), and summary is where that lives.
    features = {"title": "Fix leaking valve", "summary": "Valve on level 3 leaking.", "category": "plumbing", "cost": 500}
    state = build_state("work_class", features, [])
    assert state == {"title": "Fix leaking valve", "category": "plumbing"}
    assert "summary" not in state


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


def test_mask_names_single_token_person_alias_is_masked():
    # Task 1 feeds user first/last names in as person aliases, some single-token.
    aliases = [{"wrong_term": "Heidi", "right_term": "Heidi", "kind": "person"}]
    text, mapping = mask_names("Heidi flagged a defect", aliases)
    assert text == "PERSON_1 flagged a defect"
    assert mapping == {"Heidi": "PERSON_1"}


def test_mask_names_single_token_alias_respects_word_boundaries():
    # "Ben" must not match inside "Bench" or "Benefit" -- word-boundary, not substring.
    aliases = [{"wrong_term": "Ben", "right_term": "Ben", "kind": "person"}]
    text, mapping = mask_names("Bench pour and Benefit review scheduled.", aliases)
    assert text == "Bench pour and Benefit review scheduled."
    assert mapping == {}


def test_mask_names_single_token_alias_masks_at_word_boundary():
    aliases = [{"wrong_term": "Ben", "right_term": "Ben", "kind": "person"}]
    text, mapping = mask_names("Ben said the slab is late, and Ben's crew agreed.", aliases)
    assert text == "PERSON_1 said the slab is late, and PERSON_1's crew agreed."
    assert mapping == {"Ben": "PERSON_1"}


def test_mask_names_ben_and_ben_lin_from_separate_alias_rows_get_separate_placeholders():
    # "Ben" (a first-name alias row) and "Ben Lin" (an unrelated full-name alias row)
    # are NOT tied together as the same person -- nothing in the data says they are the
    # same row, so they get separate placeholders. Longest term ("Ben Lin") still wins
    # the match where the two overlap.
    aliases = [
        {"wrong_term": "Ben", "right_term": "Ben", "kind": "person"},
        {"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"},
    ]
    text, mapping = mask_names("Ben Lin confirmed it, and Ben also agreed.", aliases)
    assert text == "PERSON_1 confirmed it, and PERSON_2 also agreed."
    assert mapping == {"Ben Lynn": "PERSON_1", "Ben Lin": "PERSON_1", "Ben": "PERSON_2"}


def test_mask_names_ben_and_ben_lin_from_the_same_alias_row_share_a_placeholder():
    # When a single alias row's own wrong/right pair contains both a first name and a
    # full name, they DO share a placeholder -- the row itself asserts they're the same
    # person.
    aliases = [{"wrong_term": "Ben", "right_term": "Ben Lin", "kind": "person"}]
    text, mapping = mask_names("Ben said it, then Ben Lin confirmed it.", aliases)
    assert text == "PERSON_1 said it, then PERSON_1 confirmed it."
    assert mapping == {"Ben": "PERSON_1", "Ben Lin": "PERSON_1"}


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

# ---------------------------------------------------------------------------
# I1: over-masking fixes
# ---------------------------------------------------------------------------

def test_task_name_never_hits_the_generic_two_token_pass():
    # Probe from the brief: task.name must never be over-masked by the
    # generic pass, even without a stoplist word or alias protecting it.
    features = {"task": {"name": "Roof Framing Inspection", "status": "in_progress"}}
    state = build_state("programme_match", features, [])
    assert state["task"]["name"] == "Roof Framing Inspection"


def test_task_name_short_form_never_masked_either():
    features = {"task": {"name": "Roof Framing"}}
    state = build_state("programme_match", features, [])
    assert state["task"]["name"] == "Roof Framing"


def test_task_name_still_masks_known_person_aliases():
    # Person aliases still apply to task.name -- only the generic pass is
    # disabled for it.
    aliases = [{"wrong_term": "Ben Lin", "right_term": "Ben Lin", "kind": "person"}]
    features = {"task": {"name": "Ben Lin door install"}}
    state = build_state("programme_match", features, aliases)
    assert state["task"]["name"] == "PERSON_1 door install"


def test_stoplist_prevents_construction_phrase_masking_in_free_text():
    text, mapping = mask_names("The Scaffold crew finished Level Two", [])
    assert text == "The Scaffold crew finished Level Two"
    assert mapping == {}


def test_stoplist_word_first_token():
    text, mapping = mask_names("Roof Framing needs another look", [])
    assert text == "Roof Framing needs another look"
    assert mapping == {}


def test_stoplist_does_not_suppress_a_real_two_word_name():
    text, mapping = mask_names("Sarah Jones confirmed the pour", [])
    assert text == "PERSON_1 confirmed the pour"
    assert mapping == {"Sarah Jones": "PERSON_1"}


# ---------------------------------------------------------------------------
# I4: privacy gaps
# ---------------------------------------------------------------------------

def test_cjk_alias_masks_without_word_boundary():
    aliases = [{"wrong_term": "林本", "right_term": "林本", "kind": "person"}]
    text, mapping = mask_names("林本说明天去医院", aliases)
    assert "林本" not in text
    assert text.startswith("PERSON_1")
    assert mapping == {"林本": "PERSON_1"}


def test_alias_matching_is_case_insensitive():
    aliases = [{"wrong_term": "Ben", "right_term": "Ben", "kind": "person"},
               {"wrong_term": "Lin", "right_term": "Lin", "kind": "person"}]
    text, mapping = mask_names("ben and lin are on site", aliases)
    assert "ben" not in text.lower().replace("person", "")
    assert "PERSON_" in text
    assert mapping["Ben"] == "PERSON_1"
    assert mapping["Lin"] == "PERSON_2"


def test_user_first_last_full_name_share_one_placeholder_via_alias_group():
    aliases = [
        {"wrong_term": "Ben", "right_term": "Ben", "kind": "person", "alias_group": "user-0"},
        {"wrong_term": "Lin", "right_term": "Lin", "kind": "person", "alias_group": "user-0"},
        {"wrong_term": "Ben Lin", "right_term": "Ben", "kind": "person", "alias_group": "user-0"},
    ]
    text, mapping = mask_names("Ben Lin said it, then Lin confirmed, then Ben agreed.", aliases)
    assert text == "PERSON_1 said it, then PERSON_1 confirmed, then PERSON_1 agreed."
    assert mapping == {"Ben": "PERSON_1", "Lin": "PERSON_1", "Ben Lin": "PERSON_1"}


def test_ungrouped_two_term_alias_row_still_works():
    # Backwards compatibility: a row with no alias_group behaves exactly as
    # before.
    aliases = [{"wrong_term": "Ben Lynn", "right_term": "Ben Lin", "kind": "person"}]
    text, mapping = mask_names("Ben Lynn said it, then Ben Lin confirmed it.", aliases)
    assert text == "PERSON_1 said it, then PERSON_1 confirmed it."
    assert mapping == {"Ben Lynn": "PERSON_1", "Ben Lin": "PERSON_1"}


def test_email_is_masked_in_every_string():
    text, _mapping = mask_names("Contact ben.lin@example.com for details.", [])
    assert text == "Contact EMAIL for details."


def test_phone_number_is_masked():
    text, _mapping = mask_names("Call 021 234 5678 about the delivery.", [])
    assert text == "Call PHONE about the delivery."


def test_phone_number_with_plus_and_dashes_is_masked():
    text, _mapping = mask_names("Reach +64-21-234-5678 anytime.", [])
    assert text == "Reach PHONE anytime."


def test_short_digit_runs_are_not_treated_as_phone_numbers():
    text, _mapping = mask_names("Level 2 slab pour, 6 workers on site.", [])
    assert text == "Level 2 slab pour, 6 workers on site."


def test_iso_date_is_not_masked_as_a_phone_number():
    features = {"observation": {"title": "x", "summary": "y", "date": "2026-09-20"}}
    state = build_state("programme_match", features, [])
    assert state["observation"]["date"] == "2026-09-20"


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
