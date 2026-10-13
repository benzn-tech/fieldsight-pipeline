"""Deploy tolerance: `sam deploy` runs before `migrate`, so for a minute lake_objects may not
exist. Every public repository function must return its empty value and log a WARNING."""
import contextlib
import logging

import psycopg.errors as pe
import pytest
from repositories import lake_objects

U = "11111111-1111-1111-1111-111111111111"


class _Cur:
    def __init__(self, exc):
        self.exc = exc

    def execute(self, *a, **k):
        raise self.exc

    def executemany(self, *a, **k):
        raise self.exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, exc):
        self.exc = exc

    def transaction(self):
        return contextlib.nullcontext()

    def execute(self, *a, **k):
        raise self.exc

    def cursor(self, *a, **k):
        return _Cur(self.exc)


CALLS = [
    ("upsert", lambda c: lake_objects.upsert(c, "k", company_id=U, kind="media", source="upload"), False),
    ("upsert_many", lambda c: lake_objects.upsert_many(
        c, [{"key": "k", "kind": "media", "source": "upload"}]), 0),
    ("reassign_site", lambda c: lake_objects.reassign_site(c, U, U), 0),
    ("owners_for_company", lambda c: lake_objects.owners_for_company(c, U), []),
    ("count", lambda c: lake_objects.count(c), 0),
    ("unresolved", lambda c: lake_objects.unresolved(c), []),
    ("list_reports", lambda c: lake_objects.list_reports(c), []),
    ("report_folders_for_day", lambda c: lake_objects.report_folders_for_day(c, "2026-10-01"), []),
]


@pytest.mark.parametrize("exc", [pe.UndefinedTable("x"), pe.UndefinedColumn("x")])
@pytest.mark.parametrize("name,call,empty", CALLS, ids=[c[0] for c in CALLS])
def test_missing_schema_returns_empty_and_warns(name, call, empty, exc, caplog):
    with caplog.at_level(logging.WARNING, logger="repositories.lake_objects"):
        assert call(_Conn(exc)) == empty
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_other_errors_still_raise():
    with pytest.raises(pe.UniqueViolation):
        lake_objects.count(_Conn(pe.UniqueViolation("x")))
