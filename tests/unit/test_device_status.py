"""device_status — the backlog uplink, and the thaw channel that stays shut in Phase 1.

Asserted against the SQL text and the returned body rather than a live database, in the
same style as test_device_heartbeat: what matters is that the vitals are written for the
right device and that nothing here can raise into a user's request.
"""

import src.device_status as ds


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


# --- the answer ------------------------------------------------------------

def test_phase1_never_thaws():
    """The register exists and the endpoint records; no device is ever told to try again."""
    out = ds.record(FakeConn(), "dev-1", {
        "oldestPendingAgeS": 93600, "pending": 12, "frozen": 3, "dead": 0,
        "fingerprints": ["uploadurl_401"],
    })
    assert out["thaw"] == []


def test_reports_the_deployed_build(monkeypatch):
    monkeypatch.setattr(ds, "SERVER_BUILD", "9495bcd")
    out = ds.record(FakeConn(), "dev-1", {"pending": 0, "frozen": 0, "dead": 0})
    assert out["serverBuild"] == "9495bcd"


def test_an_unset_build_is_null_not_empty_string():
    """The device treats a blank build as unknown; the wire should say so plainly."""
    monkeypatch_value = ds.SERVER_BUILD
    try:
        ds.SERVER_BUILD = ""
        out = ds.record(FakeConn(), "dev-1", {"pending": 0, "frozen": 0, "dead": 0})
        assert out["serverBuild"] is None
    finally:
        ds.SERVER_BUILD = monkeypatch_value


# --- the vitals ------------------------------------------------------------

def test_writes_the_vitals_for_that_device():
    conn = FakeConn()
    ds.record(conn, "dev-1", {"oldestPendingAgeS": 93600, "pending": 12, "frozen": 0, "dead": 0})
    sql, params = conn.cur.executed[-1]
    assert "update devices" in sql.lower()
    assert params == (93600, 12, "dev-1")


def test_an_idle_device_reports_a_null_age_not_a_zero():
    """"Nothing is waiting" and "something has waited no time" are different facts."""
    conn = FakeConn()
    ds.record(conn, "dev-1", {"pending": 0, "frozen": 0, "dead": 0})
    _, params = conn.cur.executed[-1]
    assert params[0] is None


def test_a_malformed_body_is_absorbed_not_raised():
    """Telemetry never fails a user's request. Same rule as device_heartbeat."""
    out = ds.record(FakeConn(), "dev-1", {"pending": "twelve", "oldestPendingAgeS": "old"})
    assert out["thaw"] == []
    conn = FakeConn()
    ds.record(conn, "dev-1", {"pending": "twelve"})
    _, params = conn.cur.executed[-1]
    assert params[1] is None


def test_an_unidentified_device_writes_nothing_and_still_answers():
    """A device whose headers did not resolve still gets a valid answer, and no row."""
    conn = FakeConn()
    out = ds.record(conn, None, {"pending": 1, "frozen": 0, "dead": 0})
    assert out["thaw"] == []
    assert conn.cur.executed == []


def test_a_database_failure_still_answers():
    class Exploding(FakeConn):
        def cursor(self, *a, **k):
            raise RuntimeError("connection reset")

    out = ds.record(Exploding(), "dev-1", {"pending": 1, "frozen": 0, "dead": 0})
    assert out["thaw"] == []


def test_a_body_that_is_not_a_dict_is_absorbed():
    out = ds.record(FakeConn(), "dev-1", None)
    assert out["thaw"] == []
