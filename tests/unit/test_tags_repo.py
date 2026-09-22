"""repositories/tags.py — the taxonomy read/write, and what it refuses.

Three scopes share one table, and which rows a caller may SEE is a different
question from which rows a caller may WRITE. Both are pinned here, because the
failure mode of getting either wrong is silent: a too-wide read shows one
company another company's vocabulary, and a too-wide write lets a site manager
edit the base set for everyone.

`FakeConn` records SQL and returns canned rows; it does not parse it. The SQL
itself was executed against a real Postgres (the migration, inside a
rollback-only transaction on TEST) -- these tests are about the CALLS, and this
file says so rather than implying the double proved the query runs.
"""
import pytest

# importorskip on `repositories.tags` would turn "the module does not exist
# yet" into a SKIP, and a skipped test reads exactly like a passing one in the
# run summary. The skip belongs on the dependency that is genuinely absent in
# some environments; a missing module of ours is a failure.
pytest.importorskip("psycopg", reason="the repo layer needs psycopg")
from repositories import tags  # noqa: E402


class _Cursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows=()):
        self.executed = []
        self.rows = list(rows)

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return _Cursor(self.rows)

    def cursor(self, row_factory=None):
        return _RecordingCursor(self)

    def sql(self):
        return [s for s, _ in self.executed]


class _RecordingCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.executed.append((" ".join(sql.split()), params))
        return _Cursor(self._conn.rows)


# ---------------------------------------------------------------------------
# What a caller can SEE
# ---------------------------------------------------------------------------

def test_the_visible_set_is_global_plus_own_company_plus_reachable_sites():
    conn = FakeConn()
    tags.list_visible(conn, company_id="co-1", site_ids=["s-1", "s-2"])
    sql, params = conn.executed[0]
    assert "company_id IS NULL" in sql, "the base set must always be visible"
    assert params[0] == "co-1"
    assert list(params[1]) == ["s-1", "s-2"]


def test_a_caller_with_no_sites_still_sees_global_and_company_tags():
    """`[]` means "no sites", not "every site". The empty-list-means-no-filter
    trap has cost this repo a cross-tenant leak before, so the site arm must
    match nothing rather than everything."""
    conn = FakeConn()
    tags.list_visible(conn, company_id="co-1", site_ids=[])
    sql, params = conn.executed[0]
    assert list(params[1]) == []
    assert "= ANY(" in sql, (
        "an empty site list has to go through a predicate that matches "
        "nothing, never through a branch that drops the filter")


def test_another_companys_tags_are_never_in_the_query():
    conn = FakeConn()
    tags.list_visible(conn, company_id="co-1", site_ids=["s-1"])
    sql, _ = conn.executed[0]
    assert "company_id = %s" in sql or "company_id=%s" in sql


def test_inactive_tags_are_excluded_by_default_and_included_on_request():
    """A deactivated tag must stay readable for the admin screen -- otherwise
    nothing can ever re-activate it -- and stay out of the picker."""
    # The WHERE clause only. `is_active` is also a RETURNED COLUMN, so
    # searching the whole statement would pass either way -- the first draft of
    # this assertion did exactly that and proved nothing.
    def where_of(sql):
        return sql.split(" WHERE ", 1)[1].split(" ORDER BY ")[0]

    conn = FakeConn()
    tags.list_visible(conn, company_id="co-1", site_ids=[])
    assert "is_active" in where_of(conn.executed[0][0])

    conn2 = FakeConn()
    tags.list_visible(conn2, company_id="co-1", site_ids=[], include_inactive=True)
    assert "is_active" not in where_of(conn2.executed[0][0])


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def test_a_company_tag_is_written_with_its_company_and_no_site():
    conn = FakeConn(rows=[{"id": "t-1"}])
    tags.create(conn, company_id="co-1", site_id=None, parent_id=None,
                slug="prefab", label="Prefab", created_by="u-1")
    sql, params = conn.executed[0]
    assert sql.startswith("INSERT INTO tag")
    assert "co-1" in params and None in params


def test_a_site_tag_carries_both_its_company_and_its_site():
    """A site tag without a company_id would be invisible to the company-scoped
    read, and a site's slug must not collide with a company-wide one silently."""
    conn = FakeConn(rows=[{"id": "t-1"}])
    tags.create(conn, company_id="co-1", site_id="s-1", parent_id="p-1",
                slug="architecture.soffits", label="Soffits", created_by="u-1")
    _, params = conn.executed[0]
    assert "co-1" in params and "s-1" in params


# ---------------------------------------------------------------------------
# What may be CHANGED
# ---------------------------------------------------------------------------

def test_a_global_tag_cannot_be_updated_through_this_repo():
    """Refused here, not only at the route. The base set is shared by every
    customer, and a repo that will happily write it is one route away from
    someone renaming 'Safety' for everyone."""
    conn = FakeConn(rows=[{"id": "t-1", "company_id": None, "site_id": None}])
    with pytest.raises(tags.NotWritable):
        tags.update(conn, "t-1", company_id="co-1", label="Ours")


def test_a_tag_belonging_to_another_company_cannot_be_updated():
    conn = FakeConn(rows=[{"id": "t-1", "company_id": "co-2", "site_id": None}])
    with pytest.raises(tags.NotWritable):
        tags.update(conn, "t-1", company_id="co-1", label="Ours")


def test_the_slug_is_not_updatable():
    """Labels get renamed; slugs do not. An assignment row and a prompt both
    name the slug, so changing it silently orphans every row that used it."""
    conn = FakeConn(rows=[{"id": "t-1", "company_id": "co-1", "site_id": None}])
    tags.update(conn, "t-1", company_id="co-1", label="Renamed", is_active=False)
    update = next(s for s in conn.sql() if s.startswith("UPDATE tag"))
    # The SET list only -- `slug` is in RETURNING on every one of these
    # statements, so scanning the whole thing would fail a correct query.
    sets = update.split(" SET ", 1)[1].split(" WHERE ")[0]
    assert "slug" not in sets, sets


def test_deactivating_is_an_update_and_never_a_delete():
    conn = FakeConn(rows=[{"id": "t-1", "company_id": "co-1", "site_id": None}])
    tags.update(conn, "t-1", company_id="co-1", is_active=False)
    assert not any(s.startswith("DELETE") for s in conn.sql())
    assert any("is_active" in s for s in conn.sql() if s.startswith("UPDATE"))


def test_an_update_with_nothing_to_change_writes_nothing():
    """An UPDATE with an empty SET list is a syntax error, and building one is
    the ordinary consequence of a PATCH body carrying only unknown keys."""
    conn = FakeConn(rows=[{"id": "t-1", "company_id": "co-1", "site_id": None}])
    tags.update(conn, "t-1", company_id="co-1")
    assert not any(s.startswith("UPDATE") for s in conn.sql())
