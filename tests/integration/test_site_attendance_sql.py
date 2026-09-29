"""Integration: migration 0072 and repositories/site_attendance.py against real Postgres.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py); rolled back per test (the `db`
fixture). What a connection double cannot prove: the UNIQUE conflict target actually dedupes
a second upsert, the CHECK actually refuses an unknown source, ON DELETE SET NULL actually
survives a profile deletion, and the three-arm join in `on_roster_profile_ids` matches a real
`lower()` comparison.
"""
import pytest

from repositories import site_attendance

pytestmark = pytest.mark.integration


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
        (cid,)).fetchone()[0]
    return cid, sid


def test_unique_dedupes_a_second_upsert(db):
    cid, sid = _seed(db)
    site_attendance.upsert(db, cid, sid, "2026-09-30",
                           [{"displayName": "Sam Yu"}], source="manual")
    site_attendance.upsert(db, cid, sid, "2026-09-30",
                           [{"displayName": "Sam Yu"}], source="manual")
    rows = site_attendance.for_day(db, cid, sid, "2026-09-30")
    assert len(rows) == 1


def test_check_refuses_an_unknown_source(db):
    cid, sid = _seed(db)
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO site_attendance (company_id, site_id, attend_date, display_name, "
            " source, source_ref) VALUES (%s, %s, '2026-09-30', 'X', 'outlook', 'x')",
            (cid, sid))
        db.execute("SELECT 1")  # force the constraint to fire before rollback


def test_voiceprint_id_becomes_null_when_the_profile_is_deleted(db):
    cid, sid = _seed(db)
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, display_name, status) "
        "VALUES (%s, 'Sam Yu', 'confirmed') RETURNING id", (cid,)).fetchone()[0]
    db.execute(
        "INSERT INTO site_attendance (company_id, site_id, attend_date, display_name, "
        " voiceprint_id, source, source_ref) "
        "VALUES (%s, %s, '2026-09-30', 'Sam Yu', %s, 'manual', 'sam yu')",
        (cid, sid, vp))
    db.execute("DELETE FROM speaker_voiceprints WHERE id = %s", (vp,))
    row = db.execute(
        "SELECT voiceprint_id FROM site_attendance WHERE company_id = %s", (cid,)
    ).fetchone()
    assert row[0] is None


def test_on_roster_profile_ids_matches_by_name_when_user_id_is_null(db):
    """A profile with `user_id NULL` and `display_name 'Sam Yu'` -- the common case, most
    profiles today are subcontractors with no directory account (correction 4) -- must be
    found by the NAME arm against a manual row spelled in a different case."""
    cid, sid = _seed(db)
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, display_name, status) "
        "VALUES (%s, 'Sam Yu', 'confirmed') RETURNING id", (cid,)).fetchone()[0]
    site_attendance.upsert(db, cid, sid, "2026-09-30",
                           [{"displayName": "sam yu"}], source="manual")
    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) in ids
