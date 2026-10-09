"""/me site_ids must list every project the caller can record on.

Capture accepts any live membership (project-owned tenancy P2), but an admin/gm's
read reach is their own company's sites, so a gm added to another company's
project as an external worker could not pick that project: /me left it out
(TEST, 2026-10-09)."""
import json

import pytest

org = pytest.importorskip("lambda_org_api")


def test_an_external_membership_is_listed_for_a_gm(monkeypatch):
    home, foreign = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {home})
    monkeypatch.setattr(org.memberships, "caller_site_roles",
                        lambda conn, uid: {foreign: "worker"})
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, caller: {"user_scope": "ALL"})
    monkeypatch.setattr(org.companies, "get_company_by_id", lambda conn, cid: {"name": "Home"})
    caller = {"id": "u1", "global_role": "gm", "company_id": "c1"}
    body = json.loads(org.get_me(None, caller)["body"])
    assert body["site_ids"] == sorted([home, foreign])
