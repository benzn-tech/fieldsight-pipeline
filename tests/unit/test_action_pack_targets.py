"""Target phrase containment (spec 2026-11-05 §3.2.2; plan PR-3 Task 3.2): an item the
instruction names, by task number or a short phrase, resolves to one stable_id, to
`ambiguous`, or to `none` -- in code, not by the writer."""
import pytest

import action_pack_targets as tg

A = "44444444-5555-4666-8777-00000000000a"
B = "44444444-5555-4666-8777-00000000000b"
C = "44444444-5555-4666-8777-00000000000c"


def item(sid, text, task_no=None, status="open"):
    return {"stable_id": sid, "text": text, "task_no": task_no, "status": status}


def items(*rows):
    return {r["stable_id"]: r for r in rows}


def test_constants_are_the_specs():
    assert tg.TARGET_MATCH_MIN == 0.8 and tg.AMBIGUITY_MARGIN == 0.15


def test_tokens_latin_words_cjk_chars_and_stop_words():
    assert tg.tokens("Fix the  Door-Hardware on Level 2.") == ["fix", "door", "hardware",
                                                               "level", "2"]
    assert tg.tokens("检查 三楼的窗户") == ["检", "查", "三", "楼", "的", "窗", "户"]
    assert tg.tokens("Check 窗户, item C4") == ["check", "窗", "户", "c4"]
    assert tg.tokens("") == [] and tg.tokens(None) == []
    assert tg.tokens("the task of an item") == []


def test_containment_is_the_share_of_phrase_tokens():
    assert tg.containment("door hardware", "Install the door hardware on level 2") == 1.0
    assert tg.containment("door hardware level 9", "Install door hardware level 2") == 0.75
    assert tg.containment("", "anything") == 0.0
    assert tg.containment("the item", "anything") == 0.0       # only stop words
    assert tg.containment("door door", "door") == 1.0          # distinct tokens


def test_short_phrase_matches_long_item():
    pool = items(item(A, "Supplier to confirm delivery date for the level 3 door hardware "
                         "before the end of next week, Ann to chase"),
                 item(B, "Pour the level 4 slab"))
    r = tg.resolve({"id": "t1", "task_no": None, "phrase": "door hardware"}, pool)
    assert r == tg.Resolution("resolved", A, [A])


def test_two_close_items_ambiguous():
    pool = items(item(A, "Check the scaffold on the north side"),
                 item(B, "Fix the scaffold tie on the north elevation"),
                 item(C, "Order more concrete"))
    r = tg.resolve({"id": "t1", "task_no": None, "phrase": "north scaffold"}, pool)
    assert r.kind == "ambiguous" and r.stable_id is None
    assert sorted(r.candidates) == sorted([A, B])


def test_a_clear_winner_beats_a_weaker_candidate_outside_the_margin():
    # best 1.0, the other 0.8: both pass the threshold, but 0.2 apart -> resolved.
    pool = items(item(A, "north scaffold tie east bay level"),
                 item(B, "north scaffold tie east level"))
    r = tg.resolve({"id": "t1", "task_no": None,
                    "phrase": "north scaffold tie east bay"}, pool)
    assert r == tg.Resolution("resolved", A, [A])


def test_cjk_phrase_resolves():
    """Review Focus 1: a Chinese instruction naming an item by a CJK phrase."""
    pool = items(item(A, "三楼东侧窗户的密封胶需要在下周五之前重新打一遍"),
                 item(B, "二楼卫生间防水层检查"))
    r = tg.resolve({"id": "t1", "task_no": None, "phrase": "窗户密封胶"}, pool)
    assert r == tg.Resolution("resolved", A, [A])


def test_task_no_wins_and_never_falls_back():
    pool = items(item(A, "door hardware", task_no=7), item(B, "something else", task_no=8))
    r = tg.resolve({"id": "t1", "task_no": 8, "phrase": "door hardware"}, pool)
    assert r == tg.Resolution("resolved", B, [B])
    # A number nothing carries is `none`, even though the phrase would match A.
    r = tg.resolve({"id": "t1", "task_no": 99, "phrase": "door hardware"}, pool)
    assert r == tg.Resolution("none", None, [])
    # A string task_no on the row (as rows sometimes carry it) still matches.
    pool = items(item(C, "x", task_no="12"))
    assert tg.resolve({"id": "t1", "task_no": 12, "phrase": None}, pool).stable_id == C


def test_below_threshold_none():
    pool = items(item(A, "Install door hardware on level 3"))
    r = tg.resolve({"id": "t1", "task_no": None,
                    "phrase": "window hardware level 3 east"}, pool)
    assert r == tg.Resolution("none", None, [])
    assert tg.resolve({"id": "t1", "task_no": None, "phrase": "x"}, {}).kind == "none"


def test_done_item_still_resolves():
    """The status is not the matcher's business: a named done item resolves to itself,
    so the write render can say "already done" instead of finding an open look-alike."""
    pool = items(item(A, "Seal the level 3 windows", status="done"))
    r = tg.resolve({"id": "t1", "task_no": None, "phrase": "seal level 3 windows"}, pool)
    assert r == tg.Resolution("resolved", A, [A])


@pytest.mark.parametrize("target", [{}, {"id": "t1"}, {"id": "t1", "task_no": None,
                                                         "phrase": "   "}])
def test_a_target_naming_nothing_is_none(target):
    assert tg.resolve(target, items(item(A, "anything"))).kind == "none"
