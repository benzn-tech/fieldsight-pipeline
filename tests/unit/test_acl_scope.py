from repositories.acl import resolve_scope
from repositories.acl import is_cross_company, visible_user_scope


def test_admin_and_gm_see_all():
    assert resolve_scope("admin") == "ALL"
    assert resolve_scope("gm") == "ALL"


def test_others_scoped_to_memberships():
    assert resolve_scope("pm") == "MEMBERSHIPS"
    assert resolve_scope("site_manager") == "MEMBERSHIPS"
    assert resolve_scope("worker") == "MEMBERSHIPS"


def test_visible_user_scope_all_roles_have_no_author_filter():
    for r in ("platform_admin", "admin", "gm"):
        assert visible_user_scope(r, set()) == "ALL"


def test_visible_user_scope_regional_is_site():
    assert visible_user_scope("regional_manager", set()) == "SITE"


def test_visible_user_scope_pm_global_or_membership_is_site():
    assert visible_user_scope("pm", set()) == "SITE"
    # D1: a global 'worker' who holds a pm membership still gets SITE
    assert visible_user_scope("worker", {"pm"}) == "SITE"


def test_visible_user_scope_site_manager_is_self_plus_workers():
    assert visible_user_scope("site_manager", set()) == "SELF+WORKERS"
    assert visible_user_scope("worker", {"site_manager"}) == "SELF+WORKERS"


def test_visible_user_scope_worker_is_self():
    assert visible_user_scope("worker", set()) == "SELF"
    assert visible_user_scope("worker", {"worker"}) == "SELF"


def test_visible_user_scope_membership_is_a_floor_not_a_cap():
    # a global regional_manager who is only a worker on a site is still SITE
    assert visible_user_scope("regional_manager", {"worker"}) == "SITE"


def test_is_cross_company_only_platform_admin():
    assert is_cross_company("platform_admin") is True
    for r in ("admin", "gm", "regional_manager", "pm", "site_manager", "worker"):
        assert is_cross_company(r) is False
