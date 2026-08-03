"""
Tests for src/repositories/programme_import.py — applying a reconciliation
plan, plus version history, rollback and baseline. Spec §6.5.

FakeConn/FakeCursor record every execute()'s SQL and params, as in
tests/unit/test_programme_suggestions_repo.py.

What is pinned here: an import never issues a DELETE, a rename touches exactly
one column, and restore records the state it is leaving before rolling back —
otherwise the rollback itself is unrecoverable, which defeats the point of
having version history at all.
"""
import pytest

from repositories import programme_import as repo

from tests.unit.test_programme_tasks_repo import FakeConn

PROG = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"


def _sql(conn):
    return " ".join(c["sql"] for c in conn.calls)


EMPTY = {"insert": [], "update": [], "remove": [],
         "rename_candidates": [], "summary": {}}


def plan(**kw):
    return dict(EMPTY, **kw)


# ---- apply_plan ----------------------------------------------------------

def test_removals_are_soft_and_never_delete():
    """Allocations, recorded progress and local subtrees hang off these rows.
    A DELETE would take all of it with no way back."""
    conn = FakeConn([[{"id": "t1"}]])
    repo.apply_plan(conn, PROG, plan(remove=[
        {"id": "t1", "removed_in_version": 3, "archived_with_parent": False},
    ]), version_no=3, updated_by=USER)
    assert "removed_in_version" in _sql(conn)
    assert "DELETE" not in _sql(conn).upper(), "an import must never delete a task row"


def test_apply_plan_writes_nothing_for_an_empty_plan():
    conn = FakeConn([])
    counts = repo.apply_plan(conn, PROG, plan(), version_no=2, updated_by=USER)
    assert counts == {"inserted": 0, "updated": 0, "removed": 0}
    assert conn.calls == []


def test_a_group_is_inserted_before_the_leaf_that_references_it():
    """The file expresses parentage with its own ids; rows link by our uuids,
    so a leaf inserted first would have nothing to point at."""
    conn = FakeConn([{"id": "uuid-G1"}, [], []])
    repo.apply_plan(conn, PROG, plan(insert=[
        {"source_task_id": "A1", "parent_source_id": "G1", "name": "leaf",
         "wbs_code": None, "start_date": "2026-04-01", "end_date": "2026-04-10",
         "duration_days": 10, "first_seen_version": 2},
        {"source_task_id": "G1", "parent_source_id": None, "name": "group",
         "wbs_code": "1", "start_date": None, "end_date": None,
         "duration_days": None, "first_seen_version": 2},
    ]), version_no=2, updated_by=USER)
    first_params = conn.calls[0]["params"]
    assert "G1" in first_params, "the group must be written first"
    leaf_call = [c for c in conn.calls if c["params"] and "A1" in c["params"]][0]
    assert "uuid-G1" in leaf_call["params"], "the leaf links to the group's uuid"


def test_an_update_clears_the_locally_modified_flag():
    """The file and the row agree again once the import has run; leaving the
    flag set would warn about an overwrite on every future import."""
    conn = FakeConn([[]])
    repo.apply_plan(conn, PROG, plan(update=[
        {"id": "t1", "source_task_id": "A1", "fields": {"name": "From file"}},
    ]), version_no=2, updated_by=USER)
    assert "locally_modified = false" in _sql(conn)


def test_an_update_bumps_row_version():
    conn = FakeConn([[]])
    repo.apply_plan(conn, PROG, plan(update=[
        {"id": "t1", "source_task_id": "A1", "fields": {"name": "x"}},
    ]), version_no=2, updated_by=USER)
    assert "row_version = row_version + 1" in _sql(conn)


def test_an_update_with_no_fields_is_skipped_entirely():
    conn = FakeConn([])
    counts = repo.apply_plan(conn, PROG, plan(update=[
        {"id": "t1", "source_task_id": "A1", "fields": {}},
    ]), version_no=2, updated_by=USER)
    assert counts["updated"] == 0
    assert conn.calls == []


def test_counts_report_what_was_actually_written():
    conn = FakeConn([{"id": "uuid-G"}, [], []])
    counts = repo.apply_plan(conn, PROG, plan(
        insert=[{"source_task_id": "G", "parent_source_id": None, "name": "g",
                 "wbs_code": None, "start_date": None, "end_date": None,
                 "duration_days": None, "first_seen_version": 2}],
        update=[{"id": "t1", "source_task_id": "A1", "fields": {"name": "x"}}],
        remove=[{"id": "t2", "removed_in_version": 2, "archived_with_parent": False}],
    ), version_no=2, updated_by=USER)
    assert counts == {"inserted": 1, "updated": 1, "removed": 1}


# ---- apply_rename --------------------------------------------------------

def test_apply_rename_updates_only_the_source_id():
    """The payoff of keeping identity and matching keys in separate columns:
    repairing a renamed Activity ID costs one column and the row's history —
    allocations, progress, local subtree — stays attached."""
    conn = FakeConn([{"id": "t1"}])
    repo.apply_rename(conn, "t1", "A1020R1")
    sql = conn.calls[0]["sql"]
    assert "source_task_id" in sql
    for col in ("progress_pct", "start_date", "end_date", "programme_id"):
        assert col not in sql, f"a rename must not touch {col}"


def test_apply_rename_refuses_a_local_row():
    """A local row has no file identity by construction (migration 0027's
    CHECK), so giving it one would make the row unrepresentable."""
    conn = FakeConn([[]])
    assert repo.apply_rename(conn, "t1", "A1") is False
    assert "origin = 'imported'" in conn.calls[0]["sql"]


# ---- versions, rollback, baseline ----------------------------------------

def test_list_versions_returns_newest_first():
    conn = FakeConn([[]])
    repo.list_versions(conn, PROG)
    assert "ORDER BY version_no DESC" in conn.calls[0]["sql"]


def test_restore_archives_the_current_state_before_rolling_back():
    """Otherwise rolling back is itself unrecoverable, and version history
    stops being a safety net."""
    conn = FakeConn([{"version_no": 3}, [], [], {"current_version": 5},
                     {"id": "v", "version_no": 6}])
    repo.restore_version(conn, PROG, 3, restored_by=USER)
    assert "programme_versions" in _sql(conn), \
        "restore must record a version row for the state it is leaving"


def test_restore_flips_flags_rather_than_deleting_or_reinserting():
    conn = FakeConn([{"version_no": 3}, [], [], {"current_version": 5},
                     {"id": "v", "version_no": 6}])
    repo.restore_version(conn, PROG, 3, restored_by=USER)
    sql = _sql(conn)
    assert "DELETE" not in sql.upper()
    assert "removed_in_version" in sql


def test_restore_rejects_a_version_that_does_not_exist():
    conn = FakeConn([[]])
    with pytest.raises(ValueError):
        repo.restore_version(conn, PROG, 99, restored_by=USER)


def test_set_baseline_rejects_a_version_that_does_not_exist():
    conn = FakeConn([[]])
    with pytest.raises(ValueError):
        repo.set_baseline(conn, PROG, 99)


def test_set_baseline_records_the_chosen_version():
    """The contractually approved revision is often not the first import, and
    lateness measured against the wrong baseline is worse than not measuring
    it at all."""
    conn = FakeConn([{"version_no": 4}, {"id": PROG, "baseline_version": 4}])
    row = repo.set_baseline(conn, PROG, 4)
    assert row["baseline_version"] == 4
    assert 4 in conn.calls[-1]["params"]
