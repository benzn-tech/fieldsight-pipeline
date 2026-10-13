"""An upload through org-api writes a lake_objects row owned by the SITE's company.

Real Postgres: a connection double never parses SQL, so the row that actually lands (and the
fact that a missing table cannot break the upload) is only proven here.
"""
import json
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration


class _S3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]


@pytest.fixture(autouse=True)
def _s3(monkeypatch):
    monkeypatch.setattr(api, "s3", lambda: _S3())


def _world(db):
    tag = uuid.uuid4().hex[:6]
    co_b = companies.create_company(db, f"SiteCo-{tag}")
    co_a = companies.create_company(db, f"HomeCo-{tag}")
    site_b = sites.create_site(db, co_b["id"], "Tower B")
    folder = f"Eve_Xu_{tag}"
    eve = users.upsert_user(db, f"sub-eve-{tag}", f"eve-{tag}@a.nz", company_id=co_a["id"],
                            first_name="Eve", last_name="Xu", global_role="worker")
    users.set_folder_name(db, f"sub-eve-{tag}", folder)
    eve = users.get_by_id(db, eve["id"])
    return dict(co_a=co_a, co_b=co_b, site_b=site_b, eve=eve, folder=folder)


def _upload(db, w, site_id, name):
    body = {"kind": "video", "clientUuid": uuid.uuid4().hex, "siteId": site_id,
            "fileName": name, "contentType": "video/mp4", "startedAt": "2026-10-12T10:00:00Z"}
    return api.create_recording_upload_url(db, w["eve"], body)


def _rows(db, key):
    return db.execute("SELECT company_id, site_id, user_id, source, kind, folder, day::text "
                      "FROM lake_objects WHERE s3_key=%s", (key,)).fetchall()


def test_a_no_site_upload_is_owned_by_the_recorders_home_company(db):
    w = _world(db)
    res = _upload(db, w, None, "Eve_20261012_100000.mp4")
    assert res["statusCode"] == 200, res
    key = json.loads(res["body"])["s3Key"]
    rows = _rows(db, key)
    assert len(rows) == 1
    company, site, user, source, kind, folder, day = rows[0]
    assert company == w["co_a"]["id"] and site is None
    assert user == w["eve"]["id"] and source == "upload"
    assert (kind, folder, day) == ("media", w["folder"], "2026-10-12")


def test_an_external_members_upload_is_owned_by_the_sites_company(db):
    """The D1 rule: the SITE's company, not the recorder's home company."""
    w = _world(db)
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    res = _upload(db, w, str(w["site_b"]["id"]), "Eve_20261012_100001.mp4")
    assert res["statusCode"] == 200, res
    key = json.loads(res["body"])["s3Key"]
    rows = _rows(db, key)
    assert len(rows) == 1
    assert rows[0][0] == w["co_b"]["id"] and rows[0][1] == w["site_b"]["id"]


def test_a_resend_keeps_exactly_one_row(db):
    w = _world(db)
    cu = uuid.uuid4().hex
    body = {"kind": "video", "clientUuid": cu, "fileName": "Eve_20261012_100002.mp4",
            "contentType": "video/mp4", "startedAt": "2026-10-12T10:00:00Z"}
    k1 = json.loads(api.create_recording_upload_url(db, w["eve"], dict(body))["body"])["s3Key"]
    k2 = json.loads(api.create_recording_upload_url(db, w["eve"], dict(body))["body"])["s3Key"]
    assert k1 == k2 and len(_rows(db, k1)) == 1


def test_a_missing_lake_objects_table_does_not_break_the_upload(db):
    """sam deploy runs before migrate: for a minute the table does not exist. The upload
    must still succeed and the recording row must survive the failed index write."""
    w = _world(db)
    db.execute("ALTER TABLE lake_objects RENAME TO lake_objects_away")
    try:
        res = _upload(db, w, None, "Eve_20261012_100003.mp4")
        assert res["statusCode"] == 200, res
        rec_id = json.loads(res["body"])["recordingId"]
        assert db.execute("SELECT count(*) FROM recordings WHERE id=%s",
                          (rec_id,)).fetchone()[0] == 1
    finally:
        db.execute("ALTER TABLE lake_objects_away RENAME TO lake_objects")
