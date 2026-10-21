"""Check done (step A): the DB hop's two new queries against Postgres.

The unit doubles record SQL and never parse it, so the session pick's interval
arithmetic and the uuid[] filter are only proven here.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from repositories import inspection_windows as iw
from repositories import meeting_session as ms
from repositories import report_templates as rt

pytestmark = pytest.mark.integration

PRESSED = datetime(2026, 10, 19, 21, 5, 0, tzinfo=timezone.utc)
CHECKLIST = {"sections": [{"key": "c", "title": "Pre-pour", "purpose": "p", "kind": "checklist",
                           "items": ["Formwork"]}],
             "catch_all": {"key": "o", "title": "Other", "purpose": "o"},
             "excluded_subjects": [], "style": []}


def _company(db):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                      ("Gaps %s" % uuid.uuid4().hex[:6],)).fetchone()[0]


def _user(db, cid):
    return db.execute("INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, 'gm') "
                      "RETURNING id", (cid, "%s@example.test" % uuid.uuid4().hex)).fetchone()[0]


def _session(db, cid, uid, opened, last=None):
    sid = uuid.uuid4().hex
    db.execute("INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
               "opened_at, last_segment_at) VALUES (%s, %s, %s, 'audio', 'open', %s, %s)",
               (sid, cid, uid, opened, last))
    return sid


def test_the_callers_most_recently_active_session_is_picked(db):
    cid = _company(db)
    me, other = _user(db, cid), _user(db, cid)
    older = _session(db, cid, me, PRESSED - timedelta(minutes=30), PRESSED - timedelta(minutes=4))
    live = _session(db, cid, me, PRESSED - timedelta(minutes=20), PRESSED - timedelta(seconds=10))
    _session(db, cid, other, PRESSED - timedelta(minutes=5), PRESSED)        # newer, not mine
    row = ms.latest_for_user_near(db, me, int(PRESSED.timestamp()))
    assert row["session_id"] == live and row["session_id"] != older


def test_a_session_idle_for_longer_than_five_minutes_is_not_live(db):
    cid = _company(db)
    me = _user(db, cid)
    _session(db, cid, me, PRESSED - timedelta(minutes=40), PRESSED - timedelta(minutes=6))
    assert ms.latest_for_user_near(db, me, int(PRESSED.timestamp())) is None


def test_a_session_with_no_chunk_yet_is_found_by_its_open_time(db):
    cid = _company(db)
    me = _user(db, cid)
    fresh = _session(db, cid, me, PRESSED - timedelta(seconds=40), None)
    assert ms.latest_for_user_near(db, me, int(PRESSED.timestamp()))["session_id"] == fresh


def test_live_windows_are_this_folders_and_this_companys_only(db):
    cid, other = _company(db), _company(db)
    uid = _user(db, cid)
    t = rt.create(db, cid, "org", None, "pre-pour", "Pre-pour Inspection Checklist", "", "custom", uid)
    rt.add_version(db, t["id"], CHECKLIST, "first", uid)
    row = {"name": "pre-pour", "kind": "", "start_at": "10:01:00", "end_at": None,
           "end_source": "recording_stop", "start_quote": "q", "template_id": t["id"], "match_score": 1.0}
    iw.replace_for_session(db, cid, "Ben", "2026-10-20", "sidA", [row])
    # Raw insert: replace_for_session replaces by (company, session), so a second
    # folder under the same session base can only be planted by hand.
    db.execute("INSERT INTO inspection_windows (company_id, user_folder, report_date, session, name, "
               "start_at, end_source) VALUES (%s, 'Someone', '2026-10-20', 'sidA', 'theirs', "
               "'10:02:00', 'recording_stop')", (cid,))
    iw.replace_for_session(db, other, "Ben", "2026-10-20", "sidA", [dict(row, name="other co", template_id=None)])
    got = iw.for_session(db, [cid], "Ben", "sidA")
    assert [(r["name"], str(r["template_id"]), str(r["report_date"])) for r in got] == [
        ("pre-pour", str(t["id"]), "2026-10-20")]
    assert len(iw.for_session(db, [cid, other], "Ben", "sidA")) == 2
    assert iw.for_session(db, [cid], "Ben", "sidB") == []
