"""Tests for the Jev shadow-eval question sets (Track A, Task 4).

`QUESTION_SETS[set_name]` holds five things per decision (`programme_match`,
`threads`, `work_class`): the `broad` and `decomposed` question dicts (the
exact shape `systemone_client.ask()` sends on the wire -- `type` /
`instructions` / `criteria`, per the controller's 2026-09-27 ruling that the
public docs.typesafe.ai schema is the real contract, not Task 2's
pass-through test fixtures), the code-side `composite` and `broad_score`
functions over `ask()`'s normalised `answers`, and the `control` function
that swaps in donor content for the control arm.
"""
import copy
import hashlib
import json

import pytest

from scripts.jev_eval.questions import (
    QUESTION_SETS,
    JevQuestionsError,
    question_hash,
)

SET_NAMES = ("programme_match", "threads", "work_class")


# ---------------------------------------------------------------------------
# Shape: every set has all five keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("set_name", SET_NAMES)
def test_every_set_has_all_five_keys(set_name):
    entry = QUESTION_SETS[set_name]
    assert set(entry.keys()) == {
        "broad", "decomposed", "composite", "control", "broad_score",
    }
    assert callable(entry["composite"])
    assert callable(entry["control"])
    assert callable(entry["broad_score"])
    assert isinstance(entry["broad"], dict) and entry["broad"]
    assert isinstance(entry["decomposed"], dict) and entry["decomposed"]


def _all_questions():
    for set_name in SET_NAMES:
        for arm in ("broad", "decomposed"):
            for name, question in QUESTION_SETS[set_name][arm].items():
                yield set_name, arm, name, question


# ---------------------------------------------------------------------------
# Wire shape: type / instructions / criteria (docs.typesafe.ai contract)
# ---------------------------------------------------------------------------

def test_every_question_has_type_and_instructions_under_300_chars():
    for set_name, arm, name, question in _all_questions():
        assert question["type"] in ("noul", "choice", "score"), (
            f"{set_name}.{arm}.{name}: unexpected type {question.get('type')!r}")
        assert "instructions" in question, f"{set_name}.{arm}.{name}: missing instructions"
        assert isinstance(question["instructions"], str)
        assert len(question["instructions"]) < 300, (
            f"{set_name}.{arm}.{name}: instructions too long")


def test_no_score_questions_are_used():
    for set_name, arm, name, question in _all_questions():
        assert question["type"] != "score", f"{set_name}.{arm}.{name} is a score question"


def test_noul_questions_omit_criteria():
    for set_name, arm, name, question in _all_questions():
        if question["type"] == "noul":
            assert "criteria" not in question, (
                f"{set_name}.{arm}.{name}: noul question should not carry criteria")


def test_every_choice_has_at_least_two_options_with_short_rubric_lines():
    found_a_choice = False
    for set_name, arm, name, question in _all_questions():
        if question["type"] != "choice":
            continue
        found_a_choice = True
        criteria = question["criteria"]
        assert isinstance(criteria, dict)
        assert len(criteria) >= 2, f"{set_name}.{arm}.{name}: fewer than 2 options"
        for option, rubric in criteria.items():
            assert isinstance(option, str) and option
            assert isinstance(rubric, str) and rubric
            assert len(rubric) < 300, f"{set_name}.{arm}.{name}[{option}]: rubric too long"
    assert found_a_choice, "expected at least one choice question across all sets"


def test_status_claimed_has_the_five_options_from_the_brief():
    criteria = QUESTION_SETS["programme_match"]["decomposed"]["status_claimed"]["criteria"]
    assert set(criteria.keys()) == {"none", "in_progress", "completed", "blocked", "delayed"}


def test_work_class_broad_options_are_verbatim_from_the_ruling():
    criteria = QUESTION_SETS["work_class"]["broad"]["work_class"]["criteria"]
    assert criteria["work"] == (
        "The conversation is about construction work, a site, a "
        "programme, a subcontractor or the business of running the job."
    )
    assert criteria["non_work"] == (
        "The conversation is about something other than the job: the "
        "speaker's private life, family, health, unrelated business, or "
        "testing the recording device."
    )


def test_options_are_mutually_exclusive_rubric_text():
    # Not a semantic proof, just a sanity check that no two rubric lines in
    # the same criteria map are literally identical text.
    for set_name, arm, name, question in _all_questions():
        if question["type"] != "choice":
            continue
        rubrics = list(question["criteria"].values())
        assert len(rubrics) == len(set(rubrics)), f"{set_name}.{arm}.{name}: duplicate rubric text"


# ---------------------------------------------------------------------------
# broad question texts match the brief verbatim
# ---------------------------------------------------------------------------

def test_programme_match_broad_text_is_verbatim():
    q = QUESTION_SETS["programme_match"]["broad"]["match"]
    assert q["type"] == "noul"
    assert q["instructions"] == "This site observation is about the scheduled task."


def test_threads_broad_text_is_verbatim():
    q = QUESTION_SETS["threads"]["broad"]["same_subject"]
    assert q["type"] == "noul"
    assert q["instructions"] == "The later topic is a restatement or follow-up of the earlier topic."


# ---------------------------------------------------------------------------
# question_hash
# ---------------------------------------------------------------------------

def test_question_hash_is_deterministic_and_matches_manual_computation():
    h1 = question_hash("programme_match", "decomposed")
    h2 = question_hash("programme_match", "decomposed")
    assert h1 == h2
    expected = hashlib.sha256(
        json.dumps(QUESTION_SETS["programme_match"]["decomposed"],
                   sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert h1 == expected


def test_question_hash_differs_across_sets_and_arms():
    hashes = {
        (set_name, arm): question_hash(set_name, arm)
        for set_name in SET_NAMES
        for arm in ("broad", "decomposed")
    }
    assert len(set(hashes.values())) == len(hashes)


def test_question_hash_raises_for_unknown_set_or_arm():
    with pytest.raises(JevQuestionsError):
        question_hash("not_a_set", "broad")
    with pytest.raises(JevQuestionsError):
        question_hash("programme_match", "not_an_arm")


def test_question_hash_raises_for_arm_other_than_broad_or_decomposed():
    # These are real keys in QUESTION_SETS[set_name] but not valid `arm`
    # values for question_hash -- must be rejected explicitly, not produce
    # a TypeError from trying to json.dumps a function.
    with pytest.raises(JevQuestionsError):
        question_hash("programme_match", "composite")
    with pytest.raises(JevQuestionsError):
        question_hash("programme_match", "control")
    with pytest.raises(JevQuestionsError):
        question_hash("programme_match", "broad_score")


# ---------------------------------------------------------------------------
# composites
# ---------------------------------------------------------------------------

def _noul_answers(**values):
    return {name: {"noul": value} for name, value in values.items()}


def test_programme_match_composite_boundaries():
    composite = QUESTION_SETS["programme_match"]["composite"]
    zero = _noul_answers(same_work_item=0.0, task_named=0.0, same_trade=0.0)
    one = _noul_answers(same_work_item=1.0, task_named=1.0, same_trade=1.0)
    assert composite(zero) == pytest.approx(0.0)
    assert composite(one) == pytest.approx(1.0)
    mid = _noul_answers(same_work_item=0.5, task_named=0.5, same_trade=0.5)
    assert composite(mid) == pytest.approx(0.5)


def test_programme_match_composite_raises_on_missing_sub_answer():
    composite = QUESTION_SETS["programme_match"]["composite"]
    with pytest.raises(JevQuestionsError):
        composite(_noul_answers(same_work_item=1.0, task_named=1.0))  # same_trade missing


def test_composite_raises_jevquestionserror_when_sub_answer_is_not_a_dict():
    composite = QUESTION_SETS["programme_match"]["composite"]
    with pytest.raises(JevQuestionsError):
        # same_work_item is a bare float, not {"noul": ...} -- would raise a
        # bare TypeError on subscript if not caught explicitly.
        composite({"same_work_item": 0.5, "task_named": {"noul": 1.0},
                    "same_trade": {"noul": 1.0}})


def test_threads_composite_boundaries_and_clipping():
    composite = QUESTION_SETS["threads"]["composite"]
    # All decomposed 0 -> 0.
    zero = _noul_answers(same_subject_noun=0.0, continuation=0.0, same_location=0.0, generic_only=0.0)
    assert composite(zero) == pytest.approx(0.0)
    # All 1 -> 0.5+0.3+0.2-0.4 = 0.6, in range, no clip needed.
    one = _noul_answers(same_subject_noun=1.0, continuation=1.0, same_location=1.0, generic_only=1.0)
    assert composite(one) == pytest.approx(0.6)
    # Everything 0 except generic_only=1 -> raw value -0.4, must clip to 0.
    negative = _noul_answers(same_subject_noun=0.0, continuation=0.0, same_location=0.0, generic_only=1.0)
    assert composite(negative) == pytest.approx(0.0)


def test_threads_composite_raises_on_missing_sub_answer():
    composite = QUESTION_SETS["threads"]["composite"]
    with pytest.raises(JevQuestionsError):
        composite(_noul_answers(same_subject_noun=1.0, continuation=1.0, same_location=1.0))


def test_work_class_composite_boundaries():
    composite = QUESTION_SETS["work_class"]["composite"]
    all_work = _noul_answers(about_the_site=1.0, personal=0.0, recording_test=0.0)
    assert composite(all_work) == pytest.approx(0.0)
    all_personal = _noul_answers(about_the_site=0.0, personal=1.0, recording_test=0.0)
    assert composite(all_personal) == pytest.approx(1.0)
    all_recording_test = _noul_answers(about_the_site=0.0, personal=0.0, recording_test=1.0)
    assert composite(all_recording_test) == pytest.approx(1.0)
    mixed = _noul_answers(about_the_site=0.5, personal=1.0, recording_test=0.0)
    assert composite(mixed) == pytest.approx(0.5)


def test_work_class_composite_raises_on_missing_sub_answer():
    composite = QUESTION_SETS["work_class"]["composite"]
    with pytest.raises(JevQuestionsError):
        composite(_noul_answers(about_the_site=1.0, personal=0.0))  # recording_test missing


# ---------------------------------------------------------------------------
# broad_score
# ---------------------------------------------------------------------------

def test_programme_match_broad_score_reads_noul_answer():
    broad_score = QUESTION_SETS["programme_match"]["broad_score"]
    assert broad_score({"match": {"noul": 0.73}}) == pytest.approx(0.73)


def test_programme_match_broad_score_raises_if_missing():
    broad_score = QUESTION_SETS["programme_match"]["broad_score"]
    with pytest.raises(JevQuestionsError):
        broad_score({})


def test_threads_broad_score_reads_noul_answer():
    broad_score = QUESTION_SETS["threads"]["broad_score"]
    assert broad_score({"same_subject": {"noul": 0.2}}) == pytest.approx(0.2)


def test_work_class_broad_score_is_p_non_work_from_choice_probabilities():
    broad_score = QUESTION_SETS["work_class"]["broad_score"]
    answers = {
        "work_class": {
            "choice": "non_work",
            "probabilities": {"work": 0.1, "non_work": 0.9},
            "confidence": 0.8,
        }
    }
    assert broad_score(answers) == pytest.approx(0.9)


def test_work_class_broad_score_raises_if_probabilities_missing():
    broad_score = QUESTION_SETS["work_class"]["broad_score"]
    with pytest.raises(JevQuestionsError):
        broad_score({"work_class": {"choice": "work", "probabilities": {}}})
    with pytest.raises(JevQuestionsError):
        broad_score({})


def test_work_class_broad_score_raises_jevquestionserror_when_not_a_dict():
    broad_score = QUESTION_SETS["work_class"]["broad_score"]
    # probabilities present but not a dict -- subscripting raises TypeError,
    # not KeyError, and must still surface as JevQuestionsError.
    with pytest.raises(JevQuestionsError):
        broad_score({"work_class": {"probabilities": ["work", "non_work"]}})
    # the whole answer entry is a string, not a dict.
    with pytest.raises(JevQuestionsError):
        broad_score({"work_class": "non_work"})


# ---------------------------------------------------------------------------
# control: programme_match
# ---------------------------------------------------------------------------

def test_programme_match_control_swaps_only_task_name():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {
        "observation": {"title": "Doors delayed", "summary": "Waiting on hardware."},
        "task": {"name": "Level 2 door install", "status": "in_progress"},
    }
    original = copy.deepcopy(state)
    donors = [
        {"task": {"name": "Roof flashing repair"}},
        {"task": {"name": "Ground floor door frames"}},  # shares "door"
    ]
    result = control(state, donors, key="row-1")

    # Input untouched.
    assert state == original

    # Only task.name changed.
    assert result["observation"] == state["observation"]
    assert result["task"]["status"] == state["task"]["status"]
    assert result["task"]["name"] != state["task"]["name"]
    # Prefers the donor sharing a word ("door") over the unrelated one.
    assert result["task"]["name"] == "Ground floor door frames"


def test_programme_match_control_falls_back_to_any_donor_when_no_word_shared():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Level 2 door install"}}
    donors = [{"task": {"name": "Roof flashing"}}]
    result = control(state, donors, key="row-2")
    assert result["task"]["name"] == "Roof flashing"


def test_programme_match_control_is_deterministic_across_reruns():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Level 2 door install"}}
    donors = [{"task": {"name": "Roof flashing"}}, {"task": {"name": "Carpark line marking"}}]
    first = control(state, donors, key="row-3")
    second = control(state, donors, key="row-3")
    assert first == second


def test_programme_match_control_raises_with_no_usable_donor():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Level 2 door install"}}
    with pytest.raises(JevQuestionsError):
        control(state, [], key="row-4")
    with pytest.raises(JevQuestionsError):
        control(state, [{"task": {}}, {"observation": {}}], key="row-4")


def test_programme_match_control_never_picks_an_identical_task_name():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Site establishment"}}
    donors = [
        {"task": {"name": "Site establishment"}},  # identical -- must be excluded
        {"task": {"name": "Level 3 slab pour"}},
    ]
    for k in ("row-a", "row-b", "row-c", "row-d", "row-e"):
        result = control(state, donors, key=k)
        assert result["task"]["name"] == "Level 3 slab pour"


def test_programme_match_control_excludes_donor_name_equal_after_normalisation():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Site Establishment."}}
    donors = [
        {"task": {"name": "site   establishment"}},  # same after casefold/punct/whitespace
        {"task": {"name": "Roof flashing repair"}},
    ]
    result = control(state, donors, key="row-f")
    assert result["task"]["name"] == "Roof flashing repair"


def test_programme_match_control_raises_when_only_identical_name_donors_exist():
    control = QUESTION_SETS["programme_match"]["control"]
    state = {"task": {"name": "Site establishment"}}
    donors = [
        {"task": {"name": "Site establishment"}},
        {"task": {"name": "SITE ESTABLISHMENT!!"}},
    ]
    with pytest.raises(JevQuestionsError):
        control(state, donors, key="row-g")


# ---------------------------------------------------------------------------
# control: threads
# ---------------------------------------------------------------------------

def test_threads_control_swaps_only_earlier_preferring_closest_gap():
    control = QUESTION_SETS["threads"]["control"]
    state = {
        "earlier": {"title": "Slab pour", "summary": "Pour scheduled Monday."},
        "later": {"title": "Slab follow-up", "summary": "Pour completed."},
        "gap_days": 3,
    }
    original = copy.deepcopy(state)
    donors = [
        {"earlier": {"title": "Roof inspection"}, "gap_days": 10},
        {"earlier": {"title": "Fence repair"}, "gap_days": 4},  # closest to 3
    ]
    result = control(state, donors, key="row-5")

    assert state == original
    assert result["later"] == state["later"]
    assert result["gap_days"] == state["gap_days"]
    assert result["earlier"] == {"title": "Fence repair"}


def test_threads_control_falls_back_to_any_donor_when_gap_days_absent():
    control = QUESTION_SETS["threads"]["control"]
    state = {"earlier": {"title": "Slab pour"}, "later": {"title": "Slab follow-up"}}
    donors = [{"earlier": {"title": "Roof inspection"}}]
    result = control(state, donors, key="row-6")
    assert result["earlier"] == {"title": "Roof inspection"}


def test_threads_control_raises_with_no_usable_donor():
    control = QUESTION_SETS["threads"]["control"]
    state = {"earlier": {"title": "Slab pour"}, "gap_days": 3}
    with pytest.raises(JevQuestionsError):
        control(state, [], key="row-7")
    with pytest.raises(JevQuestionsError):
        control(state, [{"later": {"title": "x"}}], key="row-7")


def test_threads_control_never_picks_an_identical_earlier_title():
    control = QUESTION_SETS["threads"]["control"]
    state = {"earlier": {"title": "Site walk"}}
    donors = [
        {"earlier": {"title": "Site walk"}},  # identical -- must be excluded
        {"earlier": {"title": "Fence repair"}},
    ]
    for k in ("row-a", "row-b", "row-c", "row-d", "row-e"):
        result = control(state, donors, key=k)
        assert result["earlier"]["title"] == "Fence repair"


def test_threads_control_excludes_donor_title_equal_after_normalisation():
    control = QUESTION_SETS["threads"]["control"]
    state = {"earlier": {"title": "Site Walk."}}
    donors = [
        {"earlier": {"title": "site   walk"}},  # same after casefold/punct/whitespace
        {"earlier": {"title": "Roof inspection"}},
    ]
    result = control(state, donors, key="row-f")
    assert result["earlier"]["title"] == "Roof inspection"


def test_threads_control_raises_when_only_identical_title_donors_exist():
    control = QUESTION_SETS["threads"]["control"]
    state = {"earlier": {"title": "Site walk"}}
    donors = [
        {"earlier": {"title": "Site walk"}},
        {"earlier": {"title": "SITE WALK!!"}},
    ]
    with pytest.raises(JevQuestionsError):
        control(state, donors, key="row-g")


# ---------------------------------------------------------------------------
# control: work_class
# ---------------------------------------------------------------------------

def test_work_class_control_replaces_title_and_summary_only():
    control = QUESTION_SETS["work_class"]["control"]
    state = {"title": "Site walk", "summary": "Discussed formwork.", "category": "site"}
    original = copy.deepcopy(state)
    result = control(state, donors=[], key="row-8")

    assert state == original
    assert result["title"] == "General discussion."
    assert result["summary"] == "General discussion."
    assert result["category"] == "site"


def test_work_class_control_ignores_donors_and_key():
    control = QUESTION_SETS["work_class"]["control"]
    state = {"title": "Site walk", "summary": "Discussed formwork."}
    result_a = control(state, donors=[{"title": "irrelevant"}], key="row-9")
    result_b = control(state, donors=[], key="another-row")
    assert result_a == result_b


def test_work_class_control_raises_when_already_general_discussion():
    control = QUESTION_SETS["work_class"]["control"]
    state = {
        "title": "General discussion.",
        "summary": "General discussion.",
        "category": "site",
    }
    original = copy.deepcopy(state)
    with pytest.raises(JevQuestionsError):
        control(state, donors=[], key="row-10")
    assert state == original
