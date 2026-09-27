"""Integration tests for the Jev shadow-eval export SQL against a real
PostgreSQL (Track A, Task 1, fix round 1).

`tests/unit/test_jev_eval_export_shapes.py` proves the pure mappers on fake
records; it CANNOT prove the SQL strings themselves are correct, in
particular that a customer-facing delete (a `redactions` row with
`scope='deleted'`) is actually excluded. This file seeds minimal rows —
including a deleted/redacted topic that must NOT come back and a live one
that must — and runs the real `sql_programme_match()` / `sql_threads()` /
`sql_work_class()` strings through a real connection, then feeds the
returned rows through the real mappers.

Skipped unless `TEST_DATABASE_URL` is set (tests/conftest.py) — same as
every other file in this directory.
"""
import pytest
from psycopg.rows import dict_row

from repositories import companies, programme_tasks, redactions, sites, topics
from scripts.jev_eval import export_labels as ex

pytestmark = pytest.mark.integration


def _seed_company_site(db):
    co = companies.create_company(db, "Jev-Co")
    s = sites.create_site(db, co["id"], "Jev-Site")
    return co, s


def _topic(db, site_id, title, report_date="2026-06-01", **kwargs):
    return topics.upsert_topic(db, site_id, report_date, title, **kwargs)


def _fetch(db, sql):
    return db.cursor(row_factory=dict_row).execute(sql).fetchall()


# ---------------------------------------------------------------------------
# programme_match
# ---------------------------------------------------------------------------

def _insert_programme_match_suggestion(db, *, site_id, topic_id, state, dedupe_key,
                                       source_s3_key):
    db.execute(
        "INSERT INTO programme_progress_suggestions "
        "(site_id, task_id, topic_id, topic_title, report_date, source_s3_key, "
        " task_name, suggested_progress, confidence, state, dedupe_key) "
        "VALUES (%s,%s,%s,'Slab pour','2026-06-01',%s,'Pour slab',40,0.8,%s,%s)",
        (site_id, "T1", topic_id, source_s3_key, state, dedupe_key))


def test_programme_match_excludes_a_customer_deleted_topic(db):
    co, s = _seed_company_site(db)
    live_topic = _topic(db, s["id"], "Live topic")
    deleted_topic = _topic(db, s["id"], "Deleted topic")

    _insert_programme_match_suggestion(
        db, site_id=s["id"], topic_id=live_topic["id"], state="confirmed",
        dedupe_key="pm-live", source_s3_key="k/pm-live")
    _insert_programme_match_suggestion(
        db, site_id=s["id"], topic_id=deleted_topic["id"], state="confirmed",
        dedupe_key="pm-deleted", source_s3_key="k/pm-deleted")

    redactions.create_redaction(
        db, co["id"], deleted_topic["id"], "user deleted", None, "worker",
        scope="deleted")

    records = _fetch(db, ex.sql_programme_match())
    mapped = [ex.map_programme_match_row(r) for r in records]
    kept = [m for m in mapped if "_excluded" not in m]

    assert len(kept) == 1
    assert kept[0]["features"]["task"]["name"] == "Pour slab"
    # The row belonging to the redacted topic must not appear AT ALL —
    # excluded by the WHERE clause, not merely mapped to an exclusion marker.
    assert len(records) == 1


# ---------------------------------------------------------------------------
# threads
# ---------------------------------------------------------------------------

def test_threads_excludes_redacted_parent_and_redacted_later_topic(db):
    co, s = _seed_company_site(db)

    parent_live = _topic(db, s["id"], "Door hardware ordered",
                         report_date="2026-05-20", summary="Order placed.")
    later_live = _topic(db, s["id"], "Door hardware install",
                        report_date="2026-06-01", summary="Handles fitted.")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.5,12,'confirmed')",
        (later_live["id"], parent_live["id"]))

    parent_to_delete = _topic(db, s["id"], "Floor box ordered",
                              report_date="2026-05-01", summary="Ordered.")
    later_for_deleted_parent = _topic(db, s["id"], "Floor box install",
                                      report_date="2026-06-02", summary="Fitted.")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.4,32,'confirmed')",
        (later_for_deleted_parent["id"], parent_to_delete["id"]))
    redactions.create_redaction(
        db, co["id"], parent_to_delete["id"], "user deleted", None, "worker",
        scope="deleted")

    later_to_delete = _topic(db, s["id"], "Scaffold install",
                             report_date="2026-06-03", summary="Erected.")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status) "
        "VALUES (%s,%s,0.3,10,'confirmed')",
        (later_to_delete["id"], parent_live["id"]))
    redactions.create_redaction(
        db, co["id"], later_to_delete["id"], "user deleted", None, "worker",
        scope="deleted")

    records = _fetch(db, ex.sql_threads())
    mapped = [ex.map_threads_row(r) for r in records]
    kept = [m for m in mapped if "_excluded" not in m]
    excluded_reasons = [m["_excluded"] for m in mapped if "_excluded" in m]

    # The row whose LATER topic was redacted never comes back from the SQL
    # at all (excluded via WHERE on `t`).
    assert len(records) == 2

    assert len(kept) == 1
    assert kept[0]["features"]["earlier"]["title"] == "Door hardware ordered"
    assert kept[0]["features"]["later"]["title"] == "Door hardware install"

    # The row whose PARENT was redacted comes back (later topic is live) but
    # the LEFT JOIN's visibility condition makes `p` NULL, so the mapper
    # counts it as orphaned_parent rather than silently dropping it.
    assert excluded_reasons == ["orphaned_parent"]


# ---------------------------------------------------------------------------
# work_class
# ---------------------------------------------------------------------------

def _insert_classification_feedback(db, *, company_id, topic_id, human_verdict):
    db.execute(
        "INSERT INTO classification_feedback "
        "(company_id, topic_id, classifier_verdict, classifier_confidence, human_verdict) "
        "VALUES (%s,%s,'work',0.6,%s)",
        (company_id, topic_id, human_verdict))


def test_work_class_excludes_a_customer_deleted_topic(db):
    co, s = _seed_company_site(db)
    live_topic = _topic(db, s["id"], "Chat about weekend plans",
                        summary="Not work related.", category="personal")
    deleted_topic = _topic(db, s["id"], "Also chat", summary="Also personal.",
                           category="personal")

    _insert_classification_feedback(
        db, company_id=co["id"], topic_id=live_topic["id"],
        human_verdict="confirm_non_work")
    _insert_classification_feedback(
        db, company_id=co["id"], topic_id=deleted_topic["id"],
        human_verdict="confirm_non_work")

    redactions.create_redaction(
        db, co["id"], deleted_topic["id"], "user deleted", None, "worker",
        scope="deleted")

    records = _fetch(db, ex.sql_work_class())
    mapped = [ex.map_work_class_row(r) for r in records]
    kept = [m for m in mapped if "_excluded" not in m]
    excluded_reasons = [m["_excluded"] for m in mapped if "_excluded" in m]

    assert len(records) == 2
    assert len(kept) == 1
    assert kept[0]["features"]["title"] == "Chat about weekend plans"
    # A soft-deleted topic reads exactly like a physically-missing one to
    # this query (visibility is ANDed into the LEFT JOIN's ON clause), so it
    # lands in the SAME exclusion bucket a hard-missing topic would.
    assert excluded_reasons == ["topic_missing"]


# ---------------------------------------------------------------------------
# sites / programme task names -- masking-protection queries (fix wave 2 I1)
# ---------------------------------------------------------------------------

def test_sql_sites_excludes_archived_sites(db):
    co, s = _seed_company_site(db)
    archived = sites.create_site(db, co["id"], "Old Site")
    sites.archive_site(db, archived["id"], co["id"])

    records = _fetch(db, ex.sql_sites([co["id"]]))
    names = {r["name"] for r in records}

    assert "Jev-Site" in names
    assert "Old Site" not in names


def test_sql_programme_task_names_excludes_removed_tasks(db):
    co, s = _seed_company_site(db)
    programme = programme_tasks.create_programme(
        db, site_id=s["id"], name="Main programme", source_format="p6")
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, name, removed_in_version) "
        "VALUES (%s, 'local', 'Roof Framing', NULL)", (programme["id"],))
    db.execute(
        "INSERT INTO programme_tasks (programme_id, origin, name, removed_in_version) "
        "VALUES (%s, 'local', 'Superseded Task', 1)", (programme["id"],))

    records = _fetch(db, ex.sql_programme_task_names([co["id"]]))
    names = {r["name"] for r in records}

    assert "Roof Framing" in names
    assert "Superseded Task" not in names


def test_sql_programme_task_names_empty_when_no_programme_data(db):
    co, _s = _seed_company_site(db)
    # No programme/tasks created for this company at all -- must return
    # zero rows quietly, not raise.
    records = _fetch(db, ex.sql_programme_task_names([co["id"]]))
    assert records == []
