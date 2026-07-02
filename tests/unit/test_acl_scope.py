from repositories.acl import resolve_scope


def test_admin_and_gm_see_all():
    assert resolve_scope("admin") == "ALL"
    assert resolve_scope("gm") == "ALL"


def test_others_scoped_to_memberships():
    assert resolve_scope("pm") == "MEMBERSHIPS"
    assert resolve_scope("site_manager") == "MEMBERSHIPS"
    assert resolve_scope("worker") == "MEMBERSHIPS"
