"""The pairing behind the extraction-vs-brief diff.

Stage 2 of the briefing plan turns on whether the two paths agree, and this is
what will answer that. If it pairs badly, the answer is wrong in a way nobody
would notice: a mis-pair reads as "both found it" and a missed pair reads as a
disagreement that is not real.

The strings below are taken from the two paths on the same real session
(sid93396a..., 2026-08-27), which is where the phrasing gap comes from -- one
side writes for a UI column, the other writes for a person.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
cmp_mod = pytest.importorskip("compare_extraction_vs_brief")


def texts(items):
    return [i["text"] for i in items]


# --- similarity -------------------------------------------------------------

def test_unrelated_items_score_exactly_zero_not_merely_low():
    """Why the character ratio was removed. It scored ANY two English sentences
    of similar length at 0.26-0.41 -- "Outlook calendar integration" against
    "Pour the slab on level two" came out at 0.41, higher than a pair that IS
    the same item. Shared content words score those pairs at exactly 0, which is
    the separation the whole report depends on."""
    for a, b in (("Outlook calendar integration -- develop for app",
                  "Pour the slab on level two"),
                 ("AI knowledge base -- build for construction terms",
                  "AWS cloud credits -- setup independent front-end"),
                 ("Pipe capping -- maintain 2m clearance",
                  "Cables -- pull back above ceiling")):
        assert cmp_mod.similarity(a, b) == 0.0, (a, b)


def test_the_same_item_worded_by_both_paths_scores_above_the_threshold():
    for a, b in (("Downtown proposal review -- attend Friday 2 PM",
                  "Join the downtown proposal review on Friday at 2:00 PM"),
                 ("Recording devices -- deploy to James (South Island)",
                  "Distribute devices to James for South Island customers")):
        assert cmp_mod.similarity(a, b) >= cmp_mod.MATCH_RATIO, (a, b)


def test_a_pair_that_shares_only_one_word_is_shown_but_not_joined():
    # The photo bug: the same item in both paths, but the only word they share
    # is "photos". Joining on that would join anything; hiding it entirely would
    # lose the one pair a reader most wants to see. So it is surfaced as a near
    # miss with its score, and a person decides.
    left = [{"text": "Photo visibility bug -- investigate upload display issue"}]
    right = [{"text": "Fix the bug where photos taken during a recording do not appear"}]
    pairs, only_l, _r = cmp_mod.pair_up(left, right, key=lambda x: x["text"])
    assert pairs == []
    assert only_l[0]["_near"].startswith("Fix the bug")
    assert cmp_mod.NEAR_MISS <= only_l[0]["_near_score"] < cmp_mod.MATCH_RATIO


def test_an_unrelated_leftover_carries_no_candidate():
    left = [{"text": "Outlook calendar integration -- develop for app"}]
    right = [{"text": "Pour the slab on level two"}]
    _p, only_l, _r = cmp_mod.pair_up(left, right, key=lambda x: x["text"])
    assert "_near" not in only_l[0]


def test_two_site_instructions_sharing_boilerplate_do_not_match():
    # Both are real work; pairing them would hide one of them.
    a = "Pipe capping -- maintain 2m clearance"
    b = "Cables -- pull back above ceiling"
    assert cmp_mod.similarity(a, b) < cmp_mod.MATCH_RATIO


def test_the_same_item_in_two_languages_is_not_expected_to_match():
    # Recorded rather than fixed: until both paths write one language, a
    # cross-language pair shows up as one-only-each, and the reader has to see
    # that rather than have it quietly joined.
    a = "Recording devices -- deploy to James (South Island)"
    b = "把 device 分配给 James，由其负责南岛客户的设备分发"
    assert cmp_mod.similarity(a, b) < cmp_mod.MATCH_RATIO


def test_similarity_is_symmetric_and_bounded():
    a, b = "Fix the photo linking bug", "Photo linking -- fix it"
    assert cmp_mod.similarity(a, b) == cmp_mod.similarity(b, a)
    assert 0.0 <= cmp_mod.similarity(a, b) <= 1.0
    assert cmp_mod.similarity("", "") == 0.0


# --- pairing ----------------------------------------------------------------

def test_each_item_is_paired_at_most_once():
    left = [{"text": "Fix the photo linking bug"}, {"text": "Fix the photo display bug"}]
    right = [{"text": "Fix the photo linking bug in the app right now"}]
    pairs, only_l, only_r = cmp_mod.pair_up(left, right, key=lambda x: x["text"])
    assert len(pairs) == 1 and len(only_l) == 1 and only_r == []


def test_the_best_pair_wins_not_the_first_one_seen():
    # The weaker candidate is listed first; the closer one must still take it.
    left = [{"text": "Attend the downtown proposal review"}]
    right = [{"text": "Attend a review"},
             {"text": "Attend the downtown proposal review on Friday at 2pm"}]
    pairs, _l, only_r = cmp_mod.pair_up(left, right, key=lambda x: x["text"])
    assert pairs[0][1]["text"].startswith("Attend the downtown proposal review on Friday")
    assert [i["text"] for i in only_r] == ["Attend a review"]


def test_an_item_only_one_side_found_is_reported_not_forced_into_a_pair():
    left = [{"text": "Outlook calendar integration -- develop for app"}]
    right = [{"text": "Take two devices upstairs for Clement to test"}]
    pairs, only_l, only_r = cmp_mod.pair_up(left, right, key=lambda x: x["text"])
    assert pairs == [] and len(only_l) == 1 and len(only_r) == 1


def test_empty_sides_are_handled():
    assert cmp_mod.pair_up([], [], key=lambda x: x["text"]) == ([], [], [])
    pairs, only_l, only_r = cmp_mod.pair_up([{"text": "a task"}], [], key=lambda x: x["text"])
    assert pairs == [] and len(only_l) == 1 and only_r == []


# --- the shape the report is read from --------------------------------------

def test_a_session_with_no_brief_is_flagged_rather_than_counted_as_agreement():
    # The whole point of stage 2's evidence: a session the brief never produced
    # is the gap being looked for, so zero-versus-zero must not read as a match.
    r = {"session": "sid1", "has_extraction": True, "has_brief": False,
         "extraction_items": 3, "brief_items": 0, "extraction_assigned": 1,
         "brief_assigned": 0, "matched": [], "only_extraction": [], "only_brief": [],
         "brief_stats": None}
    out = cmp_mod.render(r)
    assert "NO BRIEF" in out


def test_render_marks_which_side_each_unmatched_item_came_from():
    r = {"session": "sid1", "has_extraction": True, "has_brief": True,
         "extraction_items": 1, "brief_items": 1, "extraction_assigned": 0,
         "brief_assigned": 1, "matched": [],
         "only_extraction": [{"text": "Restrict Josh's access", "who": None}],
         "only_brief": [{"text": "Call the south team", "who": "spk_0"}],
         "brief_stats": {"reanchored": 2, "unmatched": 1}}
    out = cmp_mod.render(r)
    assert "only extraction: Restrict Josh" in out
    assert "only brief:      Call the south team" in out
    assert "spk_0" in out
