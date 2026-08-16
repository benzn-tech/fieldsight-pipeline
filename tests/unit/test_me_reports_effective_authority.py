"""Unit: /me must report the authority the read paths actually apply.

Reported from prod, 2026-08-16: a user promoted to site_manager still showed as "worker"
in their profile and everywhere in the UI.

It was not a failed promotion. Authority has two dimensions:

* `users.global_role` — one value per person;
* `memberships.role` — one per site, and `acl.visible_user_scope` treats it as a FLOOR
  OVER the global role. Its own docstring names the case: "a global 'worker' with a pm
  membership -> SITE".

So the promotion took effect for what data the person can reach, while `/me` returned
`global_role` and a `scope` computed from `global_role` alone, and never mentioned the
site memberships at all. The frontend had no other source, so it could only render
"worker" — correctly, from what it was given.

These tests pin both halves: the new fields exist, and `effective_scope` reports what the
graded path really applies rather than what the global role suggests.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

org = pytest.importorskip("lambda_org_api")

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "worker",
          "folder_name": "Ben_UCPK2"}


def _base(monkeypatch, site_roles, user_scope):
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1"})
    monkeypatch.setattr(org.memberships, "caller_site_roles",
                        lambda conn, uid: dict(site_roles))
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, caller: {
        "user_scope": user_scope, "site_ids": {"s-1"}, "author_ids": None,
        "self_folder": "Ben_UCPK2", "cross_company": False})
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, cid: {"name": "UCPK"})


def test_a_site_promotion_is_visible_to_the_frontend(monkeypatch):
    """The reported bug. A global 'worker' with a site_manager membership reaches
    SELF+WORKERS, and the UI must be able to see that."""
    _base(monkeypatch, {"s-1": "site_manager"}, "SELF+WORKERS")
    body = json.loads(org.get_me(object(), dict(CALLER))["body"])
    assert body["site_roles"] == {"s-1": "site_manager"}
    assert body["effective_scope"] == "SELF+WORKERS"


def test_the_global_role_is_still_reported_unchanged(monkeypatch):
    """The promotion did not change `users.global_role`, and /me must not pretend it did.
    Two dimensions, both reported — inventing a global role here would make the admin
    Team page and the profile disagree about the same person."""
    _base(monkeypatch, {"s-1": "site_manager"}, "SELF+WORKERS")
    body = json.loads(org.get_me(object(), dict(CALLER))["body"])
    assert body["global_role"] == "worker"


def test_a_plain_worker_is_unchanged(monkeypatch):
    """No membership authority, no promotion. The common case must not shift."""
    _base(monkeypatch, {"s-1": "worker"}, "SELF")
    body = json.loads(org.get_me(object(), dict(CALLER))["body"])
    assert body["effective_scope"] == "SELF"
    assert body["site_roles"] == {"s-1": "worker"}


def test_effective_scope_is_null_when_grading_is_off(monkeypatch):
    """With GRADED_ROLES off the membership floor is not applied to reads either, so
    reporting SELF+WORKERS would be a second wrong answer rather than a fix. Say nothing
    rather than say something false."""
    _base(monkeypatch, {"s-1": "site_manager"}, "SELF+WORKERS")
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    monkeypatch.setattr(org.memberships, "accessible_site_ids",
                        lambda conn, uid, role: ["s-1"])
    body = json.loads(org.get_me(object(), dict(CALLER))["body"])
    assert body["effective_scope"] is None


def test_the_internal_memo_is_still_not_echoed(monkeypatch):
    """`visible_scope` memoizes itself onto the caller dict, and reading it here is a new
    chance to leak it into the response — the exact thing the existing strip guards."""
    _base(monkeypatch, {"s-1": "site_manager"}, "SELF+WORKERS")
    caller = dict(CALLER)
    caller["_visible_scope"] = {"internal": True}
    body = json.loads(org.get_me(object(), caller)["body"])
    assert "_visible_scope" not in body
