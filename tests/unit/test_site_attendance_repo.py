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

# The exact statement `on_roster_profile_ids` executed for #969, before the derived roster
# (2026-09-30 plan Task 1), whitespace-normalised the way FakeCursor normalises every call
# (" ".join(sql.split())). `derived=False` must run this verbatim -- byte-for-byte, not
# "equivalent" -- forever, since it is the switch's rollback.
_ROSTER_SQL_969 = " ".join("""
    SELECT DISTINCT p.id FROM speaker_voiceprints p
    JOIN site_attendance a ON a.company_id = p.company_id
      AND (p.id = a.voiceprint_id
           OR (p.user_id IS NOT NULL AND p.user_id = a.user_id)
           OR lower(p.display_name) = lower(a.display_name))
    WHERE p.company_id = %s AND p.status <> 'withdrawn'
      AND a.site_id = %s AND a.attend_date = %s
""".split())


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


# ---- derived roster (2026-09-30 plan, Task 1) --------------------------------


def test_off_runs_exactly_the_969_query():
    """`derived=False` is the rollback: it must run #969's statement byte-for-byte, with
    #969's own positional params -- not a derived query that happens to return the same
    rows today."""
    conn = FakeConn(results=[[{"id": "vp-1"}]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30", derived=False)
    assert len(conn.calls) == 1
    call = conn.calls[0]
    assert call["sql"] == _ROSTER_SQL_969, call["sql"]
    assert call["params"] == (CO, SITE, "2026-09-30"), call["params"]


def test_derived_is_the_default():
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "FROM recordings" in sql
    assert "FROM meeting_session" in sql


def test_lookback_default_is_fourteen():
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    assert conn.calls[0]["params"]["lookback"] == 14, conn.calls[0]["params"]


def test_company_id_is_still_required_on_the_derived_path():
    with pytest.raises(ValueError):
        site_attendance.on_roster_profile_ids(FakeConn(), None, SITE, "2026-09-30")


# ---- arm 2: recorded at this site, this NZ day (Task 2) ----------------------


def test_arm_2_dates_by_the_key_segment_not_started_at():
    """Correction 1: `recordings` has no date column. The day is the S3 key's date
    segment (`split_part(r.s3_key, '/', 4)`); `started_at` (UTC, device wall clock only
    coincidentally) must not appear anywhere in the derived statement."""
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "split_part(r.s3_key, '/', 4) = %(day)s" in sql
    assert "started_at" not in sql


def test_arm_2_scopes_site_company_and_kind():
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "r.site_id = %(site)s" in sql
    assert "r.company_id = %(co)s" in sql
    assert "r.kind IN ('audio', 'video')" in sql


def test_arm_2_matches_the_recorder_by_account_or_by_name():
    """Correction 5: most profiles have `user_id IS NULL`, so the recorder arm must also
    match by name via `concat_ws` (not `||`, which is NULL-poisoned by a missing last name,
    `users.py:278`)."""
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "p.user_id = r.user_id" in sql
    assert "lower(concat_ws(' ', u.first_name, u.last_name))" in sql


def test_arm_2_excludes_withdrawn_profiles():
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert sql.count("status <> 'withdrawn'") >= 2, (
        "both the explicit arm and arm 2 must exclude withdrawn profiles")


# ---- arm 3: named at this site in the last N NZ days (Task 3) ----------------


def test_arm_3_session_base_and_human_source():
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "t.session_base = 'sid' || ms.session_id" in sql
    assert "t.source = 'correction'" in sql
    assert "t.superseded_at IS NULL" in sql


def test_arm_3_anchors_on_the_roster_day_not_the_clock():
    """Correction 3: the lookback is anchored on `attend_date`, never `now()` or
    `CURRENT_DATE`, so re-running an old session is reproducible."""
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "AT TIME ZONE 'Pacific/Auckland'" in sql
    assert "BETWEEN %(day)s::date - %(lookback)s AND %(day)s::date" in sql
    assert "now()" not in sql
    assert "CURRENT_DATE" not in sql


def test_arm_3_name_arm_covers_a_null_voiceprint_id():
    """Correction 4: a correction row's `voiceprint_id` may be NULL (0040 dropped the NOT
    NULL for exactly this case), so the profile join must also match by name."""
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "p.id = t.voiceprint_id" in sql
    assert "t.voiceprint_id IS NULL AND" in sql
    assert "lower(p.display_name) = lower(t.display_name)" in sql


def test_arm_3_site_test_falls_back_to_a_recording_of_the_session():
    """Correction 2: `meeting_session.site_id` is NULL for every offline-opened session, so
    the site test must also try a recording of that session (BUG-41 authority)."""
    conn = FakeConn(results=[[]])
    site_attendance.on_roster_profile_ids(conn, CO, SITE, "2026-09-30")
    sql = conn.calls[0]["sql"]
    assert "ms.site_id = %(site)s OR EXISTS (SELECT 1 FROM recordings" in sql
    assert "r2.company_id = ms.company_id" in sql
    assert "r2.user_id = ms.user_id" in sql
    assert "r2.site_id = %(site)s" in sql
    assert "ESCAPE '\\'" in sql


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


def test_the_key_date_comparison_casts_the_bound_day_to_text():
    """`split_part(s3_key, '/', 4)` is text; the caller binds a date. Postgres refuses
    text = date ("operator does not exist"), and the whole roster query fails. A Data API
    run with the day pasted in as a string literal did not catch it; CI's real parameters
    did (2026-09-30)."""
    import inspect
    from repositories import site_attendance
    sql = " ".join(inspect.getsource(site_attendance).split())
    assert "split_part(r.s3_key, '/', 4) = (%(day)s)::date::text" in sql
