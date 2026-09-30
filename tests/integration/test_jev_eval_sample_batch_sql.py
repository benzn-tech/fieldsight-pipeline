"""Integration tests for the Task 10 owner-labelled batch sampler's SQL
against a real PostgreSQL (Track A).

Same pattern as tests/integration/test_jev_eval_export_labels_sql.py: seed
minimal rows -- including a deleted topic that must not come back, and a
pair/topic already resolved that must be excludable -- and run the real
`sql_threads_site_ids()` / `sql_threads_topics_for_site()` /
`sql_threads_existing_pairs()` / `sql_work_class_topics_stratum()` /
`sql_work_class_existing()` strings through a real connection.

Fix wave 3, I3: threads topics are now paged one site at a time (bounded by
LIMIT + a 1,000-char summary truncation) and work_class topics are sampled
per (work_class, confidence-band) stratum (also LIMIT-bounded) -- both
replacing a single unbounded query, to stay well under the RDS Data API's
1 MiB single-statement response cap on prod. These tests cover the new SQL's
LIMIT/ordering behaviour in addition to the deleted-topic exclusion the old
tests already covered.

Skipped unless `TEST_DATABASE_URL` is set (tests/conftest.py).
"""
from datetime import date, timedelta

import pytest
from psycopg.rows import dict_row

from repositories import companies, redactions, sites, topics
from scripts.jev_eval import sample_batch as sb

pytestmark = pytest.mark.integration


def _seed_company_site(db):
    co = companies.create_company(db, "Jev-Batch-Co")
    s = sites.create_site(db, co["id"], "Jev-Batch-Site")
    return co, s


def _days_ago(n):
    """An ISO date n days before today.

    The sampler's windows count back from CURRENT_DATE, so a literal date in a
    fixture ages out: "2026-06-01" fell outside the 120-day window on 2026-09-30
    and three tests went red on every branch at once. Every date that must land
    INSIDE a window is relative, with margins of weeks so the UTC/NZ day
    boundary can never matter.
    """
    return (date.today() - timedelta(days=n)).isoformat()


def _topic_with_open_item(db, site_id, title, report_date=None, **kwargs):
    report_date = report_date or _days_ago(30)
    return topics.upsert_topic(
        db, site_id, report_date, title,
        action_items=[{"text": "Do the thing"}], **kwargs)


def _fetch(db, sql):
    return db.cursor(row_factory=dict_row).execute(sql).fetchall()


# ---------------------------------------------------------------------------
# threads: candidate topics excludes a deleted topic
# ---------------------------------------------------------------------------

def test_threads_site_ids_and_topics_for_site_excludes_a_deleted_topic(db):
    co, s = _seed_company_site(db)
    live = _topic_with_open_item(db, s["id"], "Door hardware install",
                                 report_date=_days_ago(30), summary="Handles fitted.")
    deleted = _topic_with_open_item(db, s["id"], "Door hardware also install",
                                    report_date=_days_ago(29), summary="Also fitted.")
    redactions.create_redaction(
        db, co["id"], deleted["id"], "user deleted", None, "worker", scope="deleted")

    site_records = _fetch(db, sb.sql_threads_site_ids(120))
    site_ids = {r["site_id"] for r in site_records}
    assert s["id"] in site_ids

    records = _fetch(db, sb.sql_threads_topics_for_site(s["id"], 120, 200))
    ids = {r["id"] for r in records}
    assert live["id"] in ids
    assert deleted["id"] not in ids


def test_threads_topics_for_site_respects_window(db):
    co, s = _seed_company_site(db)
    old = _topic_with_open_item(db, s["id"], "Old topic", report_date="2020-01-01")

    records = _fetch(db, sb.sql_threads_topics_for_site(s["id"], 30, 200))
    ids = {r["id"] for r in records}
    # 2020 is never inside any realistic 30-day window from CURRENT_DATE.
    assert old["id"] not in ids


def test_threads_topics_for_site_limit_keeps_most_recent(db):
    co, s = _seed_company_site(db)
    older = _topic_with_open_item(db, s["id"], "Older topic", report_date=_days_ago(300))
    newer = _topic_with_open_item(db, s["id"], "Newer topic", report_date=_days_ago(30))

    records = _fetch(db, sb.sql_threads_topics_for_site(s["id"], 365, 1))
    assert len(records) == 1
    assert records[0]["id"] == newer["id"]
    assert older["id"] not in {r["id"] for r in records}


def test_threads_topics_for_site_truncates_summary_to_1000_chars(db):
    co, s = _seed_company_site(db)
    long_summary = "x" * 2000
    topic = _topic_with_open_item(db, s["id"], "Long summary topic",
                                  report_date=_days_ago(30), summary=long_summary)

    records = _fetch(db, sb.sql_threads_topics_for_site(s["id"], 120, 200))
    row = next(r for r in records if r["id"] == topic["id"])
    assert len(row["summary"]) == 1000


def test_threads_topics_for_site_only_returns_the_named_site(db):
    co, s1 = _seed_company_site(db)
    s2 = sites.create_site(db, co["id"], "Jev-Batch-Site-2")
    t1 = _topic_with_open_item(db, s1["id"], "Site 1 topic", report_date=_days_ago(30))
    t2 = _topic_with_open_item(db, s2["id"], "Site 2 topic", report_date=_days_ago(30))

    records = _fetch(db, sb.sql_threads_topics_for_site(s1["id"], 120, 200))
    ids = {r["id"] for r in records}
    assert t1["id"] in ids
    assert t2["id"] not in ids


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
                                    report_date=_days_ago(60), summary="Order placed for handles.")
    later_new = _topic_with_open_item(db, s["id"], "Door hardware installed",
                                      report_date=_days_ago(46), summary="Handles fitted at last.")
    later_already_suggested = _topic_with_open_item(
        db, s["id"], "Door hardware handover", report_date=_days_ago(41),
        summary="Handover of the handles completed.")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.4,19,'rejected')",
        (later_already_suggested["id"], earlier["id"]))

    topic_records = _fetch(db, sb.sql_threads_topics_for_site(s["id"], 120, 200))
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
# work_class -- I3's four per-stratum queries, merged the same way
# sample_work_class() merges them (dedup on id).
# ---------------------------------------------------------------------------

def _fetch_all_work_class_strata(db, window_days=365, seed=0, limit=500):
    rows = []
    seen = set()
    for work_class, low_confidence in sb.WORK_CLASS_STRATA:
        sql = sb.sql_work_class_topics_stratum(work_class, low_confidence, window_days, seed, limit)
        for row in _fetch(db, sql):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
    return rows


def test_work_class_stratum_queries_exclude_a_deleted_topic(db):
    co, s = _seed_company_site(db)
    live = topics.upsert_topic(db, s["id"], "2026-06-01", "Chat about weekend",
                               summary="Personal chat.", category="personal",
                               work_class="non_work", work_confidence=0.9)
    deleted = topics.upsert_topic(db, s["id"], "2026-06-02", "Also chat",
                                  summary="Also personal.", category="personal",
                                  work_class="non_work", work_confidence=0.9)
    redactions.create_redaction(
        db, co["id"], deleted["id"], "user deleted", None, "worker", scope="deleted")

    ids = {r["id"] for r in _fetch_all_work_class_strata(db)}
    assert live["id"] in ids
    assert deleted["id"] not in ids


def test_work_class_stratum_queries_require_matching_work_class(db):
    co, s = _seed_company_site(db)
    classified = topics.upsert_topic(db, s["id"], "2026-06-01", "Slab pour",
                                     work_class="work", work_confidence=0.95)
    unclassified = topics.upsert_topic(db, s["id"], "2026-06-01", "Unlabelled topic")

    ids = {r["id"] for r in _fetch_all_work_class_strata(db)}
    assert classified["id"] in ids
    assert unclassified["id"] not in ids


def test_work_class_stratum_query_respects_confidence_band(db):
    co, s = _seed_company_site(db)
    low = topics.upsert_topic(db, s["id"], "2026-06-01", "Low confidence work",
                              work_class="work", work_confidence=0.4)
    high = topics.upsert_topic(db, s["id"], "2026-06-01", "High confidence work",
                               work_class="work", work_confidence=0.95)

    low_sql = sb.sql_work_class_topics_stratum("work", True, 365, 0, 500)
    high_sql = sb.sql_work_class_topics_stratum("work", False, 365, 0, 500)
    low_ids = {r["id"] for r in _fetch(db, low_sql)}
    high_ids = {r["id"] for r in _fetch(db, high_sql)}

    assert low["id"] in low_ids and low["id"] not in high_ids
    assert high["id"] in high_ids and high["id"] not in low_ids


def test_work_class_stratum_query_limit_is_honoured(db):
    co, s = _seed_company_site(db)
    for i in range(5):
        topics.upsert_topic(db, s["id"], "2026-06-01", f"Work topic {i}",
                            work_class="work", work_confidence=0.95)

    sql = sb.sql_work_class_topics_stratum("work", False, 365, 0, 2)
    records = _fetch(db, sql)
    assert len(records) == 2


def test_work_class_stratum_query_truncates_summary_to_1000_chars(db):
    co, s = _seed_company_site(db)
    long_summary = "y" * 2000
    topic = topics.upsert_topic(db, s["id"], "2026-06-01", "Long summary work topic",
                                summary=long_summary, work_class="work", work_confidence=0.95)

    sql = sb.sql_work_class_topics_stratum("work", False, 365, 0, 500)
    records = _fetch(db, sql)
    row = next(r for r in records if r["id"] == topic["id"])
    assert len(row["summary"]) == 1000


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

    topic_records = _fetch_all_work_class_strata(db)
    existing_records = _fetch(db, sb.sql_work_class_existing())
    already_fed_back = {r["topic_id"] for r in existing_records}

    filtered = sb.apply_work_class_exclusions(topic_records, already_fed_back)
    ids = {t["id"] for t in filtered}
    assert fed_back["id"] not in ids
    assert fresh["id"] in ids


def test_work_class_stratum_max_row_bytes_counts_octets_and_title_is_truncated(db):
    # Fix wave 5, item 7 (D18): bytes, not characters -- a CJK character is
    # 3 bytes in UTF-8 -- and the title is truncated in SQL like the summary.
    co, s = _seed_company_site(db)
    title = "林" * 400
    topics.upsert_topic(db, s["id"], "2026-06-01", title, summary="x" * 10,
                        category="general", work_class="work", work_confidence=0.95)

    measured = _fetch(db, sb.sql_work_class_stratum_max_row_bytes("work", False, 365))
    assert measured[0]["max_bytes"] == 3 * sb.WORK_CLASS_TITLE_MAX_CHARS + 10 + len("general")

    rows = _fetch(db, sb.sql_work_class_topics_stratum("work", False, 365, 0, 500))
    assert len(rows[0]["title"]) == sb.WORK_CLASS_TITLE_MAX_CHARS
