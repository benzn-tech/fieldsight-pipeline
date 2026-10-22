"""Voice-note /open against a real Postgres (M2 plan Task 5, decision 6).

A phone voice note is its own session flagged session_type='voice_note'; its
/open carries the clientUuids of the photos it claims. The claim is keyed per
user and the LATEST note wins by the note's opened_at — not by /open arrival —
so a retried older /open cannot steal a photo back. A connection double never
parses the ON CONFLICT ... WHERE guard, so it is only proven here.
"""
import json
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, meeting_session, users

pytestmark = pytest.mark.integration


def _user(db, co, tag, n):
    u = users.upsert_user(db, f"sub-vn{n}-{tag}", f"vn{n}-{tag}@a.nz", company_id=co["id"],
                          first_name="Vee", last_name=f"N{n}", global_role="worker")
    return users.get_by_id(db, u["id"])


def _world(db):
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"VoiceCo-{tag}")
    return _user(db, co, tag, 1), _user(db, co, tag, 2)


def _rows(db, user=None):
    sql = "SELECT user_id, photo_client_uuid, session_id FROM voice_note_photo"
    if user is None:
        return db.execute(sql).fetchall()
    return db.execute(sql + " WHERE user_id=%s ORDER BY photo_client_uuid",
                      (user["id"],)).fetchall()


def _note(photos, started="2026-10-21T10:00:00Z"):
    return {"kind": "audio", "sessionType": "voice_note", "startedAt": started,
            "photos": photos}


def test_device_body_stores_meeting(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    res = api.session_open(db, u1, sid, {"kind": "audio", "sessionType": "meeting"})
    assert res["statusCode"] == 200, res
    assert meeting_session.get(db, sid)["session_type"] == "meeting"
    assert _rows(db, u1) == []


def test_body_without_type_leaves_it_null(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    assert api.session_open(db, u1, sid, {"kind": "audio"})["statusCode"] == 200
    assert meeting_session.get(db, sid)["session_type"] is None


def test_voice_note_links_photos(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
    res = api.session_open(db, u1, sid, _note([p1, p2]))
    assert res["statusCode"] == 200, res
    assert meeting_session.get(db, sid)["session_type"] == "voice_note"
    rows = _rows(db, u1)
    assert sorted(r[1] for r in rows) == sorted([p1, p2])
    assert {r[2] for r in rows} == {sid}


def test_photo_uuid_is_stored_as_sent(db):
    # It must equal the photo's recordings.client_uuid, which is stored as sent.
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    raw = uuid.uuid4().hex.upper()
    assert api.session_open(db, u1, sid, _note([raw]))["statusCode"] == 200
    assert [r[1] for r in _rows(db, u1)] == [raw]


def test_later_note_wins(db):
    u1, _ = _world(db)
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    p1 = str(uuid.uuid4())
    assert api.session_open(db, u1, a, _note([p1], "2026-10-21T10:00:00Z"))["statusCode"] == 200
    assert api.session_open(db, u1, b, _note([p1], "2026-10-21T10:05:00Z"))["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [b]
    # A retried /open of the OLDER note arrives last; it must not steal the photo back.
    assert api.session_open(db, u1, a, _note([p1], "2026-10-21T10:00:00Z"))["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [b]


def test_note_with_no_opened_at_loses_to_one_with(db):
    from repositories import voice_note_photo
    u1, _ = _world(db)
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    p1 = str(uuid.uuid4())
    # A note opened without startedAt claims first; a dated note then takes it.
    assert api.session_open(db, u1, a, {"sessionType": "voice_note", "photos": [p1]})["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [a]
    assert api.session_open(db, u1, b, _note([p1]))["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [b]
    # And the undated one cannot take it back.
    assert voice_note_photo.link(db, u1["id"], a, [p1]) == 0
    assert [r[2] for r in _rows(db, u1)] == [b]


def test_link_returns_rows_inserted_or_moved(db):
    from repositories import voice_note_photo
    u1, _ = _world(db)
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    p1, p2 = str(uuid.uuid4()), str(uuid.uuid4())
    meeting_session.ensure_open(db, a, u1["company_id"], u1["id"], None, "audio",
                                "2026-10-21T10:00:00Z", session_type="voice_note")
    meeting_session.ensure_open(db, b, u1["company_id"], u1["id"], None, "audio",
                                "2026-10-21T10:05:00Z", session_type="voice_note")
    assert voice_note_photo.link(db, u1["id"], a, [p1, p2]) == 2
    assert voice_note_photo.link(db, u1["id"], b, [p1]) == 1
    assert voice_note_photo.link(db, u1["id"], a, [p1, p2]) == 1  # p2 re-asserted, p1 refused
    assert voice_note_photo.link(db, u1["id"], a, []) == 0


def test_other_users_photo_is_their_own(db):
    u1, u2 = _world(db)
    p1 = str(uuid.uuid4())
    s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
    assert api.session_open(db, u1, s1, _note([p1], "2026-10-21T10:00:00Z"))["statusCode"] == 200
    assert api.session_open(db, u2, s2, _note([p1], "2026-10-21T10:05:00Z"))["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [s1]
    assert [r[2] for r in _rows(db, u2)] == [s2]


@pytest.mark.parametrize("body", [
    {"sessionType": "chat"},
    {"sessionType": "meeting", "photos": [str(uuid.uuid4())]},
    {"photos": [str(uuid.uuid4())]},
    {"sessionType": "voice_note", "photos": [str(uuid.uuid4()) for _ in range(201)]},
    {"sessionType": "voice_note", "photos": ["not-a-uuid"]},
    {"sessionType": "voice_note", "photos": [str(uuid.uuid4()), 7]},
    {"sessionType": "voice_note", "photos": str(uuid.uuid4())},
    {"sessionType": "voice_note", "photos": {"a": 1}},
], ids=["unknown-type", "photos-on-meeting", "photos-without-type", "201-photos",
        "not-a-uuid", "non-string", "photos-a-string", "photos-a-dict"])
def test_bad_bodies_are_400(db, body):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    res = api.session_open(db, u1, sid, body)
    assert res["statusCode"] == 400, res
    assert json.loads(res["body"])
    assert meeting_session.get(db, sid) is None
    assert _rows(db, u1) == []


def test_unknown_type_message(db):
    u1, _ = _world(db)
    res = api.session_open(db, u1, uuid.uuid4().hex, {"sessionType": "chat"})
    assert "sessionType must be 'meeting' or 'voice_note'" in res["body"]


def test_200_photos_is_allowed(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    photos = [str(uuid.uuid4()) for _ in range(api.VOICE_NOTE_MAX_PHOTOS)]
    assert api.session_open(db, u1, sid, _note(photos))["statusCode"] == 200
    assert len(_rows(db, u1)) == 200


def test_second_open_without_type_keeps_it(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    assert api.session_open(db, u1, sid, _note([str(uuid.uuid4())]))["statusCode"] == 200
    assert api.session_open(db, u1, sid, {})["statusCode"] == 200
    assert meeting_session.get(db, sid)["session_type"] == "voice_note"


# --- fix round 1 -------------------------------------------------------------

class _S3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]


def _chunk_open(db, user, sid, nz_wall="2026-10-21_10-00-00"):
    """Open `sid` the way an offline group join does: via the chunk's upload-url,
    whose filename carries the device's NAIVE NZ wall-clock start."""
    name = f"{user['folder_name']}_{nz_wall}_sid{sid}_c0001.m4a"
    body = {"kind": "audio", "clientUuid": uuid.uuid4().hex, "fileName": name,
            "contentType": "audio/mp4", "startedAt": "2026-10-20T21:00:00Z",
            "groupId": uuid.uuid4().hex}
    res = api.create_recording_upload_url(db, user, body)
    assert res["statusCode"] == 200, res
    assert meeting_session.get(db, sid) is not None


@pytest.mark.parametrize("order", ["a_first", "b_first"])
def test_chunk_opened_note_compares_in_utc(db, monkeypatch, order):
    """Note A was opened by its chunk (filename 10:00 NZ, naive) and its /open
    retried later; note B by /open at 10:05 NZ sent as UTC. B is later and must
    hold the photo whichever order the calls land in. Storing the filename time
    raw put A at 10:00 'UTC' -- 13h later than it was -- so A stole the photo."""
    monkeypatch.setattr(api, "s3", lambda: _S3())
    u1, _ = _world(db)
    users.set_folder_name(db, u1["cognito_sub"], f"Vee_N1_{uuid.uuid4().hex[:6]}")
    u1 = users.get_by_id(db, u1["id"])
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    p1 = str(uuid.uuid4())
    a_open = _note([p1], "2026-10-20T21:00:00Z")   # 10:00 NZDT
    b_open = _note([p1], "2026-10-20T21:05:00Z")   # 10:05 NZDT
    if order == "a_first":
        _chunk_open(db, u1, a)
        assert api.session_open(db, u1, a, a_open)["statusCode"] == 200
        assert api.session_open(db, u1, b, b_open)["statusCode"] == 200
    else:
        assert api.session_open(db, u1, b, b_open)["statusCode"] == 200
        _chunk_open(db, u1, a)
        assert api.session_open(db, u1, a, a_open)["statusCode"] == 200
    assert [r[2] for r in _rows(db, u1)] == [b]


def test_photos_on_a_session_that_is_already_a_meeting_are_not_linked(db):
    u1, _ = _world(db)
    sid = uuid.uuid4().hex
    assert api.session_open(db, u1, sid, {"kind": "audio", "sessionType": "meeting"})["statusCode"] == 200
    res = api.session_open(db, u1, sid, _note([str(uuid.uuid4())]))
    assert res["statusCode"] == 200, res
    assert meeting_session.get(db, sid)["session_type"] == "meeting"
    assert db.execute("SELECT count(*) FROM voice_note_photo WHERE session_id=%s",
                      (sid,)).fetchone()[0] == 0


def test_photos_on_another_users_session_are_not_linked(db):
    u1, u2 = _world(db)
    sid = uuid.uuid4().hex
    assert api.session_open(db, u1, sid, _note([]))["statusCode"] == 200
    res = api.session_open(db, u2, sid, _note([str(uuid.uuid4())]))
    assert res["statusCode"] == 200, res
    assert db.execute("SELECT count(*) FROM voice_note_photo WHERE session_id=%s",
                      (sid,)).fetchone()[0] == 0
    assert _rows(db, u2) == []
