"""
Unit: recordings.site_for_media LIKE-pattern construction — SP-Ask G5b.
user_folder and session_base contain '_' (a SQL LIKE wildcard) and MUST be
escaped, or the match would hit unrelated s3_keys. Real match/company/null
semantics are covered by tests/integration/test_recordings_repo.py (real DB).
FakeConn/FakeCursor record each execute() call; cursor() accepts row_factory.
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


# ---------------------------------------------------------------------------
# site_for_day -- report-level sibling of site_for_media (no session_base;
# matches the whole day for a folder). Same safety properties: company
# double-scope via the sites join, r.site_id IS NOT NULL, LIKE with
# _escape_like on the folder, ESCAPE '\'.
# ---------------------------------------------------------------------------

def test_site_for_day_escapes_like_wildcards_and_scopes_by_company(monkeypatch):
    conn = FakeConn(results=[[{"site_id": "site-1", "cnt": 3, "latest": "t3"}]])
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: {"id": sid, "company_id": "co-1"})

    site = recordings.site_for_day(conn, "co-1", "Ben_Lin", "2026-07-16")

    assert site == {"id": "site-1", "company_id": "co-1"}
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "LIKE %s ESCAPE '\\'" in sql
    assert "LIMIT 1" in sql
    assert "r.company_id = %s" in sql and "s.company_id = %s" in sql
    assert "r.site_id IS NOT NULL" in sql
    assert params == ("co-1", "co-1", r"users/Ben\_Lin/%/2026-07-16/%")


def test_site_for_day_no_match_returns_none_and_skips_get_site(monkeypatch):
    conn = FakeConn(results=[[]])
    called = []
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: called.append(sid))

    assert recordings.site_for_day(conn, "co-1", "Ben_Lin", "2026-07-16") is None
    assert called == []


def test_site_for_day_picks_site_with_most_recordings(monkeypatch):
    # SQL does the majority-vote grouping/ordering itself; the fake DB just
    # returns whatever the (correctly-ordered) query would return -- this
    # test asserts the repo function trusts the first row and the query is
    # shaped to order by count then recency (see SQL-shape assertions above).
    conn = FakeConn(results=[[{"site_id": "site-majority", "cnt": 5, "latest": "t5"}]])
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: {"id": sid})

    site = recordings.site_for_day(conn, "co-1", "Ben_Lin", "2026-07-16")

    assert site == {"id": "site-majority"}
    sql = conn.calls[0]["sql"]
    assert "GROUP BY r.site_id" in sql
    assert "ORDER BY cnt DESC, latest DESC" in sql


def test_site_for_day_tie_breaks_by_most_recent_created_at(monkeypatch):
    # Tie-break is expressed in the SQL ORDER BY (cnt DESC, latest DESC);
    # confirm the query shape carries both keys in that order.
    conn = FakeConn(results=[[{"site_id": "site-newer", "cnt": 2, "latest": "t9"}]])
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: {"id": sid})

    site = recordings.site_for_day(conn, "co-1", "Ben_Lin", "2026-07-16")

    assert site == {"id": "site-newer"}
    sql = conn.calls[0]["sql"]
    assert sql.index("ORDER BY cnt DESC, latest DESC") > sql.index("GROUP BY r.site_id")
