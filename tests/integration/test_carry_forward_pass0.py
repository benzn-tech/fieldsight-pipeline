"""Integration: Track B Task 6 -- carry-forward pass 0 pairs on the extractor's own `item_id`
declaration (Task 5), against a real Postgres.

Same two-real-passes harness as tests/integration/test_supersede_two_passes.py's
`test_carry_forward_survives_a_live_then_final_pass` (a committed connection, not the
rolled-back `db` fixture -- the final pass has to see the live pass's row and the tick made
between them), pared down to the one case pass 0 exists for: text so far reworded it falls
BELOW the 0.90 fuzzy floor, so only a shared item_id -- not text -- can carry the tick
forward.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import io
import uuid

import pytest

from db.connection import get_connection
from repositories import action_items, companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-09-29"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


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


def _extraction(tier, extracted_at, action_text, item_id):
    return {
        "schema_version": 1,
        "tier": tier,
        "extracted_at": extracted_at,
        "topics": [{
            "topic_title": "Site access -- " + tier,
            "category": "progress",
            "summary": "summary",
            "time_range": "10:00 – 10:05",
            "participants": [],
            "action_items": [{"action": action_text, "item_id": item_id}],
            "safety_flags": [],
        }],
    }


def test_ticked_item_with_far_reworded_text_still_carries_by_item_id(
        monkeypatch, migrated_db_url):
    """The case only pass 0 can solve: a person ticks an action item while the meeting is
    still live, the final pass rewords the same commitment so heavily the text-based fuzzy
    pass would refuse it (well under the 0.90 floor) -- but the extractor stamped both
    copies with the SAME item_id, and that declaration alone is enough to carry the
    stable_id, carried_from and the tick across the reword."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = extraction_key = None
    item_id = str(uuid.uuid4())
    ACTION_LIVE = "Platform initial login using temporary password"
    ACTION_FINAL = "Login via temp credential on the site office platform"
    try:
        company_name = f"Pass0-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site = sites.create_site(seed, co["id"], f"Pass0-Site-{tag}")
        folder = f"Pass0-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        date = DATE
        session_base = f"sid{tag}"
        extraction_key = f"extractions/{folder}/{date}/{session_base}.json"
        fake_s3 = _FakeS3({
            extraction_key: json.dumps(
                _extraction("live", "2026-09-29T10:00:00Z", ACTION_LIVE, item_id)),
        })

        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        result_live = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_live == {"skipped": False, "topics": 1}, result_live

        old_action = seed.execute(
            "SELECT a.id, a.stable_id FROM action_items a JOIN topics t ON t.id=a.topic_id "
            "WHERE t.source_s3_key=%s AND a.text=%s", (extraction_key, ACTION_LIVE)).fetchone()
        old_action_id, old_action_stable_id = old_action

        # Sanity: the two texts really are below the fuzzy floor, so this test actually
        # exercises pass 0 rather than the pre-existing text passes matching anyway.
        import carry_forward as cf
        ratio_ok = cf.match(
            [{"id": "o", "text": ACTION_LIVE, "human_touched": False}],
            [{"id": "n", "text": ACTION_FINAL, "human_touched": False}])
        assert ratio_ok[0] == [], (
            "the reworded text must NOT clear the fuzzy floor -- otherwise this proves "
            "nothing pass 0 specific")

        # The PATCH-equivalent: a person ticks the item off while the meeting is still live.
        updated_by = str(user["id"])
        action_items.update_action_item_fields(
            seed, old_action_id, {"status": "done"}, updated_by)

        # The FINAL pass: same key, same item_id, text reworded far past the fuzzy floor.
        fake_s3.objects[extraction_key] = json.dumps(
            _extraction("final", "2026-09-29T10:30:00Z", ACTION_FINAL, item_id))
        result_final = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_final == {"skipped": False, "topics": 1}, result_final

        new_action = seed.execute(
            "SELECT a.id, a.stable_id, a.carried_from, a.status, a.updated_by "
            "FROM action_items a JOIN topics t ON t.id=a.topic_id "
            "WHERE t.source_s3_key=%s AND t.superseded_at IS NULL AND a.text=%s",
            (extraction_key, ACTION_FINAL)).fetchone()
        assert new_action is not None
        new_action_id, new_stable_id, new_carried_from, new_status, new_updated_by = new_action
        assert new_action_id != old_action_id, "must be the NEW row, not the old one"
        assert new_stable_id == old_action_stable_id, (
            "pass 0 must carry the OLD row's stable_id forward by item_id alone")
        assert new_carried_from == old_action_id
        assert new_status == "done", "the tick must survive the reword via item_id"
        assert new_updated_by == updated_by
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
