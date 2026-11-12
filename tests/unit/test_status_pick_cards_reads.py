"""Pick-card reads and CAS resolve (repositories.status_pick_cards), fake conn.

A fake conn never executes SQL -- the SQL itself is proven by
tests/integration/test_status_pick_cards_repo.py. Here: the no-query short cuts, the
degrade-to-empty on a missing table, and that any other error still raises."""
import contextlib

import psycopg.errors as pe
import pytest
from repositories import status_pick_cards as spc

CARD = "11111111-1111-1111-1111-111111111111"
SITE = "22222222-2222-2222-2222-222222222222"


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    """Records every statement; raises `exc` on execute when given one."""

    def __init__(self, exc=None, rows=()):
        self.exc = exc
        self.rows = list(rows)
        self.calls = []
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return contextlib.nullcontext()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.exc is not None:
            raise self.exc
        return _Result(self.rows)

    def cursor(self, *a, **k):
        return self


def test_get_malformed_id_no_query():
    conn = _Conn()
    assert spc.get_pick_card(conn, "not-a-uuid") is None
    assert spc.get_pick_card(conn, None) is None
    assert conn.calls == []


def test_get_returns_row_by_canonical_id():
    row = {"id": CARD, "state": "pending"}
    conn = _Conn(rows=[row])
    assert spc.get_pick_card(conn, CARD.upper()) == row
    sql, params = conn.calls[0]
    assert "FROM status_pick_cards" in sql and "WHERE id = %s" in sql
    assert params == (CARD,)
    assert conn.transactions == 1


def test_pending_empty_in_empty_out():
    conn = _Conn()
    assert spc.pending_for_sites(conn, []) == []
    assert spc.pending_for_sites(conn, None) == []
    assert conn.calls == []


def test_pending_sql_shape():
    conn = _Conn(rows=[{"id": CARD}])
    assert spc.pending_for_sites(conn, [SITE]) == [{"id": CARD}]
    sql, params = conn.calls[0]
    assert "state = 'pending'" in sql
    assert "site_id = ANY(%s::uuid[])" in sql
    assert "ORDER BY created_at DESC, id DESC" in sql
    assert params == ([SITE],)
    assert conn.transactions == 1


@pytest.mark.parametrize("exc", [pe.UndefinedTable("x"), pe.UndefinedColumn("x")])
def test_reads_degrade_when_not_migrated(exc):
    assert spc.get_pick_card(_Conn(exc), CARD) is None
    assert spc.pending_for_sites(_Conn(exc), [SITE]) == []


@pytest.mark.parametrize("call", [
    lambda c: spc.get_pick_card(c, CARD),
    lambda c: spc.pending_for_sites(c, [SITE]),
], ids=["get", "pending"])
def test_other_errors_raise(call):
    with pytest.raises(pe.UniqueViolation):
        call(_Conn(pe.UniqueViolation("x")))


def test_resolve_is_a_cas_update():
    row = {"id": CARD, "state": "chosen"}
    conn = _Conn(rows=[row])
    uid = "33333333-3333-3333-3333-333333333333"
    assert spc.resolve_pick_card(conn, CARD, "chosen", SITE, uid) == row
    sql, params = conn.calls[0]
    assert sql.startswith("UPDATE status_pick_cards SET state = %s, chosen_stable_id = %s, "
                          "resolved_by = %s, resolved_at = now()")
    assert "WHERE id = %s AND state = 'pending'" in sql
    assert "RETURNING" in sql
    assert params == ("chosen", SITE, uid, CARD)


def test_resolve_malformed_id_no_query():
    conn = _Conn()
    assert spc.resolve_pick_card(conn, "nope", "none", None, CARD) is None
    assert conn.calls == []
