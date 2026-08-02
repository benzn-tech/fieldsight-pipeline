"""
Two-phase import endpoint — Task 4 of the programme import reconciliation
plan. Spec §6.1, §6.2.

`dry_run: true` reconciles in memory and returns a diff without writing; the
client shows it, the user picks a mode, and a second call commits. The user
never chooses blind — which matters because Replace discards work that Update
would have kept, and that cost is invisible from the plan itself: it is
measured in what Update would have preserved.

The case worth reading is `test_accepting_a_rename_reruns_reconciliation`.
Applying a rename turns what looked like a remove-plus-insert into a plain
update, so the plan computed before the rename is stale. Applying the stale
plan would soft-remove the row the rename just repaired — and because removal
is soft, the symptom is a task quietly vanishing from the Gantt rather than an
error.
"""
import json

import pytest

import lambda_org_api as org

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
PROG_ID = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"

PM = {"id": "u-pm", "global_role": "pm", "folder_name": "Pat_PM"}
SM = {"id": "u-sm", "global_role": "site_manager", "folder_name": "Sam_SM"}

PROGRAMME = {"id": PROG_ID, "site_id": SITE_ID, "name": "Main",
             "current_version": 1, "baseline_version": None}

GROUP = [{"task_id": "G1", "name": "Foundations", "wbs": "1"}]


def leaf(src, *, name="T", start="2026-04-01", end="2026-04-10"):
    return {"task_id": src, "parent_id": "G1", "name": name,
            "start": start, "end": end, "duration_days": 10}


def existing(uid, src, *, origin="imported", name="T", progress=0,
             start="2026-04-01", end="2026-04-10"):
    return {"id": uid, "source_task_id": src, "parent_id": None,
            "origin": origin, "name": name, "wbs_code": None,
            "start_date": start, "end_date": end, "duration_days": 10,
            "progress_pct": progress, "status": "not_started",
            "removed_in_version": None, "locally_modified": False}


class Calls:
    def __init__(self):
        self.applied = []
        self.replaced = []
        self.renames = []
        self.versions = []
        self.snapshots = 0
        self.existing = []


@pytest.fixture
def wired(monkeypatch):
    c = Calls()
    c.existing = [existing("u-g", "G1", name="Foundations",
                           start=None, end=None),
                  existing("u-a", "A1", name="Pour slab")]

    monkeypatch.setattr(org, "_resolve_site_param",
                        lambda conn, caller, p: (SITE_ID, None))
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme",
                        lambda conn, site_id: PROGRAMME)
    monkeypatch.setattr(org.programme_tasks, "get_primary_programme_by_id",
                        lambda conn, pid: PROGRAMME)
    monkeypatch.setattr(org.programme_tasks, "list_tasks",
                        lambda conn, pid, include_removed=False: list(c.existing))
    monkeypatch.setattr(org.programme_tasks, "list_assignees",
                        lambda conn, ids: {"u-a": ["Sam_SM"]})
    monkeypatch.setattr(org.programme_tasks, "create_programme",
                        lambda conn, **kw: PROGRAMME)

    def replace_all(conn, pid, *, parents, leaves, version_no, updated_by):
        c.replaced.append(len(parents) + len(leaves))
        return len(parents) + len(leaves)
    monkeypatch.setattr(org.programme_tasks, "replace_all_tasks", replace_all)

    def apply_plan(conn, pid, plan, *, version_no, updated_by):
        c.applied.append(plan)
        return {"inserted": len(plan["insert"]), "updated": len(plan["update"]),
                "removed": len(plan["remove"])}
    monkeypatch.setattr(org.programme_import, "apply_plan", apply_plan)

    def apply_rename(conn, existing_id, new_src):
        c.renames.append((existing_id, new_src))
        for row in c.existing:
            if row["id"] == existing_id:
                row["source_task_id"] = new_src
        return True
    monkeypatch.setattr(org.programme_import, "apply_rename", apply_rename)

    monkeypatch.setattr(org.programme_import, "record_version",
                        lambda conn, pid, **kw: c.versions.append(kw) or dict(kw))

    def snap(conn, site_id, pid):
        c.snapshots += 1
    monkeypatch.setattr(org, "_write_snapshot", snap)

    class Cur:
        def execute(self, *a, **k):
            return self
    monkeypatch.setattr(org, "_import_exec", lambda conn, sql, params: None,
                        raising=False)
    return c


def _event():
    return {"queryStringParameters": {"site": SITE_ID}}


class FakeConn:
    def cursor(self, row_factory=None):
        class C:
            def execute(self, *a, **k):
                return self

            def fetchone(self):
                return None
        return C()


# ---- dry run -------------------------------------------------------------

def test_a_dry_run_writes_nothing(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "dry_run": True, "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 200
    assert wired.applied == [] and wired.replaced == []
    assert wired.versions == [] and wired.snapshots == 0


def test_a_dry_run_returns_both_previews(wired):
    """Update's cost and Replace's cost are different quantities. Replace's is
    invisible from the plan — it is measured in what Update would have kept."""
    res = org.import_programme(FakeConn(), PM, _event(), {
        "dry_run": True, "parents": GROUP, "leaves": [leaf("A1")]})
    body = json.loads(res["body"])
    assert "update_preview" in body and "replace_preview" in body
    assert "suggested_mode" in body


def test_the_replace_preview_counts_what_would_be_discarded(wired):
    wired.existing.append(
        existing("u-local", None, origin="local", name="Formwork"))
    wired.existing.append(existing("u-b", "B1", name="Done", progress=60))
    res = org.import_programme(FakeConn(), PM, _event(), {
        "dry_run": True, "parents": GROUP, "leaves": [leaf("A1")]})
    rp = json.loads(res["body"])["replace_preview"]
    assert rp["local_tasks_discarded"] == 1
    assert rp["tasks_with_progress_discarded"] == 1
    assert rp["allocations_discarded"] == 1


def test_a_dry_run_surfaces_rename_candidates(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "dry_run": True, "parents": GROUP,
        "leaves": [leaf("A1R1", name="Pour slab")]})
    cands = json.loads(res["body"])["rename_candidates"]
    assert len(cands) == 1
    assert cands[0]["incoming_source_task_id"] == "A1R1"


# ---- commit --------------------------------------------------------------

def test_committing_without_a_mode_is_rejected(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 400
    assert wired.applied == []


def test_an_unknown_mode_is_rejected(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "mode": "obliterate", "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 400


def test_update_applies_the_plan_and_records_a_version(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "mode": "update", "parents": GROUP, "leaves": [leaf("A1")],
        "filename": "rev-b.xml"})
    assert res["statusCode"] == 200
    assert len(wired.applied) == 1
    assert wired.versions[0]["version_no"] == 2
    assert wired.versions[0]["filename"] == "rev-b.xml"
    assert wired.snapshots == 1


def test_replace_requires_explicit_confirmation(wired):
    """Destructive, and confirmed twice: the client requires the site name
    typed, and this flag has to be sent as well."""
    res = org.import_programme(FakeConn(), PM, _event(), {
        "mode": "replace", "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 400
    assert wired.replaced == []


def test_replace_with_confirmation_goes_through(wired):
    res = org.import_programme(FakeConn(), PM, _event(), {
        "mode": "replace", "confirm_replace": True,
        "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 200
    assert wired.replaced == [2]
    assert wired.applied == [], "replace must not also run the plan"


def test_an_empty_import_is_rejected(wired):
    res = org.import_programme(FakeConn(), PM, _event(),
                               {"mode": "update", "parents": [], "leaves": []})
    assert res["statusCode"] == 400


def test_a_site_manager_may_not_import(wired):
    res = org.import_programme(FakeConn(), SM, _event(), {
        "dry_run": True, "parents": GROUP, "leaves": [leaf("A1")]})
    assert res["statusCode"] == 403


# ---- the rename trap -----------------------------------------------------

def test_accepting_a_rename_reruns_reconciliation(wired):
    """Applying a rename turns a remove-plus-insert into a plain update, so
    the plan computed beforehand is stale. Applying it would soft-remove the
    row the rename just repaired — and because removal is soft, the symptom is
    a task quietly vanishing from the Gantt, not an error."""
    res = org.import_programme(FakeConn(), PM, _event(), {
        "mode": "update", "parents": GROUP,
        "leaves": [leaf("A1R1", name="Pour slab")],
        "accept_renames": [
            {"existing_id": "u-a", "incoming_source_task_id": "A1R1"}],
    })
    assert res["statusCode"] == 200
    assert wired.renames == [("u-a", "A1R1")]

    plan = wired.applied[0]
    assert plan["remove"] == [], \
        "the repaired row must not be soft-removed by a stale plan"
    assert plan["insert"] == [], "and must not be re-inserted as a new task"


def test_no_renames_means_no_rerun_and_no_rename_calls(wired):
    org.import_programme(FakeConn(), PM, _event(), {
        "mode": "update", "parents": GROUP, "leaves": [leaf("A1")]})
    assert wired.renames == []
