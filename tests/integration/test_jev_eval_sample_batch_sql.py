"""Integration tests for the Task 10 owner-labelled batch sampler's SQL
against a real PostgreSQL (Track A).

Same pattern as tests/integration/test_jev_eval_export_labels_sql.py: seed
minimal rows -- including a deleted topic that must not come back, and a
pair/topic already resolved that must be excludable -- and run the real
`sql_threads_topics()` / `sql_threads_existing_pairs()` /
`sql_work_class_topics()` / `sql_work_class_existing()` strings through a
real connection.

Skipped unless `TEST_DATABASE_URL` is set (tests/conftest.py).
"""
import pytest
from psycopg.rows import dict_row

from repositories import companies, redactions, sites, topics
from scripts.jev_eval import sample_batch as sb

pytestmark = pytest.mark.integration


def _seed_company_site(db):
    co = companies.create_company(db, "Jev-Batch-Co")
    s = sites.create_site(db, co["id"], "Jev-Batch-Site")
    return co, s


def _topic_with_open_item(db, site_id, title, report_date="2026-06-01", **kwargs):
    return topics.upsert_topic(
        db, site_id, report_date, title,
        action_items=[{"text": "Do the thing"}], **kwargs)


def _fetch(db, sql):
    return db.cursor(row_factory=dict_row).execute(sql).fetchall()


# ---------------------------------------------------------------------------
# threads: candidate topics excludes a deleted topic
# ---------------------------------------------------------------------------

def test_threads_topics_excludes_a_deleted_topic(db):
    co, s = _seed_company_site(db)
    live = _topic_with_open_item(db, s["id"], "Door hardware install",
                                 report_date="2026-06-01", summary="Handles fitted.")
    deleted = _topic_with_open_item(db, s["id"], "Door hardware also install",
                                    report_date="2026-06-02", summary="Also fitted.")
    redactions.create_redaction(
        db, co["id"], deleted["id"], "user deleted", None, "worker", scope="deleted")

    records = _fetch(db, sb.sql_threads_topics(120))
    ids = {r["id"] for r in records}
    assert live["id"] in ids
    assert deleted["id"] not in ids


def test_threads_topics_respects_window(db):
    co, s = _seed_company_site(db)
    recent = _topic_with_open_item(db, s["id"], "Recent topic", report_date="2026-06-01")
    old = _topic_with_open_item(db, s["id"], "Old topic", report_date="2020-01-01")

    records = _fetch(db, sb.sql_threads_topics(30))
    ids = {r["id"] for r in records}
    # `recent` may or may not fall inside a 30-day window depending on
    # CURRENT_DATE at test time, so only assert on what is unambiguous:
    # the topic from 2020 is never inside any realistic window.
    assert old["id"] not in ids


def test_threads_existing_pairs_lists_parent_topic_id_pairs(db):
    co, s = _seed_company_site(db)
    parent = _topic_with_open_item(db, s["id"], "Floor box ordered", report_date="2026-05-01")
    later = _topic_with_open_item(db, s["id"], "Floor box install", report_date="2026-06-01")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.4,31,'rejected')",
        (later["id"], parent["id"]))

    records = _fetch(db, sb.sql_threads_existing_pairs())
    pairs = {(r["topic_id"], r["parent_topic_id"]) for r in records}
    assert (later["id"], parent["id"]) in pairs


def test_threads_pipeline_excludes_deleted_and_already_suggested(db):
    co, s = _seed_company_site(db)
    earlier = _topic_with_open_item(db, s["id"], "Door hardware ordered",
                                    report_date="2026-05-01", summary="Order placed for handles.")
    later_new = _topic_with_open_item(db, s["id"], "Door hardware installed",
                                      report_date="2026-05-15", summary="Handles fitted at last.")
    later_already_suggested = _topic_with_open_item(
        db, s["id"], "Door hardware handover", report_date="2026-05-20",
        summary="Handover of the handles completed.")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.4,19,'rejected')",
        (later_already_suggested["id"], earlier["id"]))

    topic_records = _fetch(db, sb.sql_threads_topics(120))
    existing_records = _fetch(db, sb.sql_threads_existing_pairs())
    existing_pairs = {(r["topic_id"], r["parent_topic_id"]) for r in existing_records}

    topics_by_site = sb.group_by_site(topic_records)
    pairs, _ = sb.generate_thread_pairs(topics_by_site)
    filtered = sb.apply_thread_exclusions(pairs, existing_pairs)

    kept_pairs = {(p["later"]["id"], p["earlier"]["id"]) for p in filtered}
    assert (later_already_suggested["id"], earlier["id"]) not in kept_pairs
    # The genuinely new pair is still a candidate (score may or may not clear
    # any stratum floor, but it must survive the exclusion step itself).
    all_pairs_before = {(p["later"]["id"], p["earlier"]["id"]) for p in pairs}
    if (later_new["id"], earlier["id"]) in all_pairs_before:
        assert (later_new["id"], earlier["id"]) in kept_pairs


# ---------------------------------------------------------------------------
# work_class
# ---------------------------------------------------------------------------

def test_work_class_topics_excludes_a_deleted_topic(db):
    co, s = _seed_company_site(db)
    live = topics.upsert_topic(db, s["id"], "2026-06-01", "Chat about weekend",
                               summary="Personal chat.", category="personal",
                               work_class="non_work", work_confidence=0.9)
    deleted = topics.upsert_topic(db, s["id"], "2026-06-02", "Also chat",
                                  summary="Also personal.", category="personal",
                                  work_class="non_work", work_confidence=0.9)
    redactions.create_redaction(
        db, co["id"], deleted["id"], "user deleted", None, "worker", scope="deleted")

    records = _fetch(db, sb.sql_work_class_topics())
    ids = {r["id"] for r in records}
    assert live["id"] in ids
    assert deleted["id"] not in ids


def test_work_class_topics_requires_non_null_work_class(db):
    co, s = _seed_company_site(db)
    classified = topics.upsert_topic(db, s["id"], "2026-06-01", "Slab pour",
                                     work_class="work", work_confidence=0.95)
    unclassified = topics.upsert_topic(db, s["id"], "2026-06-01", "Unlabelled topic")

    records = _fetch(db, sb.sql_work_class_topics())
    ids = {r["id"] for r in records}
    assert classified["id"] in ids
    assert unclassified["id"] not in ids


def test_work_class_existing_and_pipeline_excludes_already_fed_back(db):
    co, s = _seed_company_site(db)
    fed_back = topics.upsert_topic(db, s["id"], "2026-06-01", "Already reviewed",
                                   work_class="work", work_confidence=0.5)
    fresh = topics.upsert_topic(db, s["id"], "2026-06-01", "Never reviewed",
                                work_class="non_work", work_confidence=0.4)
    db.execute(
        "INSERT INTO classification_feedback "
        "(company_id, topic_id, classifier_verdict, classifier_confidence, human_verdict) "
        "VALUES (%s,%s,'work',0.5,'reject_is_work')",
        (co["id"], fed_back["id"]))

    topic_records = _fetch(db, sb.sql_work_class_topics())
    existing_records = _fetch(db, sb.sql_work_class_existing())
    already_fed_back = {r["topic_id"] for r in existing_records}

    filtered = sb.apply_work_class_exclusions(topic_records, already_fed_back)
    ids = {t["id"] for t in filtered}
    assert fed_back["id"] not in ids
    assert fresh["id"] in ids
