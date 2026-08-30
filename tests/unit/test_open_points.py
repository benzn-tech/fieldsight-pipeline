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
