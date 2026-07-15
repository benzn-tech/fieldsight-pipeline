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
