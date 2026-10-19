"""Check done (step A), the DB hop: the caller, his live session, the templates
and the session's live check windows -- one in-VPC call, read-only.

Spec: docs/superpowers/specs/2026-10-20-check-done-gaps-design.md ("DB hop")
"""
import json
import uuid
from datetime import datetime, timezone

import lambda_check_gaps_db as hop
from repositories import inspection_windows, meeting_session

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
UID, HOME, SITE_CO, SITE = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


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


def test_the_session_is_the_callers_own_most_recently_active_one():
    conn = FakeConn([[{"session_id": SID}]])
    row = meeting_session.latest_for_user_near(conn, UID, 1760000000)
    assert row == {"session_id": SID}
    call = conn.calls[0]
    sql = call["sql"]
    assert "FROM meeting_session" in sql and "user_id = %s" in sql
    assert "COALESCE(last_segment_at, opened_at)" in sql
    assert "ORDER BY COALESCE(last_segment_at, opened_at) DESC" in sql and "LIMIT 1" in sql
    # Bound by the caller only: no parameter but his id and the clock.
    assert call["params"] == (str(UID), 1760000000, 300, 1760000000, 300)


def test_no_user_id_never_queries():
    conn = FakeConn()
    assert meeting_session.latest_for_user_near(conn, None, 1760000000) is None
    assert conn.calls == []


def test_live_windows_are_filtered_by_company_and_folder_and_session():
    conn = FakeConn([[{"name": "pre-pour", "segments": "[]"}]])
    rows = inspection_windows.for_session(conn, [HOME, SITE_CO], "Ben_Lin", "sid" + SID)
    sql = conn.calls[0]["sql"]
    assert "company_id = ANY(%s::uuid[])" in sql
    assert "user_folder = %s" in sql and "session = %s" in sql
    assert conn.calls[0]["params"] == ([str(HOME), str(SITE_CO)], "Ben_Lin", "sid" + SID)
    assert rows[0]["name"] == "pre-pour"


def test_no_company_reads_no_windows():
    conn = FakeConn()
    assert inspection_windows.for_session(conn, [], "Ben_Lin", "sidx") == []
    assert conn.calls == []


def _wire(monkeypatch, user, session, site=None, templates=None, windows=None,
          media_site=None, day_site=None):
    seen = {}
    monkeypatch.setattr(hop, "get_cached_connection", lambda: "conn")
    monkeypatch.setattr(hop, "close_cached_connection", lambda: seen.setdefault("closed", True))
    monkeypatch.setattr(hop.users, "get_user_by_sub", lambda conn, sub: seen.setdefault("sub", sub) and user)
    monkeypatch.setattr(hop.meeting_session, "latest_for_user_near",
                        lambda conn, uid, at, window_s=300: seen.setdefault("near", (uid, at)) and session)
    monkeypatch.setattr(hop.sites, "get_site", lambda conn, sid: site)
    monkeypatch.setattr(hop.recordings, "site_for_media",
                        lambda conn, uid, folder, date, base: seen.setdefault(
                            "media", (uid, folder, date, base)) and media_site)
    monkeypatch.setattr(hop.recordings, "site_for_day",
                        lambda conn, uid, folder, date: seen.setdefault(
                            "day", (uid, folder, date)) and day_site)

    def tpls(conn, company_id, user_id):
        seen["templates_for"] = (company_id, user_id)
        return templates or []
    monkeypatch.setattr(hop.inspection_windows, "checklist_templates", tpls)

    def wins(conn, company_ids, folder, session_base):
        seen["windows_for"] = (company_ids, folder, session_base)
        return windows or []
    monkeypatch.setattr(hop.inspection_windows, "for_session", wins)
    return seen


USER = {"id": UID, "company_id": HOME, "folder_name": "Ben_Lin", "email": "x@y"}
SESSION = {"session_id": SID, "company_id": HOME, "user_id": UID, "site_id": SITE, "kind": "audio",
           "status": "open", "opened_at": datetime(2026, 10, 19, 20, 50, tzinfo=timezone.utc),
           "last_segment_at": datetime(2026, 10, 19, 21, 4, 50, tzinfo=timezone.utc)}


def test_the_hop_answers_json_safe_facts_with_the_sites_company(monkeypatch):
    seen = _wire(monkeypatch, USER, SESSION, site={"id": SITE, "company_id": SITE_CO},
                 templates=[{"id": uuid.uuid4(), "name": "Pre-pour", "scope": "org", "body": {}}],
                 windows=[{"name": "pre-pour", "template_id": uuid.uuid4(), "start_at": "10:01:00",
                           "report_date": datetime(2026, 10, 20).date()}])
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    json.dumps(out)                       # what Lambda will do with it
    assert seen["sub"] == "sub-1" and seen["near"] == (UID, 1760000000)
    assert out["user"] == {"id": str(UID), "company_id": str(HOME), "folder_name": "Ben_Lin"}
    assert out["owner_company_id"] == str(SITE_CO)
    # Templates are matched the way item-writer matches them: the site's company.
    assert seen["templates_for"] == (SITE_CO, UID)
    assert set(seen["windows_for"][0]) == {str(SITE_CO), str(HOME)}
    assert seen["windows_for"][1:] == ("Ben_Lin", "sid" + SID)
    assert out["session"]["session_id"] == SID and out["session"]["opened_at"].startswith("2026-10-19T20:50")
    assert out["live_windows"][0]["report_date"] == "2026-10-20"
    assert seen["closed"] is True


def test_no_session_site_climbs_item_writers_rungs(monkeypatch):
    """meeting_session.site_id is NULL whenever the device's /open never reached the
    server; item-writer then finds the site from the recordings rows, and so must
    this, or the windows it wrote under the site's company are never found."""
    day = {"id": uuid.uuid4(), "company_id": SITE_CO}
    seen = _wire(monkeypatch, USER, dict(SESSION, site_id=None), day_site=day)
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    # opened 2026-10-19 20:50 UTC == 2026-10-20 09:50 NZDT: the device-date folder.
    assert seen["media"] == (UID, "Ben_Lin", "2026-10-20", "sid" + SID)
    assert seen["day"] == (UID, "Ben_Lin", "2026-10-20")
    assert out["owner_company_id"] == str(SITE_CO)
    assert seen["templates_for"] == (SITE_CO, UID)


def test_the_tagged_recording_wins_over_the_days_majority(monkeypatch):
    media = {"id": uuid.uuid4(), "company_id": SITE_CO}
    seen = _wire(monkeypatch, USER, dict(SESSION, site_id=None), media_site=media,
                 day_site={"id": uuid.uuid4(), "company_id": uuid.uuid4()})
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    assert out["owner_company_id"] == str(SITE_CO) and "day" not in seen


def test_no_site_anywhere_falls_back_to_the_sessions_then_the_users_company(monkeypatch):
    seen = _wire(monkeypatch, USER, dict(SESSION, site_id=None))
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    assert out["owner_company_id"] == str(HOME)


def test_an_unknown_caller_has_no_user_and_nothing_else_is_read(monkeypatch):
    seen = _wire(monkeypatch, None, SESSION)
    out = hop.lambda_handler({"caller_sub": "sub-x", "pressed_at": 1760000000}, None)
    assert out["user"] is None and out["session"] is None and "near" not in seen


def test_no_live_session_reads_no_templates(monkeypatch):
    seen = _wire(monkeypatch, USER, None)
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    assert out["session"] is None and "templates_for" not in seen


def test_a_missing_caller_or_a_db_failure_is_an_error_never_a_raise(monkeypatch):
    seen = _wire(monkeypatch, USER, SESSION)
    assert "error" in hop.lambda_handler({"pressed_at": 1}, None)

    def boom(conn, sub):
        raise RuntimeError("db down")
    monkeypatch.setattr(hop.users, "get_user_by_sub", boom)
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1}, None)
    assert out == {"error": "db"}


def test_no_company_anywhere_reads_no_templates_and_is_not_an_error(monkeypatch):
    # checklist_templates(conn, None, ...) would send the text "None" as a uuid and
    # fail the whole hop; nothing to match against is no_template downstream.
    seen = _wire(monkeypatch, dict(USER, company_id=None),
                 dict(SESSION, site_id=None, company_id=None))
    out = hop.lambda_handler({"caller_sub": "sub-1", "pressed_at": 1760000000}, None)
    assert "error" not in out and out["owner_company_id"] is None
    assert out["templates"] == [] and out["live_windows"] == []
    assert "templates_for" not in seen
