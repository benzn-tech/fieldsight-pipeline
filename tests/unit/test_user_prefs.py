"""
Tests for per-user UI preferences (migration 0028) — Task 1 of the programme
time-window plan. Spec §7.

The programme time window has to follow a user between their office desktop
and the site tablet, which is why it is a column rather than localStorage.

The property worth pinning is the SHALLOW MERGE. Assigning `prefs = %s`
instead of `prefs = prefs || %s` would mean the programme page saving its
window silently wipes every other surface's preferences — and nothing would
report it; those settings would just be back to default the next morning.
"""
import pytest

from repositories import users as repo

from tests.unit.test_programme_tasks_repo import FakeConn

SUB = "cognito-sub-abc"


def test_merge_prefs_merges_rather_than_assigns():
    conn = FakeConn([{"cognito_sub": SUB, "prefs": {"programmeWindow": "2-4"}}])
    repo.merge_prefs(conn, SUB, {"programmeWindow": "2-4"})
    sql = conn.calls[0]["sql"]
    assert "prefs = prefs || %s" in sql, (
        "an assignment would wipe every other surface's preferences, silently")


def test_merge_prefs_binds_the_payload_as_jsonb():
    """A plain dict param would be adapted as a Postgres composite/hstore
    guess, not jsonb, and the || would fail at runtime rather than here."""
    from psycopg.types.json import Jsonb
    conn = FakeConn([{"cognito_sub": SUB}])
    repo.merge_prefs(conn, SUB, {"programmeWindow": "2-4"})
    assert isinstance(conn.calls[0]["params"][0], Jsonb)


def test_merge_prefs_tolerates_an_empty_payload():
    conn = FakeConn([{"cognito_sub": SUB}])
    repo.merge_prefs(conn, SUB, {})
    assert conn.calls, "an empty merge is a no-op write, not a crash"


def test_merge_prefs_tolerates_none():
    conn = FakeConn([{"cognito_sub": SUB}])
    repo.merge_prefs(conn, SUB, None)
    assert conn.calls


def test_merge_prefs_returns_none_for_an_unknown_user():
    conn = FakeConn([[]])
    assert repo.merge_prefs(conn, "no-such-sub", {"a": 1}) is None


def test_prefs_is_in_the_echoed_column_list():
    """get_me echoes the caller row wholesale, so prefs reaches the client
    only if it is in _COLS."""
    assert "prefs" in repo._COLS


@pytest.mark.parametrize("bad", ["a string", 42, ["a", "list"], True])
def test_patch_me_rejects_a_non_object_prefs(bad):
    """Postgres' || would raise on a non-object, surfacing as a 500 rather
    than telling the caller what they got wrong."""
    import lambda_org_api as org
    res = org.patch_me(FakeConn([]), {"cognito_sub": SUB}, {"prefs": bad})
    assert res["statusCode"] == 400
