"""Voice notes settle without an email and always have one topic (M2 Task 7).

A voice note (`meeting_session.session_type='voice_note'`) is claimed like any
session -- its final extraction is still requested -- but the claim settles it as
`sent` with no email, writes a "Voice note HH:MM" placeholder topic under the
note's own extraction key, and pins the note's claimed photos under it. The real
extraction later replaces the placeholder with ONE merged topic (plan decision 9);
an extraction with no topics keeps the placeholder.

Real Postgres (the rolled-back `db` fixture), S3 faked. Skipped without
TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import contextlib
import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import lambda_finalize_claim as fc
from repositories import meeting_session, memberships, voice_note_photo

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")

pytestmark = pytest.mark.integration

UTC = timezone.utc
# 2026-10-21 01:32Z is 14:32 NZDT (+13, daylight time from late September), so the
# note's NZ day is 2026-10-21 and its placeholder reads "Voice note 14:32".
# Hard-coded rather than derived from the code under test.
OPENED = datetime(2026, 10, 21, 1, 32, tzinfo=UTC)
CLOSED = datetime(2026, 10, 21, 1, 34, tzinfo=UTC)
DATE = "2026-10-21"
TITLE = "Voice note 14:32"
RANGE = "14:32 - 14:34"


def _world(db, *, email=True, site=True, session_type="voice_note"):
    # users.email is NOT NULL; "no email on file" is the empty string the claim strips.
    tag = uuid.uuid4().hex[:8]
    folder = f"VN_{tag}"           # the underscore is a LIKE wildcard unless escaped
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     (f"VN Co {tag}",)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, %s, 'worker', %s) RETURNING id",
        (f"sub-vn-{tag}", cid, f"vn-{tag}@x.com" if email else "", folder)).fetchone()[0]
    site_id = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, %s) RETURNING id",
                         (cid, f"VN Site {tag}")).fetchone()[0]
    memberships.add_membership(db, uid, site_id, "worker")
    sid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, site_id, kind, "
        "status, opened_at, last_segment_at, closed_at, close_intent, version, "
        "session_type) VALUES (%s,%s,%s,%s,'audio','pending_close',%s,%s,%s,'end',3,%s)",
        (sid, cid, uid, site_id if site else None, OPENED, CLOSED, CLOSED, session_type))
    return {"cid": cid, "uid": uid, "site": site_id, "sid": sid, "folder": folder,
            "key": f"extractions/{folder}/{DATE}/sid{sid}.json",
            "photo": f"users/{folder}/pictures/{DATE}/{folder}_{DATE}_14-33-00.jpg"}


def _claim(db, w, rec=None, version=3):
    rec = rec if rec is not None else []
    return fc.finalize_claim(
        db, w["sid"], version, resolve_context=fc._resolve_context,
        read_rolling=lambda *a: {},
        request_extraction=lambda *a, **k: rec.append((a, k)),
        place_voice_note=fc._place_voice_note)


def _visible(db, key):
    return db.execute(
        "SELECT id, title, time_range FROM topics t WHERE t.source_s3_key = %s "
        "AND t.superseded_at IS NULL ORDER BY created_at", (key,)).fetchall()


def _photo(db, w, client_uuid="11111111-1111-1111-1111-111111111111"):
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        "started_at, uploaded_at) VALUES (%s,%s,%s,'photo',%s,%s,now(),now())",
        (w["cid"], w["uid"], w["site"], w["photo"], client_uuid))
    voice_note_photo.link(db, w["uid"], w["sid"], [client_uuid])


def _photo_rows(db, key):
    return [(str(r[0]), r[1]) for r in db.execute(
        "SELECT topic_id, source FROM topic_photos WHERE s3_key = %s", (key,)).fetchall()]


def test_claim_settles_without_email(db):
    w = _world(db)
    rec = []
    out = _claim(db, w, rec)
    assert out == {"status": "voice_note", "sessionId": w["sid"]}
    assert len(rec) == 1                       # the final extraction is still asked for
    assert meeting_session.get(db, w["sid"])["status"] == "sent"
    rows = _visible(db, w["key"])
    assert [(r[1], r[2]) for r in rows] == [(TITLE, RANGE)]


def test_claim_without_email_on_file_still_voice_note(db):
    w = _world(db, email=False)
    assert _claim(db, w)["status"] == "voice_note"
    assert meeting_session.get(db, w["sid"])["status"] == "sent"


def test_claim_binds_the_notes_photos(db):
    w = _world(db)
    _photo(db, w)
    _claim(db, w)
    (placeholder,) = _visible(db, w["key"])
    assert _photo_rows(db, w["photo"]) == [(str(placeholder[0]), "voice_note")]


def test_no_site_no_placeholder(db, caplog):
    caplog.set_level(logging.INFO)
    w = _world(db, site=False)
    assert _claim(db, w)["status"] == "voice_note"
    assert _visible(db, w["key"]) == []
    assert meeting_session.get(db, w["sid"])["status"] == "sent"
    assert any(r.levelno == logging.WARNING and w["sid"] in r.getMessage()
               for r in caplog.records)


def test_placeholder_is_idempotent(db):
    w = _world(db)
    _claim(db, w)
    # A reopen round: the late chunk reopened it, it idled out again and is due.
    db.execute("UPDATE meeting_session SET status='pending_close', version=4, "
               "reopen_count=1 WHERE session_id=%s", (w["sid"],))
    assert _claim(db, w, version=4)["status"] == "voice_note"
    assert len(_visible(db, w["key"])) == 1


def test_a_failing_placement_still_settles_the_note(db):
    """Best effort for real: a SQL error while placing the note must not abort the
    sweep's transaction (it runs in a savepoint), so the note still settles."""
    w = _world(db)
    out = fc.finalize_claim(
        db, w["sid"], 3, resolve_context=fc._resolve_context, read_rolling=lambda *a: {},
        request_extraction=lambda *a, **k: None,
        place_voice_note=lambda c, r, x: c.execute("SELECT 1/0"))
    assert out == {"status": "voice_note", "sessionId": w["sid"]}
    assert meeting_session.get(db, w["sid"])["status"] == "sent"


def test_backstop_never_mails_a_voice_note(db):
    w = _world(db)
    db.execute("UPDATE meeting_session SET status='finalizing', finalizing_at=%s "
               "WHERE session_id=%s", (CLOSED, w["sid"]))
    enqueued = []
    mailed = fc.backstop(
        db,
        # Scoped to this note: the shared local database may hold other suites'
        # finalizing rows. The row itself still comes from the real list_finalizing.
        list_waiting=lambda c: [r for r in meeting_session.list_finalizing(c)
                                if r["session_id"] == w["sid"]],
        resolve_context=fc._resolve_context, read_rolling=lambda *a: {},
        enqueue=lambda a: enqueued.append(a) or True,
        now=CLOSED + timedelta(seconds=fc.FINALIZE_EMAIL_WAIT_SEC + 60))
    assert mailed == []
    assert enqueued == []


# ---- the real extraction lands ------------------------------------------------

class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        return {"Body": io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _artifact(topics):
    return {"schema_version": 1, "tier": "final", "extracted_at": "2026-10-21T01:40:00Z",
            "topics": topics}


def _t(title, time_range, action):
    return {"topic_title": title, "category": "progress", "summary": f"{title}.",
            "time_range": time_range, "participants": [], "safety_flags": [],
            "action_items": [{"action": action}]}


def _write(db, monkeypatch, w, artifact):
    fake = _FakeS3({w["key"]: json.dumps(artifact), w["photo"]: b"jpg"})
    monkeypatch.setattr(lambda_item_writer, "_s3_client", fake)
    # The writer's own `with get_connection() as conn:` runs inside this test's
    # transaction: no commit, no close, everything rolls back with the fixture.
    monkeypatch.setattr(lambda_item_writer, "get_connection",
                        lambda *a, **k: contextlib.nullcontext(db))
    monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(lambda_item_writer, "_request_match", lambda *a, **k: None)
    monkeypatch.setattr(lambda_item_writer, "_request_rebind", lambda *a, **k: None)
    return lambda_item_writer.write_extraction_items(DATE, w["folder"], w["key"])


def test_real_extraction_replaces_the_placeholder(db, monkeypatch):
    w = _world(db)
    _photo(db, w)
    _claim(db, w)
    (placeholder,) = _visible(db, w["key"])

    out = _write(db, monkeypatch, w, _artifact([
        _t("Crack on level 2", "14:32 - 14:33", "Fill the crack"),
        _t("Order more ties", "14:33 - 14:34", "Order ties")]))
    assert out == {"skipped": False, "topics": 1}, out

    rows = _visible(db, w["key"])
    assert [(r[1], r[2]) for r in rows] == [("Crack on level 2", "14:32 - 14:34")]
    new_id = rows[0][0]
    assert db.execute("SELECT superseded_at IS NOT NULL FROM topics WHERE id=%s",
                      (placeholder[0],)).fetchone()[0] is True
    assert sorted(r[0] for r in db.execute(
        "SELECT text FROM action_items WHERE topic_id=%s", (new_id,)).fetchall()) == [
        "Fill the crack", "Order ties"]
    assert _photo_rows(db, w["photo"]) == [(str(new_id), "voice_note")]


def test_empty_extraction_keeps_the_placeholder(db, monkeypatch):
    w = _world(db)
    _claim(db, w)
    (placeholder,) = _visible(db, w["key"])
    out = _write(db, monkeypatch, w, _artifact([]))
    assert [r[0] for r in _visible(db, w["key"])] == [placeholder[0]]
    assert out == {"skipped": True, "reason": "voice note: no topics, placeholder kept"}


def test_meeting_session_unchanged(db):
    w = _world(db, session_type=None)
    out = _claim(db, w)
    assert out["status"] == "waiting"
    assert meeting_session.get(db, w["sid"])["status"] == "finalizing"
    assert _visible(db, w["key"]) == []
