"""Integration: Task 7 (spec 2026-09-30-extractor-declares-item-continuity D6) -- every
continuity claim becomes a decision_records row, against a real Postgres.

Uses the `db` fixture (rolled back after the test, no manual cleanup) rather than a committed
connection: `record_claims` is driven directly, twice, on the SAME open transaction -- Postgres
reads uncommitted rows written earlier in that same transaction, so the re-delivery dedupe and
the `list_for_eval` visibility check both see the first call's writes without anything needing
to be committed.

Seeds a retired row and its replacement with `item_id`s via the real inserts
(topics.upsert_topic's action_items= kwarg), the same shape lambda_item_writer/carry_forward_
apply produce -- not a hand-built decision_records row -- so the proof covers the real
item_id -> stable_id resolution path, not just the SQL that happens to insert."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from repositories import companies, decision_records, sites, topics

pytestmark = pytest.mark.integration

continuity_records = pytest.importorskip(
    "continuity_records", reason="requires psycopg (installed in CI)")


def _seed_company_site(db, tag):
    co = companies.create_company(db, f"T7-Co-{tag}")
    site = sites.create_site(db, co["id"], f"T7-Site-{tag}")
    return co, site


def _seed_retired_and_replacement(db, site, tag):
    """A retired action item carrying `prior_id`, and its replacement topic with TWO fresh
    action items: one the claim will accept (`accepted_new_id`) and one it will reject
    (`rejected_new_id`, standing in for a claim the guards did not pass) -- same shared
    source_s3_key prefix a real re-extraction of the same recording would use."""
    prior_id = str(uuid.uuid4())
    accepted_new_id = str(uuid.uuid4())
    rejected_new_id = str(uuid.uuid4())
    key = f"extractions/T7-{tag}/2026-09-30/sid{uuid.uuid4().hex[:24]}.json"

    old_topic = topics.upsert_topic(
        db, site["id"], "2026-09-29", "Old pass", source_s3_key=key,
        action_items=[{"text": "chase concrete supplier", "item_id": prior_id}])
    db.execute("UPDATE topics SET superseded_at=now(), superseded_by_run='final:t2' "
              "WHERE id=%s", (old_topic["id"],))
    new_topic = topics.upsert_topic(
        db, site["id"], "2026-09-30", "New pass", source_s3_key=key,
        action_items=[
            {"text": "chase concrete supplier again", "item_id": accepted_new_id},
            {"text": "chase steel supplier", "item_id": rejected_new_id},
        ])
    return old_topic, new_topic, prior_id, accepted_new_id, rejected_new_id


def _make_extraction(prior_id, accepted_new_id, rejected_new_id):
    return {
        "extracted_at": "2026-09-30T10:05:00Z",
        "llm_provider": "anthropic", "llm_model": "claude-sonnet-4-6",
        "continuity": {
            "question_set": "item_continuity:abc123",
            "claims": [
                {"alias": "A1", "prior_item_id": prior_id, "new_item_id": accepted_new_id,
                 "outcome": "accepted", "guard": None, "list_name": "action_items"},
                {"alias": "A2", "prior_item_id": None, "new_item_id": rejected_new_id,
                 "outcome": "rejected", "guard": "existence", "list_name": "action_items"},
            ],
        },
    }


def test_record_claims_dedupes_on_redelivery_and_is_visible_through_list_for_eval(db):
    tag = uuid.uuid4().hex[:8]
    co, site = _seed_company_site(db, tag)
    old_topic, new_topic, prior_id, accepted_new_id, rejected_new_id = (
        _seed_retired_and_replacement(db, site, tag))
    extraction_key = f"extractions/T7-{tag}/2026-09-30/sid-final.json"
    extraction = _make_extraction(prior_id, accepted_new_id, rejected_new_id)

    stats_first = continuity_records.record_claims(
        db, extraction, extraction_key, co["id"], site["id"],
        [old_topic["id"]], [new_topic["id"]])
    assert stats_first == {"inserted": 2, "skipped_duplicate": 0, "unresolved": 0}

    # Re-delivery: the same writer invocation (or a retry) processes the SAME extraction
    # again -- no duplicate rows.
    stats_second = continuity_records.record_claims(
        db, extraction, extraction_key, co["id"], site["id"],
        [old_topic["id"]], [new_topic["id"]])
    assert stats_second == {"inserted": 0, "skipped_duplicate": 2, "unresolved": 0}

    rows = db.execute(
        "SELECT object_ref, auto_outcome, subject_type, subject_stable_id, output "
        "FROM decision_records WHERE kind='item_continuity' AND site_id=%s ORDER BY object_ref",
        (site["id"],),
    ).fetchall()
    assert len(rows) == 2, "still exactly two rows after the re-delivery, not four"

    by_ref = {r[0]: r for r in rows}
    accepted_row = by_ref["A1"]
    assert accepted_row[1] == "accepted"
    assert accepted_row[2] == "action_item"
    assert accepted_row[4] == {"prior_item_id": prior_id, "new_item_id": accepted_new_id,
                               "outcome": "accepted", "guard": None}
    # subject_stable_id is the RETIRED row's own stable_id.
    old_stable = db.execute(
        "SELECT stable_id FROM action_items WHERE item_id=%s", (prior_id,)).fetchone()[0]
    assert accepted_row[3] == old_stable

    rejected_row = by_ref["A2"]
    assert rejected_row[1] == "rejected"
    assert rejected_row[2] == "action_item"
    assert rejected_row[4]["guard"] == "existence"
    # subject_stable_id is the NEW row's own stable_id (no prior row to point at).
    new_stable = db.execute(
        "SELECT stable_id FROM action_items WHERE item_id=%s", (rejected_new_id,)).fetchone()[0]
    assert rejected_row[3] == new_stable

    # The visibility predicate resolves both rows' subject_type/subject_stable_id through the
    # action_items table -- proves the writer-side kind mapping and the eval-side predicate
    # agree, not just that each looks plausible alone.
    since = datetime.now(timezone.utc) - timedelta(days=1)
    visible = decision_records.list_for_eval(db, co["id"], "item_continuity", since)
    assert {r["object_ref"] for r in visible} == {"A1", "A2"}
    assert {r["auto_outcome"] for r in visible} == {"accepted", "rejected"}
