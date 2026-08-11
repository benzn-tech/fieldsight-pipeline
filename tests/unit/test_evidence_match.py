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


# ---- why it was unverified --------------------------------------------
#
# The status alone cannot be acted on. `unverified` has two disjoint causes with
# disjoint fixes -- our matcher was too strict, or the model invented the quote
# -- and the first splits again by which rule was too strict. Without a code on
# every unverified path, a person has to re-derive by hand what the matcher
# already knew, including for the cases the matcher knew for certain.

def test_every_unverified_path_carries_a_machine_readable_reason():
    codes = {
        em.REASON_NO_TURNS: em.check_quote("x y z a b", [], AT, **KW),
        em.REASON_EMPTY_QUOTE: em.check_quote("", [_turn("anything")], AT, **KW),
        em.REASON_NOTHING_CLOSE: em.check_quote(
            "the market is coming down sharply now",
            [_turn("the slab pour is pushed to Thursday")], AT, **KW),
        em.REASON_BELOW_FUZZY: em.check_quote(
            "the slab pour is pushed to Friday",
            [_turn("the slab pour is pushed to Thursday")], AT, **KW),
    }
    for expected, r in codes.items():
        assert r["status"] == "unverified"
        assert r["reason"] == expected


def test_a_fuzzy_miss_keeps_its_ratio_alongside_the_reason():
    # The ratio is what says whether the THRESHOLD was too strict or the quote
    # was never there; the reason alone cannot distinguish them.
    r = em.check_quote("the slab pour is pushed to Friday",
                       [_turn("the slab pour is pushed to Thursday")], AT, **KW)
    assert r["reason"] == em.REASON_BELOW_FUZZY
    assert 0 < r["fuzzy_ratio"] < 0.9


def test_a_window_holding_only_empty_turns_is_told_apart_from_an_empty_window():
    # Both used to be indistinguishable in the artifact, and they mean opposite
    # things: one is a window too narrow, the other is a transcript with nothing
    # in it. Widening W would never fix the second.
    r = em.check_quote("x y z a b", [_turn("")], AT, **KW)
    assert r["status"] == "unverified"
    assert r["reason"] == em.REASON_NO_CANDIDATE_TEXT


def test_a_matched_quote_carries_no_reason():
    # Only failures explain themselves; a reason on every row would bloat the
    # artifact for the healthy majority.
    r = em.check_quote("the slab pour is pushed to Thursday",
                       [_turn("the slab pour is pushed to Thursday")], AT, **KW)
    assert "reason" not in r


# ---- "not close" is not "not close ENOUGH" -----------------------------
#
# One code for both sends the reader to the wrong place. The first real
# unverified citation this feature produced carried `below_fuzzy_threshold` at
# a ratio of 0.331 -- which reads as "the cut is too strict, loosen it" when
# the truth was that the quote was 560 seconds away and nothing resembling it
# was in the window at all. A near-miss of genuinely similar text scores around
# 0.85; 0.33 is the score of unrelated speech.

def test_a_quote_with_nothing_resembling_it_is_told_apart_from_a_near_miss():
    turns = [_turn("the slab pour is pushed to Thursday because of the pump")]
    far = em.check_quote("the market is now coming down sharply in wellington",
                         turns, AT, **KW)
    assert far["status"] == "unverified"
    assert far["reason"] == em.REASON_NOTHING_CLOSE
    assert far["fuzzy_ratio"] < em.NOTHING_CLOSE_RATIO


def test_a_genuine_near_miss_still_blames_the_threshold():
    # One word changed out of eight: this IS the case the fuzzy tier exists for,
    # and the reason must point at the cut rather than at the transcript.
    turns = [_turn("the slab pour is pushed to Thursday because of the pump")]
    near = em.check_quote("the slab pour is pushed to Friday because of the pump",
                          turns, AT, w_seconds=90, floor_tokens=5,
                          fuzzy_threshold=0.99)
    assert near["status"] == "unverified"
    assert near["reason"] == em.REASON_BELOW_FUZZY
    assert near["fuzzy_ratio"] >= em.NOTHING_CLOSE_RATIO


def test_the_cut_sits_where_the_two_populations_stop_overlapping():
    # Measured 2026-08-10: no honest sample scored below 0.634, and 99% of
    # spurious ones scored below 0.625. The constant is that gap, and it is
    # deliberately NOT the fuzzy threshold -- tightening the threshold must not
    # silently start relabelling near-misses as "nothing there".
    assert 0.62 <= em.NOTHING_CLOSE_RATIO <= 0.64


# ---- quotes the model spliced with an ellipsis -------------------------
#
# The dominant real cause of a fuzzy-branch citation, measured across 949 live
# citations: the model joins two non-contiguous spans with "..." and the elided
# middle reads as a missing chunk, scoring 0.75-0.92. Both spans are verbatim.
# The threshold cannot fix this class -- how much was elided is the model's
# choice, so the ratio has no lower bound -- and a quote with 0.746 was being
# filed as the fabrication signal while every word of it was in the transcript.
#
# Transcripts carry no punctuation (parse_transcribe_json keeps pronunciation
# items only), so "..." never occurs in the candidate text. It is unambiguously
# the model saying "I left something out here".

def test_a_spliced_quote_verifies_when_every_fragment_is_verbatim():
    turns = [_turn("the slab pour is pushed to Thursday because the pump is late "
                   "and we cannot get one until the morning")]
    r = em.check_quote("the slab pour is pushed to Thursday... "
                       "we cannot get one until the morning", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"
    assert r["spliced"] is True
    assert r["fragments"] == 2


def test_a_spliced_quote_is_not_promoted_to_verified():
    # "A... B" reads as one continuous sentence and is two moments. In a
    # feature whose whole job is provenance, that difference cannot be quietly
    # dropped -- so it stays in the tier that is counted apart.
    turns = [_turn("alpha bravo charlie delta echo foxtrot golf hotel india")]
    r = em.check_quote("alpha bravo charlie... golf hotel india", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"


def test_fragments_need_not_appear_in_the_order_they_were_quoted():
    # Real case, 2026-08-10: overlapping chunks put the LATER-spoken fragment
    # first in the candidate text, because turns sort by absolute start and the
    # ring buffer overlaps. Requiring increasing positions would fail an honest
    # citation -- and this is exactly the shape that produced the 0.746.
    turns = [_turn("golf hotel india juliet kilo", 0.0, fn="c0068.json"),
             _turn("alpha bravo charlie delta echo", 0.0, fn="c0069.json")]
    r = em.check_quote("alpha bravo charlie... india juliet kilo", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"
    assert r["spliced"] is True


def test_a_spliced_quote_with_one_invented_fragment_does_not_verify():
    turns = [_turn("the slab pour is pushed to Thursday because the pump is late")]
    r = em.check_quote("the slab pour is pushed to Thursday... "
                       "and the budget was approved on Tuesday", turns, AT, **KW)
    assert r["status"] == "unverified", \
        "every fragment must be present -- one real half cannot carry an invented one"


def test_a_short_fragment_does_not_make_a_specific_quote_weak():
    # The floor is about how much was cited in total, not how the model chose
    # to break it up.
    turns = [_turn("the slab pour is pushed to Thursday because the pump is late "
                   "and we cannot get one until the morning yes")]
    r = em.check_quote("the slab pour is pushed to Thursday because the pump "
                       "is late... yes", turns, AT, **KW)
    assert r["status"] == "verified_fuzzy"


def test_an_ellipsis_with_nothing_either_side_falls_back_to_the_normal_path():
    turns = [_turn("the slab pour is pushed to Thursday")]
    r = em.check_quote("... the slab pour is pushed to Thursday", turns, AT, **KW)
    assert r["status"] == "verified", "one fragment is not a splice"


# ---- the model wrote the wrong hour ------------------------------------
#
# Measured over 1,576 real citations: of the nine distinct quotes that came out
# `unverified`, SEVEN existed verbatim in the transcript exactly one hour after
# the time the model cited -- minutes and seconds identical, hour off by one.
# Not fabrication, and not our rendering either: every one of the 279 turns in
# that session had its prompt label equal to its abs_start, so the prompt said
# 15:13:58 and the model wrote 14:13:58.
#
# The probe never verifies. Accepting a match an hour away would destroy the
# property that makes the number mean anything -- a quote matching somewhere
# else is a mis-citation. It labels, so the class can be subtracted from the
# headline and a reader is pointed at the audio instead of hunting for it.

def _hour_turns(text):
    later = datetime(2026, 8, 7, 15, 23, 7)
    return [_turn(text, 4.0, at=later, fn="c0121.json")]


def test_a_quote_an_hour_off_is_named_rather_than_called_fabrication():
    turns = _hour_turns("it is because in the enabling it says remove that panel")
    r = em.check_quote("it is because in the enabling it says", turns, AT, **KW)
    assert r["status"] == "unverified", "an hour away must never verify"
    assert r["reason"] == em.REASON_ANCHOR_HOUR_SLIP
    assert r["slip_hours"] == 1


def test_the_slip_reports_where_to_listen():
    turns = _hour_turns("it is because in the enabling it says remove that panel")
    r = em.check_quote("it is because in the enabling it says", turns, AT, **KW)
    assert r["segment_key_source"] == "c0121.json"
    assert r["offset_sec"] == 4.0


def test_a_short_quote_is_not_slip_probed():
    # "yes" turns up an hour later in almost any recording. Probing below the
    # specificity floor would manufacture slips out of coincidence.
    turns = _hour_turns("yes we should stop")
    r = em.check_quote("yes", turns, AT, **KW)
    assert r.get("reason") != em.REASON_ANCHOR_HOUR_SLIP


def test_a_genuinely_absent_quote_reports_no_slip():
    turns = _hour_turns("we talked about the crane and the weather today")
    r = em.check_quote("the budget was approved on Tuesday afternoon", turns, AT, **KW)
    assert r["status"] == "unverified"
    assert "slip_hours" not in r


def test_the_slip_probe_also_catches_a_spliced_quote_an_hour_off():
    turns = _hour_turns("alpha bravo charlie delta echo foxtrot golf hotel india")
    r = em.check_quote("alpha bravo charlie... golf hotel india", turns, AT, **KW)
    assert r["reason"] == em.REASON_ANCHOR_HOUR_SLIP
