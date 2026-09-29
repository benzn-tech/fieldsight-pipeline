"""Integration: Track B Task 5 -- decisions/questions as rows, dual-written, and a question
that stays answered across a re-extraction.

Two layers, against a real Postgres (skipped without TEST_DATABASE_URL -- a skip is NOT a
pass):

  * End-to-end (a real committed connection, NOT the `db` fixture -- its rollback would hide
    the first pass's writes from the second): drives `lambda_item_writer.write_extraction_items`
    twice, live pass then final pass, on the SAME extraction key -- same two-real-passes
    harness as tests/integration/test_supersede_two_passes.py (Task 3/4), reused verbatim for
    the same reason that file gives: `extract_session.out_key` is computed once, so live and
    final really do write the same S3 key in production.

  * The org-api handler (the `db` fixture -- rolled back, no manual cleanup needed): drives
    `lambda_org_api.patch_question` directly against a real Postgres, the same way
    tests/integration/test_confirm_suggestion_superseded_topic.py drives `confirm_suggestion`
    -- proving the stable_id lookup (through `visible_topics_predicate`), the UPDATE and the
    `content_edits` insert all actually execute, not just that the Python control flow is
    right (a FakeConn double can typo a column name and still pass).
"""
import difflib
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-09-30"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")

import carry_forward  # noqa: E402  (after the importorskip gates, pure module)


# ---------------------------------------------------------------------------
# End-to-end harness -- copied from test_supersede_two_passes.py's _FakeS3/_FakePaginator
# (that file's own docstring: "integration tests in this repo do not import from tests/unit").
# ---------------------------------------------------------------------------
class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _extraction(tier, extracted_at, title, decision_text=None, question_text=None):
    topic = {
        "topic_title": title,
        "category": "progress",
        "summary": f"{title} summary",
        "time_range": "10:00 – 10:05",
        "participants": [],
        "action_items": [],
        "safety_flags": [],
    }
    if decision_text is not None:
        topic["decisions"] = [{"decision": decision_text, "rationale": "because",
                              "decided_by": "Ben"}]
    if question_text is not None:
        topic["questions"] = [{"question": question_text}]
    return {"schema_version": 1, "tier": tier, "extracted_at": extracted_at, "topics": [topic]}


QUESTION_LIVE = "Confirm crane booking timing for the Thursday pour"
QUESTION_FINAL = "Confirm the crane booking timing for the Thursday pour"  # reworded, in floor
DECISION_TEXT = "Use precast panels for level 3"                 # identical both passes


def _measured_ratio(a, b):
    key_a = carry_forward._key(a)
    key_b = carry_forward._key(b)
    return difflib.SequenceMatcher(None, key_a, key_b).ratio()


# The SAME normalisation carry_forward.match itself keys on (Ruling R4) -- measuring
# anything else would not prove what the writer's own fuzzy pass actually sees.
_QUESTION_RATIO = _measured_ratio(QUESTION_LIVE, QUESTION_FINAL)
assert _QUESTION_RATIO >= 0.90, (
    f"fixture reword must clear the fuzzy floor to test carry-forward at all "
    f"(measured {_QUESTION_RATIO:.4f})")


def test_question_answered_survives_a_live_then_final_pass(monkeypatch, migrated_db_url):
    """The defect this task exists to fix: a person answers a question while a session is
    still live, the final pass rewords the same question within the fuzzy floor, and the
    answer must land on the NEW row -- by stable_id, carried across the supersession Task 3
    proved happens underneath (test_supersede_two_passes.py) and matched by Task 4's
    carry_forward (already proven generic over action_items/findings; this is the SAME
    `_carry_forward_one_table` call, now also driving topic_questions).

    Measured ratio (SequenceMatcher on carry_forward's own normalised key, QUESTION_LIVE ->
    QUESTION_FINAL) is asserted at module scope above as `_QUESTION_RATIO`, so the module
    fails to even collect if a future edit to either fixture string drops it below the 0.90
    floor this test exists to exercise.
    """
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = extraction_key = None
    try:
        company_name = f"Writer5-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site = sites.create_site(seed, co["id"], f"Writer5-Site-{tag}")
        folder = f"Writer5-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")
        caller_user = users.upsert_user(
            seed, f"sub-q5-{tag}", f"admin-q5-{tag}@example.com",
            company_id=co["id"], global_role="admin")
        caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}

        date = DATE
        session_base = f"sid{tag}"
        extraction_key = f"extractions/{folder}/{date}/{session_base}.json"
        fake_s3 = _FakeS3({
            extraction_key: json.dumps(
                _extraction("live", "2026-09-30T10:00:00Z", "Crane briefing -- live pass",
                           question_text=QUESTION_LIVE)),
        })

        monkeypatch.setattr(lambda_item_writer.lambda_ingest, "COMPANY_NAME", company_name)
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        result_live = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_live == {"skipped": False, "topics": 1}, result_live

        live_question = seed.execute(
            "SELECT q.id, q.stable_id, q.status FROM topic_questions q "
            "JOIN topics t ON t.id = q.topic_id "
            "WHERE t.source_s3_key=%s AND q.question=%s",
            (extraction_key, QUESTION_LIVE)).fetchone()
        assert live_question is not None, "the LIVE pass must have written a topic_questions row"
        live_question_id, live_stable_id, live_status = live_question
        assert live_status == "open"

        # Answer it, through the REAL org-api handler -- proves the handler's own SQL
        # (get_live_by_stable_id's join + predicate, the UPDATE, the content_edits insert)
        # runs for real, not just that write_extraction_items' insert does.
        answer_result = lambda_org_api.patch_question(
            seed, caller, str(live_stable_id), {"status": "answered"})
        assert answer_result["statusCode"] == 200, answer_result["body"]
        answered_body = json.loads(answer_result["body"])
        assert answered_body["status"] == "answered"
        assert str(answered_body["answered_by"]) == str(caller_user["id"])

        edits = seed.execute(
            "SELECT field, before_text, after_text FROM content_edits "
            "WHERE table_name='topic_questions' AND row_id=%s",
            (live_question_id,)).fetchall()
        assert edits == [("status", "open", "answered")]

        # The FINAL pass: same key, question reworded within the fuzzy floor.
        fake_s3.objects[extraction_key] = json.dumps(
            _extraction("final", "2026-09-30T10:30:00Z", "Crane briefing -- final pass",
                       question_text=QUESTION_FINAL))
        result_final = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_final == {"skipped": False, "topics": 1}, result_final

        new_question = seed.execute(
            "SELECT q.id, q.stable_id, q.carried_from, q.status, q.answered_by "
            "FROM topic_questions q JOIN topics t ON t.id = q.topic_id "
            "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL AND q.question=%s",
            (extraction_key, QUESTION_FINAL)).fetchone()
        assert new_question is not None, "the FINAL pass must have written the reworded row"
        (new_id, new_stable_id, new_carried_from, new_status, new_answered_by) = new_question
        assert new_id != live_question_id, "must be the NEW row, not the old one"
        assert new_stable_id == live_stable_id, (
            "the answered question's stable_id must survive the re-extraction")
        assert new_carried_from == live_question_id
        assert new_status == "answered", "the answer must survive the reword"
        assert str(new_answered_by) == str(caller_user["id"])

        # And the SAME stable_id, addressed again after the re-extraction, resolves through
        # to the NEW row -- proving get_live_by_stable_id's visible_topics_predicate join
        # follows supersession, not just that the carry_forward UPDATE set the column.
        reopen_result = lambda_org_api.patch_question(
            seed, caller, str(live_stable_id), {"status": "open"})
        assert reopen_result["statusCode"] == 200
        reopened = json.loads(reopen_result["body"])
        assert reopened["id"] == str(new_id)
        assert reopened["status"] == "open"
        assert reopened["answered_by"] is None and reopened["answered_at"] is None
    finally:
        co_id = co["id"] if co is not None else None
        site_id = site["id"] if site is not None else None
        user_id = user["id"] if user is not None else None
        if site_id is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if user_id is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user_id,))
        # content_edits.actor_user_id has no ON DELETE CASCADE (unlike topics/action_items/
        # findings under sites) -- the caller user's own audit rows (patch_question's writes
        # above) must go first, or deleting that user below is a FK violation.
        if co_id is not None:
            seed.execute("DELETE FROM content_edits WHERE company_id=%s", (co_id,))
        # The caller user (a separate row, no site membership) and the company itself.
        if co_id is not None:
            seed.execute("DELETE FROM users WHERE company_id=%s", (co_id,))
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
        if co_id is not None:
            remaining = seed.execute(
                "SELECT "
                "(SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM users WHERE company_id=%s), "
                "(SELECT count(*) FROM memberships WHERE site_id=%s), "
                "(SELECT count(*) FROM topics WHERE site_id=%s), "
                "(SELECT count(*) FROM content_edits WHERE company_id=%s)",
                (co_id, site_id, co_id, site_id, site_id, co_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()


def test_decisions_round_trip_stable_id_carried_on_identical_text(monkeypatch, migrated_db_url):
    """A decision round-trips through two passes with UNCHANGED text -- the exact-match pass
    of carry_forward.match (not the fuzzy one the question test above exercises), and proves
    topic_decisions rows are written and carried at all, independent of the PATCH endpoint
    (decisions have none -- Task 5 scope)."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = extraction_key = None
    try:
        company_name = f"Writer5b-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site = sites.create_site(seed, co["id"], f"Writer5b-Site-{tag}")
        folder = f"Writer5b-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        date = DATE
        session_base = f"sid{tag}"
        extraction_key = f"extractions/{folder}/{date}/{session_base}.json"
        fake_s3 = _FakeS3({
            extraction_key: json.dumps(
                _extraction("live", "2026-09-30T10:00:00Z", "Precast decision -- live pass",
                           decision_text=DECISION_TEXT)),
        })

        monkeypatch.setattr(lambda_item_writer.lambda_ingest, "COMPANY_NAME", company_name)
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        result_live = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_live == {"skipped": False, "topics": 1}, result_live

        live_decision = seed.execute(
            "SELECT d.id, d.stable_id FROM topic_decisions d JOIN topics t ON t.id=d.topic_id "
            "WHERE t.source_s3_key=%s AND d.decision=%s",
            (extraction_key, DECISION_TEXT)).fetchone()
        assert live_decision is not None
        live_id, live_stable_id = live_decision

        # The jsonb mirror wrote too (Step 4/Ruling R6: dual-write, jsonb unchanged).
        jsonb = seed.execute(
            "SELECT decisions FROM topics WHERE source_s3_key=%s", (extraction_key,)).fetchone()[0]
        assert jsonb == [{"decision": DECISION_TEXT, "rationale": "because", "decided_by": "Ben"}]

        fake_s3.objects[extraction_key] = json.dumps(
            _extraction("final", "2026-09-30T10:30:00Z", "Precast decision -- final pass",
                       decision_text=DECISION_TEXT))          # UNCHANGED text -- exact match
        result_final = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_final == {"skipped": False, "topics": 1}, result_final

        new_decision = seed.execute(
            "SELECT d.id, d.stable_id, d.carried_from FROM topic_decisions d "
            "JOIN topics t ON t.id=d.topic_id "
            "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL AND d.decision=%s",
            (extraction_key, DECISION_TEXT)).fetchone()
        assert new_decision is not None
        new_id, new_stable_id, new_carried_from = new_decision
        assert new_id != live_id
        assert new_stable_id == live_stable_id, (
            "identical text must carry the stable_id forward via the EXACT match pass")
        assert new_carried_from == live_id
    finally:
        co_id = co["id"] if co is not None else None
        site_id = site["id"] if site is not None else None
        user_id = user["id"] if user is not None else None
        if site_id is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if user_id is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user_id,))
        if co_id is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))
        if co_id is not None:
            remaining = seed.execute(
                "SELECT "
                "(SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM users WHERE id=%s), "
                "(SELECT count(*) FROM memberships WHERE site_id=%s), "
                "(SELECT count(*) FROM topics WHERE site_id=%s)",
                (co_id, site_id, user_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()


# ---------------------------------------------------------------------------
# The org-api handler's SQL, in isolation (the `db` fixture -- rolled back automatically,
# same pattern as test_confirm_suggestion_superseded_topic.py).
# ---------------------------------------------------------------------------

def _seed_question(db, *, superseded=False):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"PatchQ5-Co-{tag}")
    site = sites.create_site(db, co["id"], f"PatchQ5-Site-{tag}")
    caller_user = users.upsert_user(
        db, f"sub-patchq5-{tag}", f"admin-patchq5-{tag}@example.com",
        company_id=co["id"], global_role="admin")
    source = f"extractions/PatchQ5-{tag}/{DATE}/sid{'e' * 32}.json"

    from repositories import topic_questions, topics
    topic = topics.upsert_topic(db, site["id"], DATE, "Site meeting",
                                source_s3_key=source, summary="s")
    rows = topic_questions.insert_questions(
        db, topic["id"], site["id"], [{"question": "Who owns the crane booking?"}])
    if superseded:
        db.execute("UPDATE topics SET superseded_at=now(), superseded_by_run='final:t2' "
                  "WHERE id=%s", (topic["id"],))
    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    return caller, rows[0], co, site


def test_patch_question_handler_updates_and_audits_for_real(db):
    caller, row, co, site = _seed_question(db)

    result = lambda_org_api.patch_question(db, caller, str(row["stable_id"]),
                                           {"status": "dropped"})

    assert result["statusCode"] == 200, result["body"]
    body = json.loads(result["body"])
    assert body["status"] == "dropped"
    assert str(body["answered_by"]) == str(caller["id"])

    updated = db.execute(
        "SELECT status, answered_by FROM topic_questions WHERE id=%s", (row["id"],)).fetchone()
    assert updated[0] == "dropped"
    assert str(updated[1]) == str(caller["id"])

    edits = db.execute(
        "SELECT field, before_text, after_text, actor_user_id FROM content_edits "
        "WHERE table_name='topic_questions' AND row_id=%s", (row["id"],)).fetchall()
    assert len(edits) == 1
    assert edits[0][:3] == ("status", "open", "dropped")
    assert str(edits[0][3]) == str(caller["id"])


def test_patch_question_handler_404s_on_a_superseded_only_stable_id(db):
    """The live predicate at the SQL layer, not just the Python control flow: once the
    topic is superseded, the SAME stable_id must no longer resolve through
    get_live_by_stable_id -- proven here against a real join + WHERE, not a FakeConn double
    that could typo `superseded_at` and still pass."""
    caller, row, co, site = _seed_question(db, superseded=True)

    result = lambda_org_api.patch_question(db, caller, str(row["stable_id"]),
                                           {"status": "answered"})

    assert result["statusCode"] == 404, result["body"]
    unchanged = db.execute(
        "SELECT status FROM topic_questions WHERE id=%s", (row["id"],)).fetchone()
    assert unchanged[0] == "open", "a 404 must not have touched the row"
