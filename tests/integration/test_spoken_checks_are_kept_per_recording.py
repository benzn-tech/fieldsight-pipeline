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


def test_stretches_are_kept_and_an_old_row_reads_as_one(db):
    cid, uid, tid = _setup(db)
    two = dict(row("pre-pour", "11:02:03", tid, "11:03:21"),
               segments=[{"from": "11:02:03", "to": "11:02:21"}, {"from": "11:02:41", "to": "11:03:21"}])
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidA", [two])
    iw.replace_for_session(db, cid, FOLDER, DATE, "sidB", [row("steel", "12:00:00", None, "12:10:00")])
    a, b = iw.for_day(db, cid, FOLDER, DATE)
    assert a["segments"] == two["segments"]
    assert b["segments"] == [{"from": "12:00:00", "to": "12:10:00"}]


def test_a_checklist_report_is_recorded_once_and_listed(db):
    import checklist_reports as cr
    cid, uid, tid = _setup(db)
    w = dict(row("pre-pour", "11:02:03", tid, "11:03:21"),
             segments=[{"from": "11:02:03", "to": "11:03:21"}])
    segs = [{"from": "11:02:03", "to": "11:03:21"}]
    assert not cr.already_made(db, "sidA", tid, "11:02:03", "11:03:21", segs)
    early = dict(w, end_at="11:02:30", segments=[{"from": "11:02:03", "to": "11:02:30"}])
    cr._record(db, cid, FOLDER, DATE, "sidA", early, "Pre-pour Inspection Checklist", "r1", "k1")
    assert cr.already_made(db, "sidA", tid, "11:02:03", "11:02:30", early["segments"])
    assert not cr.already_made(db, "sidA", tid, "11:02:03", "11:03:21", segs), "a fuller window"
    cr._record(db, cid, FOLDER, DATE, "sidA", w, "Pre-pour Inspection Checklist", "r2", "k2")
    day = cr.for_day(db, cid, FOLDER, DATE)
    assert [(r["request_id"], r["end_at"], r["segments"]) for r in day] == [("r2", "11:03:21", segs)]
    assert [r["request_id"] for r in cr.recent_for_folder(db, cid, FOLDER, "2026-10-01")] == ["r2"]


def test_two_recordings_of_one_walk_share_one_walk_key(db):
    import lambda_item_writer as lam
    cid, uid, tid = _setup(db)
    opening = {"name": "pre-pour lots", "kind": "pre-pour", "start_at": "10:00",
               "start_at_s": "10:00:04", "end_at": "10:20", "end_at_s": "10:20:00",
               "end_source": "recording_stop", "start_quote": "q",
               "units": {"names": ["Lot 150", "Lot 151", "Lot 152"], "count": 3}}
    later = {"name": "pre-pour", "kind": "pre-pour", "start_at": "11:00",
             "start_at_s": "11:00:09", "end_at": None, "end_source": "recording_stop",
             "start_quote": "q"}
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidA", [opening])
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidB", [later])
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidA", [opening])   # re-extracted
    day = iw.for_day(db, cid, FOLDER, DATE)
    keys = [r["walk_key"] for r in day]
    assert [r["session"] for r in day] == ["sidA", "sidB"]
    assert keys[0] and keys[0] == keys[1]
    assert [w["walk_key"] for w in iw.open_walks(db, cid, FOLDER, DATE, tid)] == keys


def test_a_walk_extracted_after_its_later_recording_still_claims_it(db):
    """R13: B (no opening, 10:30) is extracted before A (the opening, 10:00)."""
    import lambda_item_writer as lam
    cid, uid, tid = _setup(db)
    opening = {"name": "pre-pour lots", "kind": "pre-pour", "start_at": "10:00",
               "start_at_s": "10:00:00", "end_source": "recording_stop", "start_quote": "q",
               "units": {"names": ["Lot 150", "Lot 151"], "count": 2}}
    later = {"name": "pre-pour", "kind": "pre-pour", "start_at": "10:30",
             "start_at_s": "10:30:00", "end_source": "recording_stop", "start_quote": "q"}
    earlier = dict(later, start_at="09:30", start_at_s="09:30:00")
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidB", [later])
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidC", [earlier])
    assert [r["walk_key"] for r in iw.for_day(db, cid, FOLDER, DATE)] == [None, None]
    lam._store_inspections(db, cid, uid, FOLDER, DATE, "sidA", [opening])
    day = {r["session"]: r["walk_key"] for r in iw.for_day(db, cid, FOLDER, DATE)}
    import units
    key = units.walk_key(cid, FOLDER, DATE, tid, "10:00:00")
    assert day == {"sidA": key, "sidB": key, "sidC": None}, "a check before the opening is not his walk"
    other, ouid, otid = _setup(db)
    assert iw.backfill_walk(db, other, FOLDER, DATE, otid, "09:00:00", "walk:x", "sidZ") == 0
