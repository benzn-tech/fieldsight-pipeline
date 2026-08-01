"""
Contract test: a snapshot built from Aurora rows must still satisfy
lambda_programme_matcher.candidate_tasks().

tests/unit/test_programme_snapshot.py asserts the snapshot's SHAPE against
what the matcher was read to require. This file removes the reading from the
loop: it feeds a real snapshot into the matcher's own gate function and
asserts on what comes back.

That distinction matters because the failure mode is silent. A snapshot the
matcher cannot use does not raise — candidate_tasks simply returns [], the
matcher finds nothing to match, and programme suggestions quietly stop
appearing. Nobody gets an error; the feature just goes dark.
"""
from lambda_programme_matcher import candidate_tasks
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


ROWS = [
    task("uuid-g", source="G1", name="Foundations"),
    task("uuid-a", source="A1020", parent="uuid-g", name="Pour slab",
         start="2026-04-01", end="2026-04-10"),
    task("uuid-b", source="A1030", parent="uuid-g", name="Strip formwork",
         start="2026-06-01", end="2026-06-10"),
    task("uuid-c", source="A1040", parent="uuid-g", name="Backfill",
         start="2026-04-01", end="2026-04-05", status="completed"),
    task("uuid-local", source=None, origin="local", parent="uuid-a",
         name="Rebar fixing", start="2026-04-02", end="2026-04-06"),
]


def test_the_matcher_finds_candidates_in_a_snapshot_built_from_aurora_rows():
    doc = snap.build_snapshot(PROGRAMME, ROWS)
    got = candidate_tasks(doc, "2026-04-05")
    assert got, "an empty candidate set is how this failure presents in production"
    assert "A1020" in [t["task_id"] for t in got]


def test_local_breakdown_subtasks_are_matchable_too():
    """A site manager saying 'rebar is done' should be able to land on the
    breakdown subtask, not only on the contract-level parent."""
    doc = snap.build_snapshot(PROGRAMME, ROWS)
    got = candidate_tasks(doc, "2026-04-05")
    assert "uuid-local" in [t["task_id"] for t in got]


def test_group_rows_never_reach_the_candidate_set():
    """The matcher drops status='group', but our snapshot should not be
    relying on that: groups belong in `parents` and never in `leaves`."""
    doc = snap.build_snapshot(PROGRAMME, ROWS)
    assert "G1" not in [leaf["task_id"] for leaf in doc["leaves"]]
    assert "G1" not in [t["task_id"] for t in candidate_tasks(doc, "2026-04-05")]


def test_completed_tasks_are_gated_out_by_the_matcher():
    doc = snap.build_snapshot(PROGRAMME, ROWS)
    got = candidate_tasks(doc, "2026-04-03")
    assert "A1040" not in [t["task_id"] for t in got]


def test_tasks_far_outside_the_report_date_window_are_gated_out():
    doc = snap.build_snapshot(PROGRAMME, ROWS)
    got = candidate_tasks(doc, "2026-04-05")
    assert "A1030" not in [t["task_id"] for t in got], \
        "a June task is not a candidate for an April report"


def test_soft_removed_tasks_are_not_candidates():
    rows = ROWS + [task("uuid-gone", source="OLD", parent="uuid-g",
                        name="Cancelled work",
                        start="2026-04-01", end="2026-04-10", removed=3)]
    doc = snap.build_snapshot(PROGRAMME, rows)
    assert "OLD" not in [t["task_id"] for t in candidate_tasks(doc, "2026-04-05")]


def test_date_objects_from_psycopg_do_not_break_the_matcher():
    """psycopg returns datetime.date. If the snapshot passed those through,
    candidate_tasks' _coerce_date would still cope — but json.dumps in
    write_programme would not, so the document would never reach S3 at all."""
    import datetime
    import json
    rows = [
        task("uuid-g", source="G1"),
        task("uuid-a", source="A1", parent="uuid-g",
             start=datetime.date(2026, 4, 1), end=datetime.date(2026, 4, 10)),
    ]
    doc = snap.build_snapshot(PROGRAMME, rows)
    json.dumps(doc)
    assert [t["task_id"] for t in candidate_tasks(doc, "2026-04-05")] == ["A1"]
