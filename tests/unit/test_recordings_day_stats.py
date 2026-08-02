"""
Unit: recordings.day_stats — the timeline KPI counts.

The two behaviours worth pinning are the ones a naive COUNT(*) gets wrong:
the chunk-session fold (one 9-minute meeting is 21 rows, not 21 recordings)
and the date clock (the s3_key local day, never the UTC started_at). Both are
expressed in SQL, so these assert on the emitted statement; the real counting
runs against a live DB in tests/integration/test_recordings_repo.py.
"""
from repositories import recordings


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop()
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def _pop(self):
        return self._results.pop(0) if self._results else []


def test_day_stats_returns_folded_sessions_and_summed_duration():
    conn = FakeConn(results=[[{"sessions": 1, "duration_s": 569}]])

    assert recordings.day_stats(conn, "co-1", "Ben_UCPK2", "2026-07-31") == {
        "sessions": 1, "duration_s": 569}


def test_day_stats_folds_chunk_rows_by_session_id_not_row_count():
    """A chunked session must collapse to one. The fold is a DISTINCT over the
    sid parsed from the key, with the key itself as the fallback value."""
    conn = FakeConn(results=[[{"sessions": 1, "duration_s": 569}]])
    recordings.day_stats(conn, "co-1", "Ben_UCPK2", "2026-07-31")

    sql = conn.calls[0]["sql"]
    assert "COUNT(DISTINCT COALESCE(" in sql
    assert "sid[0-9a-f]{32}" in sql        # the chunk-session key contract
    assert "_c[0-9]+" in sql
    assert "COUNT(*)" not in sql           # the bug this method exists to avoid


def test_day_stats_matches_the_local_date_in_the_key_not_started_at():
    """started_at is UTC; `date` is the device's NZ local day. Filtering on the
    timestamp would shift an evening recording into the next day."""
    conn = FakeConn(results=[[{"sessions": 0, "duration_s": 0}]])
    recordings.day_stats(conn, "co-1", "Ben_UCPK2", "2026-07-31")

    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "started_at" not in sql
    assert "LIKE %s ESCAPE '\\'" in sql
    # folder underscores escaped (they are LIKE wildcards), date a fixed segment
    assert params == ("co-1", r"users/Ben\_UCPK2/%/2026-07-31/%")


def test_day_stats_counts_only_audio_and_video():
    conn = FakeConn(results=[[{"sessions": 0, "duration_s": 0}]])
    recordings.day_stats(conn, "co-1", "Ben_UCPK2", "2026-07-31")

    assert "kind IN ('audio','video')" in conn.calls[0]["sql"]


def test_day_stats_scopes_to_company():
    conn = FakeConn(results=[[{"sessions": 0, "duration_s": 0}]])
    recordings.day_stats(conn, "co-1", "Ben_UCPK2", "2026-07-31")

    assert "company_id = %s" in conn.calls[0]["sql"]
    assert conn.calls[0]["params"][0] == "co-1"


def test_day_stats_empty_day_is_zero_not_none():
    """A day with no recordings is an honest zero — the caller distinguishes
    "no recordings" from "metric unavailable" by whether it asked at all."""
    conn = FakeConn(results=[[{"sessions": 0, "duration_s": None}]])

    assert recordings.day_stats(conn, "co-1", "Nobody", "2026-07-31") == {
        "sessions": 0, "duration_s": 0}


def test_day_stats_no_row_returns_zeros():
    conn = FakeConn(results=[[]])

    assert recordings.day_stats(conn, "co-1", "Nobody", "2026-07-31") == {
        "sessions": 0, "duration_s": 0}
