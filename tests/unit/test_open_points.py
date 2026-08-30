"""The gate that decides an open point may exist at all.

Rules, not a classifier, and the property that buys is asymmetric: a marker list
that MISSES yields nothing, where a classifier that misfires yields a confident
invention. Everything downstream of this gate may be model-produced; the gate
itself may not be.

An open point needs a marker AND an asserted fact. This module is only the first
half — hedging with no claim is filtered by admission, which is a separate test.

Not `pytest.importorskip`: this module is ours and pure. importorskip would turn
"does not exist yet" into a green skip, which is how a suite passes over a path
nothing runs.
"""
import pytest

import open_points as op


@pytest.mark.parametrize("text", [
    "I think this pile is 150 in 3604",
    "I believe it was the 2011 amendment",
    "I can't remember the exact size",
    "I cannot remember what they quoted",
    "I don't remember who signed it off",
    "I'll have to check that",
    "I'll need to confirm the lead time",
    "I will look it up when I'm back",
    "not sure whether they still have stock",
    "not quite sure about the span",
    "unsure if that's the current revision",
    "off the top of my head it was 40 grand",
    "from memory it was three weeks",
    "from recollection they were on site Tuesday",
    "我记得这个柱子是 150",
    "我觉得应该走第八章",
    "记不清了，回头查一下",
    "记不得是哪一版",
    "不确定还有没有库存",
    "不太确定是不是这个规格",
    "回去确认一下再说",
    "查一下再回复你",
    "大概是四十万吧",
    "应该是周二到货",
    "好像是上个月签的",
])
def test_markers_fire(text):
    assert op.has_uncertainty_marker(text) is True


@pytest.mark.parametrize("text", [
    "The pile is 150 in 3604.",
    "Two Specialists will replace the doors by Tuesday.",
    "Ben confirmed the re-inspection is booked.",
    "他们周二来换门。",
    "这批货已经到了。",
    "",
    "   ",
])
def test_a_plain_assertion_is_not_an_open_point(text):
    assert op.has_uncertainty_marker(text) is False


def test_a_question_to_someone_else_is_not_a_marker():
    """"Can you check the stock?" is a task, and the task extractor owns it.
    An open point is a speaker flagging their OWN recall."""
    for text in ("Can you check the stock?", "Could someone confirm the size?",
                 "你能查一下库存吗？"):
        assert op.has_uncertainty_marker(text) is False


def test_a_non_string_is_not_a_crash():
    """This reads a model-produced field. On any given run it may be null, a
    number, or a list."""
    for junk in (None, 12, [], {}, 3.5, True):
        assert op.has_uncertainty_marker(junk) is False


# ==========================================================================
# admission -- three mechanical checks, and every rejection is counted
# ==========================================================================

from datetime import datetime, timedelta   # noqa: E402

_T0 = datetime(2026, 8, 27, 14, 9, 0)


def _turn(offset_sec, text):
    start = _T0 + timedelta(seconds=offset_sec)
    return {"abs_start": start, "abs_end": start + timedelta(seconds=8),
            "abs_start_str": start.strftime("%H:%M:%S"),
            "speaker": "spk_0", "text": text}


TURNS = [
    _turn(0, "I think this pile is 150 in 3604 but I will have to check."),
    _turn(20, "The doors go in on Tuesday."),
]


def _candidate(**over):
    c = {"quote": "I think this pile is 150 in 3604 but I will have to check.",
         "at": "14:09:00",
         "claim": "The pile size in NZS 3604 is 150mm",
         "kind": "standard",
         "subject": "3604"}
    c.update(over)
    return c


def test_a_good_candidate_is_admitted():
    admitted, stats = op.admit([_candidate()], TURNS)
    assert len(admitted) == 1
    assert stats == {"admitted": 1, "rejected": {}}
    assert admitted[0]["subject"] == "3604"
    assert admitted[0]["kind"] == "standard"


def test_no_marker_in_the_quote_is_rejected():
    """The gate is on the QUOTE, not on the model's paraphrase. A model that
    decides a plain statement was uncertain does not get to say so."""
    admitted, stats = op.admit(
        [_candidate(quote="The doors go in on Tuesday.", at="14:09:20")], TURNS)
    assert admitted == []
    assert stats["rejected"] == {"no_marker": 1}


def test_a_subject_the_speaker_never_said_is_rejected():
    """`subject` is the only string permitted to leave the building later. If the
    model may compose it, a free composition is a free exfiltration -- so it has
    to appear verbatim inside the quote it came from."""
    admitted, stats = op.admit([_candidate(subject="Ellesmere College budget")], TURNS)
    assert admitted == []
    assert stats["rejected"] == {"subject_not_in_quote": 1}


def test_case_and_spacing_do_not_count_as_composition():
    """The model normalises capitalisation and whitespace. That is not the model
    inventing a term, and rejecting it would reject almost every real one."""
    admitted, _ = op.admit([_candidate(subject="NZS  3604")], [
        _turn(0, "I think this pile is 150 in nzs 3604 but I will have to check.")])
    assert admitted == []          # 'nzs 3604' is in the TURN, not in the QUOTE

    admitted, _ = op.admit(
        [_candidate(quote="I think this pile is 150 in NZS  3604 but I will have to check.",
                    subject="nzs 3604")],
        [_turn(0, "I think this pile is 150 in NZS 3604 but I will have to check.")])
    assert len(admitted) == 1


def test_a_quote_that_was_never_said_is_rejected():
    """Nothing on the brief path verifies a quote today -- `_snap_to_quote` only
    re-anchors a timestamp and leaves unmatched quotes in place. An open point
    built on an invented sentence would be indistinguishable from a real one."""
    admitted, stats = op.admit(
        [_candidate(quote="I think the roof is 200 but I will have to check.",
                    subject="roof")], TURNS)
    assert admitted == []
    assert stats["rejected"] == {"quote_unverified": 1}


def test_an_unparseable_timestamp_is_a_rejection_not_a_crash():
    admitted, stats = op.admit([_candidate(at="later on")], TURNS)
    assert admitted == []
    assert stats["rejected"] == {"bad_anchor": 1}


def test_junk_from_the_model_is_survivable():
    admitted, stats = op.admit([None, 7, {}, "text", _candidate()], TURNS)
    assert len(admitted) == 1
    assert stats["rejected"] == {"malformed": 4}


def test_a_verifier_that_raises_costs_the_point_not_the_brief():
    def boom(quote, at):
        raise RuntimeError("verifier exploded")

    admitted, stats = op.admit([_candidate()], TURNS, check=boom)
    assert admitted == []
    assert stats["rejected"] == {"verifier_error": 1}


def test_an_unknown_kind_becomes_the_one_that_promises_nothing():
    """A kind the model made up must not route to a resolver. `needs_a_person`
    is the state that claims nothing and is displayed honestly."""
    admitted, _ = op.admit([_candidate(kind="astrology")], TURNS)
    assert admitted[0]["kind"] == "needs_a_person"


def test_no_candidates_still_returns_countable_stats():
    """"It ran and admitted nothing" and "it never ran" are otherwise the same
    observation, and the caller logs this."""
    assert op.admit([], TURNS) == ([], {"admitted": 0, "rejected": {}})
    assert op.admit(None, TURNS) == ([], {"admitted": 0, "rejected": {}})


def test_the_anchor_crosses_midnight_to_the_nearest_occurrence():
    """A session running past midnight has turns on two dates, and the model
    returns a bare HH:MM:SS. Resolving it against the wrong day puts the quote
    outside the verification window and manufactures evidence of fabrication --
    BUG-37's family, inside the matcher."""
    late = datetime(2026, 8, 27, 23, 58, 0)
    turns = [{"abs_start": late, "abs_end": late + timedelta(seconds=10),
              "abs_start_str": "23:58:00", "speaker": "spk_0",
              "text": "I think this pile is 150 in 3604 but I will have to check."},
             {"abs_start": late + timedelta(minutes=5),
              "abs_end": late + timedelta(minutes=5, seconds=10),
              "abs_start_str": "00:03:00", "speaker": "spk_1",
              "text": "Right, we will pick it up tomorrow."}]

    admitted, stats = op.admit([_candidate(at="23:58:00")], turns)
    assert len(admitted) == 1, stats
