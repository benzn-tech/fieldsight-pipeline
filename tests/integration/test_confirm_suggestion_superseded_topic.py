"""Integration: confirming a suggestion whose topic was superseded, not deleted.

Fix round 1 (Track B Task 3 review): `confirm_suggestion` only checked `topic_id IS NULL`,
which is what an actual physical delete (topics.delete_topics_for_source[_prefix]) still
looks like -- but that path is no longer used by the pipeline. A re-extraction now
SUPERSEDES the topic instead (an UPDATE, `topic_id` stays set), so confirm's old check never
fired for the case Task 3 actually creates: a pending suggestion whose `topic_id` still names
a real row, but one that is now hidden behind `topics.superseded_at`.

This drives `lambda_org_api.confirm_suggestion` directly against a real Postgres. No
programme or task is seeded on purpose: the superseded-topic check must short-circuit BEFORE
`programme_tasks.get_primary_programme` is ever called, so if it did not, this test would get
"programme not found" (409, but the WRONG reason) instead of the stale-source-topic message
-- the assertion on the error text doubles as the "does not touch the programme task" proof.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import pytest

lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")

from repositories import companies, programme_suggestions, sites, topics, users  # noqa: E402

pytestmark = pytest.mark.integration

DATE = "2026-09-30"


def _seed(db, *, superseded):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"ConfirmStale-Co-{tag}")
    site = sites.create_site(db, co["id"], f"ConfirmStale-Site-{tag}")
    caller_user = users.upsert_user(
        db, f"sub-confirm-{tag}", f"admin-{tag}@example.com",
        company_id=co["id"], global_role="admin")

    source = f"extractions/ConfirmStale-{tag}/{DATE}/sid{'d' * 32}.json"
    topic = topics.upsert_topic(
        db, site["id"], DATE, "Slab pour", source_s3_key=source, summary="s")
    if superseded:
        db.execute(
            "UPDATE topics SET superseded_at=now(), superseded_by_run='final:t2' "
            "WHERE id=%s", (topic["id"],))

    suggestion = db.execute(
        "INSERT INTO programme_progress_suggestions "
        "(site_id, task_id, topic_id, topic_title, topic_user_id, report_date, "
        " source_s3_key, task_name, task_status_before, task_progress_before, "
        " suggested_status, suggested_progress, confidence, dedupe_key) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (site["id"], "A1020", topic["id"], "Slab pour", None, DATE, source,
         "Pour slab", "in_progress", 40, "completed", 100, 0.9,
         f"dedupe-{tag}"),
    ).fetchone()

    caller = {"id": caller_user["id"], "company_id": co["id"], "global_role": "admin"}
    return caller, suggestion[0]


def test_confirm_marks_stale_and_never_reaches_the_programme_task_when_superseded(db):
    caller, suggestion_id = _seed(db, superseded=True)

    result = lambda_org_api.confirm_suggestion(db, caller, suggestion_id, {})

    assert result["statusCode"] == 409
    assert "superseded" in result["body"].lower(), result["body"]

    row = programme_suggestions.get(db, suggestion_id)
    assert row["state"] == "stale", "the suggestion must be marked stale, not left pending"


def test_confirm_still_proceeds_past_the_check_when_the_topic_is_live(db):
    """The control: same seed, minus the supersede -- confirm must reach PAST the
    superseded-topic check (and fail for the ordinary "no programme seeded" reason instead),
    proving the new check is not simply refusing every confirm."""
    caller, suggestion_id = _seed(db, superseded=False)

    result = lambda_org_api.confirm_suggestion(db, caller, suggestion_id, {})

    assert result["statusCode"] == 409
    assert "superseded" not in result["body"].lower(), (
        "a LIVE topic must not be treated as superseded: " + result["body"])
    assert "programme" in result["body"].lower(), result["body"]
