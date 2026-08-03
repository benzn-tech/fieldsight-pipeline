"""
Tests for src/programme_reconcile.py — Task 1 of the programme import
reconciliation plan. Spec §6.3.

The property this module exists to protect: a client's monthly revision
updates the rows the file owns and leaves everything we built underneath
them alone. The failure mode is silent — a reconciliation bug does not
raise, it quietly drops a site manager's allocations and a month of recorded
progress — so these tests assert survival explicitly, not just counts.
"""
from programme_reconcile import reconcile, suggest_mode


def existing_task(uid, source, *, origin="imported", parent=None, name="T",
                  start=None, end=None, progress=0, removed=None,
                  locally_modified=False):
    return {
        "id": uid, "source_task_id": source, "parent_id": parent,
        "origin": origin, "name": name, "wbs_code": None,
        "start_date": start, "end_date": end, "duration_days": None,
        "progress_pct": progress, "status": "not_started",
        "removed_in_version": removed, "locally_modified": locally_modified,
    }


def incoming_leaf(source, *, parent="G1", name="T", start="2026-04-01",
                  end="2026-04-10"):
    return {"task_id": source, "parent_id": parent, "name": name,
            "start": start, "end": end, "duration_days": 10,
            "progress_pct": 0, "status": "not_started"}


GROUP = [{"task_id": "G1", "name": "Foundations", "wbs": "1"}]


def _updates(plan):
    return {u["id"]: u for u in plan["update"]}


def test_a_task_present_in_both_is_updated_not_reinserted():
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g")]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1")], version_no=2)
    assert plan["insert"] == []
    assert plan["remove"] == []
    assert "u-a" in _updates(plan)


def test_a_rename_updates_in_place_because_the_id_is_the_join_key():
    """Source ids are stable between revisions; names are not."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", name="Pour slab")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("A1", name="Pour slab to level 3")],
                     version_no=2)
    assert _updates(plan)["u-a"]["fields"]["name"] == "Pour slab to level 3"
    assert plan["insert"] == [] and plan["remove"] == []


def test_a_task_missing_from_the_file_is_soft_removed_never_deleted():
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g")]
    plan = reconcile(existing, GROUP, [], version_no=3)
    removed = {r["id"] for r in plan["remove"]}
    assert "u-a" in removed
    assert all(r["removed_in_version"] == 3 for r in plan["remove"])


def test_local_rows_are_never_removed_updated_or_touched():
    """The whole point. A breakdown subtask is ours; the file has no opinion
    about it and must not be able to express one."""
    existing = [
        existing_task("u-g", "G1"),
        existing_task("u-a", "A1", parent="u-g"),
        existing_task("u-local", None, origin="local", parent="u-a",
                      name="Formwork"),
    ]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1")], version_no=2)
    touched = {u["id"] for u in plan["update"]} | {r["id"] for r in plan["remove"]}
    assert "u-local" not in touched


def test_a_local_row_survives_even_when_its_imported_parent_leaves_the_file():
    """Archived with the parent, not deleted — completed work hangs off it."""
    existing = [
        existing_task("u-g", "G1"),
        existing_task("u-a", "A1", parent="u-g"),
        existing_task("u-local", None, origin="local", parent="u-a"),
    ]
    plan = reconcile(existing, GROUP, [], version_no=4)
    removed = {r["id"]: r for r in plan["remove"]}
    assert "u-a" in removed
    assert removed["u-local"]["archived_with_parent"] is True
    assert all(r["removed_in_version"] == 4 for r in plan["remove"])


def test_a_new_task_is_inserted_and_stamped_with_the_version_it_arrived_in():
    existing = [existing_task("u-g", "G1")]
    plan = reconcile(existing, GROUP, [incoming_leaf("B9")], version_no=5)
    assert len(plan["insert"]) == 1
    assert plan["insert"][0]["source_task_id"] == "B9"
    assert plan["insert"][0]["first_seen_version"] == 5


def test_a_previously_removed_task_that_reappears_is_revived_not_duplicated():
    """Its allocations and progress are still attached to that row."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", removed=2)]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1")], version_no=6)
    assert plan["insert"] == [], "reviving must not create a second row"
    assert _updates(plan)["u-a"]["fields"]["removed_in_version"] is None


def test_progress_recorded_here_is_never_overwritten_by_the_file():
    """Progress is site truth. The file's 0% is a planning artefact, not an
    observation, and must not erase what someone recorded."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", progress=60)]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1")], version_no=2)
    assert "progress_pct" not in _updates(plan).get("u-a", {}).get("fields", {})


def test_status_is_never_taken_from_the_file_either():
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g")]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1")], version_no=2)
    assert "status" not in _updates(plan).get("u-a", {}).get("fields", {})


def test_an_unchanged_task_produces_no_update_at_all():
    """An import that changes nothing must not bump every row's version and
    make the diff read as a hundred changes.

    Every import-owned field has to match for this to hold — including the
    ones that are easy to forget, like wbs_code and duration_days.
    """
    group = dict(existing_task("u-g", "G1", name="Foundations"), wbs_code="1")
    leaf = dict(existing_task("u-a", "A1", parent="u-g", name="T",
                              start="2026-04-01", end="2026-04-10"),
                duration_days=10)
    plan = reconcile([group, leaf], GROUP, [incoming_leaf("A1")], version_no=2)
    assert plan["update"] == []
    assert plan["summary"]["updated"] == 0
    assert plan["summary"]["date_shifted"] == 0


def test_locally_modified_rows_are_listed_so_the_diff_can_warn():
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", name="Edited here",
                              locally_modified=True)]
    plan = reconcile(existing, GROUP, [incoming_leaf("A1", name="From file")],
                     version_no=2)
    assert "u-a" in [t["id"] for t in
                     plan["summary"]["locally_modified_overwritten"]]


def test_summary_counts_what_the_diff_screen_shows():
    existing = [existing_task("u-g", "G1", name="Foundations"),
                existing_task("u-a", "A1", parent="u-g",
                              start="2026-04-01", end="2026-04-10"),
                existing_task("u-b", "B1", parent="u-g")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("A1", start="2026-04-15", end="2026-04-24"),
                      incoming_leaf("C1")],
                     version_no=2)
    s = plan["summary"]
    assert s["added"] == 1 and s["removed"] == 1
    assert s["date_shifted"] == 1
    assert s["max_shift_days"] == 14


def test_rename_candidate_pairs_a_disappearance_with_an_arrival():
    """A planner changing an Activity ID presents as one removal plus one
    addition. Offer the repair rather than silently orphaning the row."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1020", parent="u-g", name="Pour slab L3",
                              start="2026-04-01", end="2026-04-10")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("A1020R1", name="Pour slab L3",
                                    start="2026-04-01", end="2026-04-10")],
                     version_no=2)
    cands = plan["rename_candidates"]
    assert len(cands) == 1
    assert cands[0]["existing_id"] == "u-a"
    assert cands[0]["incoming_source_task_id"] == "A1020R1"


def test_a_rename_candidate_is_rejected_when_the_dates_moved_too_far():
    """A wrong pairing transplants one task's history onto another, which is
    worse than making the user redo an allocation."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1020", parent="u-g", name="Pour slab",
                              start="2026-04-01", end="2026-04-10")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("A1020R1", name="Pour slab",
                                    start="2026-09-01", end="2026-09-10")],
                     version_no=2)
    assert plan["rename_candidates"] == []


def test_unrelated_add_and_remove_do_not_become_a_rename_candidate():
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", name="Excavate",
                              start="2026-01-01", end="2026-01-10")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("Z9", name="Landscaping",
                                    start="2027-06-01", end="2027-06-30")],
                     version_no=2)
    assert plan["rename_candidates"] == []


def test_one_arrival_is_not_offered_as_a_rename_for_two_departures():
    """Two identically-named tasks disappearing and one arriving must not
    produce two candidates claiming the same new id."""
    existing = [existing_task("u-g", "G1"),
                existing_task("u-a", "A1", parent="u-g", name="Pour slab",
                              start="2026-04-01", end="2026-04-10"),
                existing_task("u-b", "A2", parent="u-g", name="Pour slab",
                              start="2026-04-02", end="2026-04-11")]
    plan = reconcile(existing, GROUP,
                     [incoming_leaf("A9", name="Pour slab",
                                    start="2026-04-01", end="2026-04-10")],
                     version_no=2)
    assert len(plan["rename_candidates"]) == 1


def test_suggest_mode_says_update_when_the_ids_mostly_overlap():
    existing = [existing_task(f"u{i}", f"A{i}") for i in range(10)]
    incoming = [incoming_leaf(f"A{i}") for i in range(10)]
    assert suggest_mode(existing, [], incoming) == "update"


def test_suggest_mode_says_replace_when_it_looks_like_a_different_programme():
    existing = [existing_task(f"u{i}", f"A{i}") for i in range(10)]
    incoming = [incoming_leaf(f"Z{i}") for i in range(10)]
    assert suggest_mode(existing, [], incoming) == "replace"


def test_suggest_mode_says_update_for_a_first_import():
    """Nothing to replace. Preselecting Replace on an empty programme would
    put a destructive-looking confirmation in front of a harmless action."""
    assert suggest_mode([], GROUP, [incoming_leaf("A1")]) == "update"
