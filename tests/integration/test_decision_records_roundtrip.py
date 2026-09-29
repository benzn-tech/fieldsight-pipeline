"""Integration: Track B Task 6a/6b -- decision_records against a real Postgres
(skipped without TEST_DATABASE_URL -- a skip is NOT a pass).

Three layers, extending the split from tests/integration/test_question_answered_survives.py:

  * Repo-level (`db` fixture -- rolled back, no manual cleanup needed):
    `decision_records.insert`/`list_for_eval`/`set_human_outcome` driven
    directly against a real Postgres, proving the SQL actually executes
    (a FakeConn double can typo a column name and still pass) --
    `set_human_outcome`'s "latest record only" claim in particular is a
    claim about `ORDER BY created_at DESC` against real timestamps, which
    a mocked cursor cannot exercise at all.

  * End-to-end (a real committed connection, NOT the `db` fixture -- its
    rollback would hide the writer's insert from this test's own read):
    drives `lambda_suggestion_writer.lambda_handler` directly against a
    real Postgres, proving `verdicts` handling -- company_id resolution
    via `sites.get_site`, the programme_impact row-id -> stable_id
    resolution via `findings.get_stable_id`, and that the decision record
    lands in the SAME transaction as the suggestion/impact writes.

  * Task 6b seam proofs (`db` fixture -- lambda_org_api's endpoint functions
    take `conn` as a plain argument, no internal get_connection() of their
    own, so they run directly against the same rolled-back connection the
    writer-side insert used -- no manual cleanup needed here either): each
    proof writes the decision_records row with the EXACT (kind, subject_type,
    subject_stable_id, object_ref) 6a's real writer code produces for that
    verdict kind, then calls the REAL confirm/reject/thread/classification-
    feedback endpoint function and reads the row back, proving the two sides
    of the seam actually agree -- not that each one, tested alone, looks
    plausible. Plus Ruling R7 (deletion visibility) proofs against
    `list_for_eval`.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from db.connection import get_connection
from repositories import (companies, decision_records, findings, programme_tasks,
                          redactions, sites, threads, topics, users)

pytestmark = pytest.mark.integration

lambda_suggestion_writer = pytest.importorskip(
    "lambda_suggestion_writer", reason="requires psycopg (installed in CI)")
lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")
lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


def _seed_company_site(conn, tag):
    co = companies.create_company(conn, f"DR6a-Co-{tag}")
    site = sites.create_site(conn, co["id"], f"DR6a-Site-{tag}")
    return co, site


def _insert(conn, co, site, topic_id, *, kind="programme_match", object_ref="T-1",
           auto_outcome="accepted", output=None):
    return decision_records.insert(
        conn, company_id=co["id"], site_id=site["id"], kind=kind,
        subject_type="topic", subject_stable_id=topic_id, object_ref=object_ref,
        provider="anthropic", model="claude-sonnet-4-6", model_version=None,
        question_set="programme_match:abc123", input_key="match_requests/x.json",
        input_hash="deadbeef", output=output or {"task_id": object_ref, "confidence": 0.9},
        score=0.9, threshold=0.7, auto_outcome=auto_outcome,
    )


# ---------------------------------------------------------------------------
# Repo-level: insert / list_for_eval / set_human_outcome
# ---------------------------------------------------------------------------

def test_insert_round_trips_every_column_including_jsonb_output(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    topic_id = uuid.uuid4()

    row = _insert(db, co, site, topic_id, output={"task_id": "T-1", "confidence": 0.9,
                                                   "suggested_status": "completed"})

    assert row["id"] is not None
    assert row["company_id"] == co["id"]
    assert row["site_id"] == site["id"]
    assert row["kind"] == "programme_match"
    assert row["subject_type"] == "topic"
    assert row["subject_stable_id"] == topic_id
    assert row["object_ref"] == "T-1"
    assert row["provider"] == "anthropic"
    # jsonb round-trips as a real dict, not a string -- proves Jsonb() was
    # actually applied, not just that the column accepted text.
    assert row["output"] == {"task_id": "T-1", "confidence": 0.9,
                             "suggested_status": "completed"}
    assert row["score"] == 0.9
    assert row["threshold"] == 0.7
    assert row["auto_outcome"] == "accepted"
    assert row["human_outcome"] is None
    assert row["human_actor"] is None
    assert row["human_at"] is None
    assert row["created_at"] is not None


def test_insert_rejects_unknown_column(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    with pytest.raises(ValueError):
        decision_records.insert(
            db, company_id=co["id"], site_id=site["id"], kind="thread",
            subject_type="topic", subject_stable_id=uuid.uuid4(), provider="lexical",
            output={}, auto_outcome="accepted", bogus_column="nope")


def test_insert_rejects_generated_columns():
    # Pure validation -- no DB call is made before the ValueError, so this
    # needs no `db` fixture at all, but lives here next to the column-
    # whitelist test above rather than duplicated into a unit file.
    with pytest.raises(ValueError):
        decision_records.insert(None, id=uuid.uuid4(), company_id="x")


def _seed_topic(db, site, tag):
    """A real, resolvable topic -- Ruling R7's visible_decision_records_predicate
    (Task 6b) resolves every decision_records row's subject to a topic and hides
    rows that resolve to none at all, so a `list_for_eval` test needs a real topic
    behind subject_stable_id, not a bare `uuid.uuid4()` (see
    test_r7_orphaned_subject_resolves_to_no_topic_and_is_fail_closed_invisible for
    the dedicated proof of that fail-closed behaviour itself)."""
    return topics.upsert_topic(
        db, site["id"], "2026-09-30", "Seed topic",
        source_s3_key=f"extractions/Seed-{tag}/2026-09-30/sid{uuid.uuid4().hex[:24]}.json")


def test_list_for_eval_scopes_by_company_and_kind_since(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    other_co, other_site = _seed_company_site(db, tag + "-b")
    topic_id = _seed_topic(db, site, tag)["id"]

    match_row = _insert(db, co, site, topic_id, kind="programme_match")
    _insert(db, co, site, topic_id, kind="work_class")  # different kind -- excluded
    _insert(db, other_co, other_site, _seed_topic(db, other_site, tag + "-b")["id"],
           kind="programme_match")  # different company

    since = datetime.now(timezone.utc) - timedelta(days=1)
    rows = decision_records.list_for_eval(db, co["id"], "programme_match", since)

    assert [r["id"] for r in rows] == [match_row["id"]]

    # A `since` in the future excludes everything -- the boundary is real,
    # not just "return whatever matches company/kind".
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert decision_records.list_for_eval(db, co["id"], "programme_match", future) == []


def test_list_for_eval_newest_first(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    topic_id = _seed_topic(db, site, tag)["id"]

    first = _insert(db, co, site, topic_id, object_ref="T-1")
    db.execute("UPDATE decision_records SET created_at = created_at - interval '1 hour' "
              "WHERE id=%s", (first["id"],))
    second = _insert(db, co, site, topic_id, object_ref="T-2")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    rows = decision_records.list_for_eval(db, co["id"], "programme_match", since)

    assert [r["id"] for r in rows] == [second["id"], first["id"]]


def test_set_human_outcome_stamps_only_the_latest_record(db):
    """The core claim of `set_human_outcome`'s docstring: re-extraction can
    write a fresh record for the SAME (kind, subject_type, subject_stable_id,
    object_ref) on every pass -- only the newest one gets stamped, an older
    record for that exact same key is left untouched."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller = users.upsert_user(
        db, f"sub-dr6a-{tag}", f"admin-dr6a-{tag}@example.com",
        company_id=co["id"], global_role="admin")
    topic_id = uuid.uuid4()

    older = _insert(db, co, site, topic_id, object_ref="T-1")
    db.execute("UPDATE decision_records SET created_at = created_at - interval '1 hour' "
              "WHERE id=%s", (older["id"],))
    newer = _insert(db, co, site, topic_id, object_ref="T-1")

    n = decision_records.set_human_outcome(
        db, "programme_match", "topic", topic_id, "T-1", "confirmed", caller["id"])
    assert n == 1

    older_after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (older["id"],)).fetchone()
    newer_after = db.execute(
        "SELECT human_outcome, human_actor, human_at FROM decision_records WHERE id=%s",
        (newer["id"],)).fetchone()

    assert older_after[0] is None, "an OLDER record for the same key must not be stamped"
    assert newer_after[0] == "confirmed"
    assert newer_after[1] == caller["id"]
    assert newer_after[2] is not None


def test_set_human_outcome_never_stamps_a_different_tasks_record(db):
    """The other half of the same claim: two DIFFERENT tasks matched to the
    SAME topic (object_ref differs) must not cross-stamp each other."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller = users.upsert_user(
        db, f"sub-dr6a-{tag}", f"admin-dr6a-{tag}@example.com",
        company_id=co["id"], global_role="admin")
    topic_id = uuid.uuid4()

    for_task_1 = _insert(db, co, site, topic_id, object_ref="T-1")
    for_task_2 = _insert(db, co, site, topic_id, object_ref="T-2")

    n = decision_records.set_human_outcome(
        db, "programme_match", "topic", topic_id, "T-1", "confirmed", caller["id"])
    assert n == 1

    t1_after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (for_task_1["id"],)).fetchone()
    t2_after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (for_task_2["id"],)).fetchone()
    assert t1_after[0] == "confirmed"
    assert t2_after[0] is None, "a DIFFERENT task's record for the same topic must be untouched"


def test_set_human_outcome_matches_null_object_ref_for_real(db):
    """object_ref may be legitimately NULL (a programme_match verdict where
    Claude picked no task) -- IS NOT DISTINCT FROM must match a NULL
    argument against a NULL column, not silently match zero rows."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller = users.upsert_user(
        db, f"sub-dr6a-{tag}", f"admin-dr6a-{tag}@example.com",
        company_id=co["id"], global_role="admin")
    topic_id = uuid.uuid4()

    row = _insert(db, co, site, topic_id, object_ref=None)

    n = decision_records.set_human_outcome(
        db, "programme_match", "topic", topic_id, None, "rejected", caller["id"])
    assert n == 1

    after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (row["id"],)).fetchone()
    assert after[0] == "rejected"


def test_set_human_outcome_returns_zero_for_no_match(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller = users.upsert_user(
        db, f"sub-dr6a-{tag}", f"admin-dr6a-{tag}@example.com",
        company_id=co["id"], global_role="admin")

    n = decision_records.set_human_outcome(
        db, "programme_match", "topic", uuid.uuid4(), "T-nonexistent", "confirmed", caller["id"])
    assert n == 0


# ---------------------------------------------------------------------------
# End-to-end -- lambda_suggestion_writer.lambda_handler against a REAL,
# committed connection (get_connection monkeypatched, same pattern as
# test_question_answered_survives.py -- the `db` fixture's rollback would
# hide the writer's own insert from this test's own read).
# ---------------------------------------------------------------------------

def test_suggestion_writer_inserts_verdicts_for_real(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = topic = finding_rows = None
    try:
        co = companies.create_company(seed, f"DR6a-SW-Co-{tag}")
        site = sites.create_site(seed, co["id"], f"DR6a-SW-Site-{tag}")
        topic = topics.upsert_topic(
            seed, site["id"], "2026-09-30", "Steel frame progress",
            source_s3_key=f"extractions/x/{tag}.json")
        finding_rows = findings.insert_findings(
            seed, topic["id"], site["id"],
            [{"observation": "Steel delivery delayed", "domain": "progress",
              "severity": "major", "entity": {"name": "SteelCo", "trade": "Steel"}}])
        finding_id = finding_rows[0]["id"]

        monkeypatch.setattr(lambda_suggestion_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))

        verdicts = [
            # programme_match, rejected -- subject is the topic's OWN id.
            {"kind": "programme_match", "subject_type": "topic", "subject": str(topic["id"]),
             "object_ref": "T-1", "site_id": str(site["id"]), "provider": "anthropic",
             "model": "claude-sonnet-4-6", "model_version": None,
             "question_set": "programme_match:abc123", "input_key": "match_requests/x.json",
             "input_hash": "deadbeef", "output": {"task_id": "T-1", "confidence": 0.5},
             "score": 0.5, "threshold": 0.7, "auto_outcome": "rejected"},
            # programme_impact, accepted -- subject is a findings ROW id,
            # resolved to stable_id by the writer.
            {"kind": "programme_impact", "subject_type": "finding", "subject": str(finding_id),
             "subject_is_row_id": True, "object_ref": "T-1", "site_id": str(site["id"]),
             "provider": "anthropic", "model": "claude-sonnet-4-6", "model_version": None,
             "question_set": "programme_impact:def456", "input_key": "match_requests/x.json",
             "input_hash": "deadbeef", "output": {"finding_id": str(finding_id), "task_id": "T-1",
                                                    "impact_severity": "major", "confidence": 0.9},
             "score": 0.9, "threshold": 0.7, "auto_outcome": "accepted"},
        ]

        result = lambda_suggestion_writer.lambda_handler({"verdicts": verdicts}, None)
        assert result["verdicts_recorded"] == 2

        rows = seed.execute(
            "SELECT kind, subject_type, subject_stable_id, object_ref, auto_outcome, company_id "
            "FROM decision_records WHERE site_id=%s ORDER BY kind", (site["id"],)).fetchall()
        assert len(rows) == 2

        by_kind = {r[0]: r for r in rows}
        match_row = by_kind["programme_match"]
        assert match_row[1] == "topic"
        assert match_row[2] == topic["id"]
        assert match_row[3] == "T-1"
        assert match_row[4] == "rejected"
        assert match_row[5] == co["id"]

        # findings._COLS (insert_findings' RETURNING list) does not include
        # stable_id -- read it straight from the table.
        expected_stable_id = seed.execute(
            "SELECT stable_id FROM findings WHERE id=%s", (finding_id,)).fetchone()[0]

        impact_row = by_kind["programme_impact"]
        assert impact_row[1] == "finding"
        # The written subject_stable_id is the FINDING's stable_id, not the
        # row id the matcher handed in -- proves the id->stable_id
        # resolution actually ran in SQL.
        assert impact_row[2] == expected_stable_id
        assert impact_row[2] != finding_id
        assert impact_row[4] == "accepted"
    finally:
        site_id = site["id"] if site is not None else None
        co_id = co["id"] if co is not None else None
        if site_id is not None:
            # ON DELETE CASCADE from sites covers topics, findings and
            # decision_records (all FK to sites) in one statement.
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if co_id is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
        if co_id is not None:
            remaining = seed.execute(
                "SELECT (SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM decision_records WHERE site_id=%s)",
                (co_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()


def test_verdict_insert_failure_does_not_lose_the_suggestion_row(monkeypatch, migrated_db_url):
    """Ruling R14 -- against REAL Postgres, not a mock: a genuinely bad
    verdict entry must not take the suggestion it shares a transaction
    with down too. Forces a real exception inside decision_records.insert
    (a set() is not JSON-serializable, so Jsonb()'s binding raises for
    real) rather than monkeypatching the function, so this proves the
    SAVEPOINT itself works against the real driver, not just that Python
    control flow reaches a try/except."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = topic = None
    try:
        co = companies.create_company(seed, f"DR6a-R14-Co-{tag}")
        site = sites.create_site(seed, co["id"], f"DR6a-R14-Site-{tag}")
        topic = topics.upsert_topic(
            seed, site["id"], "2026-09-30", "Steel frame progress",
            source_s3_key=f"extractions/x-r14-{tag}.json")

        monkeypatch.setattr(lambda_suggestion_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))

        suggestion = {
            "site_id": str(site["id"]), "task_id": "T-1", "topic_id": str(topic["id"]),
            "topic_title": "Steel frame progress", "topic_summary": "s",
            "topic_user_id": None, "report_date": "2026-09-30",
            "source_s3_key": f"extractions/x-r14-{tag}.json", "task_name": "Steel frame",
            "task_status_before": "in_progress", "task_progress_before": 40,
            "suggested_status": "completed", "suggested_progress": 100,
            "confidence": 0.9, "match_evidence": {"cosine": 0.1},
        }
        bad_verdict = {
            "kind": "programme_match", "subject_type": "topic", "subject": str(topic["id"]),
            "object_ref": "T-1", "site_id": str(site["id"]), "provider": "anthropic",
            "model": None, "model_version": None, "question_set": "programme_match:abc",
            "input_key": "match_requests/x.json", "input_hash": "deadbeef",
            "output": {"cannot_serialize": {1, 2, 3}},   # a set -- not JSON
            "score": 0.9, "threshold": 0.7, "auto_outcome": "accepted",
        }

        result = lambda_suggestion_writer.lambda_handler(
            {"suggestions": [suggestion], "verdicts": [bad_verdict]}, None)

        assert result["written"] == 1
        assert result["verdicts_recorded"] == 0
        assert result["verdicts_failed"] == 1

        suggestion_row = seed.execute(
            "SELECT id FROM programme_progress_suggestions WHERE site_id=%s",
            (site["id"],)).fetchone()
        assert suggestion_row is not None, "the suggestion row must have committed"

        decision_row = seed.execute(
            "SELECT id FROM decision_records WHERE site_id=%s", (site["id"],)).fetchone()
        assert decision_row is None, "the failed verdict must not have left a row behind"
    finally:
        site_id = site["id"] if site is not None else None
        co_id = co["id"] if co is not None else None
        if site_id is not None:
            # ON DELETE CASCADE from sites covers topics, programme_progress_suggestions
            # and decision_records (all FK to sites) in one statement.
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if co_id is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
        if co_id is not None:
            remaining = seed.execute(
                "SELECT (SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM programme_progress_suggestions WHERE site_id=%s), "
                "(SELECT count(*) FROM decision_records WHERE site_id=%s)",
                (co_id, site_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()


# ---------------------------------------------------------------------------
# Task 6b seam: /programme/suggestions confirm + reject.
#
# 6a's writer (lambda_programme_matcher._build_match_verdict_record, task-6a-report.md
# note #3) stamps subject_type='topic', subject_stable_id=<the suggestion's OWN
# topic_id> (no resolution step -- topics are not re-keyed), object_ref=<the
# suggestion's OWN task_id>. confirm_suggestion/reject_suggestion must stamp the
# SAME key or set_human_outcome silently matches zero rows.
# ---------------------------------------------------------------------------

def _seed_admin(db, co, tag):
    return users.upsert_user(
        db, f"sub-6b-{tag}", f"admin-6b-{tag}@example.com",
        company_id=co["id"], global_role="admin")


def _seed_suggestion(db, co, site, caller_user, tag, *, suggested_status="completed",
                     suggested_progress=100):
    topic = topics.upsert_topic(
        db, site["id"], "2026-09-30", "Foundations pour",
        source_s3_key=f"extractions/Co6b-{tag}/2026-09-30/sid{'e' * 32}.json")
    prog = programme_tasks.create_programme(
        db, site_id=site["id"], name="P", source_format="local")
    task = programme_tasks.create_task(
        db, programme_id=prog["id"], parent_id=None, name="Foundations", wbs_code=None,
        start_date=None, end_date=None, duration_days=None, status="in_progress",
        zone=None, sort_order=0, updated_by=caller_user["id"])
    task_doc_id = str(task["id"])
    suggestion_id = db.execute(
        "INSERT INTO programme_progress_suggestions "
        "(site_id, task_id, topic_id, topic_title, topic_user_id, report_date, "
        " source_s3_key, task_name, task_status_before, task_progress_before, "
        " suggested_status, suggested_progress, confidence, dedupe_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (site["id"], task_doc_id, topic["id"], topic["title"], None, "2026-09-30",
         topic["source_s3_key"], "Foundations", "in_progress", 0,
         suggested_status, suggested_progress, 0.9, f"dedupe-6b-{tag}"),
    ).fetchone()[0]
    # The EXACT shape lambda_programme_matcher._build_match_verdict_record ->
    # lambda_suggestion_writer._record_verdict writes for a programme_match verdict.
    verdict_row = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="programme_match",
        subject_type="topic", subject_stable_id=topic["id"], object_ref=task_doc_id,
        provider="anthropic", model="claude-sonnet-4-6", model_version=None,
        question_set="programme_match:abc123", input_key="match_requests/x.json",
        input_hash="deadbeef",
        output={"task_id": task_doc_id, "confidence": 0.9,
                "suggested_status": suggested_status, "suggested_progress": suggested_progress},
        score=0.9, threshold=0.7, auto_outcome="accepted")
    return topic, task, suggestion_id, verdict_row


def test_confirm_suggestion_stamps_the_verdict_the_matcher_wrote(db, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    _topic, _task, suggestion_id, verdict_row = _seed_suggestion(db, co, site, caller_user, tag)
    monkeypatch.setattr(lambda_org_api.programme, "write_programme",
                        lambda s3c, bucket, site_id, doc_, updated_at: doc_)

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.confirm_suggestion(db, caller, suggestion_id, {})
    assert result["statusCode"] == 200

    after = db.execute(
        "SELECT human_outcome, human_actor FROM decision_records WHERE id=%s",
        (verdict_row["id"],)).fetchone()
    assert after[0] == "confirmed"
    assert after[1] == caller_user["id"]


def test_confirm_suggestion_with_reviewer_override_stamps_edited(db, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    _topic, _task, suggestion_id, verdict_row = _seed_suggestion(db, co, site, caller_user, tag)
    monkeypatch.setattr(lambda_org_api.programme, "write_programme",
                        lambda s3c, bucket, site_id, doc_, updated_at: doc_)

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.confirm_suggestion(
        db, caller, suggestion_id, {"status": "in_progress", "progress_pct": 75})
    assert result["statusCode"] == 200

    after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (verdict_row["id"],)).fetchone()
    assert after[0] == "edited"


def test_reject_suggestion_stamps_the_verdict_the_matcher_wrote(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    _topic, _task, suggestion_id, verdict_row = _seed_suggestion(db, co, site, caller_user, tag)

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.reject_suggestion(db, caller, suggestion_id)
    assert result["statusCode"] == 200

    after = db.execute(
        "SELECT human_outcome, human_actor FROM decision_records WHERE id=%s",
        (verdict_row["id"],)).fetchone()
    assert after[0] == "rejected"
    assert after[1] == caller_user["id"]


# ---------------------------------------------------------------------------
# Task 6b seam: /threads/suggestions confirm + reject -- both branches of
# `topic_thread_suggestions`'s `thread_id XOR parent_topic_id`.
#
# Runs the REAL writer, `lambda_item_writer._suggest_threads`, against real
# candidate topics -- not a hand-built decision_records row -- so the proof
# covers the whole seam: the writer's own object_ref choice, and the
# endpoint's ability to recover it.
# ---------------------------------------------------------------------------

def _open_topic(db, site, tag, suffix, report_date, title, summary):
    return topics.upsert_topic(
        db, site["id"], report_date, title, summary=summary,
        source_s3_key=f"extractions/Thr6b-{tag}/{report_date}/sid{suffix * 32}.json",
        action_items=[{"text": f"chase {title.lower()}", "status": "open"}])


def test_confirm_thread_suggestion_anchoring_a_new_thread(db):
    """parent_topic_id branch: the writer's object_ref (the earlier candidate
    topic's OWN id) is directly on the topic_thread_suggestions row."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    title = "Ground floor concrete pour delay"
    summary = "Waiting on the concrete supplier for the ground floor pour"
    earlier = _open_topic(db, site, tag, "a", "2026-08-01", title, summary)
    new = _open_topic(db, site, tag, "b", "2026-09-10", title, summary)

    made = lambda_item_writer._suggest_threads(
        db, co["id"], site["id"], "2026-09-10",
        [{"topic_id": new["id"], "title": title, "summary": summary, "open_items": 1}])
    assert made == 1

    suggestion = threads.get_suggestion(
        db, db.execute("SELECT id FROM topic_thread_suggestions WHERE topic_id=%s",
                       (new["id"],)).fetchone()[0])
    assert suggestion["parent_topic_id"] == earlier["id"]
    assert suggestion["thread_id"] is None

    accepted = db.execute(
        "SELECT id, object_ref FROM decision_records WHERE kind='thread' "
        "AND subject_stable_id=%s AND auto_outcome='accepted'", (new["id"],)).fetchone()
    assert accepted[1] == str(earlier["id"])

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.confirm_thread_suggestion(db, caller, suggestion["id"])
    assert result["statusCode"] == 200

    after = db.execute(
        "SELECT human_outcome, human_actor FROM decision_records WHERE id=%s",
        (accepted[0],)).fetchone()
    assert after[0] == "confirmed"
    assert after[1] == caller_user["id"]


def test_reject_thread_suggestion_into_an_existing_thread(db):
    """thread_id branch: the winning candidate already belongs to a thread, so
    the topic_thread_suggestions row carries only thread_id -- object_ref has
    to be recovered from decision_records.object_ref_for_accepted."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    title = "Steel frame delivery delay"
    summary = "Steel frame delivery pushed back a second time"
    earliest = _open_topic(db, site, tag, "c", "2026-07-01", title, summary)
    thread = threads.create_thread(db, site["id"], title, "2026-07-01", "2026-07-01")
    threads.attach_topic(db, earliest["id"], thread["id"], "2026-07-01")
    # earliest now carries thread_id -- the candidate _suggest_threads_inner will
    # score against carries `c.get("thread_id")` truthy.
    later = _open_topic(db, site, tag, "d", "2026-08-15", title, summary)

    made = lambda_item_writer._suggest_threads(
        db, co["id"], site["id"], "2026-08-15",
        [{"topic_id": later["id"], "title": title, "summary": summary, "open_items": 1}])
    assert made == 1

    suggestion_id = db.execute(
        "SELECT id FROM topic_thread_suggestions WHERE topic_id=%s", (later["id"],)).fetchone()[0]
    suggestion = threads.get_suggestion(db, suggestion_id)
    assert suggestion["parent_topic_id"] is None
    assert suggestion["thread_id"] == thread["id"]

    accepted = db.execute(
        "SELECT id, object_ref FROM decision_records WHERE kind='thread' "
        "AND subject_stable_id=%s AND auto_outcome='accepted'", (later["id"],)).fetchone()
    # The writer's object_ref is the earlier TOPIC's id, never the thread's id.
    assert accepted[1] == str(earliest["id"])
    assert accepted[1] != str(thread["id"])

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    result = lambda_org_api.reject_thread_suggestion(db, caller, suggestion_id)
    assert result["statusCode"] == 200

    after = db.execute(
        "SELECT human_outcome FROM decision_records WHERE id=%s", (accepted[0],)).fetchone()
    assert after[0] == "rejected"


# ---------------------------------------------------------------------------
# Task 6b seam: /classification-feedback.
#
# 6a's writer (lambda_item_writer._record_work_class_decision) stamps
# kind='work_class', subject_type='topic', subject_stable_id=<topic id>,
# object_ref=None.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict,expected_outcome", [
    ("confirm_non_work", "confirmed"),
    ("reject_is_work", "rejected"),
    ("missed_personal", "rejected"),
])
def test_classification_feedback_stamps_the_verdict_the_item_writer_wrote(
        db, verdict, expected_outcome):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    topic = topics.upsert_topic(
        db, site["id"], "2026-09-30", "Lunch and site chat",
        source_s3_key=f"extractions/Wc6b-{tag}/2026-09-30/sid{'f' * 32}.json",
        work_class="non_work")
    verdict_row = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="work_class",
        subject_type="topic", subject_stable_id=topic["id"], object_ref=None,
        provider="anthropic", model="claude-sonnet-4-6", model_version=None,
        question_set=None, input_key=None, input_hash=None,
        output={"work_class": "non_work", "work_confidence": 0.9, "is_mixed": False},
        score=0.9, threshold=None, auto_outcome="accepted")

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    body = {"topic_id": str(topic["id"]), "human_verdict": verdict}
    result = lambda_org_api.create_classification_feedback_endpoint(db, caller, body)
    assert result["statusCode"] == 201

    after = db.execute(
        "SELECT human_outcome, human_actor FROM decision_records WHERE id=%s",
        (verdict_row["id"],)).fetchone()
    assert after[0] == expected_outcome
    assert after[1] == caller_user["id"]


# ---------------------------------------------------------------------------
# Ruling R7 -- deletion visibility. `visible_decision_records_predicate`
# carries the two DELETION arms only (topic id + source prefix), never the
# live/supersession arm: a record must outlive the row it was about being
# superseded by a later extraction pass, and only an actual customer
# deletion hides it.
# ---------------------------------------------------------------------------

def test_r7_tombstoned_recording_hides_records_superseded_and_other_company_stay_visible(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    other_co, other_site = _seed_company_site(db, tag + "-other")
    caller_user = _seed_admin(db, co, tag)
    since = datetime.now(timezone.utc) - timedelta(days=1)

    # Topic 1 -- will be tombstoned by TOPIC ID (the live-content arm of a
    # delete, delete_recordings_endpoint's per-topic redactions.create_redaction).
    prefix1 = f"extractions/R7a-{tag}/2026-09-30/"
    topic1 = topics.upsert_topic(db, site["id"], "2026-09-30", "Deleted by topic id",
                                 source_s3_key=prefix1 + f"sid{'1' * 32}.json")
    row1 = _insert(db, co, site, topic1["id"], object_ref="T-1")

    # Topic 2 -- will be tombstoned by SOURCE PREFIX (the arm that covers rows the
    # nightly pipeline re-creates tomorrow with new uuids -- create_recording_tombstone,
    # exactly what delete_recordings_endpoint calls first, per recording).
    prefix2 = f"extractions/R7b-{tag}/2026-09-30/"
    topic2 = topics.upsert_topic(db, site["id"], "2026-09-30", "Deleted by source prefix",
                                 source_s3_key=prefix2 + f"sid{'2' * 32}.json")
    row2 = _insert(db, co, site, topic2["id"], object_ref="T-2")

    # Topic 3 -- merely SUPERSEDED (a later extraction pass), never deleted.
    topic3 = topics.upsert_topic(db, site["id"], "2026-09-30", "Superseded, not deleted",
                                 source_s3_key=f"extractions/R7c-{tag}/2026-09-30/sid{'3' * 32}.json")
    row3 = _insert(db, co, site, topic3["id"], object_ref="T-3")
    db.execute("UPDATE topics SET superseded_at=now(), superseded_by_run='final:t2' "
              "WHERE id=%s", (topic3["id"],))

    # A different company's record, untouched by any of this company's tombstones.
    other_topic = topics.upsert_topic(
        db, other_site["id"], "2026-09-30", "Other company",
        source_s3_key=f"extractions/R7d-{tag}/2026-09-30/sid{'4' * 32}.json")
    other_row = _insert(db, other_co, other_site, other_topic["id"], object_ref="T-4")

    before = {r["id"] for r in decision_records.list_for_eval(db, co["id"], "programme_match", since)}
    assert before == {row1["id"], row2["id"], row3["id"]}

    redactions.create_redaction(db, co["id"], topic1["id"], "user deleted the recording",
                                caller_user["id"], "admin", target_type="topic", scope="deleted")
    redactions.create_recording_tombstone(db, co["id"], prefix2, "user deleted the recording",
                                          caller_user["id"], "admin")

    after = {r["id"] for r in decision_records.list_for_eval(db, co["id"], "programme_match", since)}
    assert after == {row3["id"]}, "only the superseded (not deleted) topic's record must survive"

    other_after = {r["id"] for r in decision_records.list_for_eval(
        db, other_co["id"], "programme_match", since)}
    assert other_after == {other_row["id"]}, (
        "a different company's records must never appear, and must never be "
        "affected by another company's deletes")


def test_r7_resolves_a_finding_subject_to_its_topic(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    caller_user = _seed_admin(db, co, tag)
    topic = topics.upsert_topic(db, site["id"], "2026-09-30", "Steel frame progress",
                                source_s3_key=f"extractions/R7f-{tag}/2026-09-30/sid{'5' * 32}.json")
    finding_rows = findings.insert_findings(
        db, topic["id"], site["id"],
        [{"observation": "Steel delivery delayed", "domain": "progress",
          "severity": "major", "entity": {"name": "SteelCo", "trade": "Steel"}}])
    finding_stable_id = db.execute(
        "SELECT stable_id FROM findings WHERE id=%s", (finding_rows[0]["id"],)).fetchone()[0]
    row = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="programme_impact",
        subject_type="finding", subject_stable_id=finding_stable_id, object_ref="T-1",
        provider="anthropic", model=None, model_version=None,
        question_set="programme_impact:def456", input_key=None, input_hash=None,
        output={"finding_id": str(finding_rows[0]["id"]), "task_id": "T-1",
                "impact_severity": "major", "confidence": 0.9},
        score=0.9, threshold=0.7, auto_outcome="accepted")

    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert [r["id"] for r in decision_records.list_for_eval(
        db, co["id"], "programme_impact", since)] == [row["id"]]

    redactions.create_redaction(db, co["id"], topic["id"], "user deleted the recording",
                                caller_user["id"], "admin", target_type="topic", scope="deleted")
    assert decision_records.list_for_eval(db, co["id"], "programme_impact", since) == []


def test_r7_orphaned_subject_resolves_to_no_topic_and_is_fail_closed_invisible(db):
    """A subject_stable_id matching no row at all (nothing under that subject_type
    references it) must resolve to NO topic and therefore not be visible --
    fail-closed, not fail-open."""
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    row = decision_records.insert(
        db, company_id=co["id"], site_id=site["id"], kind="programme_match",
        subject_type="topic", subject_stable_id=uuid.uuid4(), object_ref="T-ghost",
        provider="anthropic", model=None, model_version=None, question_set=None,
        input_key=None, input_hash=None, output={"task_id": "T-ghost"},
        score=0.5, threshold=0.7, auto_outcome="rejected")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert decision_records.list_for_eval(db, co["id"], "programme_match", since) == []
    # sanity: the row really was written, it just does not resolve to a topic.
    stored = db.execute(
        "SELECT id FROM decision_records WHERE id=%s", (row["id"],)).fetchone()
    assert stored is not None
