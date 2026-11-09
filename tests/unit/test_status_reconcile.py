"""Cross-session status reconcile, pure core (spec 2026-11-09 §5, §6, §12.2).

Open items recorded earlier are offered to the model as C1..Cn; its hits on THIS session's
transcript go through code guards. A hit survives as a suggestion only when the quote is
verified and names exactly one candidate; a verified quote that could mean 2-4 candidates
becomes a pick card; everything else is dropped by name. The system never chooses.
"""
import json

import status_reconcile as sr

ANCHOR_KEY = "audio_segments/u/d/b.wav"


def cand(sid, text, status="open"):
    return {"stable_id": sid, "text": text, "status": status}


def hit(alias, quote, status="done", at="10:00:00"):
    return {"alias": alias, "status": status, "at": at, "quote": quote}


def check(quote, at):
    """Verified, with the anchor keyed off the quote: different quotes, different anchors."""
    return {"status": "verified", "segment_key": ANCHOR_KEY,
            "offset_sec": float(len(quote)), "quote": quote}


def check_by_at(quote, at):
    """Verified, with the anchor keyed off `at`, so different quotes can share an anchor."""
    h, m, s = (int(x) for x in at.split(":"))
    return {"status": "verified", "segment_key": ANCHOR_KEY,
            "offset_sec": float(h * 3600 + m * 60 + s), "quote": quote}


WEST = cand("sid-west", "Order copper pipe for the west block")
EAST = cand("sid-east", "Order PVC pipe for the east block")


def test_numeral_normalisation():
    assert sr.normalise_numerals("unit four, level one, 三楼") == "unit 4, level 1, 3楼"
    assert sr.normalise_numerals("一起去统一") == "一起去统一"


def test_pronoun_one_is_not_a_number():
    cands = [cand("a", "Pour level 1 slab"), cand("b", "Order skip bin")]
    out = sr.judge([hit("C1", "Yeah that one's done now mate, all good")], cands, check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["no_anchor"] == 1


def test_quote_naming_both_twins_goes_to_pick_card():
    q = "The west block copper pipe, no wait, the east one, that's done"
    out = sr.judge([hit("C1", q)], [WEST, EAST], check)
    assert out.suggestions == []
    assert len(out.picks) == 1
    assert sorted(out.picks[0]["candidates"]) == ["sid-east", "sid-west"]
    assert out.counts["picks"] == 1


def test_one_candidate_card_is_dropped():
    cands = [WEST, cand("sid-skip", "Order skip bin for site")]
    q = "The west block copper pipe is ordered, that's done"
    out = sr.judge([hit("C2", q)], cands, check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["anchor"] == 1


def test_conflicting_statuses_same_anchor_dropped():
    q = "The copper pipe for the west block is ordered, that's done"
    hits = [hit("C1", q, status="done"), hit("C1", q, status="blocked")]
    out = sr.judge(hits, [WEST, EAST], check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["conflict"] == 2


def test_prompt_aliases_in_order():
    prompt = sr.build_prompt([WEST, EAST], "[10:00:00] A: hello there")
    assert "C1" in prompt and "C2" in prompt
    assert prompt.index("C1") < prompt.index("C2")
    line1 = next(line for line in prompt.splitlines() if "C1" in line and WEST["text"] in line)
    line2 = next(line for line in prompt.splitlines() if "C2" in line and EAST["text"] in line)
    assert line1 and line2
    assert "[10:00:00] A: hello there" in prompt
    assert "sid-west" not in prompt and "sid-east" not in prompt


def test_parse_rejects_unknown_alias():
    raw = json.dumps({"hits": [{"candidate": "C9", "status": "done", "at": "10:00:00",
                                "quote": "The copper pipe for the west block is done"}]})
    hits = sr.parse_hits(raw, 2)
    out = sr.judge(hits, [WEST, EAST], check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["shape"] == 1
    assert sr.parse_hits("not json {", 2) is None


def test_parse_maps_candidate_to_alias():
    raw = json.dumps([{"candidate": "C2", "status": "closed", "at": "10:01:02", "quote": "x y"}])
    assert sr.parse_hits(raw, 2) == [
        {"alias": "C2", "status": "closed", "at": "10:01:02", "quote": "x y"}]


def test_clear_match_becomes_suggestion():
    q = "The copper pipe for the west block is ordered, that's done"
    out = sr.judge([hit("C1", q)], [WEST, EAST], check)
    assert out.picks == []
    assert out.suggestions == [{"stable_id": "sid-west", "suggested_status": "done",
                                "quote": q, "segment_key": ANCHOR_KEY,
                                "offset_sec": float(len(q))}]
    assert out.counts["kept"] == 1 and out.counts["offered"] == 1


def test_twin_wrong_side_is_dropped():
    q = "The copper pipe for the west block is ordered, that's done"
    out = sr.judge([hit("C2", q)], [WEST, EAST], check)
    assert all(s["stable_id"] != "sid-east" for s in out.suggestions)
    assert out.suggestions == []
    assert len(out.picks) == 1 or out.counts["dropped_by"].get("twin") == 1


def test_generic_token_goes_to_pick_card():
    cands = [cand("leak", "Fix west pipe leak under slab"),
             cand("fit", "Order pipe fittings for level 2")]
    out = sr.judge([hit("C1", "The pipe's all done mate, finished it")], cands, check)
    assert out.suggestions == []
    assert len(out.picks) == 1
    assert sorted(out.picks[0]["candidates"]) == ["fit", "leak"]
    assert out.picks[0]["suggested_status"] == "done"


def test_argmax_without_a_word_of_its_own_goes_to_pick_card():
    """Anchor (b): the pick leads on overlap, but every word it shares with the quote is also
    in some other candidate -- so the quote does not name it alone."""
    cands = [cand("l1slab", "Level 1 slab"), cand("l1rail", "Level 1 handrail"),
             cand("pour", "Slab pour")]
    out = sr.judge([hit("C1", "Level 1 slab is all done")], cands, check)
    assert out.suggestions == []
    assert len(out.picks) == 1
    assert out.picks[0]["candidates"][0] == "l1slab"
    assert sorted(out.picks[0]["candidates"]) == ["l1rail", "l1slab", "pour"]


def test_no_item_word_is_silent_drop():
    out = sr.judge([hit("C1", "Yeah that's all done now, good job")], [WEST, EAST], check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["no_anchor"] == 1


def test_unverified_quote_dropped():
    q = "The copper pipe for the west block is ordered, that's done"
    out = sr.judge([hit("C1", q)], [WEST, EAST], lambda quote, at: {"status": "weak"})
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"] == {"weak": 1}


def test_one_anchor_two_candidates_becomes_pick():
    cands = [cand("skip", "Order skip bin"), cand("slab", "Pour level 1 slab")]
    hits = [hit("C1", "The skip bin order is done", at="10:00:00"),
            hit("C2", "The level 1 slab pour is done", at="10:00:00")]
    out = sr.judge(hits, cands, check_by_at)
    assert out.suggestions == []
    assert len(out.picks) == 1
    assert sorted(out.picks[0]["candidates"]) == ["skip", "slab"]
    assert out.picks[0]["offset_sec"] == 36000.0


def test_latest_status_wins_per_candidate():
    hits = [hit("C1", "Started on the copper pipe order for the west block",
                status="in_progress", at="10:00:00"),
            hit("C1", "The copper pipe for the west block is ordered, that's done",
                status="done", at="10:05:00")]
    out = sr.judge(hits, [WEST, EAST], check)
    assert out.picks == []
    assert [(s["stable_id"], s["suggested_status"]) for s in out.suggestions] == [
        ("sid-west", "done")]
    assert out.counts["dropped_by"] == {"superseded": 1}


def test_same_status_dropped():
    west = cand("sid-west", "Order copper pipe for the west block", status="blocked")
    q = "The copper pipe for the west block is blocked, supplier has none"
    out = sr.judge([hit("C1", q, status="blocked")], [west, EAST], check)
    assert out.suggestions == [] and out.picks == []
    assert out.counts["dropped_by"]["same_status"] == 1


def test_cjk_twins():
    cands = [cand("f2", "二楼防水膜订购"), cand("f3", "三楼防水膜订购")]
    out = sr.judge([hit("C1", "二楼的防水膜已经订好了")], cands, check)
    assert out.picks == []
    assert [s["stable_id"] for s in out.suggestions] == ["f2"]


def test_pick_caps_at_four():
    cands = [cand(f"s{i}", f"Pipe job {name}") for i, name in
             enumerate(["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"])]
    out = sr.judge([hit("C1", "The pipe job is all done now")], cands, check)
    assert out.suggestions == []
    assert len(out.picks) == 1
    assert len(out.picks[0]["candidates"]) == 4
