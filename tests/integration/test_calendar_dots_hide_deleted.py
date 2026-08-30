"""Integration: the date picker's dots must not survive a deletion.

`report_date_counts` backs the calendar's density dot and its orange safety dot. Its
deleted-topic exclusion had been spliced into the `findings` EXISTS subquery instead of the
outer WHERE, so `COUNT(*) AS topics` counted deleted topics and the safety count filtered
exactly one of its three arms. The calendar kept saying *something happened on this day* for
a day whose recording the customer had removed — not the words, but the fact.

Its sibling `list_report_dates` had the predicate in the right clause all along, which is
what makes this a misplacement rather than an omission, and why both are asserted here: one
endpoint, two queries, and until now only one of them right.

Integration, because the source-text guard in `tests/unit/test_deleted_read_paths.py` was
**green throughout** — the predicate text was present in the function, in the wrong clause.
That test says so itself: "Text is not SQL." Only a database can tell the two apart.

Both arms are asserted separately. The source arm is the one that matters for a calendar:
`lambda_ingest` re-creates a superseded day's topics with new uuids that no topic-keyed
tombstone names, so a topic-only filter hides the dot tonight and draws it again after the
nightly rebuild.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = "2026-08-29"
SOURCE = f"extractions/Folder/{DAY}/sid{'e' * 32}.json"


def _seed(db):
    co = companies.create_company(db, f"Cal-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Cal-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "c@x.nz", company_id=co["id"])
    return co, s, u


def _counts_for(db, site_id):
    return {str(r["report_date"]): r
            for r in topics.report_date_counts(db, [site_id], "2026-01-01")}


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_deleted_day_loses_its_density_dot(db, arm):
    co, site, user = _seed(db)
    t = topics.upsert_topic(db, site["id"], DAY, "Only topic that day",
                            user_id=user["id"], source_s3_key=SOURCE, summary="s")

    assert _counts_for(db, site["id"])[DAY]["topics"] == 1

    if arm == "topic":
        redactions.create_redaction(db, co["id"], t["id"], "removed", user["id"], "admin",
                                    target_type="topic", scope="deleted")
    else:
        redactions.create_recording_tombstone(db, co["id"], SOURCE, "removed", user["id"],
                                              "admin")

    assert DAY not in _counts_for(db, site["id"]), (
        f"the {arm} arm left a density dot on a day whose only recording was removed")


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_deleted_topic_loses_its_safety_dot_on_every_arm(db, arm):
    """`safety` is a UNION of three signals and only the findings one was filtered, so a
    topic whose safety came from `category` or a `safety_observations` row kept its orange
    dot after the delete."""
    co, site, user = _seed(db)
    live = topics.upsert_topic(db, site["id"], DAY, "Live", user_id=user["id"],
                               source_s3_key=f"extractions/Folder/{DAY}/sid{'f' * 32}.json",
                               summary="s")
    gone = topics.upsert_topic(
        db, site["id"], DAY, "Removed", user_id=user["id"], source_s3_key=SOURCE,
        category="safety", summary="s",
        safety=[{"observation": "no edge protection", "risk_level": "high"}])
    assert live and gone

    before = _counts_for(db, site["id"])[DAY]
    assert before["topics"] == 2 and before["safety"] == 1

    if arm == "topic":
        redactions.create_redaction(db, co["id"], gone["id"], "removed", user["id"], "admin",
                                    target_type="topic", scope="deleted")
    else:
        redactions.create_recording_tombstone(db, co["id"], SOURCE, "removed", user["id"],
                                              "admin")

    after = _counts_for(db, site["id"])[DAY]
    assert after["topics"] == 1, f"{arm} arm: the removed topic still counts"
    assert after["safety"] == 0, (
        f"{arm} arm: the removed topic still draws a safety dot — the category and "
        f"safety_observations arms were never filtered")


def test_the_two_queries_behind_one_endpoint_agree(db):
    """`/dates` calls `list_report_dates` for the days and `report_date_counts` for the dots.
    A day that one hides and the other counts is the shape this fix exists to remove."""
    co, site, user = _seed(db)
    t = topics.upsert_topic(db, site["id"], DAY, "Only topic", user_id=user["id"],
                            source_s3_key=SOURCE, summary="s")
    redactions.create_redaction(db, co["id"], t["id"], "removed", user["id"], "admin",
                                target_type="topic", scope="deleted")

    dates = {str(d) for d in topics.list_report_dates(db, [site["id"]], "2026-01-01")}
    counted = set(_counts_for(db, site["id"]))
    assert counted <= dates, (
        f"the dots name days the date list hides: {sorted(counted - dates)}")
