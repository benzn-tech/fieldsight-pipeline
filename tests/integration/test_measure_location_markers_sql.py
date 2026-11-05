"""Integration: scripts/measure_location_markers.py's SELECTs against a real
Postgres (Track L Task 1; skipped without TEST_DATABASE_URL -- a skip is NOT a
pass).

The script sends its SQL through the RDS Data API. Here the SAME strings go
through psycopg (they carry no parameters, and every column is cast to text,
so both drivers hand back the same Python values), and `measure` is asserted
on a hand-built day: what counts as a topic-day, that a superseded topic and
an agent topic do not, and that photos are placed by the day's markers.
"""
import datetime
import uuid

import pytest

import scripts.measure_location_markers as mlm

pytestmark = pytest.mark.integration

FOLDER = "Track_L_Walker"


def _seed(db):
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     ("Measure Co %s" % uuid.uuid4().hex[:6],)).fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                     (cid,)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES (%s, %s, %s, 'worker') RETURNING id",
        ("sub-" + uuid.uuid4().hex, cid, uuid.uuid4().hex[:8] + "@x.com")).fetchone()[0]
    today = db.execute("SELECT current_date").fetchone()[0]
    day = (today - datetime.timedelta(days=2)).isoformat()
    old = (today - datetime.timedelta(days=60)).isoformat()

    def topic(date, key, time_range, superseded=False):
        db.execute(
            "INSERT INTO topics (site_id, report_date, title, source_s3_key, time_range, superseded_at) "
            "VALUES (%s, %s, 't', %s, %s, %s)",
            (sid, date, key, time_range, datetime.datetime.now(datetime.timezone.utc) if superseded else None))

    ext = "extractions/%s/%s/s.json" % (FOLDER, day)
    topic(day, ext, "09:05–09:20")           # after the 09:00 marker -> placed
    topic(day, ext, "08:30–08:40")           # before any marker -> not placed
    topic(day, ext, "11:00–11:05", superseded=True)   # not counted at all
    topic(day, "agent://pack/1", "09:10–09:12")       # no folder -> not a topic-day
    topic(old, "extractions/%s/%s/s.json" % (FOLDER, old), "09:00–09:10")  # outside window
    db.execute("INSERT INTO day_location_markers (company_id, user_folder, report_date, markers) "
               "VALUES (%s, %s, %s, %s::jsonb)",
               (cid, FOLDER, day, '[{"at": "09:00", "location": "Level 1"}]'))
    for hms in ("09-10-00", "08-00-00"):
        key = "users/%s/pictures/%s/%s_%s_%s.jpg" % (FOLDER, day, FOLDER.lower(), day, hms)
        db.execute("INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
                   "VALUES (%s, %s, %s, 'photo', %s, %s, now())", (cid, uid, sid, key, uuid.uuid4().hex))
    return cid


def test_the_scripts_own_sql_measures_a_hand_built_day(db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(mlm, "run", lambda sql, database: [list(r) for r in db.execute(sql).fetchall()])
    report, private = mlm.measure("ignored", 30)
    line = report[-1]
    assert "topic-days 1 |" in line
    assert "photo-days 1 |" in line
    assert "marker-days 1 (100.0% of topic-days, 100.0% of photo-days)" in line
    assert "topics placed 50.0%" in line                    # 1 of the 2 live topics
    assert "photos 2 (2 on marker-days), placed 50.0% (locate) / 50.0% (place_at), disagree 0" in line
    assert any("Level 1" in p for p in private)
    assert not any("Level 1" in r for r in report)          # raw speech never in the printable part


def test_every_statement_is_a_select():
    for sql in (mlm.SQL_TOPICS, mlm.SQL_MARKERS, mlm.SQL_PHOTOS, mlm.SQL_COMPANIES):
        assert sql.strip().upper().startswith("SELECT")
