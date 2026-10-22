"""Integration: `/complete` on a photo re-binds that day's photos at once (M2 Task 8).

A photo often finishes uploading late, out of the phone's offline queue, after the
day's extraction already ran. Waiting for the next extraction/ingest left it
unbound for hours. `/complete` now runs the pin-aware day rebind inline -- and a
rebind failure must never turn a good upload into an error, or the phone queue
would re-upload the same photo forever.

Every query here is SQL the unit doubles never parse (the savepoint, the
lock_timeout, the advisory lock, the topic existence probe), so it is proven only
against a real Postgres.
"""
import json
import logging
import time
import uuid

import pytest

import photo_rebind
from repositories import recordings, topics, voice_note_photo

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

pytestmark = pytest.mark.integration

DATE = "2026-10-21"
FOLDER = "Reb_User"
PHOTO = f"users/{FOLDER}/pictures/{DATE}/{FOLDER}_{DATE}_10-02-00.jpg"
SIZE = 4096


def _seed(db, kind="photo", key=PHOTO):
    tag = uuid.uuid4().hex[:6]
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     (f"Reb Co {tag}",)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, %s, 'worker', %s) RETURNING id",
        (f"sub-reb-{tag}", cid, f"reb-{tag}@x.com", FOLDER)).fetchone()[0]
    site = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                      (cid,)).fetchone()[0]
    rec = recordings.insert_pending(
        db, company_id=cid, user_id=uid, site_id=site, kind=kind, s3_key=key,
        client_uuid="u1", started_at="2026-10-20T21:02:00Z")
    return cid, uid, site, rec


def _key(sid):
    return f"extractions/{FOLDER}/{DATE}/sid{sid}.json"


def _topic(db, site, title, source, time_range):
    return topics.upsert_topic(db, site, DATE, title, source_s3_key=source,
                               time_range=time_range)["id"]


def _listed(*keys):
    """The shape photo_binding.list_pictures returns."""
    out = []
    for k in keys:
        name = k.rsplit("/", 1)[-1]
        hhmmss = name.rsplit("_", 1)[-1][:-4].replace("-", ":")
        out.append({"key": k, "filename": name, "hhmm": hhmmss[:5], "hhmmss": hhmmss})
    return out


def _rows_for(db, key=PHOTO):
    return sorted((str(r[0]), r[1]) for r in db.execute(
        "SELECT topic_id, source FROM topic_photos WHERE s3_key = %s", (key,)).fetchall())


def _uploaded_at(db, rec_id):
    return db.execute("SELECT uploaded_at FROM recordings WHERE id=%s", (rec_id,)).fetchone()[0]


@pytest.fixture
def listing(monkeypatch):
    """`list_pictures` faked to return the photo; S3 is never touched."""
    calls = []

    def fake(s3_client, bucket, prefix):
        calls.append(prefix)
        return _listed(PHOTO)

    monkeypatch.setattr(org, "s3", lambda: object())
    monkeypatch.setattr(org.photo_binding, "list_pictures", fake)
    monkeypatch.setattr(org, "UPLOAD_VERIFY_MODE", "off")
    return calls


@pytest.fixture
def spy(monkeypatch):
    calls = []
    real = photo_rebind.rebind_day_photos

    def wrapped(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(photo_rebind, "rebind_day_photos", wrapped)
    return calls


def _complete(db, uid, rec):
    res = org.complete_recording(db, {"id": uid}, rec["id"], {})
    assert res["statusCode"] == 200, res
    assert json.loads(res["body"]) == {"ok": True}
    assert _uploaded_at(db, rec["id"]) is not None
    return res


def test_photo_complete_binds_on_a_day_with_topics(db, listing, spy):
    cid, uid, site, rec = _seed(db)
    m_topic = _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")

    _complete(db, uid, rec)

    assert _rows_for(db) == [(str(m_topic), "binding")]
    assert listing == [f"users/{FOLDER}/pictures/{DATE}/"]
    assert spy[0][1:4] == (cid, FOLDER, DATE), "the row's company, the key's folder and date"


def test_photo_complete_respects_a_pin(db, listing, spy):
    cid, uid, site, rec = _seed(db)
    sid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, session_type, opened_at) "
        "VALUES (%s, %s, %s, 'voice_note', %s)", (sid, cid, uid, "2026-10-21T09:00:00Z"))
    note_topic = _topic(db, site, "note", _key(sid), "09:00 - 09:01")
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    voice_note_photo.link(db, uid, sid, ["u1"])

    _complete(db, uid, rec)

    assert _rows_for(db) == [(str(note_topic), "voice_note")]


def test_no_topics_no_rebind(db, listing, spy):
    _, uid, _, rec = _seed(db)
    _complete(db, uid, rec)
    assert spy == [] and listing == []


def test_only_a_superseded_topic_no_rebind(db, listing, spy):
    _, uid, site, rec = _seed(db)
    src = _key(uuid.uuid4().hex)
    _topic(db, site, "old", src, "10:00 - 10:05")
    topics.supersede_topics_for_source(db, src, "test:run2")

    _complete(db, uid, rec)

    assert spy == [] and listing == []


def test_audio_complete_does_not_rebind(db, listing, spy):
    # The pictures-shaped key isolates the kind gate: only `kind` stops it.
    _, uid, site, rec = _seed(db, kind="audio")
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    _complete(db, uid, rec)
    assert spy == [] and listing == []


def test_unparseable_key_does_not_rebind(db, listing, spy):
    key = f"users/{FOLDER}/misc/{DATE}/x.jpg"
    _, uid, site, rec = _seed(db, key=key)
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    _complete(db, uid, rec)
    assert spy == [] and listing == []


@pytest.mark.parametrize("which", ["listing", "rebind"])
def test_rebind_failure_still_200(db, listing, monkeypatch, caplog, which):
    _, uid, site, rec = _seed(db)
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")

    if which == "listing":
        def boom(*a, **kw):
            raise RuntimeError("S3 is down")
        monkeypatch.setattr(org.photo_binding, "list_pictures", boom)
    else:
        def boom(conn, *a, **kw):
            # A failure AFTER a statement ran, so the savepoint has real work
            # to undo -- and a failure Postgres itself raises, which aborts the
            # transaction rather than merely unwinding Python.
            conn.execute("SELECT 1/0")
        monkeypatch.setattr(photo_rebind, "rebind_day_photos", boom)

    with caplog.at_level(logging.ERROR, logger=org.logger.name):
        _complete(db, uid, rec)

    assert any("photo rebind skipped" in r.getMessage() and r.exc_info
               for r in caplog.records), caplog.text
    assert _rows_for(db) == []


def test_lock_timeout_still_200(db, migrated_db_url, listing):
    import psycopg

    _, uid, site, rec = _seed(db)
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")

    holder = psycopg.connect(migrated_db_url, autocommit=True)
    try:
        holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                       (f"photobind:{FOLDER}:{DATE}",))
        t0 = time.monotonic()
        _complete(db, uid, rec)
        elapsed = time.monotonic() - t0
    finally:
        holder.execute("SELECT pg_advisory_unlock_all()")
        holder.close()

    assert 1.5 <= elapsed < 3.5, f"waited {elapsed:.2f}s -- the 2 s lock_timeout should end it"
    assert _rows_for(db) == []


def test_lock_timeout_does_not_leak_into_the_request(db, listing):
    """SET LOCAL inside a RELEASED savepoint lasts to the end of the OUTER
    transaction; the 2 s cap must not govern whatever the request does next."""
    _, uid, site, rec = _seed(db)
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    before = db.execute("SHOW lock_timeout").fetchone()[0]

    _complete(db, uid, rec)

    assert db.execute("SHOW lock_timeout").fetchone()[0] == before


def test_enforce_missing_object_is_409_and_no_rebind(db, listing, spy, monkeypatch):
    _, uid, site, rec = _seed(db)
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    monkeypatch.setattr(org, "UPLOAD_VERIFY_MODE", "enforce")
    monkeypatch.setattr(org, "_object_size", lambda key: None)

    res = org.complete_recording(db, {"id": uid}, rec["id"], {})

    assert res["statusCode"] == 409, res
    assert spy == [] and listing == []
    assert _uploaded_at(db, rec["id"]) is None
