"""Integration: a superseded topic (and its children) must not survive Task 2's read
paths, against a real Postgres -- text is not SQL, and this repo has had a predicate that
named a table alias that did not exist in its own statement while its text-only test stayed
green (topics.py:140's CHILD_OF_VISIBLE_TOPIC comment).

Task 3, next, stops DELETEing a source key's prior topics on re-extraction and marks them
`superseded_at` instead. This seeds exactly that end state directly with SQL -- two passes
of one source key, the first superseded -- and proves every read this task touched shows
only the second: the live pass, its own action item, its own finding, its own chunk; never
the superseded pass's.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import uuid

import pytest

from repositories import chunks, companies, findings, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-25"


def _unit_vec(dim, hot):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"Supersede-Co-{tag}")
    site = sites.create_site(db, co["id"], f"Supersede-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Folder-{tag}", "Fol", "Der", "worker")
    source = f"extractions/Folder-{tag}/{DATE}/sid{'a' * 32}.json"
    prefix = f"extractions/Folder-{tag}/{DATE}/"
    return co, site, user, source, prefix


def _seed_two_passes(db):
    """Topic A (superseded) and topic B (live), same source_s3_key -- exactly the shape
    Task 3 will leave behind. A is created and superseded BEFORE B is inserted: migration
    0071's `idx_topics_live_source` is a partial unique index on `(source_s3_key) WHERE
    superseded_at IS NULL`, so two LIVE rows sharing a source key would violate it."""
    co, site, user, source, prefix = _seed(db)

    topic_a = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- first pass", user_id=user["id"],
        source_s3_key=source, summary="s",
        action_items=[{"text": "Order rebar (pass 1)", "status": "open"}])
    findings.insert_findings(db, topic_a["id"], site["id"], [
        {"observation": "Edge unprotected (pass 1)", "domain": "safety", "severity": "major"}])
    chunk_a = chunks.insert_chunk(db, site["id"], DATE, "topic", "pass one transcript text",
                                  _unit_vec(1024, 0), source_s3_key=source,
                                  topic_id=topic_a["id"])

    db.execute("UPDATE topics SET superseded_at = now(), superseded_by_run = 'final:test' "
              "WHERE id = %s", (topic_a["id"],))

    topic_b = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- second pass", user_id=user["id"],
        source_s3_key=source, summary="s",
        action_items=[{"text": "Order rebar (pass 2)", "status": "open"}])
    findings.insert_findings(db, topic_b["id"], site["id"], [
        {"observation": "Edge unprotected (pass 2)", "domain": "safety", "severity": "major"}])
    chunk_b = chunks.insert_chunk(db, site["id"], DATE, "topic", "pass two transcript text",
                                  _unit_vec(1024, 1), source_s3_key=source,
                                  topic_id=topic_b["id"])

    # An unbound transcript-window chunk -- must stay visible on either arm.
    chunk_unassigned = chunks.insert_chunk(
        db, site["id"], DATE, "transcript", "unassigned window text",
        _unit_vec(1024, 2), source_s3_key=source, topic_id=None)

    return {"co": co, "site": site, "user": user, "source": source, "prefix": prefix,
            "topic_a": topic_a, "topic_b": topic_b,
            "chunk_a": chunk_a, "chunk_b": chunk_b, "chunk_unassigned": chunk_unassigned}


def test_list_topics_for_date_returns_only_the_live_pass(db):
    seeded = _seed_two_passes(db)
    site_id, date = seeded["site"]["id"], DATE

    rows = topics.list_topics_for_date(db, [site_id], date)

    assert [r["id"] for r in rows] == [seeded["topic_b"]["id"]], (
        "the superseded pass must not be shown beside the pass that replaced it")
    row = rows[0]
    assert [a["text"] for a in row["action_items"]] == ["Order rebar (pass 2)"], (
        "children of the superseded topic must not leak into the live topic's list "
        "(they were never on the live topic -- the query itself must exclude them)")
    assert [f["observation"] for f in row["findings"]] == ["Edge unprotected (pass 2)"]


def test_search_chunks_never_returns_the_superseded_passs_chunk(db):
    seeded = _seed_two_passes(db)
    site_id = seeded["site"]["id"]

    # Query with the vector CLOSEST to the superseded pass's own chunk -- if the live
    # arm were missing, this is exactly the query that would surface it (nearest by
    # cosine, ACL-visible site, nothing else to exclude it).
    results = chunks.search_chunks(db, _unit_vec(1024, 0), [site_id], k=5)
    ids = [r["id"] for r in results]

    assert seeded["chunk_a"]["id"] not in ids, (
        "a chunk bound to a superseded topic must not be retrievable through search")
    assert seeded["chunk_b"]["id"] in [r["id"] for r in
                                       chunks.search_chunks(db, _unit_vec(1024, 1),
                                                            [site_id], k=5)]


def test_search_chunks_still_returns_the_unassigned_chunk(db):
    """The cost of the fix, bounded: a transcript-window chunk with no topic_id is
    unaffected by either arm and must stay visible."""
    seeded = _seed_two_passes(db)
    site_id = seeded["site"]["id"]

    results = chunks.search_chunks(db, _unit_vec(1024, 2), [site_id], k=5)
    assert seeded["chunk_unassigned"]["id"] in [r["id"] for r in results]


def test_has_topics_for_source_is_true_on_the_live_pass(db):
    seeded = _seed_two_passes(db)
    assert topics.has_topics_for_source(db, seeded["source"]) is True


def test_has_topics_for_source_prefix_is_true_on_the_live_pass(db):
    seeded = _seed_two_passes(db)
    assert topics.has_topics_for_source_prefix(db, seeded["prefix"]) is True


def test_list_topics_for_source_prefix_default_is_live_only(db):
    seeded = _seed_two_passes(db)
    rows = topics.list_topics_for_source_prefix(db, seeded["prefix"])
    assert [r["id"] for r in rows] == [seeded["topic_b"]["id"]]
    assert [a["text"] for a in rows[0]["action_items"]] == ["Order rebar (pass 2)"]
    assert [f["observation"] for f in rows[0]["findings"]] == ["Edge unprotected (pass 2)"]


def test_list_topics_for_source_prefix_include_superseded_returns_both(db):
    """The delete/undelete enumeration posture: must see every row under the prefix,
    or a superseded copy is left outside every batch and can never be tombstoned or
    re-hidden with the pass that replaced it."""
    seeded = _seed_two_passes(db)
    rows = topics.list_topics_for_source_prefix(db, seeded["prefix"], include_superseded=True)
    assert {r["id"] for r in rows} == {seeded["topic_a"]["id"], seeded["topic_b"]["id"]}


def test_list_expired_non_work_still_sees_the_superseded_topic(db):
    """R3: the non-work retention sweep must NOT carry the live arm -- a superseded pass
    of a non_work day still needs to be found and redacted, or its vectors and text
    outlive every other copy in the table."""
    co, site, user, source, _prefix = _seed(db)
    old_topic = topics.upsert_topic(
        db, site["id"], DATE, "Personal call", user_id=user["id"],
        source_s3_key=source, summary="s", work_class="non_work")
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (old_topic["id"],))
    # Backdate created_at so it falls inside the sweep's [created_since, older_than) window.
    db.execute("UPDATE topics SET created_at = now() - interval '40 days' WHERE id = %s",
              (old_topic["id"],))

    due = topics.list_expired_non_work(
        # Relative to now(), not a fixed calendar date -- the test must not depend on
        # when it happens to run. The backdated created_at (now() - 40 days) must fall
        # strictly between created_since and older_than.
        db, older_than=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1),
        created_since=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365))

    assert old_topic["id"] in {r["id"] for r in due}, (
        "the sweep must still see a superseded non_work topic -- it is the read path "
        "that is SUPPOSED to reach it, not one more that must exclude it")


def test_company_excluded_topic_ids_includes_the_superseded_topic(db):
    seeded = _seed_two_passes(db)
    excluded = redactions.company_excluded_topic_ids(db, [seeded["site"]["id"]])
    assert seeded["topic_a"]["id"] in excluded
    assert seeded["topic_b"]["id"] not in excluded


def test_get_topic_full_still_returns_a_superseded_topics_photos(db):
    """Fix round 1: get_topic_full (the per-topic reindex builder's read, R3) must stay
    fully unfiltered for supersession -- including its photos child, which reused
    CHILD_OF_VISIBLE_TOPIC and silently started dropping a superseded topic's photos the
    moment that constant grew the live arm. The reindex builder has to be able to re-embed
    a topic's corrected content, photos included, even mid-supersession."""
    seeded = _seed_two_passes(db)
    topic_a_id = seeded["topic_a"]["id"]
    db.execute(
        "INSERT INTO topic_photos (topic_id, s3_key, caption_text) VALUES (%s, %s, %s)",
        (topic_a_id, "reports/2026-09-25/x/p1.jpg", "pass one photo"))

    full = topics.get_topic_full(db, topic_a_id)

    assert full is not None, "get_topic_full must still find a superseded topic by id"
    assert full["id"] == topic_a_id
    assert [p["s3_key"] for p in full["photos"]] == ["reports/2026-09-25/x/p1.jpg"], (
        "a superseded topic's own photos must not vanish from the reindex read")


def test_get_topic_full_still_excludes_a_deleted_topics_photos(db):
    """The other half of the same fix: CHILD_OF_UNDELETED_TOPIC dropped the live arm, not
    the deletion arm -- a photo bound to a topic that was actually DELETED (not merely
    superseded) must still be excluded here, same as before this task."""
    seeded = _seed_two_passes(db)
    topic_b_id = seeded["topic_b"]["id"]
    db.execute(
        "INSERT INTO topic_photos (topic_id, s3_key, caption_text) VALUES (%s, %s, %s)",
        (topic_b_id, "reports/2026-09-25/x/p2.jpg", "pass two photo"))
    redactions.create_redaction(
        db, seeded["co"]["id"], topic_b_id, "removed", seeded["user"]["id"], "admin",
        target_type="topic", scope="deleted")

    full = topics.get_topic_full(db, topic_b_id)

    assert full["photos"] == [], (
        "a deleted topic's photos must still be excluded from get_topic_full -- "
        "CHILD_OF_UNDELETED_TOPIC keeps the deletion arm, only drops the live one")
