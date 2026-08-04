"""lambda_device_ledger — the in-VPC leaf that reads the ledger.

It makes no outbound calls and takes no parameters: it reads the whole table
and hands it to lambda_device_report, which lives outside the VPC. These tests
pin the serialised shape, because that shape is the contract between the two.
"""

import datetime as dt

import pytest

# Gate on psycopg only. Importing the module under test directly means a
# missing or broken module is a FAILURE, not a silent skip.
pytest.importorskip("psycopg", reason="psycopg is installed in CI")
import lambda_device_ledger as ledger  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        return self

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *a, **k):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SEEN = dt.datetime(2026, 8, 4, 2, 30, tzinfo=dt.timezone.utc)


def test_serialises_a_device_row(monkeypatch):
    rows = [("FS-07", "u1", True, "1.4.2", SEEN, "sub-1", "UC PK", "UC Property")]
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn(rows))

    out = ledger.lambda_handler({}, None)

    assert out["devices"] == [{
        "asset_tag": "FS-07",
        "device_uuid": "u1",
        "uuid_trusted": True,
        "app_version": "1.4.2",
        "last_seen_at": "2026-08-04T02:30:00+00:00",
        "last_account_sub": "sub-1",
        "actual_site": "UC PK",
        "actual_company": "UC Property",
    }]


def test_a_device_that_has_never_been_seen_serialises_as_null(monkeypatch):
    rows = [("FS-11", None, True, None, None, None, None, None)]
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn(rows))

    device = ledger.lambda_handler({}, None)["devices"][0]

    assert device["last_seen_at"] is None
    assert device["actual_site"] is None
    assert device["asset_tag"] == "FS-11"


def test_an_empty_ledger_returns_an_empty_list_not_an_error(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn([]))
    assert ledger.lambda_handler({}, None) == {"devices": []}


def test_untrusted_uuid_is_reported_as_such(monkeypatch):
    rows = [("FS-07", "shared", False, "1.4.2", SEEN, "sub-1", None, None)]
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn(rows))

    assert ledger.lambda_handler({}, None)["devices"][0]["uuid_trusted"] is False


def test_quietest_devices_sort_first(monkeypatch):
    """Never-seen rows are the alert that matters most, so the query must put
    nulls first — a report that buries them defeats the purpose."""
    monkeypatch.setattr(ledger, "_connect", lambda: FakeConn([]))
    conn = FakeConn([])
    monkeypatch.setattr(ledger, "_connect", lambda: conn)

    ledger.lambda_handler({}, None)

    assert "order by d.last_seen_at asc nulls first" in conn.cur.sql.lower()
