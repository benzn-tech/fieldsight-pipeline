"""Late-chunk reopen: reconcile settles a reopened round on ITS result.

Round 1's `session_finalize_results/{sid}.json` says `sent`. After the reopen
the session is `finalizing` again with reopen_count=1, and round 2's worker
writes `{sid}-r1`. Reading the bare sid would settle round 2 on round 1's
evidence -- before round 2's email exists.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
from datetime import datetime, timezone

import pytest

import lambda_finalize_claim
from repositories import meeting_session

pytestmark = pytest.mark.integration

SID = "b" * 32
UTC = timezone.utc


def _finalizing_round_two(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    t = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, last_segment_at, closed_at, close_intent, version, reopen_count, "
        "finalizing_at) VALUES (%s,%s,%s,'audio','finalizing',%s,%s,%s,'idle',6,1,%s)",
        (SID, cid, uid, t, t, t, t))


def test_reconcile_reads_the_round_result(db):
    _finalizing_round_two(db)
    asked = []

    def bare_only(rid):
        asked.append(rid)
        return {"status": "sent"} if rid == SID else None

    assert lambda_finalize_claim.reconcile(db, read_result=bare_only) == []
    assert meeting_session.get(db, SID)["status"] == "finalizing"
    assert asked == [f"{SID}-r1"]

    out = lambda_finalize_claim.reconcile(
        db, read_result=lambda rid: {"status": "sent"} if rid == f"{SID}-r1" else None)
    assert out == [(SID, "sent")]
    assert meeting_session.get(db, SID)["status"] == "sent"
