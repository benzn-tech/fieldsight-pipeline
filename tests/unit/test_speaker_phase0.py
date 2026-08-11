"""Unit: the Phase 0 speaker-separation harness, minus the model.

Phase 0 asks one question — **can two people six metres away be told apart** — and the
canonical way to get it wrong is to be fooled by success: on a two-person recording the
failure mode (wearer forms one clean cluster, every distant speaker collapses into a single
"other") produces exactly two clusters and looks correct. So the arithmetic that decides
"separable" is worth more scrutiny than the embedding model, and it is what is tested here.

The embedding backend is I/O and a large download; it is imported lazily inside
`scripts/speaker_phase0.py` and is not exercised by these tests.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts"))

import speaker_phase0 as sp  # noqa: E402  (needs the sys.path line above)

SR = 16000


def _tone(seconds, amp=0.3, freq=220.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(seconds, sr=SR):
    return np.zeros(int(seconds * sr), dtype=np.float32)


# ---- splitting on the 4-second separators ----

def test_the_four_second_gaps_become_segment_boundaries():
    audio = np.concatenate([_tone(3), _silence(4), _tone(2), _silence(4), _tone(5)])
    segs = sp.split_on_silence(audio, SR, min_silence_s=3.0)
    assert len(segs) == 3
    starts = [round(s.start_s) for s in segs]
    assert starts == [0, 7, 13]


def test_a_short_pause_inside_a_sentence_does_not_split_it():
    """The script says 'silence 4 seconds between segments' precisely so that a breath does
    not count. A 0.4 s pause is a breath."""
    audio = np.concatenate([_tone(3), _silence(0.4), _tone(3)])
    segs = sp.split_on_silence(audio, SR, min_silence_s=3.0)
    assert len(segs) == 1


def test_leading_and_trailing_silence_are_not_segments():
    audio = np.concatenate([_silence(5), _tone(2), _silence(5)])
    segs = sp.split_on_silence(audio, SR, min_silence_s=3.0)
    assert len(segs) == 1
    assert 4.5 < segs[0].start_s < 5.5


def test_a_recording_with_no_speech_yields_nothing_rather_than_one_huge_segment():
    segs = sp.split_on_silence(_silence(20), SR, min_silence_s=3.0)
    assert segs == []


# ---- the separability verdict ----

def test_two_clearly_distinct_voices_are_reported_separable():
    same = [0.82, 0.79, 0.85, 0.80]
    diff = [0.21, 0.18, 0.25, 0.11]
    v = sp.separability(same, diff, min_n=4)
    assert v.separable is True
    assert v.overlap == 0


def test_any_overlap_is_reported_even_when_the_medians_are_far_apart():
    """A 12.6 dB median gap with complete range overlap is what withdrew Phase A. Medians
    are not a verdict."""
    same = [0.80, 0.75, 0.30]
    diff = [0.20, 0.22, 0.35]
    v = sp.separability(same, diff, min_n=3)
    assert v.separable is False
    assert v.overlap > 0


def test_the_accuracy_is_labelled_an_upper_bound_because_the_cut_is_fitted():
    v = sp.separability([0.8, 0.7], [0.2, 0.1], min_n=2)
    assert v.fitted_on_the_same_data is True
    assert v.best_accuracy == pytest.approx(1.0)


def test_too_few_turns_refuses_to_reach_a_verdict():
    """Three turns cannot answer this and must not look like they did."""
    v = sp.separability([0.8], [0.2], min_n=5)
    assert v.separable is None
    assert "too few" in v.note.lower()


# ---- the trap this whole exercise exists for ----

def test_distant_speakers_collapsing_into_one_is_reported_as_collapse_not_success():
    """Every distant speaker resembling every other distant speaker MORE than they resemble
    their own enrolment is the canonical failure, and on two people it looks like two clean
    clusters."""
    # Two distant people. Each is closer to the other than to their own profile.
    sims = {("D", "D"): [0.30], ("E", "E"): [0.28], ("D", "E"): [0.71], ("E", "D"): [0.69]}
    r = sp.collapse_report(sims)
    assert r.collapsed is True


def test_two_distant_people_who_stay_themselves_are_not_a_collapse():
    sims = {("D", "D"): [0.78], ("E", "E"): [0.74], ("D", "E"): [0.19], ("E", "D"): [0.22]}
    r = sp.collapse_report(sims)
    assert r.collapsed is False


def test_a_single_distant_speaker_cannot_answer_the_collapse_question():
    """One person at 6 m is the two-person recording that looks like success. The harness
    must say it cannot tell, not return False."""
    r = sp.collapse_report({("D", "D"): [0.8]})
    assert r.collapsed is None


# ---- cosine ----

def test_cosine_is_one_for_a_vector_with_itself_and_zero_for_orthogonal():
    a = np.array([1.0, 2.0, 3.0])
    b = np.array([-2.0, 1.0, 0.0])
    assert sp.cosine(a, a) == pytest.approx(1.0)
    assert sp.cosine(a, b) == pytest.approx(0.0, abs=1e-9)


def test_cosine_ignores_loudness():
    """Distance changes level by ~20 dB across this test's range; a similarity that moved
    with level would be measuring the microphone, not the speaker."""
    a = np.array([1.0, 2.0, 3.0])
    assert sp.cosine(a, a * 0.01) == pytest.approx(1.0)
