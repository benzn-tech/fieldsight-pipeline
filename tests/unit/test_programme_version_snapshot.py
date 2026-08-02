"""
Per-version task snapshot — what lateness against a baseline actually needs.

programme_tasks.start_date/end_date are overwritten IN PLACE by every import,
and first_seen_version / removed_in_version record only which tasks EXISTED at
version N. So "落后多少天" could not be computed at all from 0027's schema
unless the baseline happened to be the current version — the one case where
the answer is always zero, which is exactly why the gap was invisible.

The snapshot is written at import commit, the only moment the data is
certainly correct. These tests pin what goes in it and what comes back out.
"""
import pytest

from repositories import programme_import as repo

from tests.unit.test_programme_tasks_repo import FakeConn

PROG = "22222222-2222-2222-2222-222222222222"


def task(src, start, end, *, origin="imported", removed=None, days=10):
    return {"source_task_id": src, "id": "u-" + (src or "x"),
            "origin": origin, "start_date": start, "end_date": end,
            "duration_days": days, "removed_in_version": removed}


def test_the_snapshot_carries_dates_not_just_membership():
    """The whole point: membership was already derivable, dates were not."""
    snap = repo.build_task_snapshot([task("A1", "2026-04-01", "2026-04-10")])
    assert snap == [{"i": "A1", "s": "2026-04-01", "e": "2026-04-10", "d": 10}]


def test_soft_removed_tasks_are_excluded():
    """A task removed by THIS import was not part of what the import agreed
    to; including it would push the baseline finish out past the truth."""
    snap = repo.build_task_snapshot([
        task("A1", "2026-04-01", "2026-04-10"),
        task("OLD", "2026-09-01", "2026-09-30", removed=3),
    ])
    assert [r["i"] for r in snap] == ["A1"]


def test_local_rows_are_excluded():
    """The baseline is the client's programme. Our own breakdown subtasks are
    not part of what was agreed, and a local task running past the contract
    end would silently inflate the baseline finish."""
    snap = repo.build_task_snapshot([
        task("A1", "2026-04-01", "2026-04-10"),
        task(None, "2026-04-01", "2026-12-31", origin="local"),
    ])
    assert [r["i"] for r in snap] == ["A1"]


def test_undated_tasks_are_excluded():
    """They cannot contribute to a finish date, and carrying nulls just makes
    every reader handle them."""
    snap = repo.build_task_snapshot([
        task("A1", "2026-04-01", "2026-04-10"),
        task("G1", None, None),
    ])
    assert [r["i"] for r in snap] == ["A1"]


def test_date_objects_are_stringified():
    """psycopg hands back datetime.date; the column is jsonb."""
    import datetime
    snap = repo.build_task_snapshot([
        task("A1", datetime.date(2026, 4, 1), datetime.date(2026, 4, 10)),
    ])
    assert snap[0]["s"] == "2026-04-01"
    assert isinstance(snap[0]["e"], str)


def test_an_empty_task_set_produces_an_empty_snapshot_not_null():
    assert repo.build_task_snapshot([]) == []
    assert repo.build_task_snapshot(None) == []


def test_the_snapshot_is_json_serialisable():
    import datetime
    import json
    snap = repo.build_task_snapshot([
        task("A1", datetime.date(2026, 4, 1), datetime.date(2026, 4, 10)),
    ])
    json.dumps(snap)


# ---- storage / retrieval -------------------------------------------------

def test_record_version_binds_the_snapshot_as_jsonb():
    from psycopg.types.json import Jsonb
    conn = FakeConn([{"id": "v1", "version_no": 2}])
    repo.record_version(conn, PROG, version_no=2, filename=None, mode="update",
                        imported_by=None, diff_summary={},
                        task_snapshot=[{"i": "A1", "s": "2026-04-01",
                                        "e": "2026-04-10", "d": 10}])
    assert "task_snapshot" in conn.calls[0]["sql"]
    assert any(isinstance(p, Jsonb) for p in conn.calls[0]["params"])


def test_record_version_still_works_without_a_snapshot():
    """Restore writes a version row and has no task set of its own to
    snapshot; it must not be forced to invent one."""
    conn = FakeConn([{"id": "v1"}])
    repo.record_version(conn, PROG, version_no=6, filename=None, mode="replace",
                        imported_by=None, diff_summary={"restored_from": 3})
    assert conn.calls, "the insert must still happen"


def test_get_version_tasks_selects_the_snapshot_for_that_version():
    conn = FakeConn([{"task_snapshot": [{"i": "A1"}]}])
    out = repo.get_version_tasks(conn, PROG, 4)
    sql = conn.calls[0]["sql"]
    assert "task_snapshot" in sql
    assert 4 in conn.calls[0]["params"]
    assert out == [{"i": "A1"}]


def test_get_version_tasks_returns_none_for_a_version_that_does_not_exist():
    """Distinct from an empty snapshot: "no such version" and "that version
    had no dated tasks" are different answers, and the caller renders them
    differently."""
    conn = FakeConn([[]])
    assert repo.get_version_tasks(conn, PROG, 99) is None


def test_get_version_tasks_returns_an_empty_list_for_an_empty_snapshot():
    conn = FakeConn([{"task_snapshot": []}])
    assert repo.get_version_tasks(conn, PROG, 2) == []
