"""
Row-level permission rules for programme task writes — Task 5 of the
programme storage foundation plan. Spec §10.

One test per cell of the matrix. The rule under test:

    imported row  -> dates read-only for everyone but an import;
                     progress writable by managers and by the assignee
    local row     -> dates and progress both writable by managers and by
                     the assignee within their own subtree

The failure mode this guards against is not a crash but a quiet
over-permission: a site manager editing a contract date here would see it
accepted and then silently reverted by the next import, having believed the
plan was updated.
"""
from lambda_org_api import can_edit_task

MANAGER = {"global_role": "pm", "folder_name": "Pat_PM"}
GM = {"global_role": "gm", "folder_name": "Gerry_GM"}
ADMIN = {"global_role": "admin", "folder_name": "Ada_Admin"}
SITE_MGR = {"global_role": "site_manager", "folder_name": "Sam_SM"}
WORKER = {"global_role": "worker", "folder_name": "Wes_W"}

IMPORTED = {"id": "t1", "origin": "imported"}
LOCAL = {"id": "t2", "origin": "local"}


def test_every_manager_role_may_edit_progress_on_an_imported_row():
    for caller in (MANAGER, GM, ADMIN):
        assert can_edit_task(caller, IMPORTED, {"progress_pct": 50}, []) is None


def test_manager_may_edit_dates_on_an_imported_row():
    """Permitted but flagged locally_modified — the next import's diff shows
    the PM what the file is about to overwrite."""
    assert can_edit_task(MANAGER, IMPORTED, {"start_date": "2026-04-01"}, []) is None


def test_site_manager_may_report_progress_on_a_task_assigned_to_them():
    assert can_edit_task(SITE_MGR, IMPORTED, {"progress_pct": 50}, ["Sam_SM"]) is None


def test_site_manager_may_not_report_progress_on_someone_elses_task():
    assert can_edit_task(SITE_MGR, IMPORTED, {"progress_pct": 50},
                         ["Other_Person"]) is not None


def test_site_manager_may_not_move_a_contract_date():
    reason = can_edit_task(SITE_MGR, IMPORTED, {"start_date": "2026-05-01"},
                           ["Sam_SM"])
    assert reason is not None
    assert "delay flag" in reason.lower(), \
        "the refusal should point at the route that does work"


def test_every_schedule_field_is_refused_not_just_start_date():
    for field, value in [("start_date", "2026-05-01"), ("end_date", "2026-05-09"),
                         ("duration_days", 9)]:
        assert can_edit_task(SITE_MGR, IMPORTED, {field: value},
                             ["Sam_SM"]) is not None, f"{field} must be refused"


def test_a_mixed_patch_is_refused_whole_not_partly_applied():
    """Allowing the progress half through would leave the caller believing
    the date change landed too."""
    assert can_edit_task(SITE_MGR, IMPORTED,
                         {"progress_pct": 50, "start_date": "2026-05-01"},
                         ["Sam_SM"]) is not None


def test_site_manager_may_reschedule_their_own_local_subtask():
    assert can_edit_task(SITE_MGR, LOCAL, {"start_date": "2026-05-01"},
                         ["Sam_SM"]) is None


def test_site_manager_may_not_edit_a_local_task_assigned_to_someone_else():
    assert can_edit_task(SITE_MGR, LOCAL, {"progress_pct": 10},
                         ["Other_Person"]) is not None


def test_worker_may_not_write_at_all():
    assert can_edit_task(WORKER, LOCAL, {"progress_pct": 10}, ["Wes_W"]) is not None
    assert can_edit_task(WORKER, IMPORTED, {"progress_pct": 10}, ["Wes_W"]) is not None


def test_unassigned_task_is_not_open_to_every_site_manager():
    """An empty assignee list means nobody is assigned — it must not read as
    'no restriction'. This codebase has already shipped that inversion once
    (see the empty-list over-permission incident)."""
    assert can_edit_task(SITE_MGR, IMPORTED, {"progress_pct": 50}, []) is not None
    assert can_edit_task(SITE_MGR, LOCAL, {"progress_pct": 50}, []) is not None


def test_a_site_manager_without_a_folder_identity_is_denied():
    """folder_name is how programme work is attributed. Without one there is
    nothing to match against, and None must not slip past the membership
    check by comparing equal to something."""
    nameless = {"global_role": "site_manager", "folder_name": None}
    assert can_edit_task(nameless, LOCAL, {"progress_pct": 10}, ["Sam_SM"]) is not None
    assert can_edit_task(nameless, LOCAL, {"progress_pct": 10}, []) is not None


def test_an_unknown_role_is_denied_rather_than_defaulting_open():
    stranger = {"global_role": "client_viewer", "folder_name": "Cli_V"}
    assert can_edit_task(stranger, LOCAL, {"progress_pct": 10}, ["Cli_V"]) is not None
