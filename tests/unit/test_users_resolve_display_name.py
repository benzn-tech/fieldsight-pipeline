"""Resolving a typed name to a person in the directory.

Every failure here is silent. A rule that matches nobody looks exactly like "that person is
not in the directory", and a rule that matches the wrong person looks like nothing at all —
the voiceprint is filed under another identity and the site filter then hides it from the
site they are on while offering it on one they are not.

So the tests assert on the SQL as much as on the result: two of the three rules are wrong in
ways no fixture would reveal.
"""
import pytest

from repositories import users

CO = "11111111-1111-1111-1111-111111111111"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": " ".join(sql.split()), "params": params})
        self._rows = self.conn._pop()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def test_a_multi_word_name_is_normalised_the_way_folders_are_written():
    """`folder_name` stores "Neil_Blunden", not "Neil Blunden". Comparing the typed name
    without the substitution misses every multi-word name — the first rule, and the only
    exact one, dead for the common case."""
    conn = FakeConn([[{"id": "u-1"}]])
    row, how = users.resolve_display_name(conn, CO, "Neil Blunden")
    assert (row, how) == ({"id": "u-1"}, "folder_name")
    assert "Neil_Blunden" in conn.calls[0]["params"]


def test_a_person_with_no_surname_is_still_findable_by_full_name():
    """`'Ben' || ' ' || NULL` is NULL in Postgres and `NULL = 'ben'` is never true, so the
    full-name rule would match nobody for every row without a surname — and those are
    ordinary here (the `Ben_UCPK_` folder, trailing underscore and all, comes from one)."""
    conn = FakeConn([[], [{"id": "u-2"}]])
    row, how = users.resolve_display_name(conn, CO, "Ben")
    assert how == "full_name"
    assert "concat_ws" in conn.calls[1]["sql"], "a NULL surname would resolve to nobody"


def test_two_people_with_one_name_resolve_to_neither():
    conn = FakeConn([[{"id": "u-1"}, {"id": "u-2"}]])
    assert users.resolve_display_name(conn, CO, "Ben") == (None, "ambiguous")


def test_an_ambiguous_first_name_is_not_resolved_by_falling_through():
    conn = FakeConn([[], [], [{"id": "u-1"}, {"id": "u-2"}]])
    assert users.resolve_display_name(conn, CO, "Ben") == (None, "ambiguous")


def test_an_unknown_name_is_reported_as_such():
    assert users.resolve_display_name(FakeConn(), CO, "Nobody") == (None,
                                                                    "not-in-directory")


def test_every_rule_is_company_scoped():
    """Without this a voiceprint could be filed under a person from another tenant."""
    conn = FakeConn([[], [], []])
    users.resolve_display_name(conn, CO, "Ben")
    assert conn.calls, "no query ran"
    for c in conn.calls:
        assert CO in c["params"]


def test_every_rule_excludes_archived_people():
    conn = FakeConn([[], [], []])
    users.resolve_display_name(conn, CO, "Ben")
    for c in conn.calls:
        assert "archived_at IS NULL" in c["sql"]


def test_matching_ignores_case():
    conn = FakeConn([[{"id": "u-1"}]])
    users.resolve_display_name(conn, CO, "neil blunden")
    assert conn.calls[0]["sql"].count("lower(") == 2


def test_an_empty_name_asks_the_database_nothing():
    conn = FakeConn()
    assert users.resolve_display_name(conn, CO, "  ") == (None, "not-in-directory")
    assert conn.calls == []


def test_a_missing_company_resolves_to_nobody_rather_than_to_anybody():
    conn = FakeConn([[{"id": "u-1"}]])
    assert users.resolve_display_name(conn, "", "Ben") == (None, "not-in-directory")
    assert conn.calls == []


@pytest.mark.parametrize("raw,expected", [
    ("Neil Blunden", "Neil_Blunden"),
    ("  Ben  ", "Ben"),
    ("a/b", "a_b"),
    ("a\\b", "a_b"),
])
def test_the_folder_spelling_is_one_function(raw, expected):
    """org-api built this inline in three places. Survivable while it only ever WROTE;
    reading is what makes a second copy dangerous."""
    assert users.safe_folder_name(raw) == expected
