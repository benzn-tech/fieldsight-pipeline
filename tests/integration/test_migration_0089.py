"""Integration: migration 0089 -- phone sessions, late-chunk reopen, voice notes.

Checks the constraints and defaults 0089 adds (recordings.source,
meeting_session.session_type / last_chunk_at / closed_chunk_at / reopen_count,
voice_note_photo) and that the file's backfill marks device rows only and is
safe to run twice.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import uuid

import psycopg
import pytest

from repositories import companies, users

pytestmark = pytest.mark.integration

MIGRATION_0089 = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "migrations",
    "0089_phone_sessions_voice_notes.sql")


def _migration_sql():
    with open(MIGRATION_0089, encoding="utf-8") as fh:
        return fh.read()


def _seed_user(db, tag=None):
    tag = tag or uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Co-0089-{tag}")
    user = users.upsert_user(db, f"sub-0089-{tag}", f"u-{tag}@example.com",
                             company_id=co["id"])
    return co, user


def _insert_recording(db, co, user, source=None, device_id=None):
    return db.execute(
        "INSERT INTO recordings (company_id, user_id, kind, s3_key, client_uuid, "
        "started_at, device_id, source) "
        "VALUES (%s, %s, 'audio', %s, %s, now(), %s, %s) RETURNING id",
        (co["id"], user["id"], f"k/{uuid.uuid4().hex}", uuid.uuid4().hex,
         device_id, source),
    ).fetchone()[0]


def _insert_session(db, co, user, session_type=None):
    sid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, session_type) "
        "VALUES (%s, %s, %s, %s)",
        (sid, co["id"], user["id"], session_type),
    )
    return sid


def test_recordings_source_check(db):
    co, user = _seed_user(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            _insert_recording(db, co, user, source="tablet")
    rid = _insert_recording(db, co, user, source="phone")
    row = db.execute("SELECT source FROM recordings WHERE id=%s", (rid,)).fetchone()
    assert row[0] == "phone"


def test_backfill_marks_device_rows_only(db):
    co, user = _seed_user(db)
    device_id = uuid.uuid4()
    db.execute("INSERT INTO devices (id, asset_tag) VALUES (%s, %s)",
               (device_id, f"FS-0089-{uuid.uuid4().hex[:6]}"))
    with_device = _insert_recording(db, co, user, device_id=device_id)
    without_device = _insert_recording(db, co, user)

    db.execute(_migration_sql())

    got = dict(db.execute(
        "SELECT id, source FROM recordings WHERE id = ANY(%s)",
        ([with_device, without_device],)).fetchall())
    assert got[with_device] == "device"
    assert got[without_device] is None


def test_session_type_check_and_defaults(db):
    co, user = _seed_user(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            _insert_session(db, co, user, session_type="chat")
    sid = _insert_session(db, co, user)
    reopen_count, last_chunk_at, closed_chunk_at = db.execute(
        "SELECT reopen_count, last_chunk_at, closed_chunk_at "
        "FROM meeting_session WHERE session_id=%s", (sid,)).fetchone()
    assert reopen_count == 0
    assert last_chunk_at is None
    assert closed_chunk_at is None
    # 'voice_note' is the other permitted value.
    _insert_session(db, co, user, session_type="voice_note")


def test_voice_note_photo_pk(db):
    co, user = _seed_user(db)
    _co2, other = _seed_user(db)
    sid = _insert_session(db, co, user, session_type="voice_note")
    photo = uuid.uuid4().hex
    insert = ("INSERT INTO voice_note_photo (user_id, photo_client_uuid, session_id) "
              "VALUES (%s, %s, %s)")
    db.execute(insert, (user["id"], photo, sid))
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(insert, (user["id"], photo, sid))
    db.execute(insert, (other["id"], photo, sid))
    n = db.execute("SELECT count(*) FROM voice_note_photo WHERE photo_client_uuid=%s",
                   (photo,)).fetchone()[0]
    assert n == 2


def test_migration_is_idempotent(db):
    sql = _migration_sql()
    db.execute(sql)
    db.execute(sql)
