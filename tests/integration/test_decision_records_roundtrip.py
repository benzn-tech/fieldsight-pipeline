"""Integration: Track B Task 6a -- decision_records against a real Postgres
(skipped without TEST_DATABASE_URL -- a skip is NOT a pass).

Two layers, same split as tests/integration/test_question_answered_survives.py:

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
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from db.connection import get_connection
from repositories import companies, decision_records, findings, sites, topics, users

pytestmark = pytest.mark.integration

lambda_suggestion_writer = pytest.importorskip(
    "lambda_suggestion_writer", reason="requires psycopg (installed in CI)")


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


def test_list_for_eval_scopes_by_company_and_kind_since(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    other_co, other_site = _seed_company_site(db, tag + "-b")
    topic_id = uuid.uuid4()

    match_row = _insert(db, co, site, topic_id, kind="programme_match")
    _insert(db, co, site, topic_id, kind="work_class")  # different kind -- excluded
    _insert(db, other_co, other_site, uuid.uuid4(), kind="programme_match")  # different company

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
    topic_id = uuid.uuid4()

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
