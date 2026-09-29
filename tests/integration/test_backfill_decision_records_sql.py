"""Integration: scripts/backfill_decision_records.py's SQL against a real
Postgres (Track B Task 7; skipped without TEST_DATABASE_URL -- a skip is NOT a
pass).

Runs the EXACT `sql_stats_*()` / `sql_insert_*()` strings the script's own
`run_backfill` sends through the RDS Data API, here through psycopg's
`conn.execute(sql)` instead. Those strings carry no named (`:name`) or `%s`
parameters at all (tests/unit/test_backfill_decision_records.py's
`test_no_named_or_percent_s_parameters` pins this) -- there is nothing
per-row to bind, since each statement is one `INSERT ... SELECT ...
WHERE NOT EXISTS` over a whole table, so nothing needs translating between
the Data API and psycopg here.

Seeds rows shaped like PRE-Task-6a/6b history: a `programme_progress_
suggestions` / `topic_thread_suggestions` / `classification_feedback` row
already decided, with NO matching `decision_records` row (exactly the gap
this script exists to close), then runs the backfill SQL and inspects what
landed. Several tests build one record via the REAL live path
(`lambda_org_api.confirm_suggestion` / `lambda_item_writer._suggest_threads` /
`lambda_org_api.create_classification_feedback_endpoint`) and one via the
backfill SQL, then compare the (kind, subject_type, subject_stable_id,
object_ref) key tuple -- proving the two sides of the seam use the identical
formula, not merely that each one looks plausible alone.
"""
import uuid

import pytest
from psycopg.rows import dict_row

import scripts.backfill_decision_records as bk
from repositories import companies, decision_records, programme_tasks, sites, threads, topics, users
from thread_match import MIN_SCORE as THREAD_MIN_SCORE

pytestmark = pytest.mark.integration

lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")
lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


def _seed_company_site(conn, tag):
    co = companies.create_company(conn, f"BF7-Co-{tag}")
    site = sites.create_site(conn, co["id"], f"BF7-Site-{tag}")
    return co, site


def _seed_admin(conn, co, tag):
    return users.upsert_user(
        conn, f"sub-bf7-{tag}", f"admin-bf7-{tag}@example.com",
        company_id=co["id"], global_role="admin")


def _stats(conn, sql):
    return conn.cursor(row_factory=dict_row).execute(sql).fetchone()


def _inserted_ids(conn, sql):
    return [r[0] for r in conn.execute(sql).fetchall()]


def _dr_row(conn, row_id):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM decision_records WHERE id=%s", (row_id,)).fetchone()


# ---------------------------------------------------------------------------
# programme_match (programme_progress_suggestions)
# ---------------------------------------------------------------------------

# Historical rows are seeded with an explicit PAST created_at, never the
# default now() -- the `db` fixture wraps a whole test in one transaction,
# and Postgres's now()/CURRENT_TIMESTAMP returns the SAME value for every
# call within one transaction. A row genuinely predating decision_records
# would never coincide, to the microsecond, with a decision_records row
# written "live" moments later in the same test -- using a real past
# timestamp here both matches reality and avoids that coincidental collision
# tripping the idempotency predicate's created_at comparison.
_HISTORICAL_CREATED_AT = "2026-01-15T00:00:00+00:00"


def _insert_pps(conn, *, site_id, topic_id, task_id, state, confidence=0.83,
                suggested_status="completed", suggested_progress=100,
                decided_by=None, dedupe_key=None, created_at=_HISTORICAL_CREATED_AT):
    row = conn.execute(
        "INSERT INTO programme_progress_suggestions "
        "(site_id, task_id, topic_id, topic_title, report_date, source_s3_key, "
        " task_name, suggested_status, suggested_progress, confidence, state, "
        " decided_by, decided_at, dedupe_key, created_at) "
        "VALUES (%s,%s,%s,'Slab pour','2026-06-01','k/pm', 'Pour slab', %s, %s, %s, %s, "
        " %s, CASE WHEN %s IN ('confirmed','rejected') THEN %s::timestamptz ELSE NULL END, "
        " %s, %s::timestamptz) "
        "RETURNING id, created_at",
        (site_id, task_id, topic_id, suggested_status, suggested_progress, confidence,
         state, decided_by, state, created_at,
         dedupe_key or f"dedupe-{uuid.uuid4().hex[:12]}", created_at),
    ).fetchone()
    return {"id": row[0], "created_at": row[1]}


def test_programme_match_confirmed_rejected_pending_and_null_topic(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    topic_confirmed = topics.upsert_topic(
        db, site["id"], "2026-06-01", "T-confirmed",
        source_s3_key=f"extractions/BF7a-{tag}/2026-06-01/sid{'1'*32}.json")
    topic_rejected = topics.upsert_topic(
        db, site["id"], "2026-06-01", "T-rejected",
        source_s3_key=f"extractions/BF7b-{tag}/2026-06-01/sid{'2'*32}.json")
    topic_pending = topics.upsert_topic(
        db, site["id"], "2026-06-01", "T-pending",
        source_s3_key=f"extractions/BF7c-{tag}/2026-06-01/sid{'3'*32}.json")

    _insert_pps(db, site_id=site["id"], topic_id=topic_confirmed["id"], task_id="T-1",
               state="confirmed", decided_by=caller_user["id"])
    _insert_pps(db, site_id=site["id"], topic_id=topic_rejected["id"], task_id="T-2",
               state="rejected", decided_by=caller_user["id"])
    _insert_pps(db, site_id=site["id"], topic_id=topic_pending["id"], task_id="T-3",
               state="pending")
    _insert_pps(db, site_id=site["id"], topic_id=None, task_id="T-4", state="confirmed",
               decided_by=caller_user["id"])

    stats = _stats(db, bk.sql_stats_programme_match())
    # confirmed(topic_confirmed) + rejected(topic_rejected) -- the pending row
    # never matches the state filter at all, and the NULL-topic confirmed row
    # matches the state filter but is not "eligible" (topic_id IS NOT NULL),
    # so it is counted in skipped_topic_null instead.
    assert stats["eligible"] == 2
    assert stats["skipped_topic_null"] == 1
    assert stats["already_present"] == 0

    ids = _inserted_ids(db, bk.sql_insert_programme_match())
    assert len(ids) == 2  # the NULL-topic row and the pending row are both excluded

    rows = {r["subject_stable_id"]: r for r in (_dr_row(db, i) for i in ids)}
    confirmed_row = rows[topic_confirmed["id"]]
    assert confirmed_row["kind"] == "programme_match"
    assert confirmed_row["subject_type"] == "topic"
    assert confirmed_row["object_ref"] == "T-1"
    assert confirmed_row["provider"] == "legacy"
    assert confirmed_row["score"] == pytest.approx(0.83)
    assert confirmed_row["threshold"] == pytest.approx(0.70)
    assert confirmed_row["auto_outcome"] == "accepted"
    assert confirmed_row["human_outcome"] == "confirmed"
    assert confirmed_row["human_actor"] == caller_user["id"]
    assert confirmed_row["human_at"] is not None
    assert confirmed_row["output"] == {
        "task_id": "T-1", "confidence": 0.83,
        "suggested_status": "completed", "suggested_progress": 100,
    }
    rejected_row = rows[topic_rejected["id"]]
    assert rejected_row["human_outcome"] == "rejected"
    assert rejected_row["object_ref"] == "T-2"


def test_programme_match_key_parity_with_live_confirm_suggestion(db, monkeypatch):
    """One record via the LIVE path (lambda_org_api.confirm_suggestion), one
    via the backfill SQL on a different, pre-existing suggestion -- the
    (kind, subject_type) are identical by construction; this proves BOTH
    compute subject_stable_id/object_ref by the same formula: the row's own
    topic_id and task_id, verbatim."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    monkeypatch.setattr(lambda_org_api.programme, "write_programme",
                        lambda s3c, bucket, site_id, doc_, updated_at: doc_)

    # Live path: a fresh, still-pending suggestion confirmed through the real
    # endpoint -- 6a's writer would have inserted the verdict record already
    # in production, but the endpoint's OWN key computation is what matters
    # here (lambda_org_api.py's _stamp_decision call site), so seed the
    # matching verdict record by hand first, same convention the matcher
    # writer uses (mirrors test_decision_records_roundtrip.py's _seed_suggestion).
    live_topic = topics.upsert_topic(
        db, site["id"], "2026-09-30", "Foundations pour",
        source_s3_key=f"extractions/BF7live-{tag}/2026-09-30/sid{'e'*32}.json")
    prog = programme_tasks.create_programme(db, site_id=site["id"], name="P",
                                            source_format="local")
    task = programme_tasks.create_task(
        db, programme_id=prog["id"], parent_id=None, name="Foundations", wbs_code=None,
        start_date=None, end_date=None, duration_days=None, status="in_progress",
        zone=None, sort_order=0, updated_by=caller_user["id"])
    task_doc_id = str(task["id"])
    suggestion_id = db.execute(
        "INSERT INTO programme_progress_suggestions "
        "(site_id, task_id, topic_id, topic_title, report_date, source_s3_key, "
        " task_name, task_status_before, task_progress_before, "
        " suggested_status, suggested_progress, confidence, dedupe_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (site["id"], task_doc_id, live_topic["id"], live_topic["title"], "2026-09-30",
         live_topic["source_s3_key"], "Foundations", "in_progress", 0,
         "completed", 100, 0.9, f"dedupe-live-{tag}"),
    ).fetchone()[0]
    live_verdict = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="programme_match",
        subject_type="topic", subject_stable_id=live_topic["id"], object_ref=task_doc_id,
        provider="anthropic", model="claude-sonnet-4-6", model_version=None,
        question_set="programme_match:abc123", input_key="match_requests/x.json",
        input_hash="deadbeef",
        output={"task_id": task_doc_id, "confidence": 0.9,
                "suggested_status": "completed", "suggested_progress": 100},
        score=0.9, threshold=0.7, auto_outcome="accepted")
    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.confirm_suggestion(db, caller, suggestion_id, {})
    assert result["statusCode"] == 200
    live_after = _dr_row(db, live_verdict["id"])
    live_key = (live_after["kind"], live_after["subject_type"],
               live_after["subject_stable_id"], live_after["object_ref"])
    assert live_key == ("programme_match", "topic", live_topic["id"], task_doc_id)

    # Backfill path: a DIFFERENT, already-decided suggestion pre-dating
    # decision_records entirely (no verdict row seeded for it at all).
    hist_topic = topics.upsert_topic(
        db, site["id"], "2026-06-01", "History pour",
        source_s3_key=f"extractions/BF7hist-{tag}/2026-06-01/sid{'f'*32}.json")
    _insert_pps(db, site_id=site["id"], topic_id=hist_topic["id"], task_id="T-HIST",
               state="confirmed", decided_by=caller_user["id"])
    ids = _inserted_ids(db, bk.sql_insert_programme_match())
    backfilled = _dr_row(db, ids[0])
    backfill_key = (backfilled["kind"], backfilled["subject_type"],
                    backfilled["subject_stable_id"], backfilled["object_ref"])
    assert backfill_key == ("programme_match", "topic", hist_topic["id"], "T-HIST")

    # Same formula, different rows: (kind, subject_type) identical, and each
    # side's (subject_stable_id, object_ref) is exactly its OWN source row's
    # (topic_id, str(task_id)) -- neither hardcodes the other's values.
    assert live_key[:2] == backfill_key[:2]


# ---------------------------------------------------------------------------
# thread (topic_thread_suggestions)
# ---------------------------------------------------------------------------

def _open_topic(db, site, tag, suffix, report_date, title):
    return topics.upsert_topic(
        db, site["id"], report_date, title,
        source_s3_key=f"extractions/BF7t-{tag}/{report_date}/sid{suffix*32}.json")


def test_thread_parent_topic_id_branch(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    earlier = _open_topic(db, site, tag, "1", "2026-05-01", "Door hardware ordered")
    later = _open_topic(db, site, tag, "2", "2026-06-01", "Door hardware install")
    sugg = db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status, resolved_at, resolved_by, "
        " created_at) "
        "VALUES (%s,%s,0.42,31,'confirmed',now(),%s,%s::timestamptz) RETURNING id",
        (later["id"], earlier["id"], str(caller_user["id"]),
         _HISTORICAL_CREATED_AT)).fetchone()[0]

    stats = _stats(db, bk.sql_stats_thread())
    assert stats["eligible"] == 1
    assert stats["already_present"] == 0

    ids = _inserted_ids(db, bk.sql_insert_thread())
    assert len(ids) == 1
    row = _dr_row(db, ids[0])
    assert row["kind"] == "thread"
    assert row["subject_type"] == "topic"
    assert row["subject_stable_id"] == later["id"]
    assert row["object_ref"] == str(earlier["id"])
    assert row["provider"] == "legacy"
    assert row["score"] == pytest.approx(0.42)
    assert row["threshold"] == pytest.approx(THREAD_MIN_SCORE)
    assert row["auto_outcome"] == "accepted"
    assert row["human_outcome"] == "confirmed"
    assert row["human_actor"] == caller_user["id"]
    assert row["output"] == {"match_score": 0.42, "gap_days": 31,
                             "thread_id": None}


def test_thread_id_branch_resolves_object_ref_via_existing_accepted_record(db):
    """thread_id XOR parent_topic_id (migration 0032): when only thread_id
    survived on the historical row, the earlier candidate topic is only
    recoverable from a decision_records row the LIVE writer already wrote for
    this subject -- proven here by seeding that record with the EXACT
    convention lambda_item_writer._suggest_threads_inner uses (object_ref =
    the earlier topic's own id), then confirming the backfill's COALESCE
    subquery and decision_records.object_ref_for_accepted (the live lookup
    the org-api endpoint itself uses) agree."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    earliest = _open_topic(db, site, tag, "3", "2026-04-01", "Steel frame ordered")
    later = _open_topic(db, site, tag, "4", "2026-05-15", "Steel frame delivery")
    thread = threads.create_thread(db, site["id"], "Steel frame", "2026-04-01", "2026-04-01")
    threads.attach_topic(db, earliest["id"], thread["id"], "2026-04-01")

    # The record the live writer would already have inserted (accepted) the
    # first time this candidate was scored, before Task 6a shipped its own
    # 'accepted' row is exactly what makes the thread_id branch resolvable at
    # all -- both for the live endpoint (object_ref_for_accepted) and here.
    pre_existing = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="thread",
        subject_type="topic", subject_stable_id=later["id"], object_ref=str(earliest["id"]),
        provider="lexical", model=None, model_version=None, question_set=None,
        input_key=None, input_hash=None,
        output={"match_score": 0.5, "gap_days": 45, "thread_id": str(thread["id"])},
        score=0.5, threshold=THREAD_MIN_SCORE, auto_outcome="accepted")

    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, thread_id, score, gap_days, status, resolved_at, resolved_by, "
        " created_at) "
        "VALUES (%s,%s,0.5,45,'confirmed',now(),'not-a-uuid',%s::timestamptz)",
        (later["id"], thread["id"], _HISTORICAL_CREATED_AT))

    live_lookup = decision_records.object_ref_for_accepted(db, "thread", "topic", later["id"])
    assert live_lookup == str(earliest["id"])

    ids = _inserted_ids(db, bk.sql_insert_thread())
    # Exactly one NEW row (the historical suggestion's backfilled record) --
    # the pre-existing 'accepted' record used for the lookup is untouched.
    assert len(ids) == 1
    backfilled = _dr_row(db, ids[0])
    assert backfilled["id"] != pre_existing["id"]
    assert backfilled["object_ref"] == live_lookup
    assert backfilled["human_actor"] is None, (
        "resolved_by='not-a-uuid' must never raise a cast error and must "
        "resolve to no user")


def test_thread_id_branch_with_no_accepted_record_leaves_object_ref_null(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    topic = _open_topic(db, site, tag, "5", "2026-05-20", "Orphaned thread link")
    thread = threads.create_thread(db, site["id"], "Orphan", "2026-05-20", "2026-05-20")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, thread_id, score, gap_days, status, resolved_at, created_at) "
        "VALUES (%s,%s,0.3,5,'rejected',now(),%s::timestamptz)",
        (topic["id"], thread["id"], _HISTORICAL_CREATED_AT))

    ids = _inserted_ids(db, bk.sql_insert_thread())
    assert len(ids) == 1
    row = _dr_row(db, ids[0])
    assert row["object_ref"] is None
    assert row["human_outcome"] == "rejected"


# ---------------------------------------------------------------------------
# work_class (classification_feedback)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict,expected_outcome", [
    ("confirm_non_work", "confirmed"),
    ("reject_is_work", "rejected"),
    ("missed_personal", "rejected"),
])
def test_work_class_human_verdict_mapping(db, verdict, expected_outcome):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    topic = topics.upsert_topic(
        db, site["id"], "2026-06-15", "Lunch chat",
        source_s3_key=f"extractions/BF7w-{tag}-{verdict}/2026-06-15/sid{'9'*32}.json",
        work_class="non_work")
    db.execute(
        "INSERT INTO classification_feedback "
        "(company_id, topic_id, classifier_verdict, classifier_confidence, "
        " human_verdict, actor_user_id) VALUES (%s,%s,'non_work',0.77,%s,%s)",
        (co["id"], topic["id"], verdict, caller_user["id"]))

    stats = _stats(db, bk.sql_stats_work_class())
    assert stats["eligible"] == 1
    assert stats["already_present"] == 0

    ids = _inserted_ids(db, bk.sql_insert_work_class())
    assert len(ids) == 1
    row = _dr_row(db, ids[0])
    assert row["kind"] == "work_class"
    assert row["subject_type"] == "topic"
    assert row["subject_stable_id"] == topic["id"]
    assert row["object_ref"] is None
    assert row["provider"] == "legacy"
    assert row["score"] == pytest.approx(0.77)
    assert row["threshold"] is None
    assert row["auto_outcome"] == "accepted"
    assert row["human_outcome"] == expected_outcome
    assert row["human_actor"] == caller_user["id"]
    assert row["output"] == {"classifier_verdict": "non_work", "human_verdict": verdict}


def test_work_class_key_parity_with_live_endpoint(db):
    """One record via the LIVE endpoint (create_classification_feedback_endpoint),
    one via the backfill SQL on a different, pre-existing feedback row --
    same (kind, subject_type, object_ref) shape, same human_verdict->human_outcome
    mapping, proving the two never diverge."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)

    live_topic = topics.upsert_topic(
        db, site["id"], "2026-09-30", "Lunch and site chat",
        source_s3_key=f"extractions/BF7wlive-{tag}/2026-09-30/sid{'a'*32}.json",
        work_class="non_work")
    verdict_row = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="work_class",
        subject_type="topic", subject_stable_id=live_topic["id"], object_ref=None,
        provider="anthropic", model="claude-sonnet-4-6", model_version=None,
        question_set=None, input_key=None, input_hash=None,
        output={"work_class": "non_work", "work_confidence": 0.9, "is_mixed": False},
        score=0.9, threshold=None, auto_outcome="accepted")
    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    body = {"topic_id": str(live_topic["id"]), "human_verdict": "reject_is_work"}
    result = lambda_org_api.create_classification_feedback_endpoint(db, caller, body)
    assert result["statusCode"] == 201
    live_after = _dr_row(db, verdict_row["id"])
    assert (live_after["kind"], live_after["subject_type"], live_after["object_ref"],
           live_after["human_outcome"]) == ("work_class", "topic", None, "rejected")

    hist_topic = topics.upsert_topic(
        db, site["id"], "2026-06-01", "Old lunch chat",
        source_s3_key=f"extractions/BF7whist-{tag}/2026-06-01/sid{'b'*32}.json",
        work_class="non_work")
    db.execute(
        "INSERT INTO classification_feedback "
        "(company_id, topic_id, classifier_verdict, classifier_confidence, "
        " human_verdict, actor_user_id) VALUES (%s,%s,'work',0.6,'reject_is_work',%s)",
        (co["id"], hist_topic["id"], caller_user["id"]))
    ids = _inserted_ids(db, bk.sql_insert_work_class())
    backfilled = _dr_row(db, ids[0])
    assert (backfilled["kind"], backfilled["subject_type"], backfilled["object_ref"],
           backfilled["human_outcome"]) == ("work_class", "topic", None, "rejected")


# ---------------------------------------------------------------------------
# Idempotency: a second run inserts nothing, for every source.
# ---------------------------------------------------------------------------

def test_second_run_inserts_nothing(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)

    pm_topic = topics.upsert_topic(
        db, site["id"], "2026-06-01", "Idempotent pour",
        source_s3_key=f"extractions/BF7idem-{tag}/2026-06-01/sid{'c'*32}.json")
    _insert_pps(db, site_id=site["id"], topic_id=pm_topic["id"], task_id="T-IDEM",
               state="confirmed", decided_by=caller_user["id"])

    earlier = _open_topic(db, site, tag, "6", "2026-05-01", "Idempotent hardware ordered")
    later = _open_topic(db, site, tag, "7", "2026-06-01", "Idempotent hardware install")
    db.execute(
        "INSERT INTO topic_thread_suggestions "
        "(topic_id, parent_topic_id, score, gap_days, status, resolved_at) "
        "VALUES (%s,%s,0.4,30,'confirmed',now())", (later["id"], earlier["id"]))

    cf_topic = topics.upsert_topic(
        db, site["id"], "2026-06-15", "Idempotent chat",
        source_s3_key=f"extractions/BF7idemcf-{tag}/2026-06-15/sid{'d'*32}.json",
        work_class="non_work")
    db.execute(
        "INSERT INTO classification_feedback "
        "(company_id, topic_id, classifier_verdict, classifier_confidence, human_verdict) "
        "VALUES (%s,%s,'non_work',0.8,'confirm_non_work')", (co["id"], cf_topic["id"]))

    first_pm = _inserted_ids(db, bk.sql_insert_programme_match())
    first_th = _inserted_ids(db, bk.sql_insert_thread())
    first_wc = _inserted_ids(db, bk.sql_insert_work_class())
    assert (len(first_pm), len(first_th), len(first_wc)) == (1, 1, 1)

    second_pm = _inserted_ids(db, bk.sql_insert_programme_match())
    second_th = _inserted_ids(db, bk.sql_insert_thread())
    second_wc = _inserted_ids(db, bk.sql_insert_work_class())
    assert (second_pm, second_th, second_wc) == ([], [], [])

    pm_stats = _stats(db, bk.sql_stats_programme_match())
    assert pm_stats["already_present"] == pm_stats["eligible"] == 1
    th_stats = _stats(db, bk.sql_stats_thread())
    assert th_stats["already_present"] == th_stats["eligible"] == 1
    wc_stats = _stats(db, bk.sql_stats_work_class())
    assert wc_stats["already_present"] == wc_stats["eligible"] == 1
