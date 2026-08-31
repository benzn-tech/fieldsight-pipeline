"""Integration: a delete applies to every branch of a read.

`list_topics_for_date` backs the dashboard's day view. Its WHERE is built in
two halves -- an ACL half, and an OR branch naming the merged artifact keys of
multi-device groups the caller belongs to, so a joiner's day is not blank.

The deletion exclusion used to sit INSIDE the first half. So the merged branch
carried no deletion filter at all, and a merged session's topics came back for
every member of the group the moment it was deleted -- immediately, not after
the rebuild. The comment on that OR justifies bypassing the ACL, which is
deliberate. Nothing ever justified bypassing a delete.

The first half was also the topic arm alone, which stops naming a row once
`lambda_ingest` re-inserts the day under new uuids.

Both are asserted here, and only a database can tell either apart from a
correct query: the predicate text was present in the function throughout.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS -- read the CI run.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = "2026-08-23"


def _seed(db):
    tag = uuid.uuid4().hex[:6]
    co = companies.create_company(db, f"Merge-Co-{tag}")
    site = sites.create_site(db, co["id"], f"Merge-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Merge_{tag}",
                                        f"Merge_{tag}", "", "worker")
    return co, site, user, tag


def test_the_merged_branch_hides_a_deleted_topic(db):
    """The immediate leak: tombstone the topic itself, then ask for the day with
    that topic's source key in `merged_keys`. Before, the OR branch returned it
    with no deletion filter in sight."""
    co, site, user, tag = _seed(db)
    merged_key = f"reports/{DAY}/Merge_{tag}/merged_session.json"
    t = topics.upsert_topic(db, site["id"], DAY, "Merged meeting",
                            user_id=user["id"], source_s3_key=merged_key, summary="s")
    seen = topics.list_topics_for_date(db, [site["id"]], DAY, merged_keys=[merged_key])
    assert any(r["id"] == t["id"] for r in seen), "the merged branch never returned it"

    redactions.create_redaction(db, co["id"], t["id"], "removed", user["id"],
                                "admin", target_type="topic", scope="deleted")

    seen = topics.list_topics_for_date(db, [site["id"]], DAY, merged_keys=[merged_key])
    assert not any(r["id"] == t["id"] for r in seen), (
        "a deleted merged-session topic came back through the OR branch, which "
        "carried no deletion filter — for every member of the group"
    )


def test_the_merged_branch_hides_a_source_tombstoned_topic(db):
    """The dated leak, on the branch that had no filter at all: only the source
    arm can name a rebuilt row, and the merged branch had neither arm."""
    co, site, user, tag = _seed(db)
    merged_key = f"reports/{DAY}/Merge_{tag}/merged_session.json"
    redactions.create_recording_tombstone(db, co["id"], merged_key, "removed",
                                          user["id"], "admin")
    t = topics.upsert_topic(db, site["id"], DAY, "Rebuilt merged meeting",
                            user_id=user["id"], source_s3_key=merged_key, summary="s")
    seen = topics.list_topics_for_date(db, [site["id"]], DAY, merged_keys=[merged_key])
    assert not any(r["id"] == t["id"] for r in seen), (
        "a rebuilt merged topic whose recording is tombstoned by prefix came back"
    )


def test_the_acl_branch_hides_a_source_tombstoned_topic(db):
    """The ordinary branch, which had the topic arm only."""
    co, site, user, tag = _seed(db)
    source = f"extractions/Merge_{tag}/{DAY}/sid{'d' * 32}.json"
    redactions.create_recording_tombstone(db, co["id"], source, "removed",
                                          user["id"], "admin")
    t = topics.upsert_topic(db, site["id"], DAY, "Rebuilt after delete",
                            user_id=user["id"], source_s3_key=source, summary="s")
    seen = topics.list_topics_for_date(db, [site["id"]], DAY)
    assert not any(r["id"] == t["id"] for r in seen)


def test_a_live_merged_topic_still_reaches_its_group(db):
    """The cost of the fix, bounded: the OR branch exists so a joiner's day is
    not blank, and it must still do that."""
    co, site, user, tag = _seed(db)
    merged_key = f"reports/{DAY}/Merge_{tag}/merged_session.json"
    t = topics.upsert_topic(db, site["id"], DAY, "Live merged meeting",
                            user_id=user["id"], source_s3_key=merged_key, summary="s")
    # a site the caller cannot see, so only the merged branch can return it
    other = sites.create_site(db, co["id"], f"Other-Site-{tag}")
    seen = topics.list_topics_for_date(db, [other["id"]], DAY, merged_keys=[merged_key])
    assert any(r["id"] == t["id"] for r in seen), (
        "the fix cut the merged branch off from its group — the blank-day bug it "
        "was written to prevent"
    )
