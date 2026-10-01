"""The day-wide photo rebind's SQL, executed against Postgres.

Every unit test of this branch runs the rebind through a fake connection that
records SQL and never parses it. This repo has shipped two production crashes
that every unit test was green for, both of that shape. So the three queries
the rebind stands on -- the day's topics, the day's replace, a session's span
in local time -- run here for real, plus the migration's keyframe backfill.
"""
import os
import re

import pytest

from repositories import recordings, topics

pytestmark = pytest.mark.integration

DATE = "2026-09-11"
FOLDER = "Ben_A"          # the underscore is a LIKE wildcard unless escaped


def _seed(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('Rebind Co') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES ('sub-rebind', %s, 'rebind@x.com', 'worker') RETURNING id", (cid,)).fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                     (cid,)).fetchone()[0]
    return cid, uid, sid


def _topic(db, sid, title, source, time_range, date=DATE):
    return topics.upsert_topic(db, sid, date, title, source_s3_key=source,
                               time_range=time_range)["id"]


def _photos(db, topic_id):
    return sorted((r[0], r[1]) for r in db.execute(
        "SELECT s3_key, source FROM topic_photos WHERE topic_id = %s", (topic_id,)).fetchall())


# ---- list_day_topics_for_binding ------------------------------------------------

def test_the_days_topics_come_from_both_source_shapes_and_nowhere_else(db):
    _, _, sid = _seed(db)
    a = _topic(db, sid, "A", f"extractions/{FOLDER}/{DATE}/s1.json", "09:00 - 09:10")
    b = _topic(db, sid, "B", f"reports/{DATE}/{FOLDER}/daily_report.json", "08:00 - 08:05")
    _topic(db, sid, "other day", f"extractions/{FOLDER}/2026-09-12/s1.json", "09:00 - 09:10",
           date="2026-09-12")
    # `_` unescaped would match any character: BenXA must not be Ben_A.
    _topic(db, sid, "other folder", f"extractions/BenXA/{DATE}/s1.json", "09:00 - 09:10")

    rows = topics.list_day_topics_for_binding(db, FOLDER, DATE)
    assert [r["id"] for r in rows] == [b, a], "both shapes, ordered by time_range"


# ---- replace_day_photo_bindings ---------------------------------------------------

def test_the_replace_rebuilds_machine_rows_and_leaves_keyframes_and_humans(db):
    _, _, sid = _seed(db)
    t1 = _topic(db, sid, "T1", f"extractions/{FOLDER}/{DATE}/s1.json", "09:00 - 09:10")
    t2 = _topic(db, sid, "T2", f"extractions/{FOLDER}/{DATE}/s2.json", "09:20 - 09:30")
    elsewhere = _topic(db, sid, "Other day", f"extractions/{FOLDER}/2026-09-12/s1.json",
                       "09:00 - 09:10", date="2026-09-12")

    photo = f"users/{FOLDER}/pictures/{DATE}/p.jpg"
    # The defect: one photo held by two sessions' topics.
    for tid in (t1, t2, elsewhere):
        db.execute("INSERT INTO topic_photos (topic_id, s3_key) VALUES (%s, %s)", (tid, photo))
    # A keyframe through its REAL writer, and a human bind.
    kf = f"users/{FOLDER}/pictures/{DATE}/dev_{DATE}_09-05-00_kf_s090000.jpg"
    assert topics.add_topic_photo_if_absent(db, t1, kf, "Auto keyframe") is True
    db.execute("INSERT INTO topic_photos (topic_id, s3_key, source) VALUES (%s, %s, 'human')",
               (t2, "users/x/pictures/manual.jpg"))

    deleted = topics.replace_day_photo_bindings(
        db, FOLDER, DATE, [{"topic_id": t1, "s3_key": photo, "caption_text": None}])

    assert deleted == 2, "the day's two machine rows, not the other day's"
    assert _photos(db, t1) == sorted([(photo, "binding"), (kf, "keyframe")])
    assert _photos(db, t2) == [("users/x/pictures/manual.jpg", "human")]
    assert _photos(db, elsewhere) == [(photo, "binding")], "another day is not this day's to clear"


# ---- recordings.session_local_span ---------------------------------------------------

def test_a_sessions_span_is_read_in_the_local_clock_of_its_filenames(db):
    cid, uid, sid = _seed(db)
    session = "sid" + "c" * 32
    for i, (hhmmss, dur) in enumerate((("15-22-34", 300), ("15-30-00", None))):
        key = f"users/{FOLDER}/audio/{DATE}/{FOLDER}_{DATE}_{hhmmss}_{session[3:]}.wav"
        db.execute(
            "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
            "started_at, duration_s) VALUES (%s, %s, %s, 'audio', %s, %s, now(), %s)",
            (cid, uid, sid, key, f"cu-span-{i}", dur))
    # started_at is now() in UTC; the answer must not come from it.
    assert recordings.session_local_span(db, cid, FOLDER, DATE, session) == (15 * 60 + 22,
                                                                             15 * 60 + 30)
    assert recordings.session_local_span(db, cid, FOLDER, DATE, "sid" + "d" * 32) is None


# ---- migration 0063's backfill --------------------------------------------------------

def _backfill_sql():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations",
                        "0063_topic_photo_source.sql")
    with open(path, encoding="utf-8") as fh:
        sql = fh.read()
    m = re.search(r"^UPDATE topic_photos .*?;", sql, re.MULTILINE | re.DOTALL)
    assert m, "the backfill statement is in the migration"
    return m.group(0)


def test_the_backfill_marks_an_existing_keyframe_and_nothing_else(db):
    _, _, sid = _seed(db)
    t = _topic(db, sid, "T", f"extractions/{FOLDER}/{DATE}/s1.json", "09:00 - 09:10")
    kf = f"users/{FOLDER}/pictures/{DATE}/dev_{DATE}_09-05-00_kf_s090000.jpg"
    # `_` is a wildcard: an unescaped '%_kf_%' would also match 'xkfy'.
    lookalike = f"users/{FOLDER}/pictures/{DATE}/dev_{DATE}_09-06-00xkfyphoto.jpg"
    for key in (kf, lookalike):
        db.execute("INSERT INTO topic_photos (topic_id, s3_key, source) "
                   "VALUES (%s, %s, 'binding')", (t, key))
    db.execute(_backfill_sql())
    assert _photos(db, t) == sorted([(kf, "keyframe"), (lookalike, "binding")])


# ---- the location markers decide first ------------------------------------------

def test_the_rebind_binds_by_location_through_the_real_markers_table(db):
    """The marker read and its savepoint run for real (owner, 2026-10-01): an
    inspection walk keeps the photos an interrupting chat overlapped."""
    import photo_rebind
    from repositories import location_markers
    cid, _, sid = _seed(db)
    walk = _topic(db, sid, "walk", f"extractions/{FOLDER}/{DATE}/s1.json", "13:24 - 13:27")
    chat = _topic(db, sid, "chat", f"extractions/{FOLDER}/{DATE}/s1.json", "13:27 - 13:28")
    location_markers.replace_for_day(db, cid, FOLDER, DATE, [
        {"at": "13:24", "location": "Ground floor"}, {"at": "13:26", "location": "Level 1"}])
    shots = [{"key": f"users/{FOLDER}/pictures/{DATE}/p{i}.jpg", "filename": f"p{i}.jpg",
              "hhmm": hhmm} for i, hhmm in enumerate(["13:25", "13:28", "13:28"])]
    assert photo_rebind.rebind_day_photos(db, cid, FOLDER, DATE, shots) == 3
    assert len(_photos(db, walk)) == 3 and _photos(db, chat) == []
