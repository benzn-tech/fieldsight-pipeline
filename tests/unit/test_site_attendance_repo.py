"""Tests for src/repositories/site_attendance.py — the roster the voice matcher narrows
against (plan Task 2).

FakeConn/FakeCursor copied from test_voiceprints_repo.py, results served POSITIONALLY:
⚠️ adding any query near the front of a function under test shifts every queued result
after it.
"""
import pytest

from repositories import site_attendance

CO = "11111111-1111-1111-1111-111111111111"
SITE = "33333333-3333-3333-3333-333333333333"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": " ".join(sql.split()), "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


# ---- upsert ----------------------------------------------------------------


def test_manual_source_ref_is_the_normalised_name(monkeypatch):
    monkeypatch.setattr("repositories.users.resolve_display_name",
                        lambda conn, co, name: (None, "not-in-directory"))
    conn = FakeConn(results=[[], [{"inserted": True}]])
    site_attendance.upsert(conn, CO, SITE, "2026-09-30",
                           [{"displayName": "  Sam Yu  "}], source="manual")
    insert = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    assert insert["params"][8] == "sam yu", insert["params"]


def test_ambiguous_name_stores_user_id_null(monkeypatch):
    monkeypatch.setattr("repositories.users.resolve_display_name",
                        lambda conn, co, name: (None, "ambiguous"))
    conn = FakeConn(results=[[], [{"inserted": True}]])
    site_attendance.upsert(conn, CO, SITE, "2026-09-30",
                           [{"displayName": "Sam Yu"}], source="manual")
    insert = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    # (company_id, site_id, attend_date, display_name, user_id, voiceprint_id, ...)
    assert insert["params"][4] is None, insert["params"]


def test_the_conflict_target_is_the_migrations_unique_index(monkeypatch):
    monkeypatch.setattr("repositories.users.resolve_display_name",
                        lambda conn, co, name: (None, "not-in-directory"))
    conn = FakeConn(results=[[], [{"inserted": True}]])
    site_attendance.upsert(conn, CO, SITE, "2026-09-30",
                           [{"displayName": "Sam Yu"}], source="manual")
    insert = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    assert "ON CONFLICT (company_id, source, source_ref, attend_date)" in insert["sql"]


def test_a_connector_row_carries_its_own_source_ref_and_hints(monkeypatch):
    """Phase 2/3's attendance_upsert op passes an already-keyed row, never routed through
    directory resolution -- resolve_display_name must not be called."""
    called = []
    monkeypatch.setattr("repositories.users.resolve_display_name",
                        lambda conn, co, name: called.append(name) or (None, "x"))
    conn = FakeConn(results=[[{"inserted": True}]])
    site_attendance.upsert(conn, CO, SITE, "2026-09-30",
                           [{"displayName": "Sam Yu", "sourceRef": "evt-1:sam@x.com",
                             "userId": "u-1", "voiceprintId": "vp-1"}],
                           source="graph_calendar")
    assert not called, "a connector row must not be re-resolved through the directory"
    insert = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    assert insert["params"][8] == "evt-1:sam@x.com"
    assert insert["params"][4] == "u-1" and insert["params"][5] == "vp-1"


def test_blank_names_are_skipped(monkeypatch):
    monkeypatch.setattr("repositories.users.resolve_display_name",
                        lambda conn, co, name: (None, "not-in-directory"))
    conn = FakeConn()
    out = site_attendance.upsert(conn, CO, SITE, "2026-09-30",
                                 [{"displayName": "   "}], source="manual")
    assert out == {"inserted": 0, "updated": 0}
    assert not conn.calls


def test_upsert_requires_company_id():
    with pytest.raises(ValueError):
        site_attendance.upsert(FakeConn(), None, SITE, "2026-09-30", [], source="manual")


# ---- on_roster_profile_ids --------------------------------------------------


def test_on_roster_profile_ids_has_all_three_arms_and_excludes_withdrawn():
    conn = FakeConn(results=[[{"id": "vp-1"}]])
    ids = site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "p.id = a.voiceprint_id" in sql
    assert "p.user_id = a.user_id" in sql
    assert "lower(p.display_name) = lower(a.display_name)" in sql
    assert "status <> 'withdrawn'" in sql
    assert ids == {"vp-1"}


def test_on_roster_profile_ids_empty_roster_returns_empty_set():
    conn = FakeConn(results=[[]])
    assert site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30") == set()


def test_on_roster_profile_ids_requires_company_id():
    with pytest.raises(ValueError):
        site_attendance.on_roster_profile_ids(FakeConn(), None, SITE, "2026-09-30")


# ---- for_day / remove --------------------------------------------------------


def test_for_day_requires_company_id():
    with pytest.raises(ValueError):
        site_attendance.for_day(FakeConn(), None, SITE, "2026-09-30")


def test_remove_only_deletes_manual_rows():
    conn = FakeConn(results=[[{"id": "row-1"}]])
    n = site_attendance.remove(conn, CO, SITE, "2026-09-30", "row-1")
    assert n == 1
    assert "source = 'manual'" in conn.calls[0]["sql"]


def test_remove_requires_company_id():
    with pytest.raises(ValueError):
        site_attendance.remove(FakeConn(), None, SITE, "2026-09-30", "row-1")
