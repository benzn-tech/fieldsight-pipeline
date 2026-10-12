"""Moving a site to another company re-owns that site's lake objects, in the same transaction,
with no S3 call. Real Postgres; the S3 client raises on ANY call and records the attempt."""
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, lake_objects, sites

pytestmark = pytest.mark.integration


class ExplodingS3:
    """Every attribute is a method that records and raises: any S3 use is visible even if a
    caller swallows the exception."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            raise AssertionError(f"unexpected S3 call: {name}")
        return call


def _row(db, key):
    return db.execute("SELECT company_id, site_id, reason FROM lake_objects WHERE s3_key=%s",
                      (key,)).fetchone()


def _world(db):
    tag = uuid.uuid4().hex[:6]
    a = companies.create_company(db, f"MvA-{tag}")
    b = companies.create_company(db, f"MvB-{tag}")
    s = sites.create_site(db, b["id"], f"Moving-{tag}")
    other = sites.create_site(db, b["id"], f"Staying-{tag}")
    keys = {"s1": f"session_reports/{tag}/1.json", "s2": f"session_reports/{tag}/2.json",
            "nosite": f"transcripts/F_{tag}/2026-10-01/x.json",
            "other": f"session_reports/{tag}/other.json"}
    for k, site in (("s1", s), ("s2", s), ("nosite", None), ("other", other)):
        lake_objects.upsert(db, keys[k], company_id=b["id"], site_id=site and site["id"],
                            kind="x", source="upload")
    caller = {"id": str(uuid.uuid4()), "cognito_sub": f"sub-{tag}", "company_id": a["id"],
              "global_role": "platform_admin"}
    return a, b, s, keys, caller


def test_moving_a_site_re_owns_every_object_of_that_site_with_no_s3_call(db, monkeypatch):
    a, b, s, keys, caller = _world(db)
    fake = ExplodingS3()
    monkeypatch.setattr(api, "s3", lambda: fake)
    monkeypatch.setattr(api, "_s3_client", fake, raising=False)
    # The pre-existing config/site-coords.json publish is the one S3 use a move already had;
    # it is not part of the re-own, so it is stubbed to make "zero S3 calls" mean the re-own.
    monkeypatch.setattr(api, "_publish_site_coords", lambda row: None)

    res = api.patch_org_site(db, caller, str(s["id"]), {"target_company_id": str(a["id"])})

    assert res["statusCode"] == 200, res["body"]
    assert _row(db, keys["s1"])[0] == a["id"] and _row(db, keys["s2"])[0] == a["id"]
    assert _row(db, keys["nosite"])[0] == b["id"]          # no site: untouched
    assert _row(db, keys["other"])[0] == b["id"]           # another site of B: untouched
    assert sites.get_site(db, s["id"])["company_id"] == a["id"]
    assert fake.calls == []


def test_a_name_only_edit_re_owns_nothing(db, monkeypatch):
    a, b, s, keys, caller = _world(db)
    monkeypatch.setattr(api, "s3", lambda: ExplodingS3())
    res = api.patch_org_site(db, caller, str(s["id"]), {"name": "Renamed"})
    assert res["statusCode"] == 200
    assert _row(db, keys["s1"])[0] == b["id"]


def test_a_missing_table_does_not_stop_the_move(db, monkeypatch):
    a, b, s, keys, caller = _world(db)
    monkeypatch.setattr(api, "s3", lambda: ExplodingS3())
    db.execute("DROP TABLE lake_objects")
    res = api.patch_org_site(db, caller, str(s["id"]), {"target_company_id": str(a["id"])})
    assert res["statusCode"] == 200, res["body"]
    assert sites.get_site(db, s["id"])["company_id"] == a["id"]
