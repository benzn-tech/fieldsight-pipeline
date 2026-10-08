"""Project-owned tenancy P3/P4 against a real Postgres: what each company sees of
a person's day is the part captured on ITS sites; the person sees all of it.

A connection double never parses SQL, so the site clip, the author joins that no
longer pin the author's company and the foreign-session filter are only proven
here.
"""
import json
import uuid

import pytest

import lambda_ingest as ing
import lambda_org_api as api
import lambda_rag_search as rag
from repositories import (chunks, companies, memberships, recordings, sites, topics,
                          users)

pytestmark = pytest.mark.integration

DATE = "2026-10-08"
VERBATIM = {"executive_summary": "WHOLE DAY PROSE", "topics": []}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api, "GRADED_ROLES", True)
    monkeypatch.setattr(api, "_get_lake_json", lambda key: dict(VERBATIM)
                        if key.endswith("daily_report.json") else None)
    monkeypatch.setattr(api, "_merged_keys_for", lambda conn, uid, date: [])


def _user(db, co, role, tag, who):
    sub = f"sub-{who}-{tag}"
    row = users.upsert_user(db, sub, f"{who}-{tag}@x.nz", company_id=co["id"],
                            first_name=who.title(), last_name="Test", global_role=role)
    users.set_folder_name(db, sub, f"{who.title()}_Test_{tag}")
    return users.get_by_id(db, row["id"])


def _world(db):
    tag = uuid.uuid4().hex[:6]
    co_a = companies.create_company(db, f"HomeCo-{tag}")
    co_b = companies.create_company(db, f"SiteCo-{tag}")
    site_a = sites.create_site(db, co_a["id"], "Yard A")
    site_b = sites.create_site(db, co_b["id"], "Tower B")
    eve = _user(db, co_a, "worker", tag, "eve")
    admin_a = _user(db, co_a, "admin", tag, "adma")
    admin_b = _user(db, co_b, "admin", tag, "admb")
    memberships.add_membership(db, eve["id"], site_a["id"], "worker")
    memberships.add_external_membership(db, eve["id"], site_b["id"], "worker")
    return dict(tag=tag, co_a=co_a, co_b=co_b, site_a=site_a, site_b=site_b, eve=eve,
                admin_a=admin_a, admin_b=admin_b, folder=eve["folder_name"])


def _topic(db, w, site, title, sid):
    key = f"extractions/{w['folder']}/{DATE}/sid{sid}.json"
    return topics.upsert_topic(db, site["id"], DATE, title, user_id=w["eve"]["id"],
                               source_s3_key=key, time_range="09:00 - 09:30")


def _two_site_day(db, w):
    _topic(db, w, w["site_a"], "Yard A pour", "a" * 32)
    _topic(db, w, w["site_b"], "Tower B steel", "b" * 32)


def _timeline(db, caller, user=None):
    ev = {"queryStringParameters": {"date": DATE, **({"user": user} if user else {})}}
    res = api.get_timeline_compat(db, caller, ev)
    return res["statusCode"], json.loads(res["body"])


def _titles(body):
    return sorted(t.get("title") or t.get("topic_title") or "" for t in body.get("topics") or [])


def test_each_company_sees_only_its_own_sites_part_of_the_day(db):
    w = _world(db)
    _two_site_day(db, w)
    code, body = _timeline(db, w["admin_a"], w["folder"])
    assert code == 200 and _titles(body) == ["Yard A pour"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)
    code, body = _timeline(db, w["admin_b"], w["folder"])        # B resolves A's person
    assert code == 200 and _titles(body) == ["Tower B steel"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)


def test_the_person_sees_the_whole_day(db):
    w = _world(db)
    _two_site_day(db, w)
    code, body = _timeline(db, w["eve"])
    assert code == 200 and _titles(body) == ["Tower B steel", "Yard A pour"]


def test_a_day_with_only_the_other_companys_rows_is_404_for_the_home_admin(db):
    w = _world(db)
    _topic(db, w, w["site_b"], "Tower B steel", "b" * 32)
    code, _ = _timeline(db, w["admin_a"], w["folder"])
    assert code == 404                      # not the verbatim doc either
    code, body = _timeline(db, w["admin_b"], w["folder"])
    assert code == 200 and _titles(body) == ["Tower B steel"]


def test_the_verbatim_report_is_not_served_across_companies(db):
    """No topics at all, but a recording on the other company's site that day."""
    w = _world(db)
    recordings.insert_pending(db, company_id=w["co_b"]["id"], user_id=w["eve"]["id"],
                              site_id=w["site_b"]["id"], kind="audio",
                              s3_key=f"users/{w['folder']}/audio/{DATE}/Eve_100000.wav",
                              client_uuid=uuid.uuid4().hex, started_at=f"{DATE}T10:00:00Z")
    code, _ = _timeline(db, w["admin_a"], w["folder"])
    assert code == 404
    code, body = _timeline(db, w["eve"])
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"


def test_the_verbatim_report_is_still_served_when_the_whole_day_is_in_scope(db):
    w = _world(db)
    code, body = _timeline(db, w["admin_a"], w["folder"])
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"


def test_a_company_cannot_open_a_person_with_no_footprint_on_its_sites(db):
    w = _world(db)
    stranger = _user(db, w["co_a"], "worker", w["tag"], "zed")
    code, _ = _timeline(db, w["admin_b"], stranger["folder_name"])
    assert code == 404


def test_the_candidate_list_is_by_site_not_by_author_company(db, monkeypatch):
    w = _world(db)
    _two_site_day(db, w)
    monkeypatch.setattr(api, "_list_report_folders", lambda date: [])
    code, body = _timeline(db, w["admin_b"])          # no ?user=
    # one candidate (the A person) -> straight into the clipped single-user view
    assert code == 200 and body["user"] == w["folder"] and _titles(body) == ["Tower B steel"]


def test_foreign_sessions_are_hidden_from_the_media_listings(db):
    w = _world(db)
    for site, sid in ((w["site_a"], "a" * 32), (w["site_b"], "b" * 32)):
        recordings.insert_pending(db, company_id=site["company_id"], user_id=w["eve"]["id"],
                                  site_id=site["id"], kind="audio",
                                  s3_key=f"users/{w['folder']}/audio/{DATE}/x_sid{sid}_c0000.wav",
                                  client_uuid=uuid.uuid4().hex, started_at=f"{DATE}T10:00:00Z")
    assert api._foreign_session_ids(db, w["admin_a"], w["folder"], DATE) == {"sid" + "b" * 32}
    assert api._foreign_session_ids(db, w["admin_b"], w["folder"], DATE) == {"sid" + "a" * 32}
    assert api._foreign_session_ids(db, w["eve"], w["folder"], DATE) == set()


def test_observation_author_folder_is_found_for_an_external_author(db):
    from repositories import observations
    w = _world(db)
    obs = observations.create_observation(
        db, w["co_b"]["id"], "safety", "tower-b", w["eve"]["cognito_sub"], "Eve",
        "Unsecured edge", report_date=DATE)
    assert obs["author_folder"] == w["folder"]


def test_rag_search_returns_the_b_site_chunk_to_b_and_not_to_a(db, monkeypatch):
    w = _world(db)
    emb = [0.1] * 1024
    for site, text in ((w["site_a"], "yard pour"), (w["site_b"], "tower steel")):
        chunks.insert_chunk(db, site["id"], DATE, "topic", text, emb, user_id=w["eve"]["id"])
    monkeypatch.setattr(rag, "get_cached_connection", lambda: db)
    for caller, expect in ((w["admin_b"], "tower steel"), (w["admin_a"], "yard pour")):
        res = rag._search({"sub": caller["cognito_sub"], "query_embedding": emb, "k": 8,
                           "author": w["folder"]}, None)
        texts = [c["chunk_text"] for c in res["chunks"]]
        assert texts == [expect], (caller["folder_name"], res)


# ---- Part 0: the last-resort guess never moves home work into another company ----

def test_resolve_site_guess_prefers_the_home_company(db):
    w = _world(db)
    # A recorder whose EXTERNAL membership is the older row (the order a bare
    # "first accessible site" would pick).
    kai = _user(db, w["co_a"], "worker", w["tag"], "kai")
    memberships.add_external_membership(db, kai["id"], w["site_b"]["id"], "worker")
    memberships.add_membership(db, kai["id"], w["site_a"]["id"], "worker")
    got = ing.resolve_site(db, w["co_a"]["id"], {"site": "noise"}, kai["folder_name"])
    assert str(got["id"]) == str(w["site_a"]["id"])
    # Eve holds both; whatever order the memberships come back in, the guess is home.
    for _ in range(3):
        got = ing.resolve_site(db, w["co_a"]["id"], {"site": "noise"}, w["folder"])
        assert str(got["id"]) == str(w["site_a"]["id"])
    # No home-company site at all: the external site is the only thing left.
    memberships.archive_membership(db, w["eve"]["id"], w["site_a"]["id"])
    got = ing.resolve_site(db, w["co_a"]["id"], {"site": "noise"}, w["folder"])
    assert str(got["id"]) == str(w["site_b"]["id"])


def test_session_owner_lookup_finds_an_external_author_on_the_site_company(db):
    w = _world(db)
    _topic(db, w, w["site_b"], "Tower B steel", "c" * 32)
    got = topics.folders_for_session_base(db, str(w["co_b"]["id"]), "sid" + "c" * 32)
    assert got == [w["folder"]]


def test_external_membership_is_visible_to_the_company_pin_helper(db):
    w = _world(db)
    stranger = _user(db, w["co_a"], "worker", w["tag"], "zed")
    assert memberships.has_live_external(db, w["eve"]["id"]) is True
    assert memberships.has_live_external(db, stranger["id"]) is False
    memberships.archive_membership(db, w["eve"]["id"], w["site_b"]["id"])
    assert memberships.has_live_external(db, w["eve"]["id"]) is False


def test_the_persons_own_day_kpi_counts_every_companys_recordings(db):
    w = _world(db)
    for site, sid in ((w["site_a"], "a" * 32), (w["site_b"], "b" * 32)):
        recordings.insert_pending(db, company_id=site["company_id"], user_id=w["eve"]["id"],
                                  site_id=site["id"], kind="audio",
                                  s3_key=f"users/{w['folder']}/audio/{DATE}/x_sid{sid}_c0000.wav",
                                  client_uuid=uuid.uuid4().hex, started_at=f"{DATE}T10:00:00Z")
    assert recordings.day_stats(db, None, w["folder"], DATE)["sessions"] == 2
    assert recordings.day_stats(db, w["co_a"]["id"], w["folder"], DATE)["sessions"] == 1
    assert recordings.day_stats(db, w["co_b"]["id"], w["folder"], DATE)["sessions"] == 1
