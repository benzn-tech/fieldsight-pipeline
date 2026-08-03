"""device_heartbeat — header parsing and the throttled upsert.

The upsert's throttle is asserted against the SQL text rather than against a
live database: what matters is that the conditional clauses are present, since
their absence would silently turn every request into a row write.
"""

import src.device_heartbeat as dh


class FakeCursor:
    def __init__(self, results=None):
        self.executed = []
        self._results = list(results or [])

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, results=None):
        self.cur = FakeCursor(results)
        self.committed = False

    def cursor(self, *a, **k):
        return self.cur

    def commit(self):
        self.committed = True


def _sql(conn):
    return " ".join(sql for sql, _ in conn.cur.executed).lower()


# --- parse_headers ---------------------------------------------------------


def test_returns_none_without_device_headers():
    assert dh.parse_headers({"Authorization": "Bearer x"}) is None


def test_returns_none_for_empty_headers():
    assert dh.parse_headers({}) is None
    assert dh.parse_headers(None) is None


def test_is_case_insensitive():
    ident = dh.parse_headers({"x-device-tag": "FS-07", "X-App-Version": "1.4.2"})
    assert ident["asset_tag"] == "FS-07"
    assert ident["app_version"] == "1.4.2"


def test_tag_is_trimmed_and_uppercased():
    assert dh.parse_headers({"X-Device-Tag": "  fs-07 "})["asset_tag"] == "FS-07"


def test_untagged_device_becomes_an_unclaimed_row():
    ident = dh.parse_headers({"X-Device-Id": "a3f9c2d1e0b7"})
    assert ident["asset_tag"] == "unclaimed:a3f9c2d1"
    assert ident["device_uuid"] == "a3f9c2d1e0b7"


def test_blank_header_values_count_as_absent():
    assert dh.parse_headers({"X-Device-Tag": "   ", "X-Device-Id": ""}) is None


# --- record ----------------------------------------------------------------


IDENT = {"asset_tag": "FS-07", "device_uuid": "u1", "app_version": "1.4.2"}


def test_upsert_throttles_on_one_hour():
    conn = FakeConn()
    dh.record(conn, IDENT, "sub-1")
    sql = _sql(conn)
    assert "insert into devices" in sql
    assert "on conflict (asset_tag) do update" in sql
    assert "interval '1 hour'" in sql


def test_account_switch_and_version_change_are_never_throttled():
    conn = FakeConn()
    dh.record(conn, IDENT, "sub-1")
    sql = _sql(conn)
    assert "last_account_sub is distinct from excluded.last_account_sub" in sql
    assert "app_version is distinct from excluded.app_version" in sql


def test_record_commits():
    conn = FakeConn()
    dh.record(conn, IDENT, "sub-1")
    assert conn.committed is True


def test_no_identity_is_a_no_op():
    conn = FakeConn()
    dh.record(conn, None, "sub-1")
    assert conn.cur.executed == []


def test_duplicate_uuid_across_tags_clears_trust():
    conn = FakeConn(results=[[("u1",)]])
    dh.record(conn, {**IDENT, "asset_tag": "FS-08"}, "sub-1")
    assert "uuid_trusted = false" in _sql(conn)


def test_a_uuid_seen_under_one_tag_keeps_its_trust():
    conn = FakeConn(results=[[]])
    dh.record(conn, IDENT, "sub-1")
    assert "uuid_trusted = false" not in _sql(conn)


def test_no_uuid_skips_the_collision_check():
    conn = FakeConn()
    dh.record(conn, {"asset_tag": "FS-07", "device_uuid": None, "app_version": None}, "sub-1")
    assert len(conn.cur.executed) == 1


def test_record_never_raises_when_the_database_fails():
    class Boom:
        def cursor(self, *a, **k):
            raise RuntimeError("connection reset")

    dh.record(Boom(), IDENT, "sub-1")  # reaching this line is the assertion
