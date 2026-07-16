"""
Unit: recordings.site_for_media LIKE-pattern construction — SP-Ask G5b.
user_folder and session_base contain '_' (a SQL LIKE wildcard) and MUST be
escaped, or the match would hit unrelated s3_keys. Real match/company/null
semantics are covered by tests/integration/test_recordings_repo.py (real DB).
FakeConn/FakeCursor record each execute() call; cursor() accepts row_factory.
"""
import pytest

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


def test_site_for_media_escapes_like_wildcards_in_pattern(monkeypatch):
    # match row then sites.get_site row; stub get_site so the test isolates the query
    conn = FakeConn(results=[[{"site_id": "site-1"}]])
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: {"id": sid, "company_id": "co-1"})

    site = recordings.site_for_media(
        conn, "co-1", "Ben_Lin", "2026-07-16", "Ben_Lin_2026-07-16_09-50-00")

    assert site == {"id": "site-1", "company_id": "co-1"}
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "LIKE %s ESCAPE '\\'" in sql
    assert "ORDER BY r.created_at DESC" in sql and "LIMIT 1" in sql
    assert "r.company_id = %s" in sql and "s.company_id = %s" in sql
    assert "r.site_id IS NOT NULL" in sql
    # underscores in folder AND session_base escaped; date is a fixed literal
    assert params == (
        "co-1", "co-1",
        r"users/Ben\_Lin/%/2026-07-16/Ben\_Lin\_2026-07-16\_09-50-00.%",
    )


def test_site_for_media_no_match_returns_none_and_skips_get_site(monkeypatch):
    conn = FakeConn(results=[[]])  # no matching recording
    called = []
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: called.append(sid))

    assert recordings.site_for_media(
        conn, "co-1", "Ben_Lin", "2026-07-16", "Ben_Lin_2026-07-16_09-50-00") is None
    assert called == []
