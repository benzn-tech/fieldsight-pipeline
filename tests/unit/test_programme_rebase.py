"""
Tests for src/programme_rebase.py — Task 2 of the programme import
reconciliation plan. Spec §6.4.

The rule: shift what has not started, never rewrite the dates of work that
has. That can leave a visible gap between a finished child and the shifted
remainder. The gap is real and is not smoothed over — closing it would mean
asserting that work happened on days it did not.
"""
from programme_rebase import rebase_children


def parent(start, end):
    return {"start_date": start, "end_date": end}


def child(cid, start, end, progress=0):
    return {"id": cid, "start_date": start, "end_date": end,
            "progress_pct": progress}


def test_a_pure_shift_moves_every_not_started_child_by_the_same_delta():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-10")])
    assert out["invalidated"] is False
    assert out["shift"][0]["start_date"] == "2026-03-15"
    assert out["shift"][0]["end_date"] == "2026-03-24"


def test_a_shift_preserves_each_childs_offset_within_the_parent():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [
        child("first", "2026-03-01", "2026-03-05"),
        child("second", "2026-03-10", "2026-03-14"),
    ])
    by_id = {s["id"]: s for s in out["shift"]}
    assert by_id["first"]["start_date"] == "2026-03-15"
    assert by_id["second"]["start_date"] == "2026-03-24"   # 9 days in, still 9


def test_a_completed_child_keeps_its_real_dates():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [child("done", "2026-03-01", "2026-03-10", 100)])
    assert out["shift"] == [], "finished work is a record of what happened"


def test_an_in_progress_child_keeps_its_real_dates():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [child("wip", "2026-03-01", "2026-03-10", 40)])
    assert out["shift"] == []


def test_a_mixed_subtree_shifts_only_the_untouched_part():
    """The result leaves a gap between the finished child and the shifted
    remainder. That gap is real: the work genuinely is not continuous."""
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [
        child("done", "2026-03-01", "2026-03-10", 100),
        child("next", "2026-03-11", "2026-03-20", 0),
    ])
    assert [s["id"] for s in out["shift"]] == ["next"]


def test_a_small_duration_change_scales_as_well_as_shifts():
    before = parent("2026-03-01", "2026-03-20")   # 20 days
    after = parent("2026-03-01", "2026-03-22")    # 22 days, +10%
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-10")])
    assert out["invalidated"] is False
    assert out["shift"][0]["end_date"] > "2026-03-10"


def test_a_large_duration_change_invalidates_instead_of_reshaping():
    """The subtasks are allocated to named people. Silently re-planning them
    would change someone's week without telling them."""
    before = parent("2026-03-01", "2026-03-28")   # 28 days
    after = parent("2026-03-01", "2026-03-14")    # 14 days, -50%
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-10")])
    assert out["invalidated"] is True
    assert out["shift"] == [], "an invalidated breakdown must not be silently rewritten"
    assert "duration" in out["reason"].lower()


def test_a_large_expansion_invalidates_too():
    """Stretching a breakdown to fill twice the time is as much of an
    invention as compressing it."""
    before = parent("2026-03-01", "2026-03-14")   # 14 days
    after = parent("2026-03-01", "2026-04-11")    # 42 days, +200%
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-05")])
    assert out["invalidated"] is True


def test_the_invalidation_threshold_is_inclusive_of_small_changes():
    """20% exactly still rebases; only more than that invalidates."""
    before = parent("2026-03-01", "2026-03-10")   # 10 days
    after = parent("2026-03-01", "2026-03-12")    # 12 days, +20%
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-05")])
    assert out["invalidated"] is False


def test_an_unchanged_parent_produces_no_work():
    p = parent("2026-03-01", "2026-03-28")
    out = rebase_children(p, p, [child("c1", "2026-03-01", "2026-03-10")])
    assert out["shift"] == [] and out["invalidated"] is False


def test_a_parent_with_no_dates_cannot_rebase_anything():
    out = rebase_children(parent(None, None), parent("2026-03-01", "2026-03-28"),
                          [child("c1", "2026-03-01", "2026-03-10")])
    assert out["shift"] == [] and out["invalidated"] is False


def test_a_child_with_no_dates_is_skipped_not_crashed_on():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [child("undated", None, None)])
    assert out["shift"] == []


def test_no_children_is_not_an_invalidation():
    """Nothing to invalidate. Flagging here would put a warning on every
    imported task that has no breakdown yet — which is most of them."""
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-01", "2026-03-05")
    out = rebase_children(before, after, [])
    assert out["invalidated"] is False


def test_a_shifted_child_keeps_its_own_length_when_the_duration_is_unchanged():
    before, after = parent("2026-03-01", "2026-03-28"), parent("2026-03-15", "2026-04-11")
    out = rebase_children(before, after, [child("c1", "2026-03-01", "2026-03-10")])
    s = out["shift"][0]
    assert s["start_date"] == "2026-03-15" and s["end_date"] == "2026-03-24"


def test_date_objects_are_accepted_as_well_as_iso_strings():
    """psycopg hands back datetime.date; the caller should not have to
    stringify before asking."""
    import datetime
    before = {"start_date": datetime.date(2026, 3, 1),
              "end_date": datetime.date(2026, 3, 28)}
    after = {"start_date": datetime.date(2026, 3, 15),
             "end_date": datetime.date(2026, 4, 11)}
    kid = {"id": "c1", "start_date": datetime.date(2026, 3, 1),
           "end_date": datetime.date(2026, 3, 10), "progress_pct": 0}
    out = rebase_children(before, after, [kid])
    assert out["shift"][0]["start_date"] == "2026-03-15"
    assert isinstance(out["shift"][0]["end_date"], str)
