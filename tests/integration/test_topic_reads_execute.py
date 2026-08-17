"""Integration: the topic reads that carry an inlined deleted-filter must actually run.

`topics.py` applies the deleted-topic exclusion by **inlining the same NOT EXISTS eleven
times** rather than calling the `_visible()` helper written for it — which has no callers at
all. Two of those copies correlate on `r.target_id = t.id` inside statements whose FROM
clause is `action_items` / `topic_photos`, where no alias `t` exists.

Postgres resolves aliases at analysis time, so those are not weak filters. They are
`ERROR: missing FROM-clause entry for table "t"` the moment the statement runs — and both
are on live paths: `list_topics_for_date` backs the `/live-items` endpoint, and
`get_topic_full` backs the reindex.

Neither could be caught by the unit suite: its connection doubles record SQL strings and
never parse them. This file asks Postgres.

Written RED on purpose and pushed before the fix, because "the SQL is invalid" is a claim
about a database, and the only honest way to make it is to let one answer.
"""
import uuid

import pytest

from repositories import companies, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-08-17"


def _seed(db):
    co = companies.create_company(db, f"Topic-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Topic-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "t@x.nz", company_id=co["id"])
    return co, s, u


def test_list_topics_for_date_runs_when_the_day_has_topics(db):
    """The function short-circuits on an empty day, so the broken child query only
    executes in the ordinary case: a day that HAS topics, which is every day the
    dashboard is looked at."""
    _co, s, u = _seed(db)
    topics.upsert_topic(
        db, s["id"], DATE, "Pour the slab", user_id=u["id"],
        source_s3_key=f"extractions/Folder/{DATE}/sidAAA.json",
        action_items=[{"text": "Order steel", "responsible": "James"}],
        photos=[{"s3_key": "users/Folder/pictures/x.jpg", "caption_text": "rebar"}])

    rows = topics.list_topics_for_date(db, [s["id"]], DATE)

    assert len(rows) == 1
    assert rows[0]["action_items"], "the child query ran but returned nothing"


def test_get_topic_full_runs(db):
    _co, s, u = _seed(db)
    t = topics.upsert_topic(
        db, s["id"], DATE, "Stairs", user_id=u["id"],
        photos=[{"s3_key": "users/Folder/pictures/y.jpg", "caption_text": "stair"}])

    full = topics.get_topic_full(db, t["id"])

    assert full is not None and full["id"] == t["id"]
    assert full["photos"], "the photo query ran but returned nothing"
