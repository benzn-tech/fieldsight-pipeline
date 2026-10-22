"""Voice-note and human photo pins inside the day rebind, against a real Postgres
(M2 plan Task 6, decision 8).

A voice note's claimed photos (`voice_note_photo`, written by /open) sit under the
note's own topic and the time-based day rebind never moves them; a photo a person
moved by hand (`topic_photos.source='human'`) is likewise left alone. Pins are
DERIVED on every rebind -- `voice_note` rows are rebuilt from `voice_note_photo` --
so they follow the live topic across a supersede. Every query here is SQL the
unit doubles never parse, so it is proven only here.
"""
import uuid

import pytest

import photo_rebind
from repositories import topics, voice_note_photo

pytestmark = pytest.mark.integration

DATE = "2026-10-21"
FOLDER = "Pin_User"          # the underscore is a LIKE wildcard unless escaped
PHOTO = f"users/{FOLDER}/pictures/{DATE}/{FOLDER}_{DATE}_10-02-00.jpg"
PHOTO2 = f"users/{FOLDER}/pictures/{DATE}/{FOLDER}_{DATE}_10-03-00.jpg"


def _seed(db):
    tag = uuid.uuid4().hex[:6]
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id",
                     (f"Pin Co {tag}",)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, %s, 'worker', %s) RETURNING id",
        (f"sub-pin-{tag}", cid, f"pin-{tag}@x.com", FOLDER)).fetchone()[0]
    site = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id",
                      (cid,)).fetchone()[0]
    return cid, uid, site


def _photo(db, cid, uid, site, key=PHOTO, client_uuid="u1", uploaded=True):
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, "
        "started_at, uploaded_at) VALUES (%s, %s, %s, 'photo', %s, %s, now(), "
        + ("now()" if uploaded else "NULL") + ")",
        (cid, uid, site, key, client_uuid))


def _session(db, cid, uid, opened_at):
    sid = uuid.uuid4().hex
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, session_type, opened_at) "
        "VALUES (%s, %s, %s, 'voice_note', %s)", (sid, cid, uid, opened_at))
    return sid


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


def _rows_for(db, key):
    return sorted((str(r[0]), r[1]) for r in db.execute(
        "SELECT topic_id, source FROM topic_photos WHERE s3_key = %s", (key,)).fetchall())


def _world(db):
    """Company, user, site, photo u1 at 10:02, voice note S with a topic outside
    the photo's time, and a meeting topic M whose time covers it."""
    cid, uid, site = _seed(db)
    _photo(db, cid, uid, site)
    s = _session(db, cid, uid, "2026-10-21T09:00:00Z")
    s_topic = _topic(db, site, "note", _key(s), "09:00 - 09:01")
    m_topic = _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    return cid, uid, site, s, s_topic, m_topic


def _rebind(db, cid, *keys):
    return photo_rebind.rebind_day_photos(db, cid, FOLDER, DATE, _listed(*(keys or (PHOTO,))))


def test_pinned_photo_goes_to_the_note(db):
    cid, uid, _, s, s_topic, m_topic = _world(db)
    voice_note_photo.link(db, uid, s, ["u1"])
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == [(str(s_topic), "voice_note")]


def test_unpinned_photo_binds_by_time(db):
    cid, _, _, _, _, m_topic = _world(db)
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == [(str(m_topic), "binding")]


def test_claimed_but_no_topic_stays_unbound(db):
    cid, uid, _, _, _, _ = _world(db)
    s2 = _session(db, cid, uid, "2026-10-21T09:30:00Z")
    voice_note_photo.link(db, uid, s2, ["u1"])
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == []


def test_later_note_replaces_the_pin(db):
    cid, uid, site, s, _, _ = _world(db)
    s3 = _session(db, cid, uid, "2026-10-21T09:45:00Z")
    s3_topic = _topic(db, site, "later note", _key(s3), "09:45 - 09:46")
    voice_note_photo.link(db, uid, s, ["u1"])
    _rebind(db, cid)
    voice_note_photo.link(db, uid, s3, ["u1"])
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == [(str(s3_topic), "voice_note")]


def test_pin_follows_the_topic_across_supersede(db):
    cid, uid, site, s, s_topic, _ = _world(db)
    voice_note_photo.link(db, uid, s, ["u1"])
    _rebind(db, cid)
    topics.supersede_topics_for_source(db, _key(s), "test:run2")
    new_topic = _topic(db, site, "note v2", _key(s), "09:00 - 09:01")
    assert new_topic != s_topic
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == [(str(new_topic), "voice_note")]
    assert not [r for r in _rows_for(db, PHOTO) if r[1] == "binding"]


def test_human_pin_is_left_alone(db):
    cid, uid, site, _, _, m_topic = _world(db)
    _photo(db, cid, uid, site, key=PHOTO2, client_uuid="u2")
    # Moved by hand onto M -- yet a time bind would put it on M too; it must not
    # gain a second, machine row.
    db.execute("INSERT INTO topic_photos (topic_id, s3_key, source) VALUES (%s, %s, 'human')",
               (m_topic, PHOTO2))
    _rebind(db, cid, PHOTO, PHOTO2)
    assert _rows_for(db, PHOTO2) == [(str(m_topic), "human")]
    assert _rows_for(db, PHOTO) == [(str(m_topic), "binding")], "an unpinned photo still binds"


def test_photo_uploaded_after_the_note(db):
    cid, uid, site = _seed(db)
    s = _session(db, cid, uid, "2026-10-21T09:00:00Z")
    s_topic = _topic(db, site, "note", _key(s), "09:00 - 09:01")
    _topic(db, site, "meeting", _key(uuid.uuid4().hex), "10:00 - 10:05")
    _photo(db, cid, uid, site, uploaded=False)
    voice_note_photo.link(db, uid, s, ["u1"])
    _rebind(db, cid)
    # Claimed but not uploaded: no pin yet, and withheld from the time match too.
    assert _rows_for(db, PHOTO) == []
    db.execute("UPDATE recordings SET uploaded_at = now() WHERE client_uuid = 'u1'")
    _rebind(db, cid)
    assert _rows_for(db, PHOTO) == [(str(s_topic), "voice_note")]


def test_another_folder_with_a_lookalike_name_is_not_pinned(db):
    """`Pin_User` unescaped would match `PinXUser`: a photo under another folder
    must not be pinned (or deleted) by this folder's rebind."""
    cid, uid, site, s, _, _ = _world(db)
    other = f"users/PinXUser/pictures/{DATE}/PinXUser_{DATE}_10-02-00.jpg"
    _photo(db, cid, uid, site, key=other, client_uuid="u9")
    voice_note_photo.link(db, uid, s, ["u9"])
    _rebind(db, cid)
    assert _rows_for(db, other) == []
