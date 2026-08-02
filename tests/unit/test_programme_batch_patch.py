"""
Batch per-task write — the endpoint the Gantt's cascade needs.

Dragging one bar shifts every downstream dependent and recomputes the critical
path, so a single user action produces N task writes. Sending N independent
PATCHes is not atomic: a failure or a lost optimistic-lock race halfway
through leaves the programme half-shifted, and the symptom is "the Gantt looks
right and the database is wrong" — no error anywhere.

This endpoint takes the whole set. Either every row's row_version still holds
and all of them are written, or none are and the caller is told which ones
moved. The all-or-nothing property is the entire point, so most of these tests
are about what happens when one row in the middle fails.
"""
import pytest

import lambda_org_api as org

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
PROG_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

PM = {"id": "u-pm", "global_role": "pm", "folder_name": "Pat_PM"}
SM = {"id": "u-sm", "global_role": "site_manager", "folder_name": "Sam_SM"}

PROGRAMME = {"id": PROG_ID, "site_id": SITE_ID, "current_version": 1,
             "baseline_version": None}


class Store:
    """Tasks keyed by id, each carrying a row_version that only a matching
    update bumps — the same contract the real repository enforces in SQL."""

    def __init__(self, tasks):
        self.tasks = {t["id"]: dict(t) for t in tasks}
        self.writes = []
        self.snapshots = 0

    def get_task(self, conn, task_id):
        t = self.tasks.get(task_id)
        return dict(t) if t else None

    def update_task(self, conn, task_id, *, fields, row_version, updated_by):
        t = self.tasks.get(task_id)
        if t is None or t["row_version"] != row_version:
            return None
        t.update(fields)
        t["row_version"] += 1
        self.writes.append(task_id)
        return dict(t)

    def list_assignees(self, conn, ids):
        return {i: ["Sam_SM"] for i in ids}


def task(tid, *, origin="imported", rv=1):
    return {"id": tid, "programme_id": PROG_ID, "origin": origin,
            "row_version": rv, "start_date": "2026-04-01",
            "end_date": "2026-04-10", "progress_pct": 0}


@pytest.fixture
def wired(monkeypatch):
    store = Store([task("t1"), task("t2"), task("t3", rv=5)])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_resolve_site_param",
                        lambda conn, caller, p: (SITE_ID, None))
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme",
                        lambda conn, site_id: PROGRAMME)
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme_by_id",
                        lambda conn, pid: PROGRAMME)
    monkeypatch.setattr(org.programme_tasks, "get_task", store.get_task)
    monkeypatch.setattr(org.programme_tasks, "update_task", store.update_task)
    monkeypatch.setattr(org.programme_tasks, "list_assignees", store.list_assignees)

    def snap(conn, site_id, programme_id):
        store.snapshots += 1
    monkeypatch.setattr(org, "_write_snapshot", snap)
    return store


def _body(*pairs, **kw):
    return dict({"tasks": [
        {"id": tid, "row_version": rv, "start_date": "2026-05-01"}
        for tid, rv in pairs
    ]}, **kw)


def _event():
    return {"queryStringParameters": {"site": SITE_ID}}


# ---- the happy path ------------------------------------------------------

def test_a_whole_cascade_is_written_in_one_call(wired):
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t2", 1), ("t3", 5)))
    assert res["statusCode"] == 200
    assert sorted(wired.writes) == ["t1", "t2", "t3"]


def test_the_snapshot_is_regenerated_once_not_per_task(wired):
    """N snapshot rebuilds for one drag would make the endpoint pointless."""
    org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t2", 1), ("t3", 5)))
    assert wired.snapshots == 1


# ---- all-or-nothing ------------------------------------------------------

def test_one_stale_row_version_aborts_the_whole_batch(wired):
    """t2's version is stale. t1 must NOT be left written — a half-applied
    cascade is a programme that disagrees with itself, silently."""
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t2", 99), ("t3", 5)))
    assert res["statusCode"] == 409
    assert wired.writes == [], "nothing may survive a rejected batch"


def test_a_conflict_names_the_tasks_that_moved(wired):
    """The client refreshes exactly those rows; it must not have to reload
    the whole programme and lose the user's other pending edits."""
    import json
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t2", 99), ("t3", 42)))
    body = json.loads(res["body"])
    assert sorted(body["conflicts"]) == ["t2", "t3"]


def test_a_missing_task_aborts_the_batch(wired):
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("nope", 1)))
    assert res["statusCode"] in (404, 409)
    assert wired.writes == []


def test_the_snapshot_is_not_written_for_a_rejected_batch(wired):
    org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t2", 99)))
    assert wired.snapshots == 0


# ---- permissions ---------------------------------------------------------

def test_permission_is_checked_per_task_before_anything_is_written(wired):
    """A site manager may report progress but not move a contract date. A
    batch containing one forbidden row must write none of it."""
    body = {"tasks": [
        {"id": "t1", "row_version": 1, "progress_pct": 50},
        {"id": "t2", "row_version": 1, "start_date": "2026-05-01"},
    ]}
    res = org.patch_programme_tasks_batch(None, SM, _event(), body)
    assert res["statusCode"] == 403
    assert wired.writes == []


def test_a_site_manager_may_batch_progress_on_their_own_tasks(wired):
    body = {"tasks": [
        {"id": "t1", "row_version": 1, "progress_pct": 50},
        {"id": "t2", "row_version": 1, "progress_pct": 60},
    ]}
    res = org.patch_programme_tasks_batch(None, SM, _event(), body)
    assert res["statusCode"] == 200
    assert sorted(wired.writes) == ["t1", "t2"]


def test_a_worker_may_not_batch_at_all(wired):
    worker = {"id": "u-w", "global_role": "worker", "folder_name": "Sam_SM"}
    res = org.patch_programme_tasks_batch(
        None, worker, _event(), _body(("t1", 1)))
    assert res["statusCode"] == 403


# ---- input validation ----------------------------------------------------

def test_an_empty_batch_is_rejected_rather_than_silently_succeeding(wired):
    res = org.patch_programme_tasks_batch(None, PM, _event(), {"tasks": []})
    assert res["statusCode"] == 400


def test_a_task_without_row_version_is_rejected(wired):
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), {"tasks": [{"id": "t1", "progress_pct": 10}]})
    assert res["statusCode"] == 400
    assert wired.writes == []


def test_a_batch_larger_than_the_cap_is_rejected(wired):
    """A cascade touches tens of rows, not thousands. An unbounded batch is a
    transaction held open long enough to matter."""
    body = {"tasks": [{"id": "t1", "row_version": 1, "progress_pct": 1}
                      for _ in range(org._MAX_BATCH_TASKS + 1)]}
    res = org.patch_programme_tasks_batch(None, PM, _event(), body)
    assert res["statusCode"] == 400


def test_duplicate_ids_in_one_batch_are_rejected(wired):
    """Two edits to the same row in one batch means the second would fail the
    lock against a version the first just bumped — confusing, and always a
    client bug."""
    res = org.patch_programme_tasks_batch(
        None, PM, _event(), _body(("t1", 1), ("t1", 1)))
    assert res["statusCode"] == 400
    assert wired.writes == []


def test_an_inaccessible_site_is_refused_before_any_write(wired, monkeypatch):
    """The site ACL runs through _resolve_site_param, same as every other
    programme route."""
    monkeypatch.setattr(org, "_resolve_site_param",
                        lambda conn, caller, p: (None, org.error("not found", 404)))
    res = org.patch_programme_tasks_batch(None, PM, _event(), _body(("t1", 1)))
    assert res["statusCode"] == 404
    assert wired.writes == []


def test_a_task_belonging_to_another_programme_is_refused(wired, monkeypatch):
    """Second guard: the site resolved, but the task id points somewhere
    else. Without this, knowing a task id would be enough to write to a
    programme you cannot see."""
    monkeypatch.setattr(org.programme_tasks, "get_task",
                        lambda conn, tid: dict(task(tid), programme_id="other-prog"))
    res = org.patch_programme_tasks_batch(None, PM, _event(), _body(("t1", 1)))
    assert res["statusCode"] == 404
    assert wired.writes == []
