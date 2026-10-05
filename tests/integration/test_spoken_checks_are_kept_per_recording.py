"""Spoken checks and their matched checklist, against Postgres (migration 0082).

THE test is `a re-extracted recording replaces only its own checks`.
"""
import uuid

import pytest

from repositories import inspection_windows as iw
from repositories import report_templates as rt

pytestmark = pytest.mark.integration

FOLDER, DATE = "Ben_Lin_Test", "2026-10-05"
CHECKLIST = {"sections": [{"key": "c", "title": "Pre-pour", "purpose": "p", "kind": "checklist",
                           "items": ["Formwork"]}],
             "catch_all": {"key": "o", "title": "Other", "purpose": "o"},
             "excluded_subjects": [], "style": []}


def _setup(db):
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     ("Checks %s" % uuid.uuid4().hex[:6],)).fetchone()[0]
    uid = db.execute("INSERT INTO users (company_id, email, global_role) VALUES (%s, %s, 'gm') "
                     "RETURNING id", (cid, "%s@example.test" % uuid.uuid4().hex)).fetchone()[0]
    t = rt.create(db, cid, "org", None, "pre-pour", "Pre-pour Inspection Checklist", "", "custom", uid)
    rt.add_version(db, t["id"], CHECKLIST, "first", uid)
    return cid, uid, t["id"]


def row(name, start, tid=None, end=None):
    return {"name": name, "kind": "", "start_at": start, "end_at": end,
            "end_source": "said" if end else "recording_stop", "start_quote": "q",
            "template_id": tid, "match_score": 1.0 if tid else 0.0}


def test_THE_a_re_extracted_recording_replaces_only_its_own_checks(db):
    cid, uid, tid = _setup(db)
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [row("pre-pour L1", "10:59:09", tid, "11:02:14")])
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidB", [row("steel L2", "12:00:00")])
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [row("pre-pour L1", "10:59:10", tid, "11:02:14")])
    day = iw.for_day(db, cid, FOLDER, DATE)
    assert [(r["session"], r["name"], r["start_at"], r["template_name"]) for r in day] == [
        ("sidA", "pre-pour L1", "10:59:10", "Pre-pour Inspection Checklist"),
        ("sidB", "steel L2", "12:00:00", None)]


def test_the_candidates_carry_their_current_body(db):
    cid, uid, tid = _setup(db)
    ts = iw.checklist_templates(db, cid, uid)
    assert [(t["name"], t["body"]["sections"][0]["kind"]) for t in ts] == [
        ("Pre-pour Inspection Checklist", "checklist")]


def test_another_company_never_sees_the_checks(db):
    cid, uid, tid = _setup(db)
    other, _, _ = _setup(db)
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [row("pre-pour", "10:59:09", tid)])
    assert iw.for_day(db, other, FOLDER, DATE) == []
    assert len(iw.for_day(db, None, FOLDER, DATE)) == 1, "a platform admin spans companies"
