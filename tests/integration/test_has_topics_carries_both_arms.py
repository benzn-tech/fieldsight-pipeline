"""Integration: the third zero's discriminator must not survive a deletion either.

`has_topics_in_range` is what lets the metric route say *"there are notes on this
date, but no recording data was registered for it"* instead of *"you recorded
nothing"*. It carried only the topic arm, copied from `list_report_dates`.

The topic arm alone stops working overnight. `redactions.create_redaction`'s own
docstring says why: `lambda_ingest` deletes a superseded day's topics and
re-inserts them with new uuids, so a tombstone holding a topic uuid stops
matching within a day, and only the source arm — `source_s3_key` against the
deleted prefix — survives.

So the leak was dated rather than immediate, which is exactly why it read as
working. Delete a recording; the next day, ask about that date; the answer
asserts that notes still exist for the thing that was deleted, to the person who
deleted it.

Integration, because this is a SQL predicate and the repo's fake connection
records SQL without parsing it. Skipped without TEST_DATABASE_URL — A SKIP IS
NOT A PASS.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = "2026-08-25"
SOURCE = f"extractions/Folder/{DAY}/sid{'a' * 32}.json"


def _seed(db):
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"Arm-Co-{tag}")
    site = sites.create_site(db, co["id"], f"Arm-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Arm_{tag}", f"Arm_{tag}",
                                        "", "worker")
    return co, site, user


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_removed_day_stops_reporting_notes_on_either_arm(db, arm):
    co, site, user = _seed(db)
    t = topics.upsert_topic(db, site["id"], DAY, "Only topic that day",
                            user_id=user["id"], source_s3_key=SOURCE, summary="s")
    assert t
    assert topics.has_topics_in_range(db, [site["id"]], DAY, DAY) is True

    if arm == "topic":
        redactions.create_redaction(db, co["id"], t["id"], "removed", user["id"],
                                    "admin", target_type="topic", scope="deleted")
    else:
        redactions.create_recording_tombstone(db, co["id"], SOURCE, "removed",
                                              user["id"], "admin")

    assert topics.has_topics_in_range(db, [site["id"]], DAY, DAY) is False, (
        f"the {arm} arm still reports notes for a day whose only recording was "
        f"removed — the metric route turns that into 'there are notes on this "
        f"date' addressed to the person who deleted it"
    )


def test_the_source_arm_is_the_one_that_survives_a_rebuild(db):
    """The rebuild, simulated: the topic keeps its source key and gets a new
    uuid, so the topic-keyed tombstone no longer names it. Only the source arm
    can still hide it — which is the whole reason this test exists rather than
    trusting the parametrised pair above."""
    co, site, user = _seed(db)
    original = topics.upsert_topic(db, site["id"], DAY, "Before the rebuild",
                                   user_id=user["id"], source_s3_key=SOURCE,
                                   summary="s")
    redactions.create_redaction(db, co["id"], original["id"], "removed",
                                user["id"], "admin", target_type="topic",
                                scope="deleted")
    redactions.create_recording_tombstone(db, co["id"], SOURCE, "removed",
                                          user["id"], "admin")
    assert topics.has_topics_in_range(db, [site["id"]], DAY, DAY) is False

    # lambda_ingest re-inserts the day under a new uuid with the same source key
    db.cursor().execute("DELETE FROM topics WHERE id = %s", (original["id"],))
    rebuilt = topics.upsert_topic(db, site["id"], DAY, "After the rebuild",
                                  user_id=user["id"], source_s3_key=SOURCE,
                                  summary="s")
    assert rebuilt["id"] != original["id"], "the rebuild reused the uuid"

    assert topics.has_topics_in_range(db, [site["id"]], DAY, DAY) is False, (
        "the rebuilt topic reports notes again: the topic-keyed tombstone no "
        "longer names it and the source arm was missing"
    )


def test_a_live_day_still_reports_notes(db):
    """The cost of the fix, bounded: nothing that should be visible was hidden."""
    co, site, user = _seed(db)
    topics.upsert_topic(db, site["id"], DAY, "Live", user_id=user["id"],
                        source_s3_key=SOURCE, summary="s")
    assert topics.has_topics_in_range(db, [site["id"]], DAY, DAY) is True
