"""Unit: carry_forward.match, replayed against real shapes (Track B Task 4 Step 4).

PURE matcher -- no database, no S3. Text is either copied verbatim from a real TEST
extraction (`extractions/Ben_UCPK2/2026-08-13/sid9db9293e82b94a4d9611572b1233f82d.json`,
`aws s3 cp ... --profile fieldsight-deployer`, 2026-09-30) or, where that would need a real
live/final PAIR of the same session, realistic synthetic construction-site strings --
`aws s3api get-bucket-versioning --bucket fieldsight-data-test-509194952652` came back with
no `Status` field, i.e. versioning is OFF, and `extract_session.out_key` is computed once so
a session's live and final passes overwrite the SAME S3 key: there is no way to retrieve both
sides of one session's re-extraction from TEST as it stands today. Every ratio below is the
value `difflib.SequenceMatcher` actually returned for that pair, not a value chosen to make
the test pass -- the floor is a plan constant (0.90) and is never tuned to fit a case.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import carry_forward as cf


def _item(id_, text, human_touched=False, stable_id=None):
    return {"id": id_, "stable_id": stable_id, "text": text, "human_touched": human_touched}


def test_identical_text_is_an_exact_pair():
    # Real TEST action item text (no person/company/site name in it, so nothing to mask):
    # extractions/Ben_UCPK2/2026-08-13/sid9db9293e82b94a4d9611572b1233f82d.json.
    text = "Frontend timestamps -- fix recording time mixture"
    old = [_item("o1", text, human_touched=True, stable_id="s1")]
    new = [_item("n1", text)]
    pairs, orphans = cf.match(old, new)
    assert pairs == [("o1", "n1", "exact")]
    assert orphans == []


def test_the_briefs_named_reword_falls_below_the_floor_and_is_an_orphan():
    """"Check the scaffolding before Monday" -> "Scaffolding to be checked before Monday":
    the passive-voice reword the plan names as the worked example. MEASURED ratio (over the
    same normalised keys `match` itself uses) is 0.6757 -- well under the 0.90 floor. This is
    not a defect in the matcher: a subject/verb reorder is genuinely a large edit distance
    under SequenceMatcher, and the floor exists precisely so a rewording this large is
    refused as a guess rather than silently carried onto a row that might mean something
    else. It becomes an orphan (a touched one is counted and logged; see
    tests/unit/test_orphaned_human_edits_reported.py for that half)."""
    old = [_item("o1", "Check the scaffolding before Monday", human_touched=True)]
    new = [_item("n1", "Scaffolding to be checked before Monday")]
    pairs, orphans = cf.match(old, new)
    assert pairs == [], "0.6757 is below the 0.90 floor -- must not be guessed at"
    assert orphans == ["o1"]


def test_a_reword_within_the_floor_is_a_fuzzy_pair():
    """A genuine, in-floor reword (an inserted "the"). MEASURED ratio 0.9394."""
    old = [_item("o1", "Order rebar delivery for Monday", human_touched=True, stable_id="s1")]
    new = [_item("n1", "Order the rebar delivery for Monday")]
    pairs, orphans = cf.match(old, new)
    assert pairs == [("o1", "n1", "fuzzy")]
    assert orphans == []


def test_two_new_items_both_clearing_the_floor_against_one_old_is_an_orphan_not_a_guess():
    """A split: the old commitment reads like both new ones. Picking the higher-scoring one
    would be a guess dressed up as a decision, so BOTH remain unmatched and the old item is
    an orphan -- exactly the tie/near-tie rule (tie_margin), not a coincidence of these
    particular strings."""
    old = [_item("o1", "Pour concrete slab B2 tomorrow", human_touched=True)]
    new = [
        _item("n1", "Pour concrete slab B2 tomorrow morning"),
        _item("n2", "Pour concrete slab B2 tomorrow afternoon"),
    ]
    pairs, orphans = cf.match(old, new)
    assert pairs == []
    assert orphans == ["o1"]


def test_an_untouched_orphan_and_a_touched_orphan_are_both_just_orphans_here():
    """`match` itself does not look at `human_touched` at all -- it has no successor to give
    either row, full stop. The silent/counted split (Step 3) happens one layer up, in the
    caller that sums only the touched ids among the orphans it gets back (see
    lambda_item_writer._carry_forward_one_table and
    tests/unit/test_orphaned_human_edits_reported.py) -- this test pins that `match` does not
    pre-filter that decision away."""
    old = [
        _item("o_touched", "Confirm crane booking for Thursday", human_touched=True),
        _item("o_untouched", "Chase the concrete supplier", human_touched=False),
    ]
    new = []  # nothing to match against -- both are orphans regardless of who touched them
    pairs, orphans = cf.match(old, new)
    assert pairs == []
    assert set(orphans) == {"o_touched", "o_untouched"}


def test_cjk_pair_differing_only_in_inter_character_spacing_is_exact():
    """A model writing Chinese writes it unspaced; turn/topic text is often space-joined
    (Ruling R4). "我 现在 在 现场" and "我现在在现场" are the same sentence and must match on
    normalised-key equality alone, in pass 1 -- not fall through to the fuzzy pass at all."""
    old = [_item("o1", "我 现在 在 现场", human_touched=True, stable_id="s1")]
    new = [_item("n1", "我现在在现场")]
    pairs, orphans = cf.match(old, new)
    assert pairs == [("o1", "n1", "exact")]
    assert orphans == []


def test_matching_is_one_to_one_a_duplicate_old_text_does_not_double_book_one_new_id():
    old = [_item("o1", "Order rebar", human_touched=True),
           _item("o2", "Order rebar", human_touched=False)]
    new = [_item("n1", "Order rebar")]
    pairs, orphans = cf.match(old, new)
    assert len(pairs) == 1
    matched_old = {p[0] for p in pairs}
    assert len(matched_old) == 1
    assert set(orphans) == ({"o1", "o2"} - matched_old)
