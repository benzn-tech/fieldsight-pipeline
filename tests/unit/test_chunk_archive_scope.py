"""Unit: archiving a deleted recording's search index must not reach past that recording.

`archive_chunks_for_session` runs an INSERT…SELECT and a DELETE over `report_chunks` with a
predicate built from the caller's own `sessionBase`. Two things were missing from it, and
either one alone is enough to move ANOTHER COMPANY's entire search index into the archive:

  * the session id went into a LIKE pattern unescaped, so `%` is a wildcard rather than a
    character a session id happens not to contain;
  * neither statement was scoped to a tenant at all.

`_can_delete_folder` lets any user delete their OWN recordings — which is correct and was
deliberately widened — so "any authenticated account" is the reachable audience here.
`ENABLE_USER_DELETION` is true on PROD.
"""
import pytest

chunks = pytest.importorskip("repositories.chunks", reason="requires psycopg (CI)")


class RecordingConn:
    """Records the SQL and params rather than executing them: the assertions here are about
    what the statement is allowed to reach, which is a property of its text."""

    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return self

    @property
    def rowcount(self):
        return 0


def _archive(session_base, company_id="c-1"):
    conn = RecordingConn()
    chunks.archive_chunks_for_session(conn, session_base, [], "b-1", company_id=company_id)
    return conn.statements


def test_every_statement_is_scoped_to_one_company():
    """An INSERT…SELECT and a DELETE over a shared table with no tenant predicate is a
    cross-tenant write however good the rest of the filter is."""
    for sql, params in _archive("sid0123456789abcdef0123456789abcdef"):
        assert "company_id" in sql, f"unscoped statement: {sql[:80]}"
        assert params.get("company_id") == "c-1"


def test_a_wildcard_session_id_matches_nothing_extra():
    """`sessionBase='%'` was a whole-database selector: the predicate is
    `f LIKE '%' || session_base || '%'`, so an unescaped % made it `LIKE '%%%'`."""
    for sql, params in _archive("%"):
        assert params["session_base"] == r"\%", "the wildcard was not escaped"


def test_underscores_in_a_session_id_stay_literal():
    """Session ids are '_'-joined; `_` is a single-character wildcard in LIKE. Unescaped, a
    legacy session id matches its neighbours."""
    for sql, params in _archive("Benl1_2026-08-13_11-49-00"):
        assert "\_" in params["session_base"]


def test_an_empty_session_id_is_refused_rather_than_matching_everything():
    """'' makes the predicate `LIKE '%%'` — every row. The endpoint already requires a
    non-empty sessionBase, but the guard belongs where the damage is."""
    with pytest.raises(ValueError):
        chunks.archive_chunks_for_session(RecordingConn(), "  ", [], "b-1", company_id="c-1")


def test_a_company_is_required():
    """A default of None would silently restore the unscoped behaviour."""
    with pytest.raises((TypeError, ValueError)):
        chunks.archive_chunks_for_session(RecordingConn(), "sid0", [], "b-1")
