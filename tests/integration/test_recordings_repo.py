import pytest
from repositories import companies, users, sites, recordings

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Acme", industry="construction")
    u = users.upsert_user(db, "sub-rec", "r@acme.com", company_id=co["id"])
    s = sites.create_site(db, co["id"], "North Wharf", location="Auckland")
    return co, u, s


def test_insert_get_and_idempotency(db):
    co, u, s = _seed(db)
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=s["id"], kind="video",
        s3_key="users/r/video/2026-07-13/x.mp4", client_uuid="cap-1",
        started_at="2026-07-13T16:01:58Z", ended_at="2026-07-13T16:04:00Z",
        duration_s=122, resolution="1920x1080", codec="h264", size_bytes=None,
    )
    assert row["id"] and row["uploaded_at"] is None and row["site_id"] == s["id"]
    assert recordings.get_by_id(db, row["id"])["s3_key"].endswith("x.mp4")
    assert recordings.get_by_client_uuid(db, u["id"], "cap-1")["id"] == row["id"]
    assert recordings.get_by_client_uuid(db, u["id"], "nope") is None


def test_null_site_allowed(db):
    co, u, s = _seed(db)
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=None, kind="photo",
        s3_key="users/r/pictures/2026-07-13/y.jpg", client_uuid="cap-2",
        started_at="2026-07-13T16:05:00Z", ended_at=None, duration_s=None,
        resolution=None, codec=None, size_bytes=None,
    )
    assert row["site_id"] is None and row["kind"] == "photo"


def test_mark_uploaded_company_guarded(db):
    co, u, s = _seed(db)
    other = companies.create_company(db, "Other")
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=None, kind="audio",
        s3_key="users/r/audio/2026-07-13/z.wav", client_uuid="cap-3",
        started_at="2026-07-13T16:06:00Z", ended_at=None, duration_s=None,
        resolution=None, codec=None, size_bytes=None,
    )
    assert recordings.mark_uploaded(db, row["id"], other["id"], 999) is None, "wrong company must not update"
    done = recordings.mark_uploaded(db, row["id"], co["id"], 12345)
    assert done["uploaded_at"] is not None and done["size_bytes"] == 12345


def test_mark_uploaded_persists_gps_track(db):
    co, u, s = _seed(db)
    row = recordings.insert_pending(
        db, company_id=co["id"], user_id=u["id"], site_id=None, kind="video",
        s3_key="users/r/video/2026-07-13/gps.mp4", client_uuid="cap-gps",
        started_at="2026-07-13T16:07:00Z", ended_at=None, duration_s=None,
        resolution=None, codec=None, size_bytes=None,
    )
    assert row["gps_track"] is None

    track = [{"t": 1752421200000, "lat": -36.8485, "lon": 174.7633},
             {"t": 1752421205000, "lat": -36.8486, "lon": 174.7634}]
    done = recordings.mark_uploaded(db, row["id"], co["id"], 4096, track)
    assert done["gps_track"] == track

    fetched = recordings.get_by_id(db, row["id"])
    assert fetched["gps_track"] == track

    # A later call omitting gps_track (None) must not wipe the stored value —
    # COALESCE(%s, gps_track) keeps the existing track, mirroring size_bytes.
    kept = recordings.mark_uploaded(db, row["id"], co["id"], None, None)
    assert kept["gps_track"] == track


def _seed_company_user_site(db, cname):
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (cname,)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES (%s, %s, %s, 'worker') RETURNING id",
        (f"sub-{cname}", cid, f"{cname}@x.com")).fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid, sid


def _insert_recording(db, cid, uid, sid, s3_key):
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, %s, 'audio', %s, %s, now())",
        (cid, uid, sid, s3_key, s3_key))  # client_uuid unique enough for the test


def test_site_for_media_returns_in_company_tagged_site(db):
    cid, uid, sid = _seed_company_user_site(db, "A")
    _insert_recording(db, cid, uid, sid,
                      "users/Jo_Bloggs/audio/2026-07-16/Jo_Bloggs_2026-07-16_09-50-00.wav")
    site = recordings.site_for_media(db, cid, "Jo_Bloggs", "2026-07-16", "Jo_Bloggs_2026-07-16_09-50-00")
    assert site is not None and site["id"] == sid


def test_site_for_media_excludes_cross_company_and_null_and_nonmatch(db):
    cid, uid, sid = _seed_company_user_site(db, "A")
    # (a) a recording in company A but tagged with a site from company B → must be ignored
    _cidB, uidB, sidB = _seed_company_user_site(db, "B")
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, %s, 'audio', %s, 'cu-x', now())",
        (cid, uid, sidB, "users/X/audio/2026-07-16/X_2026-07-16_10-00-00.wav"))
    assert recordings.site_for_media(db, cid, "X", "2026-07-16", "X_2026-07-16_10-00-00") is None
    # (b) null site_id → ignored
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, NULL, 'audio', %s, 'cu-y', now())",
        (cid, uid, "users/Y/audio/2026-07-16/Y_2026-07-16_11-00-00.wav"))
    assert recordings.site_for_media(db, cid, "Y", "2026-07-16", "Y_2026-07-16_11-00-00") is None
    # (c) no recording matches → None
    assert recordings.site_for_media(db, cid, "Nobody", "2026-07-16", "Nobody_2026-07-16_12-00-00") is None
