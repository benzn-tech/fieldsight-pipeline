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


# ---- derived roster arm 2: recorder at this site, this NZ day (2026-09-30 Task 2) --------


def _seed_user(db, cid, first, last):
    email = f"{first}.{last}@example.com".lower()
    return db.execute(
        "INSERT INTO users (company_id, cognito_sub, first_name, last_name, email) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (cid, "sub-" + email, first, last, email)).fetchone()[0]


def _seed_recording(db, cid, uid, sid, kind, key, started_at):
    return db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, "
        " client_uuid, started_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (cid, uid, sid, kind, key, key, started_at)).fetchone()[0]


def test_recorder_at_this_site_today_is_on_the_roster(db):
    """Spec test 1. The key's date segment decides, not `started_at`: a recording started
    2026-09-29 18:00 UTC (07:00 NZDT on the 30th) but keyed under 2026-09-30 counts for the
    30th, at this site only."""
    cid, sid = _seed(db)
    sid2 = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s, 'S2') RETURNING id",
        (cid,)).fetchone()[0]
    uid = _seed_user(db, cid, "Sam", "Yu")
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Sam Yu', 'confirmed') RETURNING id", (cid, uid)).fetchone()[0]
    _seed_recording(db, cid, uid, sid, "audio",
                    "users/Sam_Yu/audio/2026-09-30/x_sid" + "a" * 32 + "_c0000.wav",
                    "2026-09-29 18:00+00")

    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) in ids

    ids_other_site = site_attendance.on_roster_profile_ids(db, cid, sid2, "2026-09-30")
    assert str(vp) not in ids_other_site


def test_arm_2_uses_the_key_date_not_the_clock(db):
    """A recording keyed 2026-09-29 but with `started_at` = 2026-09-29 23:30 UTC (12:30
    NZDT on the 30th) is NOT on the 30th's roster -- the key decides, not the clock."""
    cid, sid = _seed(db)
    uid = _seed_user(db, cid, "Sam", "Yu")
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Sam Yu', 'confirmed') RETURNING id", (cid, uid)).fetchone()[0]
    _seed_recording(db, cid, uid, sid, "audio",
                    "users/Sam_Yu/audio/2026-09-29/x_sid" + "b" * 32 + "_c0000.wav",
                    "2026-09-29 23:30+00")

    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) not in ids


def test_recorder_with_an_unlinked_profile_is_found_by_full_name(db):
    cid, sid = _seed(db)
    uid = _seed_user(db, cid, "Sam", "Yu")
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, NULL, 'sam yu', 'confirmed') RETURNING id", (cid,)).fetchone()[0]
    _seed_recording(db, cid, uid, sid, "audio",
                    "users/Sam_Yu/audio/2026-09-30/x_sid" + "c" * 32 + "_c0000.wav",
                    "2026-09-29 18:00+00")

    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) in ids


def test_a_photo_alone_does_not_put_the_recorder_on_the_roster(db):
    cid, sid = _seed(db)
    uid = _seed_user(db, cid, "Sam", "Yu")
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Sam Yu', 'confirmed') RETURNING id", (cid, uid)).fetchone()[0]
    _seed_recording(db, cid, uid, sid, "photo",
                    "users/Sam_Yu/photo/2026-09-30/x_sid" + "d" * 32 + "_c0000.jpg",
                    "2026-09-29 18:00+00")

    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) not in ids


def test_a_withdrawn_recorder_profile_is_never_on_the_roster(db):
    cid, sid = _seed(db)
    uid = _seed_user(db, cid, "Sam", "Yu")
    vp = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
        "VALUES (%s, %s, 'Sam Yu', 'withdrawn') RETURNING id", (cid, uid)).fetchone()[0]
    _seed_recording(db, cid, uid, sid, "audio",
                    "users/Sam_Yu/audio/2026-09-30/x_sid" + "e" * 32 + "_c0000.wav",
                    "2026-09-29 18:00+00")

    ids = site_attendance.on_roster_profile_ids(db, cid, sid, "2026-09-30")
    assert str(vp) not in ids
