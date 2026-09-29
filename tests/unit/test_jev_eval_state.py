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

from scripts.jev_eval.state import build_state, extract_words, mask_names

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
# Fix wave 6: common-word gate on the generic pass -- topic headings measured
# on the real exported data ("Material Procurement" x11, "Scaffolding Safety"
# x9, "Slab Rebar" x7, ...) were almost all being masked by the generic pass
# while real names of the same shape ("Hector Eggar", "Paul Smith", "Liang
# Min", "Yang Ming") were correctly caught. A candidate is now masked only if
# NEITHER token is a known common word (built-in list, or caller-supplied
# per-run corpus).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("heading", [
    "Material Procurement",
    "Scaffolding Safety Review",
    "General Reflection – Anything Else to Cover",
    "Slab Rebar",
    "Concrete Testing",
    "Device Testing",
    "Route Planning",
    "Subcontractor Access",
    "Crane Restrictions",
    "Design Issues",
    "Electrical Cables",
    "Weekly Schedule",
    "Recording Device",
])
def test_generic_pass_leaves_title_case_topic_headings_unmasked(heading):
    text, mapping = mask_names(heading, [])
    assert text == heading
    assert mapping == {}


def test_generic_pass_still_masks_real_names_not_in_the_common_word_list():
    text, mapping = mask_names("Hector Eggar rang about the delivery", [])
    assert text == "PERSON_1 rang about the delivery"
    assert mapping == {"Hector Eggar": "PERSON_1"}

    text2, mapping2 = mask_names("Paul Smith confirmed the pour", [])
    assert text2 == "PERSON_1 confirmed the pour"
    assert mapping2 == {"Paul Smith": "PERSON_1"}

    text3, mapping3 = mask_names("Liang Min will be on site", [])
    assert text3 == "PERSON_1 will be on site"
    assert mapping3 == {"Liang Min": "PERSON_1"}

    text4, mapping4 = mask_names("Yang Ming signed off the report", [])
    assert text4 == "PERSON_1 signed off the report"
    assert mapping4 == {"Yang Ming": "PERSON_1"}


def test_common_words_parameter_protects_a_corpus_specific_heading():
    # "Foundry Logistics" is not in the built-in list -- without a
    # caller-supplied corpus it reads as a plausible two-token name and gets
    # masked. Supplying it via `common_words` (as the runner would, having
    # seen "Foundry" and "Logistics" elsewhere in this run's own text)
    # suppresses the generic pass for it.
    text_without, mapping_without = mask_names("Foundry Logistics update", [])
    assert text_without == "PERSON_1 update"
    assert mapping_without == {"Foundry Logistics": "PERSON_1"}

    text_with, mapping_with = mask_names(
        "Foundry Logistics update", [], common_words={"foundry", "logistics"})
    assert text_with == "Foundry Logistics update"
    assert mapping_with == {}


def test_known_person_alias_still_masked_even_if_also_a_common_word():
    # Person aliases are unaffected by the common-word gate -- a known alias
    # is always masked, even if the term also happens to be a common word.
    # Existing case rules for common-word aliases (capitalised/ALL-CAPS only)
    # are unchanged.
    aliases = [{"wrong_term": "Mark", "right_term": "Mark", "kind": "person"}]
    text, mapping = mask_names(
        "Mark reviewed the schedule", aliases, common_words={"mark", "schedule"})
    assert text == "PERSON_1 reviewed the schedule"
    assert mapping == {"Mark": "PERSON_1"}


def test_common_word_in_corpus_leaves_a_colliding_real_name_unmasked_residual():
    # Accepted residual (documented in the module docstring and findings doc
    # section 4): if the run's own corpus contains a common word that is also
    # a real surname, that name is left unmasked.
    text, mapping = mask_names("Wood Ward confirmed the delivery", [], common_words={"wood"})
    assert text == "Wood Ward confirmed the delivery"
    assert mapping == {}


# ---------------------------------------------------------------------------
# Fix round 1 (controller correction, 2026-09-29): the corpus must count a
# word only if it occurs somewhere ALREADY WRITTEN ALL-LOWERCASE -- not any
# occurrence lowercased on the way in. Without this, a real name mentioned
# several times but always capitalised ("Hector Eggar") was entering the
# corpus as "hector"/"eggar" and being treated as common, which measured at
# 0 generic-pass masks remaining on the real data -- a privacy regression.
# ---------------------------------------------------------------------------

def test_extract_words_excludes_a_token_seen_only_capitalised():
    # "Hector" and "Eggar" appear several times, always capitalised, never
    # written lowercase anywhere -- neither may enter the corpus.
    text = "Hector Eggar rang. Hector Eggar called back. Ask Hector Eggar again."
    words = extract_words({"title": text, "summary": text})
    assert "hector" not in words
    assert "eggar" not in words


def test_extract_words_excludes_a_token_seen_only_all_caps():
    words = extract_words({"title": "HECTOR EGGAR CONFIRMED"})
    assert "hector" not in words
    assert "eggar" not in words


def test_extract_words_includes_a_token_with_a_genuine_lowercase_occurrence():
    # "procurement" occurs lower-case in the summary even though the title
    # capitalises it -- it belongs in the corpus.
    words = extract_words({
        "title": "Material Procurement",
        "summary": "Waiting on procurement paperwork before the pour.",
    })
    assert "procurement" in words
    # "Material" itself is only ever capitalised here, so it is NOT added by
    # this token's own occurrence -- a separate row's lower-case "material"
    # would be what adds it (proven by the next call, a fresh corpus).
    assert "material" not in words

    words2 = extract_words({"summary": "material handling was slow today."})
    assert "material" in words2


def test_common_word_gate_still_masks_a_name_seen_only_capitalised_in_the_corpus():
    # End-to-end: build the corpus the way the runner does (via
    # extract_words) from text where "Hector Eggar" appears several times,
    # always capitalised, and confirm it is still masked -- this is the
    # exact regression the controller flagged.
    corpus_text = (
        "Hector Eggar rang about the delivery. Hector Eggar confirmed the "
        "schedule. Ask Hector Eggar to call back."
    )
    common_words = extract_words({"summary": corpus_text})
    text, mapping = mask_names("Hector Eggar rang", [], common_words=common_words)
    assert text == "PERSON_1 rang"
    assert mapping == {"Hector Eggar": "PERSON_1"}


def test_common_word_gate_still_protects_a_heading_whose_words_recur_lowercase():
    corpus_text = "Waiting on procurement paperwork; site safety briefing done."
    common_words = extract_words({"summary": corpus_text})
    text, mapping = mask_names("Material Procurement", [], common_words=common_words)
    # "procurement" recurs lower-case in the corpus, and "material" is in
    # the built-in list regardless -- the heading stays unmasked.
    assert text == "Material Procurement"
    assert mapping == {}


def test_build_state_accepts_common_words_and_protects_headings():
    features = {"title": "Material Procurement", "summary": "s", "category": "c"}
    state = build_state("work_class", features, [], common_words=set())
    assert state["title"] == "Material Procurement"


def test_masking_stats_placeholder_fraction_drops_with_common_words(monkeypatch):
    # Re-runs `--dry-run`'s masking-stats path (jev_shadow_eval._masking_stats)
    # directly with a synthetic corpus, proving `title_all_placeholder_fraction`
    # drops once `common_words` is supplied.
    import scripts.jev_shadow_eval as runner

    # None of these words are in the built-in common-word list, so without a
    # caller-supplied corpus every heading reads as a plausible two-token
    # name and gets fully swallowed by the generic pass.
    headings = ["Foundry Logistics", "Joinery Handover", "Signage Rollout"]

    def _states(common_words):
        states = {
            i: {"state": {"title": h, "category": "c"}, "site_id": None, "company_id": None}
            for i, h in enumerate(headings)
        }
        for i, h in enumerate(headings):
            masked, _ = mask_names(h, [], common_words=common_words)
            states[i]["state"]["title"] = masked
        return states

    stats_no_corpus = runner._masking_stats("work_class", _states(None))
    assert stats_no_corpus["title_all_placeholder_fraction"] == 1.0

    common_words = {"foundry", "logistics", "joinery", "handover", "signage", "rollout"}
    stats_with_corpus = runner._masking_stats("work_class", _states(common_words))
    assert stats_with_corpus["title_all_placeholder_fraction"] == 0.0

    # The fraction dropped once the corpus was supplied.
    assert stats_with_corpus["title_all_placeholder_fraction"] < stats_no_corpus["title_all_placeholder_fraction"]


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


# ---------------------------------------------------------------------------
# Fix wave 4, C11: an ASCII alias term must mask right next to a CJK
# character with no space -- `\b` never fires there.
# ---------------------------------------------------------------------------

BEN_ALIAS = [{"wrong_term": "Ben", "right_term": "Ben", "kind": "person"}]


def test_ascii_alias_masks_adjacent_to_cjk_with_no_space():
    text, mapping = mask_names("Ben说好", BEN_ALIAS)
    assert text == "PERSON_1说好"
    assert mapping


def test_ascii_alias_still_respects_ascii_word_boundary():
    # Unchanged behaviour: "Bench"/"Benefit" must still NOT match "Ben".
    text, _ = mask_names("Bench and Benefit are not Ben.", BEN_ALIAS)
    assert text == "Bench and Benefit are not PERSON_1."


# ---------------------------------------------------------------------------
# Fix wave 4, C12: a stoplisted lead word must not swallow the real name
# that follows it -- the scan must continue past only the first token.
# ---------------------------------------------------------------------------

def test_stoplisted_lead_word_does_not_swallow_the_following_name():
    text, mapping = mask_names("Friday Sarah Jones confirmed", [])
    assert text == "Friday PERSON_1 confirmed"
    assert mapping == {"Sarah Jones": "PERSON_1"}


def test_yesterday_does_not_swallow_the_following_name():
    text, mapping = mask_names("Yesterday Sarah Jones", [])
    assert text == "Yesterday PERSON_1"
    assert mapping == {"Sarah Jones": "PERSON_1"}


# ---------------------------------------------------------------------------
# Fix wave 4, C13: month names are masked when followed by a real name;
# a date (digit-first second token) never matches the name shape at all.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_mapped", [
    ("May Chen confirmed the delivery", "May Chen"),
    ("June Wilson signed off the report", "June Wilson"),
    ("April Ng will be on site", "April Ng"),
])
def test_month_name_followed_by_a_real_name_is_masked(text, expected_mapped):
    masked, mapping = mask_names(text, [])
    assert expected_mapped in mapping
    assert expected_mapped not in masked


def test_month_name_followed_by_a_date_is_not_masked():
    text, mapping = mask_names("Delivery scheduled for May 2026", [])
    assert text == "Delivery scheduled for May 2026"
    assert mapping == {}


def test_month_name_followed_by_a_day_number_is_not_masked():
    text, mapping = mask_names("Delivery scheduled for June 3", [])
    assert text == "Delivery scheduled for June 3"
    assert mapping == {}


# ---------------------------------------------------------------------------
# Fix wave 4, C14: alias terms that are ordinary English words match
# case-sensitively (their own exact casing only); other aliases stay
# case-insensitive.
# ---------------------------------------------------------------------------

def test_common_word_alias_matches_only_its_capitalised_form():
    aliases = [{"wrong_term": "Will", "right_term": "Will", "kind": "person"}]
    masked_cap, mapping_cap = mask_names("Will is on site today", aliases)
    assert "PERSON_1" in masked_cap
    assert mapping_cap

    masked_lower, mapping_lower = mask_names(
        "the crew will arrive at noon", aliases)
    assert masked_lower == "the crew will arrive at noon"
    assert mapping_lower == {}


def test_non_common_word_alias_still_matches_case_insensitively():
    aliases = [{"wrong_term": "Heidi", "right_term": "Heidi", "kind": "person"}]
    text, mapping = mask_names("heidi flagged a defect", aliases)
    assert "PERSON_1" in text
    assert mapping


def test_ben_is_not_in_the_common_word_list_and_stays_case_insensitive():
    from scripts.jev_eval.state import _COMMON_WORD_ALIASES
    assert "ben" not in _COMMON_WORD_ALIASES


# ---------------------------------------------------------------------------
# Fix wave 4, C15: protected company/site/task terms win over BOTH the
# alias pass and the generic pass, for terms of any word count, and a
# possessive "'s".
# ---------------------------------------------------------------------------

def test_protected_multiword_company_name_with_extra_words_is_kept_intact():
    aliases = [
        {"wrong_term": "SB1108 Ellesmere College", "right_term": "SB1108 Ellesmere College",
         "kind": "company"},
    ]
    text, mapping = mask_names("SB1108 Ellesmere College pour", aliases)
    assert text == "SB1108 Ellesmere College pour"
    assert mapping == {}


def test_protected_company_name_with_ltd_suffix_is_kept_intact():
    aliases = [
        {"wrong_term": "Smith Scaffolding Ltd", "right_term": "Smith Scaffolding Ltd",
         "kind": "company"},
    ]
    text, mapping = mask_names("Invoice from Smith Scaffolding Ltd this week", aliases)
    assert "Smith Scaffolding Ltd" in text
    assert mapping == {}


def test_protected_site_name_is_kept_intact():
    aliases = [
        {"wrong_term": "UC Pharmacy Kiosk", "right_term": "UC Pharmacy Kiosk", "kind": "company"},
    ]
    text, mapping = mask_names("Delivery arrived at UC Pharmacy Kiosk", aliases)
    assert "UC Pharmacy Kiosk" in text
    assert mapping == {}


def test_protected_term_survives_possessive_and_a_colliding_person_alias():
    aliases = [
        {"wrong_term": "Naylor Love", "right_term": "Naylor Love", "kind": "company"},
        {"wrong_term": "Love", "right_term": "Love", "kind": "person"},
    ]
    text, mapping = mask_names("Naylor Love's crew arrived early", aliases)
    assert "Naylor Love's crew arrived early" == text
    assert mapping == {}


# ---------------------------------------------------------------------------
# Fix wave 4, C16: PHONE must not swallow an ISO date+time or a money
# amount.
# ---------------------------------------------------------------------------

def test_iso_date_with_bare_time_is_not_masked_as_phone():
    text, _ = mask_names("Logged at 2026-09-20 0800 this morning", [])
    assert text == "Logged at 2026-09-20 0800 this morning"


def test_dollar_amount_is_not_masked_as_phone():
    text, _ = mask_names("Invoice total was $1234567 for the job", [])
    assert text == "Invoice total was $1234567 for the job"


def test_money_amount_with_k_suffix_is_not_masked_as_phone():
    text, _ = mask_names("Budget is 1234567k for the extension", [])
    assert text == "Budget is 1234567k for the extension"


def test_money_amount_with_million_suffix_is_not_masked_as_phone():
    text, _ = mask_names("Contract value 1234567 million overall", [])
    assert text == "Contract value 1234567 million overall"


def test_plain_phone_number_is_still_masked():
    text, _ = mask_names("Call 021 555 1234 about the delivery.", [])
    assert text == "Call PHONE about the delivery."


# ---------------------------------------------------------------------------
# Fix wave 5, item 1 (Critical): protected company/site/task terms must be
# ANCHORED (never matched inside another word), a protected term shorter than
# 3 characters protects nothing, and a SINGLE-token protected term never
# overrides a person alias or a generic two-token name candidate. Wave 4's
# unanchored, case-insensitive substring protection let every one of these
# names leave unmasked (all were masked before wave 4).
# ---------------------------------------------------------------------------

def _protected(*names):
    return [{"wrong_term": n, "right_term": n, "kind": "company"} for n in names]


@pytest.mark.parametrize("text,protected_term,name", [
    ("Caroline Smith rang about the pour", "Line", "Caroline Smith"),
    ("Martin Jones rang about the pour", "Art", "Martin Jones"),
    ("Lucy Smith rang about the pour", "UC", "Lucy Smith"),
    ("Clayton Reid rang about the pour", "Lay", "Clayton Reid"),
    ("Tom Hawkins rang about the pour", "Hawkins", "Tom Hawkins"),
])
def test_protected_term_never_shields_a_name_it_is_part_of(text, protected_term, name):
    masked, mapping = mask_names(text, _protected(protected_term))
    assert name not in masked
    for token in name.split():
        assert token not in masked, (token, masked)


def test_single_character_protected_term_protects_nothing():
    masked, _ = mask_names("Friday Sarah Jones confirmed", _protected("A"))
    assert masked == "Friday PERSON_1 confirmed"


def test_single_token_protected_term_does_not_override_a_person_alias():
    aliases = _protected("Hawkins") + [
        {"wrong_term": "Hawkins", "right_term": "Hawkins", "kind": "person",
         "alias_group": "user-0"},
        {"wrong_term": "Tom", "right_term": "Tom", "kind": "person", "alias_group": "user-0"},
    ]
    masked, _ = mask_names("Hawkins rang, then Tom Hawkins rang again", aliases)
    assert "Hawkins" not in masked
    assert "Tom" not in masked


def test_protected_multiword_term_is_anchored_not_a_substring():
    # "UC PK" must not protect the inside of "LUC PKG" -- anchoring applies
    # to multi-token protected terms too.
    masked, _ = mask_names("Delivered to UC PK today", _protected("UC PK"))
    assert masked == "Delivered to UC PK today"
    masked2, _ = mask_names("Sarah Jones at UC PK", _protected("UC PK"))
    assert masked2 == "PERSON_1 at UC PK"


def test_wave4_protected_keep_cases_still_hold_together():
    aliases = _protected(
        "Naylor Love", "SB1108 Ellesmere College", "Smith Scaffolding Ltd", "UC Pharmacy Kiosk",
    ) + [{"wrong_term": "Love", "right_term": "Love", "kind": "person"}]
    for text in ("Naylor Love's crew arrived early", "SB1108 Ellesmere College pour",
                 "Invoice from Smith Scaffolding Ltd", "Delivery at UC Pharmacy Kiosk"):
        masked, mapping = mask_names(text, aliases)
        assert masked == text, (text, masked)
        assert mapping == {}


# ---------------------------------------------------------------------------
# Fix wave 5, item 2: a phone number followed by an ordinary word that
# merely STARTS with k/m is still a phone number -- only a bare k / m /
# million suffix (or a leading "$") marks a money amount.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Mike's number is 021 555 1234 mate", "Mike's number is PHONE mate"),
    ("ring 021 555 1234 mobile", "ring PHONE mobile"),
    ("021 555 1234 kept ringing", "PHONE kept ringing"),
    ("021 555 1234 Mark's phone", "PHONE Mark's phone"),
])
def test_phone_followed_by_a_word_starting_with_k_or_m_is_still_masked(text, expected):
    masked, _ = mask_names(text, [])
    assert masked == expected


@pytest.mark.parametrize("text", [
    "Budget $1 250 000 approved", "Budget 1234567k approved", "Budget 1234567 m approved",
    "Budget 1234567M approved", "Contract value 1234567 million overall",
])
def test_money_amounts_are_not_phone(text):
    masked, _ = mask_names(text, [])
    assert masked == text


# ---------------------------------------------------------------------------
# Fix wave 5, item 7 (C14): a common-word alias matches its capitalised
# form and its ALL-CAPS form regardless of how the user row stored it, and
# never the lowercase common word.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stored", ["Will", "will", "WILL"])
def test_common_word_alias_matches_capitalised_and_all_caps_forms(stored):
    aliases = [{"wrong_term": stored, "right_term": stored, "kind": "person"}]
    for text in ("Will is on site", "WILL is on site"):
        masked, _ = mask_names(text, aliases)
        assert masked == "PERSON_1 is on site", (stored, text, masked)
    masked, mapping = mask_names("the crew will arrive", aliases)
    assert masked == "the crew will arrive"
    assert mapping == {}
