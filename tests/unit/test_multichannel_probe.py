"""Unit: the judgement inside the multichannel probe.

The probe answers whether channel index can stand in for speaker identity on a
multi-device recording. Everything that decides that is a pure function here, so
it can be pinned without S3, ffmpeg or a real meeting.

Three properties matter, and each was learned by getting it wrong first:

1. **The positive peak, not the largest absolute one.** The same acoustic event
   on two microphones correlates positively; `argmax(|c|)` returns negative
   troughs and produced a confident, meaningless −0.15 "peak".
2. **One window is not a sample.** A single window said the level difference
   never changed sign and the whole idea was dead; two more windows overturned
   it. The estimator therefore reports its spread and the caller must look.
3. **The verdict ignores the mean.** A device can sit 11 dB below the other all
   session for reasons unrelated to speech — two microphone part variants on the
   same board differ by 15 dB. What carries speaker information is whether the
   difference MOVES and crosses zero.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts"))

import multichannel_probe as mp  # noqa: E402

SR = 16000


def _speechlike(n, seed=0):
    """Broadband, amplitude-modulated noise — correlates like speech does."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 3000, n)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * np.arange(n) / SR)
    return x * env


# ---- chunk parsing and pairing -------------------------------------------

def test_parse_chunk_reads_the_device_clock_and_index():
    k = "users/Ben_UCPK/audio/2026-08-07/ben_ucpk_2026-08-07_14-38-51_sid" + "a" * 32 + "_c0046.wav"
    assert mp.parse_chunk(k) == (14 * 3600 + 38 * 60 + 51, 46)


def test_parse_chunk_ignores_anything_else():
    assert mp.parse_chunk("users/x/audio/2026-08-07/notachunk.wav") is None
    assert mp.parse_chunk("") is None
    assert mp.parse_chunk(None) is None


def test_pairing_offsets_make_both_files_start_at_the_same_instant():
    """A starts at 14:38:51, B at 14:38:58 — skip 7 s into A, 0 into B."""
    a = [("A46", 14 * 3600 + 38 * 60 + 51)]
    b = [("B47", 14 * 3600 + 38 * 60 + 58)]
    assert mp.pair_chunks(a, b) == [("A46", "B47", 7, 0)]


def test_pairing_skips_a_sliver_of_overlap():
    """Two seconds of common audio cannot support a skew estimate."""
    a = [("A", 1000)]
    b = [("B", 1028)]          # 30s chunks, 2s overlap
    assert mp.pair_chunks(a, b) == []


# ---- skew estimation ------------------------------------------------------

def test_skew_recovers_a_known_delay():
    base = _speechlike(SR * 20)
    delay = int(0.915 * SR)                      # the real measured skew
    a = np.concatenate([np.zeros(delay), base])[:len(base)]
    ms, spread, used, total = mp.estimate_skew(a, base, SR)
    assert ms == pytest.approx(915, abs=5)
    assert spread < 20 and used >= 2


def test_skew_says_so_rather_than_guessing_on_unrelated_audio():
    """Two different rooms must not produce a confident number."""
    a, b = _speechlike(SR * 20, seed=1), _speechlike(SR * 20, seed=2)
    ms, _, used, total = mp.estimate_skew(a, b, SR)
    assert ms is None or used < total


def test_skew_takes_the_positive_peak_not_the_biggest_absolute_one():
    """An inverted copy correlates strongly NEGATIVELY at lag 0. Reporting that
    lag would be an accident; a positive-peak search must not return it as a
    confident estimate."""
    base = _speechlike(SR * 20)
    ms, _, used, _ = mp.estimate_skew(-base, base, SR)
    assert ms is None or abs(ms) > 1


# ---- level difference -----------------------------------------------------

def test_level_difference_applies_the_skew_before_comparing():
    n = SR * 6
    base = _speechlike(n)
    delay = int(0.5 * SR)
    a = np.concatenate([np.zeros(delay), base])[:n]
    aligned = mp.level_difference(a, base, SR, skew_ms=500)
    naive = mp.level_difference(a, base, SR, skew_ms=0)
    assert np.std([r[3] for r in aligned]) < np.std([r[3] for r in naive])


def test_silent_windows_are_dropped():
    """A window where nobody spoke says nothing about who spoke, and keeping it
    would drag the spread toward zero — making a working signal look flat."""
    n = SR * 4
    a = np.zeros(n)
    a[:SR] = _speechlike(SR)
    b = np.zeros(n)
    b[:SR] = _speechlike(SR, seed=3)
    rows = mp.level_difference(a, b, SR, 0)
    assert all(t < 1.0 for t, *_ in rows)


# ---- the verdict ----------------------------------------------------------

def _rows(diffs):
    return [(i * 0.5, -30.0, -30.0 - d, d) for i, d in enumerate(diffs)]


def test_a_constant_offset_is_not_speaker_information():
    """One device 11 dB below the other all session is a hardware difference,
    not a speaker signal. The mean is large; the swing is nil."""
    v = mp.verdict(_rows([-11.0] * 20))
    assert v["usable"] and v["swing_db"] == 0.0 and v["crosses_zero"] is False


def test_a_swinging_sign_changing_difference_is_speaker_information():
    v = mp.verdict(_rows([-15, 12, -9, 18, -14, 7, -11, 22, -6, 15]))
    assert v["crosses_zero"] is True and v["swing_db"] > 30


def test_verdict_refuses_to_judge_too_few_windows():
    assert mp.verdict(_rows([-11, 3, -8])) == {
        "usable": False, "reason": "only 3 windows with signal on both"}


# ---- scoring against ground truth ----------------------------------------

def test_scoring_credits_the_device_worn_by_the_speaker():
    rows = _rows([]) + [(0.5, -20.0, -35.0, 15.0), (1.0, -21.0, -34.0, 13.0),
                        (5.5, -35.0, -20.0, -15.0), (6.0, -34.0, -21.0, -13.0)]
    truth = [(0.0, 2.0, "Ben_UCPK"), (5.0, 7.0, "Ben_UCPK2")]
    hits, misses, _ = mp.score_against_truth(rows, truth, "Ben_UCPK", "Ben_UCPK2")
    assert (hits, misses) == (2, 0)


def test_scoring_counts_a_wrong_attribution():
    rows = [(0.5, -20.0, -35.0, 15.0)]           # A louder
    truth = [(0.0, 2.0, "Ben_UCPK2")]            # but B's wearer spoke
    hits, misses, _ = mp.score_against_truth(rows, truth, "Ben_UCPK", "Ben_UCPK2")
    assert (hits, misses) == (0, 1)


def test_a_span_with_no_windows_is_neither_hit_nor_miss():
    """Silence inside a labelled span is missing evidence, not a wrong answer."""
    hits, misses, spans = mp.score_against_truth(
        [(0.5, -20.0, -35.0, 15.0)], [(60.0, 62.0, "Ben_UCPK")], "Ben_UCPK", "Ben_UCPK2")
    assert (hits, misses) == (0, 0)
    assert spans[0][3] is None
