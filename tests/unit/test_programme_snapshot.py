"""
Tests for src/repositories/programme_snapshot.py — Task 3 of the programme
storage foundation plan.

This module regenerates the legacy programmes/{site_id}/programme.json
document from the Aurora rows, so lambda_programme_matcher.py keeps working
byte-compatibly while Aurora becomes the source of truth. The matcher reads
`leaves` and filters to schedulable ones (candidate_tasks, matcher line 167).

If any assertion here fails, the matcher stops matching SILENTLY — there is
no error path, candidates simply come back empty. Treat a failure as a stop.
"""
from repositories import programme_snapshot as snap

PROGRAMME = {"id": "p1", "name": "Main contract"}


def task(tid, *, source, parent=None, name="T", start=None, end=None,
         origin="imported", removed=None, progress=0, status="not_started"):
    return {
        "id": tid, "source_task_id": source, "parent_id": parent,
        "origin": origin, "name": name, "wbs_code": None,
        "start_date": start, "end_date": end, "duration_days": None,
        "progress_pct": progress, "status": status,
        "removed_in_version": removed,
    }


def test_leaf_task_id_prefers_the_source_id():
    """The matcher's suggestions and the confirm path key on task_id. For an
    imported row that must remain the file's identifier, or every suggestion
    already in programme_progress_suggestions orphans."""
    doc = snap.build_snapshot(PROGRAMME, [
        task("uuid-g", source="G1"),
        task("uuid-t", source="A1020", parent="uuid-g",
             start="2026-04-01", end="2026-04-10"),
    ])
    assert [t["task_id"] for t in doc["leaves"]] == ["A1020"]


def test_local_rows_fall_back_to_their_uuid():
    doc = snap.build_snapshot(PROGRAMME, [
        task("uuid-g", source="G1"),
        task("uuid-local", source=None, origin="local", parent="uuid-g",
             start="2026-04-01", end="2026-04-05"),
    ])
    assert [t["task_id"] for t in doc["leaves"]] == ["uuid-local"]


def test_a_dated_task_with_children_stays_a_leaf():
    """THE rule, and not the obvious one.

    Sorting by 'has children' looks right until a PM breaks a contract task
    down: it would acquire subtasks, become a parent, drop out of `leaves`,
    and silently stop being a match candidate — so 'we poured the slab today'
    would no longer land on it, with nothing raised anywhere.

    Dates decide instead. Both the broken-down task and its subtasks stay
    matchable, which is what you want: general speech lands on the parent,
    specific speech on the subtask.
    """
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("parent-with-dates", source="A1020", parent="g", name="Pour slab",
             start="2026-04-01", end="2026-04-10"),
        task("sub", source=None, origin="local", parent="parent-with-dates",
             name="Formwork", start="2026-04-01", end="2026-04-04"),
    ])
    leaf_ids = [t["task_id"] for t in doc["leaves"]]
    assert "A1020" in leaf_ids, \
        "a broken-down contract task must remain matchable"
    assert "sub" in leaf_ids
    assert [p["task_id"] for p in doc["parents"]] == ["G1"]


def test_dateless_ancestors_are_parents():
    """A WBS header carries no dates — that is what `parents` meant in the
    legacy document, and it is what still lands there."""
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("mid", source="M1", parent="g"),
        task("leaf", source="L1", parent="mid",
             start="2026-04-01", end="2026-04-02"),
    ])
    assert [p["task_id"] for p in doc["parents"]] == ["G1", "M1"]
    assert [t["task_id"] for t in doc["leaves"]] == ["L1"]


def test_leaf_parent_id_points_at_its_nearest_ancestor_source_id():
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("mid", source="M1", parent="g"),
        task("leaf", source="L1", parent="mid",
             start="2026-04-01", end="2026-04-02"),
    ])
    assert doc["leaves"][0]["parent_id"] == "M1"


def test_soft_deleted_rows_never_reach_the_snapshot():
    """A row removed by a later import must stop being a match candidate."""
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("gone", source="OLD", parent="g",
             start="2026-04-01", end="2026-04-02", removed=3),
        task("here", source="NEW", parent="g",
             start="2026-04-01", end="2026-04-02"),
    ])
    assert [t["task_id"] for t in doc["leaves"]] == ["NEW"]


def test_a_dated_task_whose_only_child_was_removed_is_still_a_leaf():
    """Follows from the dates rule, and is worth pinning separately: under a
    has-children rule this row would flip between lists as imports come and
    go, appearing and disappearing as a match candidate for no reason the
    user could see."""
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1", start="2026-04-01", end="2026-04-30"),
        task("gone", source="OLD", parent="g",
             start="2026-04-01", end="2026-04-02", removed=3),
    ])
    assert doc["parents"] == []
    assert [t["task_id"] for t in doc["leaves"]] == ["G1"]


def test_leaf_carries_the_keys_the_matcher_reads():
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("t", source="A1", parent="g", name="Pour slab",
             start="2026-04-01", end="2026-04-10", progress=40,
             status="in_progress"),
    ])
    leaf = doc["leaves"][0]
    for key in ("task_id", "parent_id", "name", "start", "end",
                "progress_pct", "status"):
        assert key in leaf, f"the matcher's candidate_tasks reads {key}"
    assert leaf["start"] == "2026-04-01" and leaf["end"] == "2026-04-10"


def test_dates_are_iso_strings_not_date_objects():
    """psycopg returns datetime.date; json.dumps in write_programme would
    raise on those, so the snapshot must stringify them."""
    import datetime
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("t", source="A1", parent="g",
             start=datetime.date(2026, 4, 1), end=datetime.date(2026, 4, 10)),
    ])
    assert doc["leaves"][0]["start"] == "2026-04-01"
    assert isinstance(doc["leaves"][0]["end"], str)


def test_the_whole_document_is_json_serialisable():
    """write_programme calls json.dumps on this. A date object anywhere in
    the tree would raise there, at write time, far from the cause."""
    import datetime
    import json
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("t", source="A1", parent="g",
             start=datetime.date(2026, 4, 1), end=datetime.date(2026, 4, 10)),
    ])
    json.dumps(doc)


def test_programme_span_derives_from_the_leaves():
    doc = snap.build_snapshot(PROGRAMME, [
        task("g", source="G1"),
        task("a", source="A", parent="g", start="2026-05-01", end="2026-05-10"),
        task("b", source="B", parent="g", start="2026-04-01", end="2026-06-30"),
    ])
    assert doc["start_date"] == "2026-04-01"
    assert doc["end_date"] == "2026-06-30"


def test_empty_programme_produces_a_valid_empty_document():
    doc = snap.build_snapshot(PROGRAMME, [])
    assert doc["parents"] == [] and doc["leaves"] == []
    assert doc["start_date"] is None and doc["end_date"] is None
