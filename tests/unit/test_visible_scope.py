"""Unit tests for repositories/scope.py -- visible_scope (Phase 3 Task 1,
additive/unwired). FakeConn-free: repo calls are stubbed with monkeypatch so
these run with no DB (BUG-29 -> CI is the gate). Exercises every role
(worker/site_manager/pm/regional_manager/gm/admin/platform_admin) x in-/
out-of-scope, the company pin, the platform_admin cross-company branch, and
the D1 mixed-membership floor cases.

PERFORMANCE (plan Global Constraints #1, binding): scope.py derives site_ids
straight from memberships.caller_site_roles' key set and must NEVER also call
memberships.accessible_site_ids (that would be a redundant second round-trip).
test_visible_scope_never_calls_legacy_accessible_site_ids and the two
membership-query-count tests below assert this directly."""
import pytest

scope = pytest.importorskip("repositories.scope", reason="requires psycopg (CI)")
from repositories import scope as scope_mod  # noqa: E402


def _caller(role, uid="u-self", cid="c-1", folder="Self_Folder"):
    return {"id": uid, "company_id": cid, "global_role": role, "folder_name": folder}


@pytest.fixture
def stub(monkeypatch):
    """Stub scope's repo dependencies. Tests set the four returns as needed.
    accessible_site_ids is stubbed too (so a regression that reintroduces the
    call still runs instead of erroring) but visible_scope must never invoke
    it -- see test_visible_scope_never_calls_legacy_accessible_site_ids."""
    state = {"company_sites": [], "all_sites": [], "membership_ids": [],
             "site_roles": {}, "worker_ids": set()}
    monkeypatch.setattr(scope_mod.sites, "list_company_sites",
                        lambda conn, cid, **k: [{"id": s} for s in state["company_sites"]])
    monkeypatch.setattr(scope_mod.sites, "list_all_sites",
                        lambda conn, **k: [{"id": s} for s in state["all_sites"]])
    monkeypatch.setattr(scope_mod.memberships, "accessible_site_ids",
                        lambda conn, uid, role: list(state["membership_ids"]))
    monkeypatch.setattr(scope_mod.memberships, "caller_site_roles",
                        lambda conn, uid: dict(state["site_roles"]))
    monkeypatch.setattr(scope_mod.memberships, "worker_user_ids_for_sites",
                        lambda conn, sids: set(state["worker_ids"]))
    return state


def test_visible_scope_admin_all_company_sites_no_author_filter(stub):
    stub["company_sites"] = ["s-1", "s-2"]
    sc = scope_mod.visible_scope(None, _caller("admin"))
    assert sc["site_ids"] == {"s-1", "s-2"}
    assert sc["user_scope"] == "ALL"
    assert sc["author_ids"] is None
    assert sc["cross_company"] is False


def test_visible_scope_gm_all_company_sites_no_author_filter(stub):
    stub["company_sites"] = ["s-1", "s-2"]
    sc = scope_mod.visible_scope(None, _caller("gm"))
    assert sc["site_ids"] == {"s-1", "s-2"}
    assert sc["user_scope"] == "ALL"
    assert sc["author_ids"] is None
    assert sc["cross_company"] is False


def test_visible_scope_worker_membership_sites_self_only_author(stub):
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "worker"}
    sc = scope_mod.visible_scope(None, _caller("worker"))
    assert sc["site_ids"] == {"s-9"}
    assert sc["user_scope"] == "SELF"
    assert sc["author_ids"] == {"u-self"}


def test_visible_scope_worker_out_of_scope_site_not_included(stub):
    stub["site_roles"] = {"s-9": "worker"}
    sc = scope_mod.visible_scope(None, _caller("worker"))
    assert "s-out-of-scope" not in sc["site_ids"]


def test_visible_scope_site_manager_self_plus_workers(stub):
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "site_manager"}
    stub["worker_ids"] = {"u-w1", "u-w2"}
    sc = scope_mod.visible_scope(None, _caller("site_manager"))
    assert sc["user_scope"] == "SELF+WORKERS"
    assert sc["author_ids"] == {"u-self", "u-w1", "u-w2"}   # own + workers, no other manager


def test_visible_scope_pm_membership_grants_site_no_author_filter(stub):
    # D1: global 'worker' with a pm membership -> SITE, unrestricted authors on scope
    stub["membership_ids"] = ["s-9"]
    stub["site_roles"] = {"s-9": "pm"}
    sc = scope_mod.visible_scope(None, _caller("worker"))
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None


def test_visible_scope_pm_global_role_site_no_author_filter(stub):
    stub["site_roles"] = {"s-1": "pm"}
    sc = scope_mod.visible_scope(None, _caller("pm"))
    assert sc["site_ids"] == {"s-1"}
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None
    assert sc["cross_company"] is False


def test_visible_scope_regional_union_membership_sites_company_pinned(stub):
    stub["membership_ids"] = ["s-3", "s-4"]
    stub["site_roles"] = {"s-3": "worker", "s-4": "site_manager"}
    sc = scope_mod.visible_scope(None, _caller("regional_manager"))
    assert sc["site_ids"] == {"s-3", "s-4"}       # NOT all-company; only assigned sites
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None
    assert sc["cross_company"] is False


def test_visible_scope_regional_membership_is_a_floor_not_a_cap(stub):
    # D1/D3: a global regional_manager who is only a worker on a site is
    # still graded SITE (global reach dominates the per-site floor).
    stub["site_roles"] = {"s-5": "worker"}
    sc = scope_mod.visible_scope(None, _caller("regional_manager"))
    assert sc["user_scope"] == "SITE"
    assert sc["author_ids"] is None


def test_visible_scope_platform_admin_all_sites_cross_company(stub):
    stub["all_sites"] = ["s-1", "s-2", "s-99"]    # spans companies
    sc = scope_mod.visible_scope(None, _caller("platform_admin", cid="c-platform"))
    assert sc["site_ids"] == {"s-1", "s-2", "s-99"}
    assert sc["user_scope"] == "ALL"
    assert sc["cross_company"] is True


def test_visible_scope_self_folder_and_self_user_id(stub):
    stub["site_roles"] = {"s-1": "worker"}
    sc = scope_mod.visible_scope(None, _caller("worker", uid="u-42", folder="Some_Folder"))
    assert sc["self_folder"] == "Some_Folder"
    assert sc["self_user_id"] == "u-42"
    assert sc["company_id"] == "c-1"


def test_visible_scope_non_platform_never_calls_list_all_sites(stub, monkeypatch):
    called = []
    monkeypatch.setattr(scope_mod.sites, "list_all_sites",
                        lambda *a, **k: called.append(1) or [])
    stub["membership_ids"] = ["s-9"]
    scope_mod.visible_scope(None, _caller("gm"))       # ALL but company-pinned
    scope_mod.visible_scope(None, _caller("worker"))
    assert called == []                                 # cross-company query untouched


# ---------------------------------------------------------------------------
# Binding perf constraint (plan Global Constraints #1): ONE membership query
# for non-site_manager graded roles, TWO for site_manager; the legacy
# accessible_site_ids round-trip must never fire from visible_scope.
# ---------------------------------------------------------------------------

def test_visible_scope_never_calls_legacy_accessible_site_ids(stub, monkeypatch):
    called = []
    monkeypatch.setattr(scope_mod.memberships, "accessible_site_ids",
                        lambda *a, **k: called.append(1) or [])
    stub["site_roles"] = {"s-9": "worker"}
    for role in ("worker", "site_manager", "pm", "regional_manager", "admin", "gm"):
        scope_mod.visible_scope(None, _caller(role))
    stub["all_sites"] = ["s-1"]
    scope_mod.visible_scope(None, _caller("platform_admin"))
    assert called == []


def test_non_site_manager_visible_scope_issues_exactly_one_membership_query(stub, monkeypatch):
    calls = []
    monkeypatch.setattr(scope_mod.memberships, "caller_site_roles",
                        lambda conn, uid: calls.append("caller_site_roles") or {"s-9": "worker"})
    scope_mod.visible_scope(None, _caller("worker"))
    assert calls == ["caller_site_roles"]               # exactly one membership query


def test_site_manager_visible_scope_issues_exactly_two_membership_queries(stub, monkeypatch):
    calls = []
    monkeypatch.setattr(scope_mod.memberships, "caller_site_roles",
                        lambda conn, uid: calls.append("caller_site_roles") or {"s-9": "site_manager"})
    monkeypatch.setattr(scope_mod.memberships, "worker_user_ids_for_sites",
                        lambda conn, sids: calls.append("worker_user_ids_for_sites") or set())
    scope_mod.visible_scope(None, _caller("site_manager"))
    assert calls == ["caller_site_roles", "worker_user_ids_for_sites"]   # exactly two
