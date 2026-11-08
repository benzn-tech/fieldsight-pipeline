"""The final email request picks up the day's markers, against Postgres
(Track L Task 7 Step 1). `_attach_day_markers` reads `for_day` inside a
savepoint; this runs that read for real, and checks that a failed read leaves
the surrounding transaction usable -- the extraction's own writes still have to
commit after it.
"""
import pytest

from repositories import location_markers

pytestmark = pytest.mark.integration

FOLDER, DATE = "Ben_Lin_Test", "2026-11-09"


def _company(db, name="Email Where Co"):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()[0]


def test_THE_the_sessions_own_markers_reach_the_request(db):
    import lambda_item_writer as iw
    cid = _company(db)
    location_markers.replace_for_session(db, cid, FOLDER, DATE, "sidA",
                                         [{"at": "09:00", "location": "Level 2"}])
    ctx = {"kind": "final"}
    iw._attach_day_markers(db, ctx, cid, FOLDER, DATE)
    assert [(m["at"], m["location"]) for m in ctx["locationMarkers"]] == [("09:00", "Level 2")]


def test_another_companys_markers_are_not_attached(db):
    import lambda_item_writer as iw
    cid = _company(db)
    other = _company(db, "Someone Else Co")
    location_markers.replace_for_session(db, other, FOLDER, DATE, "sidA",
                                         [{"at": "09:00", "location": "Level 2"}])
    ctx = {"kind": "final"}
    iw._attach_day_markers(db, ctx, cid, FOLDER, DATE)
    assert "locationMarkers" not in ctx


def test_a_failed_read_leaves_the_transaction_usable(db, monkeypatch):
    import lambda_item_writer as iw

    def bad_sql(conn, *a):
        conn.execute("SELECT * FROM no_such_table")
    monkeypatch.setattr(iw.location_markers, "for_day", bad_sql)
    ctx = {"kind": "final"}
    iw._attach_day_markers(db, ctx, None, FOLDER, DATE)
    assert "locationMarkers" not in ctx
    assert db.execute("SELECT 1").fetchone()[0] == 1
