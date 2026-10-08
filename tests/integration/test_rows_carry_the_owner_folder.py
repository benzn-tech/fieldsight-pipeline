"""Integration: per-row payloads carry the owner's FOLDER next to the display name.

A recording folder is an identity key (`Deandre__Alberts`); it can never be rebuilt from the
display name (`Deandre' Alberts` -> `Deandre'_Alberts` is a different, wrong key). The web
used to rebuild it from `user_name` / `author_name` on every row. These tests ask Postgres
that the directory's folder_name rides along: `user_folder` on topic rows (live-items and the
report reads) and `author_folder` on observations. Additive: the display names stay.
"""
import uuid

import pytest

from repositories import companies, observations, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-08-17"
FOLDER = "Deandre__Alberts"


def _owner(db, co):
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    u = users.upsert_user(db, sub, "d@x.nz", company_id=co["id"],
                          first_name="Deandre'", last_name="Alberts")
    db.cursor().execute("UPDATE users SET folder_name=%s WHERE id=%s", (FOLDER, u["id"]))
    return sub, u


def test_topic_rows_carry_user_folder(db):
    co = companies.create_company(db, f"Row-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Row-Site")
    _sub, u = _owner(db, co)
    src = f"extractions/{FOLDER}/{DATE}/sidROW.json"
    t = topics.upsert_topic(db, s["id"], DATE, "Slab", user_id=u["id"], source_s3_key=src)

    live = topics.list_topics_for_date(db, [s["id"]], DATE)
    assert live[0]["user_name"] == "Deandre' Alberts"
    assert live[0]["user_folder"] == FOLDER

    by_prefix = topics.list_topics_for_source_prefix(db, f"extractions/{FOLDER}/{DATE}/")
    assert by_prefix[0]["user_folder"] == FOLDER

    assert topics.get_topic_full(db, t["id"])["user_folder"] == FOLDER


def test_topic_without_a_linked_user_has_null_folder(db):
    co = companies.create_company(db, f"Row-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Row-Site")
    topics.upsert_topic(db, s["id"], DATE, "Orphan", user_id=None)

    rows = topics.list_topics_for_date(db, [s["id"]], DATE)
    assert rows[0]["user_folder"] is None


def test_observations_carry_author_folder(db):
    co = companies.create_company(db, f"Row-Co-{uuid.uuid4().hex[:6]}")
    sub, _u = _owner(db, co)
    created = observations.create_observation(
        db, co["id"], "safety", "row-site", sub, "Deandre' Alberts", "Open edge",
        report_date=DATE)
    assert created["author_folder"] == FOLDER

    rows = observations.list_observations(db, co["id"])
    assert rows[0]["author_name"] == "Deandre' Alberts"
    assert rows[0]["author_folder"] == FOLDER
    assert observations.get_observation(db, co["id"], created["id"])["author_folder"] == FOLDER
    assert observations.set_status(db, co["id"], created["id"], "closed")["author_folder"] == FOLDER


def test_observation_author_with_no_directory_row_is_null(db):
    co = companies.create_company(db, f"Row-Co-{uuid.uuid4().hex[:6]}")
    observations.create_observation(
        db, co["id"], "quality", "row-site", "sub-nobody", "Ghost", "Crack",
        report_date=DATE)
    assert observations.list_observations(db, co["id"])[0]["author_folder"] is None


def test_observation_author_in_another_company_is_not_leaked(db):
    co = companies.create_company(db, f"Row-Co-{uuid.uuid4().hex[:6]}")
    other = companies.create_company(db, f"Row-Other-{uuid.uuid4().hex[:6]}")
    sub, _u = _owner(db, other)
    observations.create_observation(
        db, co["id"], "safety", "row-site", sub, "Deandre' Alberts", "Open edge",
        report_date=DATE)
    assert observations.list_observations(db, co["id"])[0]["author_folder"] is None
