"""Unit: the citation matcher (P1-2 Task 2).

Each test here is a documented way the Phase A number could have been noise
instead of a measurement. The number is the deliverable, so an unspecified rule
in this module is noise added directly to it.
"""
from datetime import datetime

import pytest

em = pytest.importorskip("evidence_match")

AT = datetime(2026, 8, 7, 14, 23, 7)
KW = dict(w_seconds=90, floor_tokens=5, fuzzy_threshold=0.9)


def _turn(text, start_sec=0.0, at=AT, fn="c0000.json", end=None):
    return {"text": text, "start_sec": start_sec, "abs_start": at,
            "abs_end": end or at, "source_filename": fn}


def test_an_honest_quote_is_verified():
    turns = [_turn("the slab pour is pushed to Thursday because of the pump")]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT, **KW)
    assert r["status"] == "verified"
    assert r["segment_key_source"] == "c0000.json"
    assert r["offset_sec"] == 0.0


def test_casing_and_punctuation_do_not_break_it():
    turns = [_turn("the slab pour is pushed to thursday")]
    r = em.check_quote("The slab pour is pushed to Thursday.", turns, AT, **KW)
    assert r["status"] == "verified"


def test_a_quote_spanning_a_chunk_seam_verifies():
    # Turns are per-segment and never merged across chunks, so a sentence split
    # at a seam is two turns. Testing each alone would fail an honest citation;
    # the candidate text is their concatenation.
    turns = [_turn("the slab pour is", 0.0, fn="c0000.json"),
             _turn("pushed to Thursday", 0.0, fn="c0001.json")]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT, **KW)
    assert r["status"] == "verified"
    assert r["segment_key_source"] == "c0000.json", \
        "a seam-spanning quote anchors to the turn containing its START"


def test_cjk_verifies_despite_space_joined_turn_text():
    # Turn text is space-joined; a model writing Chinese writes it unspaced.
    # Without deleting whitespace inside CJK runs this fails, and on a bilingual
    # product that alone could be most of the unverified count.
    turns = [_turn("楼板 浇筑 推迟 到 周四")]
    r = em.check_quote("楼板浇筑推迟到周四", turns, AT, **KW)
    assert r["status"] == "verified"


def test_a_specific_cjk_quote_clears_the_floor():
    # Whitespace tokens would score this 1 and cap every CJK topic at `weak` —
    # a bias in the headline number that correlates with language.
    assert em.token_count("楼板浇筑推迟到周四") >= 5


def test_a_short_english_quote_is_weak_not_verified():
    turns = [_turn("yes we should stop")]
    r = em.check_quote("yes", turns, AT, **KW)
    assert r["status"] == "weak", "a one-word quote verifies against anything"


def test_a_weak_quote_still_reports_its_anchor():
    # `weak` means "not specific enough to count as evidence", not "not found".
    turns = [_turn("yes we should stop", 4.0)]
    r = em.check_quote("yes", turns, AT, **KW)
    assert r["offset_sec"] == 4.0


def test_a_quote_absent_from_the_transcript_is_unverified():
    turns = [_turn("we talked about the crane and the weather today")]
    r = em.check_quote("the market is now coming down sharply", turns, AT, **KW)
    assert r["status"] == "unverified"


def test_a_quote_found_outside_the_window_is_unverified():
    far = datetime(2026, 8, 7, 15, 30, 0)
    turns = [_turn("the slab pour is pushed to Thursday", at=far)]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT, **KW)
    assert r["status"] == "unverified", \
        "a match somewhere else entirely is a mis-citation, not a verification"


def test_regularisation_lands_in_the_fuzzy_tier():
    # ASR output has no apostrophes (parse keeps pronunciation items only), so a
    # model writing "cannot" against "can t" is honest regularisation, not
    # invention — but it must be counted apart from an exact match.
    turns = [_turn("we can t get the pump before then at all")]
    r = em.check_quote("we cannot get the pump before then at all", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"
    assert r["fuzzy_ratio"] >= 0.9


def test_a_quote_longer_than_the_window_still_matches():
    # THE common shape in this tier: regularisation makes the quote LONGER than
    # its source ("can t" -> "cannot"). A sliding window that requires
    # len(haystack) >= len(needle) returns no-match and fails every one of them.
    turns = [_turn("we can t get the pump")]
    r = em.check_quote("we cannot get the pump", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"


def test_the_fuzzy_search_is_a_sliding_window_not_whole_haystack():
    # Similarity against the whole window would be near zero for a short quote
    # in a long transcript regardless of honesty.
    long_tail = " ".join(["unrelated chatter about the weather"] * 40)
    turns = [_turn("we can t get the pump before then " + long_tail)]
    r = em.check_quote("we cannot get the pump before then", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"


def test_found_offset_is_reported_for_calibration():
    # W is chosen from this distribution, not argued for.
    turns = [_turn("the slab pour is pushed to Thursday",
                   at=datetime(2026, 8, 7, 14, 23, 40))]
    r = em.check_quote("the slab pour is pushed to Thursday", turns, AT, **KW)
    assert r["found_offset_sec"] == 33.0


def test_an_empty_quote_is_unverified_not_a_crash():
    assert em.check_quote("", [_turn("anything")], AT, **KW)["status"] == "unverified"


def test_no_turns_in_the_window_is_unverified():
    assert em.check_quote("x y z a b", [], AT, **KW)["status"] == "unverified"


# ---- rollup ------------------------------------------------------------

def test_rollup_of_no_evidence_is_absent():
    assert em.roll_up([]) == "absent"


def test_rollup_unverified_dominates():
    assert em.roll_up(["verified", "unverified"]) == "unverified"


def test_rollup_unchecked_is_never_masked_by_a_good_sibling():
    # The status exists to stop OUR bugs deflating the signal.
    assert em.roll_up(["verified", "unchecked"]) == "unchecked"


def test_rollup_unverified_outranks_unchecked():
    assert em.roll_up(["unchecked", "unverified"]) == "unverified"


def test_rollup_takes_the_worst_remaining():
    assert em.roll_up(["verified", "weak"]) == "weak"
    assert em.roll_up(["verified", "verified_fuzzy"]) == "verified_fuzzy"
    assert em.roll_up(["verified", "verified"]) == "verified"
