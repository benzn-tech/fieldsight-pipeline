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


# ==========================================================================
# resolution -- only the standard ones, and never a value stated as fact
# ==========================================================================

_PTS = [
    {"kind": "standard", "claim": "Pile size in 3604", "subject": "3604"},
    {"kind": "supply", "claim": "CZ LiDAR stock", "subject": "CZ LiDAR"},
    {"kind": "needs_a_person", "claim": "Engineer sign-off", "subject": "engineer"},
    {"kind": "in_corpus", "claim": "The date agreed last time", "subject": "date"},
]


def test_only_standard_points_are_sent_to_the_model():
    """A `supply` point is about stock right now, `needs_a_person` about
    somebody's judgement, `in_corpus` about our own records. None of those facts
    is in a model's weights, so asking produces a fabrication carrying exactly
    the confidence of a real answer."""
    prompt = op.build_resolution_prompt(_PTS)
    assert "3604" in prompt
    assert "CZ LiDAR" not in prompt
    assert "Engineer sign-off" not in prompt
    assert "The date agreed last time" not in prompt


def test_no_standard_points_means_no_model_call_at_all():
    assert op.build_resolution_prompt(_PTS[1:]) is None
    assert op.build_resolution_prompt([]) is None
    assert op.build_resolution_prompt(None) is None


def test_the_prompt_forbids_stating_a_value_as_fact():
    """The product rule: give the cases and the location, never the number. A
    reader who knows which cases exist can spot a wrong cell; a reader given a
    wrong number puts it in a variation."""
    low = op.build_resolution_prompt(_PTS).lower()
    assert "never state a specific value" in low
    assert "cases" in low
    assert "section" in low or "chapter" in low
    assert "do not quote" in low


def test_a_resolution_lands_on_the_point_it_was_asked_about():
    pts = [dict(p) for p in _PTS]
    reply = ('{"resolutions": [{"index": 0, "cases": ["by load", "by ground condition"], '
             '"where": "NZS 3604, the foundations part"}]}')
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))

    assert pts[0]["resolution"]["cases"] == ["by load", "by ground condition"]
    assert pts[0]["resolution"]["where"].startswith("NZS 3604")
    assert stats["resolved"] == 1


def test_every_point_carries_the_key_resolved_or_not():
    """Absent-vs-null is how a reader would learn "this build has no
    resolutions" instead of "we could not answer this one"."""
    pts = [dict(p) for p in _PTS]
    op.attach_resolutions(pts, lambda *a, **k: ('{"resolutions": []}', None))
    for p in pts:
        assert "resolution" in p
        assert p["resolution"] is None


def test_an_index_the_model_invented_is_dropped():
    """A wrong index attaches a pile answer to a door question, which is worse
    than no answer and looks exactly like one."""
    pts = [dict(_PTS[0])]
    reply = '{"resolutions": [{"index": 7, "cases": ["x"], "where": "y"}]}'
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))
    assert pts[0]["resolution"] is None
    assert stats["dropped"] == 1


def test_a_resolution_aimed_at_a_non_standard_point_is_dropped():
    pts = [dict(p) for p in _PTS]
    reply = '{"resolutions": [{"index": 1, "cases": ["x"], "where": "y"}]}'
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))
    assert pts[1]["resolution"] is None
    assert stats["dropped"] == 1


def test_an_empty_answer_is_not_a_resolution():
    """"I have no idea" arriving as {cases: [], where: ""} must not render as a
    resolved point with nothing in it."""
    pts = [dict(_PTS[0])]
    reply = '{"resolutions": [{"index": 0, "cases": [], "where": ""}]}'
    stats = op.attach_resolutions(pts, lambda *a, **k: (reply, None))
    assert pts[0]["resolution"] is None
    assert stats["dropped"] == 1


def test_an_llm_failure_leaves_everything_unresolved_and_says_so():
    pts = [dict(_PTS[0])]
    stats = op.attach_resolutions(pts, lambda *a, **k: (None, "timeout"))
    assert pts[0]["resolution"] is None
    assert stats["error"] == "timeout"
    assert stats["resolved"] == 0


def test_an_unparseable_reply_is_not_a_crash():
    pts = [dict(_PTS[0])]
    stats = op.attach_resolutions(pts, lambda *a, **k: ("not json at all", None))
    assert pts[0]["resolution"] is None
    assert stats["error"]


def test_thinking_is_sent_explicitly():
    """QWEN_ENABLE_THINKING is a per-function env and the brief's own call
    overrides it to True. Inheriting here would make the resolution's latency a
    property of whichever call ran last."""
    seen = {}

    def call_llm(prompt, **kw):
        seen.update(kw)
        return ('{"resolutions": []}', None)

    op.attach_resolutions([dict(_PTS[0])], call_llm)
    assert seen["enable_thinking"] is False
