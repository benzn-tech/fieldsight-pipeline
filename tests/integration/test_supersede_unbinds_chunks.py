"""Integration: Ruling R21 -- supersede must unbind report_chunks, not leave them hidden.

Before Track B, `delete_topics_for_source`'s DELETE let
`report_chunks.topic_id REFERENCES topics(id) ON DELETE SET NULL` unbind a retired
topic's chunks for free, so they fell into the `topic_id IS NULL` bucket that
`visible_chunks_predicate` always shows, and the next nightly re-ingest of that source key
deleted them by `delete_chunks_for_source` (no duplicates). Track B Task 3 stopped
deleting the row (it marks `superseded_at` instead), so that SET NULL never fires: a
topic superseded, TASK 8's live TEST check found, that the retired topic's chunk stayed
BOUND, `visible_chunks_predicate`'s superseded-topic arm hid it, and the new live topic
that replaced it had none of its own until the day's report was re-ingested -- possibly
never, for an old day. A session silently dropped out of search/Ask.

`test_search_chunks_returns_the_formerly_bound_chunk_after_a_real_supersede_call` is the
one that was proven RED against the pre-fix `supersede_topics_for_source` (git-stashed
the fix and re-ran this file: FAILED, the chunk stayed invisible) before the unbind was
added -- see the follow-up report for the exact command.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import os
import uuid

import pytest

from repositories import chunks, companies, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-29"
MIGRATION_0074 = os.path.join(
    os.path.dirname(__file__), "..", "..", "src", "migrations",
    "0074_unbind_superseded_chunks.sql")


def _unit_vec(dim, hot):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


def _seed(db, tag_suffix=""):
    tag = uuid.uuid4().hex[:8] + tag_suffix
    co = companies.create_company(db, f"UnbindChunks-Co-{tag}")
    site = sites.create_site(db, co["id"], f"UnbindChunks-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"UnbindFolder-{tag}", "Fol", "Der",
                                        "worker")
    return co, site, user


def test_search_chunks_returns_the_formerly_bound_chunk_after_a_real_supersede_call(db):
    co, site, user = _seed(db)
    source = f"extractions/{user['folder_name']}/{DATE}/sid{'d' * 32}.json"

    topic_a = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- live pass", user_id=user["id"],
        source_s3_key=source, summary="s")
    chunk_a = chunks.insert_chunk(
        db, site["id"], DATE, "topic", "pass one transcript text", _unit_vec(1024, 0),
        source_s3_key=source, topic_id=topic_a["id"])

    retired = topics.supersede_topics_for_source(db, source, "final:test")
    assert [r["id"] for r in retired] == [topic_a["id"]]

    bound = db.execute(
        "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_a["id"],)).fetchone()
    assert bound[0] is None, (
        "a chunk of a just-superseded topic must be unbound in the same call -- otherwise "
        "it is hidden by visible_chunks_predicate's superseded-topic arm with no new "
        "topic to replace it in search until the day is re-ingested")

    results = chunks.search_chunks(db, _unit_vec(1024, 0), [site["id"]], k=5)
    assert chunk_a["id"] in [r["id"] for r in results], (
        "unbound, the chunk falls into the always-visible topic_id IS NULL bucket -- "
        "restoring exactly the pre-Track-B ON DELETE SET NULL visibility")


def test_supersede_topics_for_source_prefix_also_unbinds(db):
    co, site, user = _seed(db, "px")
    prefix = f"extractions/{user['folder_name']}/{DATE}/"
    source = prefix + "sid1.json"
    topic_a = topics.upsert_topic(db, site["id"], DATE, "A", user_id=user["id"],
                                  source_s3_key=source, summary="s")
    chunk_a = chunks.insert_chunk(
        db, site["id"], DATE, "topic", "prefix pass text", _unit_vec(1024, 3),
        source_s3_key=source, topic_id=topic_a["id"])

    retired = topics.supersede_topics_for_source_prefix(db, prefix, "report:test")
    assert [r["id"] for r in retired] == [topic_a["id"]]

    bound = db.execute(
        "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_a["id"],)).fetchone()
    assert bound[0] is None


def test_a_live_topics_chunk_is_left_alone_by_a_supersede_call(db):
    """The cost of the fix, bounded: superseding one source key must not touch another
    source key's live chunk."""
    co, site, user = _seed(db, "live")
    source_retired = f"extractions/{user['folder_name']}/{DATE}/sid{'e' * 32}.json"
    source_live = f"extractions/{user['folder_name']}/{DATE}/sid{'f' * 32}.json"

    topic_retired = topics.upsert_topic(
        db, site["id"], DATE, "Retired", user_id=user["id"],
        source_s3_key=source_retired, summary="s")
    topic_live = topics.upsert_topic(
        db, site["id"], DATE, "Live", user_id=user["id"],
        source_s3_key=source_live, summary="s")
    chunk_live = chunks.insert_chunk(
        db, site["id"], DATE, "topic", "unrelated live text", _unit_vec(1024, 5),
        source_s3_key=source_live, topic_id=topic_live["id"])

    topics.supersede_topics_for_source(db, source_retired, "final:test")

    still_bound = db.execute(
        "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_live["id"],)).fetchone()
    assert still_bound[0] == topic_live["id"], (
        "superseding a different source key must not unbind an unrelated live chunk")


def test_migration_0074_unbinds_pre_existing_bound_chunks_of_superseded_topics(db):
    """Simulates the state the pre-fix code left behind: a topic superseded (by hand,
    the way the unfixed `supersede_topics_for_source` did it -- UPDATE with no unbind)
    while its chunk stayed bound. Runs the actual migration file's SQL (not a copy of
    it) and proves it repairs that row, and leaves a live topic's chunk untouched."""
    co, site, user = _seed(db, "mig")
    source_old = f"extractions/{user['folder_name']}/{DATE}/sid{'a' * 32}.json"
    source_live = f"extractions/{user['folder_name']}/{DATE}/sid{'b' * 32}.json"

    topic_old = topics.upsert_topic(
        db, site["id"], DATE, "Old pass", user_id=user["id"],
        source_s3_key=source_old, summary="s")
    chunk_old = chunks.insert_chunk(
        db, site["id"], DATE, "topic", "old pass text", _unit_vec(1024, 6),
        source_s3_key=source_old, topic_id=topic_old["id"])
    # Pre-fix supersede: mark the row retired, but (unlike the fixed function) leave the
    # chunk bound -- exactly what the unfixed UPDATE did.
    db.execute(
        "UPDATE topics SET superseded_at=now(), superseded_by_run='pre-fix' WHERE id=%s",
        (topic_old["id"],))

    topic_live = topics.upsert_topic(
        db, site["id"], DATE, "Live pass", user_id=user["id"],
        source_s3_key=source_live, summary="s")
    chunk_live = chunks.insert_chunk(
        db, site["id"], DATE, "topic", "live pass text", _unit_vec(1024, 7),
        source_s3_key=source_live, topic_id=topic_live["id"])

    with open(MIGRATION_0074, encoding="utf-8") as fh:
        migration_sql = fh.read()
    db.execute(migration_sql)

    old_bound = db.execute(
        "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_old["id"],)).fetchone()
    assert old_bound[0] is None, "the migration must unbind a pre-existing superseded chunk"

    live_bound = db.execute(
        "SELECT topic_id FROM report_chunks WHERE id=%s", (chunk_live["id"],)).fetchone()
    assert live_bound[0] == topic_live["id"], (
        "the migration must leave a live topic's chunk bound")
