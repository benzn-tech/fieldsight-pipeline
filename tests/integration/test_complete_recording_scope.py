"""Integration: `/complete` scopes on the recorder, not the caller's company.

`/upload-url` stores the SITE's company on the recordings row
(`_capture_company_id`), so a member capturing onto another company's site
owns a row whose `company_id` is not their own. `/complete` used to scope on
`caller["company_id"]` -- both in the verify pre-check and in
`mark_uploaded`'s WHERE -- so every such upload 404'd and stayed un-uploaded.
The recorder is the only stable owner of the row, so that is the scope.
"""
import json
import uuid

import pytest

from repositories import companies, memberships, recordings, sites, users

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

pytestmark = pytest.mark.integration

SIZE = 4096


def _seed(db):
    tag = uuid.uuid4().hex[:6]
    co_a = companies.create_company(db, f"Home-Co-{tag}")
    co_b = companies.create_company(db, f"Partner-Co-{tag}")
    site_b = sites.create_site(db, co_b["id"], f"Partner-Site-{tag}")
    caller = users.upsert_user(db, f"sub-a-{tag}", f"a-{tag}@home.nz", company_id=co_a["id"])
    memberships.add_membership(db, caller["id"], site_b["id"], "worker")
    rec = recordings.insert_pending(
        db, company_id=co_b["id"], user_id=caller["id"], site_id=site_b["id"], kind="photo",
        s3_key=f"users/a/pictures/2026-10-21/{tag}.jpg", client_uuid=f"cap-{tag}",
        started_at="2026-10-21T01:00:00Z",
    )
    return co_a, co_b, site_b, caller, rec


def _uploaded_at(db, rec_id):
    return db.execute("SELECT uploaded_at FROM recordings WHERE id=%s", (rec_id,)).fetchone()[0]


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_member_on_partner_site_completes(db, monkeypatch, mode):
    _, _, _, caller, rec = _seed(db)
    monkeypatch.setattr(org, "UPLOAD_VERIFY_MODE", mode)
    monkeypatch.setattr(org, "_object_size", lambda key: SIZE)

    res = org.complete_recording(db, caller, rec["id"], {})

    assert res["statusCode"] == 200, res
    assert json.loads(res["body"]) == {"ok": True}
    assert _uploaded_at(db, rec["id"]) is not None


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_other_user_gets_404(db, monkeypatch, mode):
    _, co_b, _, _, rec = _seed(db)
    other = users.upsert_user(db, f"sub-b-{uuid.uuid4().hex[:6]}", "b@partner.nz",
                              company_id=co_b["id"])
    monkeypatch.setattr(org, "UPLOAD_VERIFY_MODE", mode)
    monkeypatch.setattr(org, "_object_size", lambda key: SIZE)

    res = org.complete_recording(db, other, rec["id"], {})

    assert res["statusCode"] == 404, res
    assert _uploaded_at(db, rec["id"]) is None
