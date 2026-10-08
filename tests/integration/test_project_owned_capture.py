"""Project-owned tenancy P2 against a real Postgres: a recording belongs to the
company that owns the SITE it was captured on, and every site rung of the ingest
ladder is scoped to the recorder, not to a company.

A connection double never parses SQL, so the membership EXISTS in the rung
queries and the company written by the capture routes are only proven here.
"""
import io
import json
import uuid

import pytest

import lambda_org_api as api
from db.connection import get_connection
from repositories import directory as directory_repo
from repositories import (companies, meeting_session, memberships, recordings,
                          sites, users)

pytestmark = pytest.mark.integration

DATE = "2026-10-08"


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
    co_c = companies.create_company(db, f"OtherCo-{tag}")
    site_b = sites.create_site(db, co_b["id"], "Tower B")
    site_a = sites.create_site(db, co_a["id"], "Yard A")
    site_c = sites.create_site(db, co_c["id"], "Depot C")
    folder = f"Eve_Xu_{tag}"
    eve = users.upsert_user(db, f"sub-eve-{tag}", f"eve-{tag}@a.nz", company_id=co_a["id"],
                            first_name="Eve", last_name="Xu", global_role="worker")
    users.set_folder_name(db, f"sub-eve-{tag}", folder)
    eve = users.get_by_id(db, eve["id"])
    return dict(tag=tag, co_a=co_a, co_b=co_b, co_c=co_c, site_a=site_a, site_b=site_b,
                site_c=site_c, eve=eve, folder=folder)


def _upload(db, w, site_id, name="Eve_20261008_100000.mp4"):
    body = {"kind": "video", "clientUuid": uuid.uuid4().hex, "siteId": site_id,
            "fileName": name, "contentType": "video/mp4",
            "startedAt": "2026-10-08T10:00:00Z"}
    return api.create_recording_upload_url(db, w["eve"], body)


def _rec_company(db, res):
    assert res["statusCode"] == 200, res
    rec_id = json.loads(res["body"])["recordingId"]
    return str(db.execute("SELECT company_id FROM recordings WHERE id=%s", (rec_id,)).fetchone()[0])


def test_external_member_upload_is_owned_by_the_site_company(db):
    w = _world(db)
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    res = _upload(db, w, str(w["site_b"]["id"]))
    assert _rec_company(db, res) == str(w["co_b"]["id"])


def test_upload_without_membership_on_the_other_company_site_is_403(db):
    w = _world(db)
    res = _upload(db, w, str(w["site_b"]["id"]))
    assert res["statusCode"] == 403
    assert db.execute("SELECT count(*) FROM recordings WHERE user_id=%s",
                      (w["eve"]["id"],)).fetchone()[0] == 0


def test_an_archived_external_membership_no_longer_opens_the_site(db):
    w = _world(db)
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    memberships.archive_membership(db, w["eve"]["id"], w["site_b"]["id"])
    assert _upload(db, w, str(w["site_b"]["id"]))["statusCode"] == 403


def test_upload_on_own_company_site_and_with_no_site_stay_home(db):
    w = _world(db)
    r1 = _upload(db, w, str(w["site_a"]["id"]), name="Eve_20261008_100001.mp4")
    assert _rec_company(db, r1) == str(w["co_a"]["id"])
    r2 = _upload(db, w, None, name="Eve_20261008_100002.mp4")
    assert _rec_company(db, r2) == str(w["co_a"]["id"])


def test_session_open_on_an_external_site_is_owned_by_the_site_company(db):
    w = _world(db)
    sid = uuid.uuid4().hex
    refused = api.session_open(db, w["eve"], sid, {"siteId": str(w["site_b"]["id"]), "kind": "audio"})
    assert refused["statusCode"] == 403
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    ok = api.session_open(db, w["eve"], sid, {"siteId": str(w["site_b"]["id"]), "kind": "audio"})
    assert ok["statusCode"] == 200, ok
    assert str(meeting_session.get(db, sid)["company_id"]) == str(w["co_b"]["id"])


def _tag_recording(db, w, site, name):
    """A recording row the ingest rungs can find, as the capture route wrote it."""
    key = f"users/{w['folder']}/video/{DATE}/{name}"
    recordings.insert_pending(db, company_id=site["company_id"], user_id=w["eve"]["id"],
                              site_id=site["id"], kind="video", s3_key=key,
                              client_uuid=uuid.uuid4().hex, started_at=f"{DATE}T10:00:00Z")
    return key


def test_site_rungs_follow_the_recorder_not_a_company(db):
    w = _world(db)
    _tag_recording(db, w, w["site_b"], "Eve_x_100000.mp4")
    # No membership on B: both rungs must skip it.
    assert recordings.site_for_media(db, w["eve"]["id"], w["folder"], DATE, "Eve_x_100000") is None
    assert recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE) is None
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    got = recordings.site_for_media(db, w["eve"]["id"], w["folder"], DATE, "Eve_x_100000")
    assert str(got["id"]) == str(w["site_b"]["id"])
    assert str(recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE)["id"]) == str(w["site_b"]["id"])


def test_a_site_of_the_recorders_own_company_needs_no_membership(db):
    """Keeps admin/gm working: they hold no memberships but own the company."""
    w = _world(db)
    _tag_recording(db, w, w["site_a"], "Eve_y_100000.mp4")
    got = recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE)
    assert str(got["id"]) == str(w["site_a"]["id"])


def test_a_stray_row_on_an_unrelated_company_is_never_picked(db):
    w = _world(db)
    _tag_recording(db, w, w["site_c"], "Eve_z_100000.mp4")
    assert recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE) is None
    assert recordings.site_for_media(db, w["eve"]["id"], w["folder"], DATE, "Eve_z_100000") is None


def test_usable_site_helper(db):
    w = _world(db)
    uid = w["eve"]["id"]
    assert memberships.site_usable_by_user(db, uid, w["site_a"]["id"]) is not None
    assert memberships.site_usable_by_user(db, uid, w["site_b"]["id"]) is None
    memberships.add_external_membership(db, uid, w["site_b"]["id"], "worker")
    got = memberships.site_usable_by_user(db, uid, w["site_b"]["id"])
    assert str(got["company_id"]) == str(w["co_b"]["id"])
    assert memberships.site_usable_by_user(db, uid, w["site_c"]["id"]) is None
    assert memberships.site_usable_by_user(db, None, w["site_b"]["id"]) is None


def test_the_directory_lists_the_external_site_for_the_home_company_person(db):
    w = _world(db)
    uid = w["eve"]["id"]
    memberships.add_external_membership(db, uid, w["site_b"]["id"], "worker")
    pairs = {(str(r["user_id"]), str(r["site_id"])) for r in directory_repo.live_memberships(db)}
    assert (str(uid), str(w["site_b"]["id"])) in pairs
    # An UNflagged cross-company row stays out of the file every report reads.
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')",
               (uid, w["site_c"]["id"]))
    pairs = {(str(r["user_id"]), str(r["site_id"])) for r in directory_repo.live_memberships(db)}
    assert (str(uid), str(w["site_c"]["id"])) not in pairs


def test_ingest_helpers_resolve_the_recorder_globally(db):
    import lambda_ingest as ing
    w = _world(db)
    # Wrong company passed in on purpose: the folder is globally unique.
    assert ing.resolve_user(db, w["co_b"]["id"], w["folder"]) == w["eve"]["id"]
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    site = ing.resolve_site(db, w["co_a"]["id"], {"site": "Tower B"}, w["folder"])
    assert str(site["id"]) == str(w["site_b"]["id"])
    # A name that only exists on a company the recorder is not part of is not found.
    other = ing.resolve_site(db, w["co_a"]["id"], {"site": "Depot C"}, w["folder"])
    assert other is None or str(other["id"]) != str(w["site_c"]["id"])


# ---- item-writer end to end (committed connection, as the pass-0 harness) ----

class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        objs = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in objs if k.startswith(Prefix)]}
        return P()


def _extraction():
    return {"schema_version": 1, "tier": "final", "extracted_at": "2026-10-08T10:30:00Z",
            "topics": [{"topic_title": "Slab pour", "category": "progress",
                        "summary": "poured", "time_range": "10:00 – 10:05",
                        "participants": [], "action_items": [], "safety_flags": []}]}


def _iw_world(migrated_db_url, label, member):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co_b = companies.create_company(seed, f"{label}-SiteCo-{tag}")
    co_a = companies.create_company(seed, f"{label}-HomeCo-{tag}")
    site_b = sites.create_site(seed, co_b["id"], f"{label}-Tower-{tag}")
    folder = f"{label}-Eve-{tag}"
    eve = users.upsert_field_only_user(seed, co_a["id"], folder, "Eve", "Xu", "worker")
    if member:
        memberships.add_external_membership(seed, eve["id"], site_b["id"], "worker")
    session_base = "sid" + (tag + "0" * 32)[:32]
    # The session was opened by the app on the B site (company = B).
    meeting_session.ensure_open(seed, session_base[3:], co_b["id"], eve["id"], site_b["id"],
                                "audio", None)
    key = f"extractions/{folder}/{DATE}/{session_base}.json"
    return seed, co_a, co_b, site_b, folder, key


def _wire_iw(monkeypatch, iw, migrated_db_url, key):
    monkeypatch.setattr(iw, "_s3_client", _FakeS3({key: json.dumps(_extraction())}))
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: get_connection(migrated_db_url))
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)


def test_item_writer_writes_the_external_recorders_topics_on_site_b(monkeypatch, migrated_db_url):
    iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")
    seed, co_a, co_b, site_b, folder, key = _iw_world(migrated_db_url, "IW", member=True)
    _wire_iw(monkeypatch, iw, migrated_db_url, key)
    seen = {}
    monkeypatch.setattr(iw, "_request_match",
                        lambda company_id, *a, **k: seen.update(
                            match_company=company_id, **k))
    result = iw.write_extraction_items(DATE, folder, key)
    assert result == {"skipped": False, "topics": 1}, result
    row = seed.execute("SELECT site_id FROM topics WHERE source_s3_key=%s", (key,)).fetchone()
    assert str(row[0]) == str(site_b["id"])
    # P5: the voiceprint request is made in the SITE's company and names the home company.
    assert str(seen["match_company"]) == str(co_b["id"])
    assert str(seen["home_company_id"]) == str(co_a["id"])
    assert seen["recorder_user_id"] is not None


def test_item_writer_skips_a_session_site_the_recorder_has_no_membership_on(monkeypatch, migrated_db_url):
    iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")
    seed, _a, _b, _site, folder, key = _iw_world(migrated_db_url, "IW2", member=False)
    _wire_iw(monkeypatch, iw, migrated_db_url, key)
    result = iw.write_extraction_items(DATE, folder, key)
    assert result.get("skipped") is True
    assert seed.execute("SELECT count(*) FROM topics WHERE source_s3_key=%s", (key,)).fetchone()[0] == 0


def test_a_group_of_two_home_companies_on_one_external_site_can_be_joined(db):
    """The joiner is an A employee with an external membership; the lead is the
    site company's own employee. Before P2 the lead's company (B) != the joiner's
    home company (A) answered 'group not accessible'."""
    w = _world(db)
    lead_user = users.upsert_user(db, f"sub-lead-{w['tag']}", f"lead-{w['tag']}@b.nz",
                                  company_id=w["co_b"]["id"], first_name="Lea", last_name="Der",
                                  global_role="worker")
    lead_user = users.get_by_id(db, lead_user["id"])
    memberships.add_membership(db, lead_user["id"], w["site_b"]["id"], "worker")
    memberships.add_external_membership(db, w["eve"]["id"], w["site_b"]["id"], "worker")
    gid = uuid.uuid4().hex
    body = {"siteId": str(w["site_b"]["id"]), "kind": "audio"}
    assert api.session_open(db, lead_user, gid, body)["statusCode"] == 200
    joined = api.session_open(db, w["eve"], uuid.uuid4().hex, dict(body, groupId=gid))
    assert joined["statusCode"] == 200, joined
    members = meeting_session.list_group_members(db, gid)
    assert {str(m["company_id"]) for m in members} == {str(w["co_b"]["id"])}
    assert len(members) == 2
