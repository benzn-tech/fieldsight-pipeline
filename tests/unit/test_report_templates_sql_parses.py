"""Every statement repositories.report_templates sends is valid PostgreSQL.

WHY THIS FILE EXISTS, AND WHAT IT IS NOT. A recording connection that collects
SQL without executing it has already let two production crashes through this
repo: the double records the call, every assertion passes, and nothing ever
finds out that PostgreSQL would have rejected the statement. So this file does
not assert on substrings of the captured SQL. It hands each captured statement
to libpg_query -- PostgreSQL's own parser, via pglast -- and requires it to
parse.

That catches the failure a text double cannot: a typo, an unbalanced paren, a
RETURNING in the wrong place. It does NOT catch a wrong column name, a
constraint that does not bite, or a WHERE clause on the wrong side of a
comparison. Those need the real database, and they live in
tests/integration/test_report_templates_repository.py.

A skip here is not a pass either: without pglast installed this file proves
nothing at all.
"""
import re

import pytest

pglast = pytest.importorskip("pglast", reason="pglast not installed")

from repositories import report_templates as rt  # noqa: E402

BODY = {"sections": [{"key": "what", "title": "What this was", "purpose": "x"}],
        "catch_all": {"key": "other", "title": "Anything else", "purpose": "y"},
        "excluded_subjects": [], "style": ["short"]}

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


class _Cursor:
    def __init__(self, sink, row):
        self._sink, self._row = sink, row

    def execute(self, sql, args=None):
        self._sink.append(sql)
        return self

    def fetchone(self):
        return dict(self._row)

    def fetchall(self):
        return [dict(self._row)]


class _Conn:
    """Records SQL. Returns a row shaped like the columns these functions read
    back, so the code under test runs to completion rather than tripping over
    None on the way to the statement we are trying to capture."""

    def __init__(self):
        self.sql = []
        self.row = {
            "id": UUID_A, "company_id": UUID_B, "scope": "org", "owner_user_id": None,
            "slug": "daily", "name": "Daily", "description": "", "report_type": "daily",
            "current_version": 1, "archived_at": None, "created_by": UUID_A,
            "created_at": None, "updated_at": None,
            "template_id": UUID_A, "version": 1, "body": BODY, "change_note": None,
        }

    def cursor(self, row_factory=None):
        return _Cursor(self.sql, self.row)

    def execute(self, sql, args=None):
        self.sql.append(sql)
        return _Cursor(self.sql, {"count": 0, 0: 0})


def _capture(fn, *a, **kw):
    conn = _Conn()
    fn(conn, *a, **kw)
    assert conn.sql, "no SQL was sent at all"
    return conn.sql


def _parse(sql):
    """psycopg's %s placeholders are not PostgreSQL syntax; the server sees
    bound parameters. Rewrite them to $n so the real parser can read the
    statement the server would actually be given."""
    n = [0]

    def nxt(_m):
        n[0] += 1
        return "$%d" % n[0]

    return pglast.parse_sql(re.sub(r"%s(?!::)|%s", nxt, sql.replace("%s::jsonb", "%s")))


CASES = [
    ("list_visible", lambda: _capture(rt.list_visible, UUID_B, UUID_A)),
    ("list_visible(scope=org)", lambda: _capture(rt.list_visible, UUID_B, UUID_A, "org")),
    ("list_visible(scope=personal)",
     lambda: _capture(rt.list_visible, UUID_B, UUID_A, "personal")),
    ("get_visible", lambda: _capture(rt.get_visible, UUID_B, UUID_A, UUID_A)),
    ("list_versions", lambda: _capture(rt.list_versions, UUID_A)),
    ("get_version(current)", lambda: _capture(rt.get_version, UUID_A, None)),
    ("get_version(pinned)", lambda: _capture(rt.get_version, UUID_A, 3)),
    ("create", lambda: _capture(rt.create, UUID_B, "org", None, "daily", "Daily", "",
                                "daily", UUID_A)),
    ("add_version", lambda: _capture(rt.add_version, UUID_A, BODY, "note", UUID_A)),
    ("update_meta(name)", lambda: _capture(rt.update_meta, UUID_A, "New name")),
    ("update_meta(both)", lambda: _capture(rt.update_meta, UUID_A, "N", "D")),
    ("get_any", lambda: _capture(rt.get_any, UUID_A)),
    ("archive", lambda: _capture(rt.archive, UUID_A)),
    ("list_bindings", lambda: _capture(rt.list_bindings, UUID_B)),
    ("set_binding", lambda: _capture(rt.set_binding, UUID_B, "daily", UUID_A, None, UUID_A)),
    ("set_binding(pinned)", lambda: _capture(rt.set_binding, UUID_B, "daily", UUID_A, 2, UUID_A)),
    ("is_bound", lambda: _capture(rt.is_bound, UUID_A)),
]


@pytest.mark.parametrize("label,run", CASES, ids=[c[0] for c in CASES])
def test_postgres_parses_every_statement(label, run):
    for sql in run():
        try:
            _parse(sql)
        except Exception as e:                       # pragma: no cover - failure path
            pytest.fail("%s sent SQL PostgreSQL will not parse: %s\n%s" % (label, e, sql))


def test_copy_to_personal_parses_end_to_end():
    """Its own case: it is the one function built from three other statements,
    so a break here is a break in the composition rather than in one query."""
    conn = _Conn()
    rt.copy_to_personal(conn, UUID_A, UUID_A, "my-daily", "My Daily", UUID_A)
    assert len(conn.sql) >= 4, "expected get_any, get_version, create, add_version"
    for sql in conn.sql:
        _parse(sql)


def test_the_parser_is_actually_strict():
    """A guard on the guard. If _parse ever silently accepted anything, every
    test above would pass while proving nothing -- which is the exact failure
    mode this file was written to avoid."""
    with pytest.raises(Exception):
        _parse("SELECT FROM WHERE ORDER BY (")
