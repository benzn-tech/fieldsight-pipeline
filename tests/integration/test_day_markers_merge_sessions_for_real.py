"""A day's location markers are the union of its sessions, run against Postgres.

Prod, Ben_Lin_Test 2026-10-02: the 12:24 "level two" marker from one session
was erased by a later session that announced no place -- the writer replaced
the DAY with one SESSION's markers, and the photo rules went dark.

THE test is `a later session with no places leaves the earlier session's
places alone`.
"""
import pytest

from repositories import location_markers

pytestmark = pytest.mark.integration

FOLDER, DATE = "Ben_Lin_Test", "2026-10-02"


def _company(db):
    return db.execute("INSERT INTO companies (name) VALUES ('Markers Co') RETURNING id").fetchone()[0]


def _day(db, cid):
    return [(m["at"], m["location"], m.get("session"))
            for m in location_markers.for_day(db, cid, FOLDER, DATE)]


def test_THE_a_later_session_with_no_places_leaves_the_earlier_sessions_places_alone(db):
    cid = _company(db)
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidA",
                                         [{"at": "12:24", "location": "level two"}])
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidC", [])
    assert _day(db, cid) == [("12:24", "level two", "sidA")]


def test_sessions_merge_in_time_order_and_a_rerun_replaces_only_its_own(db):
    cid = _company(db)
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidB",
                                         [{"at": "12:44", "location": "Te Kaha room"}])
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidA",
                                         [{"at": "12:22", "location": "level one"},
                                          {"at": "12:24", "location": "level two"}])
    assert [a for a, _, _ in _day(db, cid)] == ["12:22", "12:24", "12:44"]
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidA",
                                         [{"at": "12:23", "location": "level one"}])
    assert _day(db, cid) == [("12:23", "level one", "sidA"), ("12:44", "Te Kaha room", "sidB")]


def test_markers_written_before_sessions_were_recorded_are_kept_unless_restated(db):
    cid = _company(db)
    location_markers.replace_for_day(db, cid, FOLDER, DATE,
                                     [{"at": "09:00", "location": "Gate"},
                                      {"at": "12:24", "location": "Level Two"}])
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidA",
                                         [{"at": "12:24", "location": "level two"}])
    assert _day(db, cid) == [("09:00", "Gate", None), ("12:24", "level two", "sidA")]


def test_the_restore_task_dry_runs_then_writes(db, monkeypatch):
    import lambda_org_api as org

    class Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *a):
            return False
    cid = _company(db)
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: Ctx())
    import photo_collapse
    monkeypatch.setattr(photo_collapse, "company_of", lambda conn, f: cid)
    event = {"task": "restore_day_markers", "folder": FOLDER, "date": DATE,
             "sessions": {"sidA": [{"at": "12:24", "location": "level two"}], "sidC": []}}
    dry = org.lambda_handler(event, None)
    assert dry["applied"] is False and [m["at"] for m in dry["markers"]] == ["12:24"]
    assert _day(db, cid) == [], "the dry run wrote nothing"
    done = org.lambda_handler(dict(event, apply=True), None)
    assert done["applied"] is True and _day(db, cid) == [("12:24", "level two", "sidA")]
