"""Unit: the arithmetic that decides whether to put a name on a turn.

Plan: docs/superpowers/plans/2026-08-11-speaker-identity-implementation.md phase 1.
Findings: docs/superpowers/specs/2026-08-11-speaker-phase0-results.md.

Phase 0 measured 31 of 32 turns attributed correctly with two people five metres away, and
the single miss dictated the two rules this module exists to enforce:

* the miss was a **2.1 s** turn, and it is also the lowest same-person score in the whole
  set — so a turn below a duration floor gets no name at all, whatever the scores say;
* the same-person and different-person distributions **overlap** between 0.104 and 0.205, so
  no absolute cut is shippable. Matching is nearest-profile with a required *margin*, and
  the best absolute threshold measured (+0.262 → 99%) is fitted on its own data and is an
  upper bound nobody may deploy.

A wrong confident name costs more than a missing one (v2 §1), so every ambiguous case
degrades to `tentative` or `unknown` rather than guessing.

No torch, no speechbrain, no onnxruntime — this is numpy and nothing else, so the suite
never needs an 80 MB download.
"""
import numpy as np
import pytest

import voiceprint_utils as vp  # noqa: E402  (hard import: a skip is not a RED)


# ---- cosine ----

def test_cosine_is_one_for_a_vector_with_itself_and_zero_for_orthogonal():
    a, b = np.array([1.0, 2.0, 3.0]), np.array([-2.0, 1.0, 0.0])
    assert vp.cosine(a, a) == pytest.approx(1.0)
    assert vp.cosine(a, b) == pytest.approx(0.0, abs=1e-9)


def test_cosine_ignores_loudness():
    """Across 0–6 m the level moves ~20 dB. A score that moved with it would be measuring
    the microphone, not the speaker."""
    a = np.array([1.0, 2.0, 3.0])
    assert vp.cosine(a, a * 0.01) == pytest.approx(1.0)


def test_a_zero_vector_scores_zero_rather_than_raising():
    assert vp.cosine(np.zeros(3), np.array([1.0, 0.0, 0.0])) == 0.0


# ---- the duration floor ----

def test_a_turn_below_the_floor_gets_no_name_however_good_the_scores():
    """The one miss in Phase 0 was 2.1 s, and it scored its own speaker LOWEST of anyone.
    Short turns are not weak evidence, they are unusable evidence."""
    d = vp.decide_name({"Ben": 0.90, "Zoe": 0.10}, duration_s=2.1)
    assert d.status == "unknown" and d.name is None
    assert "short" in d.reason.lower()


def test_a_turn_at_the_floor_is_allowed():
    d = vp.decide_name({"Ben": 0.90, "Zoe": 0.10}, duration_s=3.0)
    assert d.status == "confirmed" and d.name == "Ben"


# ---- nearest profile plus a margin, never an absolute cut ----

def test_a_clear_winner_is_confirmed():
    d = vp.decide_name({"Ben": 0.48, "Zoe": 0.08, "Mike": 0.07}, duration_s=6.0)
    assert d.status == "confirmed" and d.name == "Ben"
    assert d.margin == pytest.approx(0.40)


def test_two_close_scores_are_tentative_rather_than_a_coin_flip():
    """This is the shape of the Phase 0 miss: 0.111 against 0.104. Naming the winner there
    is a wrong confident name, which v2 §1 prices above a missing one."""
    d = vp.decide_name({"Ben": 0.111, "Zoe": 0.104}, duration_s=6.0)
    assert d.status == "tentative" and d.name == "Ben"


def test_a_tentative_name_still_says_who_it_leans_towards():
    """Viewer-only, per v2 §1 — the guess is shown as a guess, not withheld entirely."""
    d = vp.decide_name({"Ben": 0.30, "Zoe": 0.26}, duration_s=8.0)
    assert d.name == "Ben" and d.status == "tentative"


def test_high_scores_for_everyone_are_still_ambiguous():
    """An absolute threshold would confirm both of these. The margin is what refuses."""
    d = vp.decide_name({"Ben": 0.62, "Zoe": 0.60}, duration_s=10.0)
    assert d.status == "tentative"


def test_no_profiles_is_unknown_not_an_exception():
    d = vp.decide_name({}, duration_s=10.0)
    assert d.status == "unknown" and d.name is None


def test_a_single_profile_has_nothing_to_be_better_than():
    """With one enrolled voice there is no second-best, so there is no margin to clear.
    Confirming on an absolute score is exactly what the overlapping distributions forbid."""
    d = vp.decide_name({"Ben": 0.9}, duration_s=10.0)
    assert d.status == "tentative" and d.name == "Ben"


def test_the_margin_is_configurable_but_its_default_is_not_the_fitted_cut():
    """+0.262 reached 99% on the material that produced it. Shipping it would be shipping a
    number fitted to its own data, so the default must not be it."""
    assert vp.DEFAULT_MIN_MARGIN != pytest.approx(0.262)
    d = vp.decide_name({"Ben": 0.5, "Zoe": 0.2}, duration_s=6.0, min_margin=0.9)
    assert d.status == "tentative"


# ---- the enrolment contamination guard (v2 §6) ----

def test_a_window_containing_two_voices_is_refused_for_enrolment():
    """Enrolling a window that holds two people poisons the profile with someone else, and
    the profile cannot be un-poisoned afterwards — only the whole sample deleted."""
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([0.0, 1.0, 0.0])
    assert vp.window_is_homogeneous([a, a * 0.9, b, b * 1.1]) is False


def test_a_window_of_one_voice_is_accepted():
    a = np.array([1.0, 0.1, 0.0])
    assert vp.window_is_homogeneous([a, a * 0.8, a + 0.02]) is True


def test_a_single_frame_cannot_be_shown_to_be_homogeneous():
    """One frame is trivially self-consistent, which is not evidence. Returning True here
    would let a one-frame window enrol unchecked."""
    assert vp.window_is_homogeneous([np.array([1.0, 0.0])]) is None


def test_no_frames_at_all_is_not_a_pass():
    assert vp.window_is_homogeneous([]) is None


# ----------------------------------------------------------
# Per-person aggregation. `profiles_for_matching` returns one row per SAMPLE
# (repositories/voiceprints.py JOINs speaker_voiceprint_samples), and decide_name takes a
# flat mapping with no notion of person. Phase 0 enrolled SIX profiles for FIVE people --
# Ben has an English and a Chinese one, ~0.08 apart, and twice the Chinese one scored
# nearest of all. Fed in as two keys, Ben becomes his own runner-up, the margin is 0.08,
# and the answer is `tentative`: a person losing to himself.
#
# So Phase 0's 31/32 is NEAREST-PROFILE accuracy, not what this rule would confirm.
# ----------------------------------------------------------


def test_two_samples_of_one_person_collapse_to_their_best():
    rows = [
        {"person_key": "ben", "score": 0.31},
        {"person_key": "ben", "score": 0.43},
        {"person_key": "zoe", "score": 0.08},
    ]
    assert vp.aggregate_scores(rows) == {"ben": 0.43, "zoe": 0.08}


def test_a_person_with_two_profiles_no_longer_beats_himself():
    """The Phase 0 case, with its measured numbers rather than invented ones.

    Ben's own two profiles sit ~0.08 apart and the nearest other voice is far below both.
    Ungrouped they are each other's runner-up and the margin never clears 0.15."""
    ungrouped = {"ben_en": 0.425, "ben_zh": 0.505, "zoe": 0.078}
    assert vp.decide_name(ungrouped, duration_s=5.0).status == "tentative", (
        "precondition: without aggregation the two Ben profiles defeat each other")

    rows = [
        {"person_key": "ben", "score": 0.425},
        {"person_key": "ben", "score": 0.505},
        {"person_key": "zoe", "score": 0.078},
    ]
    decision = vp.decide_name(vp.aggregate_scores(rows), duration_s=5.0)
    assert decision.status == "confirmed"
    assert decision.name == "ben"


def test_profiles_without_a_user_do_not_collide():
    """`user_id` is nullable by design -- an unnamed recurring voice may hold a profile
    before anyone names it (0038). Two such profiles are two people until told otherwise,
    so they must not merge on a shared null."""
    rows = [
        {"person_key": "profile-a1b2", "score": 0.40},
        {"person_key": "profile-c3d4", "score": 0.12},
    ]
    assert vp.aggregate_scores(rows) == {"profile-a1b2": 0.40, "profile-c3d4": 0.12}


def test_an_empty_row_set_is_unknown_rather_than_a_crash():
    assert vp.aggregate_scores([]) == {}
    assert vp.decide_name(vp.aggregate_scores([]), duration_s=5.0).status == "unknown"
