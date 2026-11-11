"""Integration: `task_no` in org-api payloads (action packs Task 1, spec §14 D1) -- the Tasks
page's source (render_report_shape, via the timeline shim) and the PATCH
/action-items/{id} response -- against a real Postgres, including the deploy window where
migration 0093 is not applied yet. The `db` fixture rolls back after each test.
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import uuid

import pytest

from repositories import action_items, companies, sites, topics, users

pytestmark = pytest.mark.integration

lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-11-01"


def _day(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"TNO-Co-{tag}")
    site = sites.create_site(db, co["id"], f"TNO-Site-{tag}")
    folder = f"TNO-{tag}"
    prefix = f"extractions/{folder}/{DATE}/"
    t = topics.upsert_topic(db, site["id"], DATE, "Deliveries",
                            source_s3_key=prefix + f"sid{tag}.json",
                            action_items=[{"text": "Order timber"}, {"text": "Book crane"}])
    items = {r[1]: (r[0], r[2]) for r in db.execute(
        "SELECT id, text, stable_id FROM action_items WHERE topic_id=%s", (t["id"],))}
    admin = users.upsert_user(db, f"sub-tno-{tag}", f"tno-{tag}@example.com",
                              company_id=co["id"], global_role="admin")
    caller = {"id": admin["id"], "company_id": co["id"], "global_role": "admin",
              "cognito_sub": f"sub-tno-{tag}"}
    return co, site, folder, prefix, items, caller


def _shaped_items(db, folder, prefix, company_id):
    rows = topics.list_topics_for_source_prefix(db, prefix)
    shaped = lambda_org_api.render_report_shape(rows, {}, DATE, folder, db,
                                                company_id=company_id)
    return {a["action"]: a for t in shaped["topics"] for a in t["action_items"]}


def test_task_list_payload_carries_task_no(db):
    co, site, folder, prefix, items, _caller = _day(db)
    action_items.assign_task_no(db, items["Order timber"][1], site["id"])
    out = _shaped_items(db, folder, prefix, co["id"])
    assert out["Order timber"]["task_no"] == 1
    assert out["Book crane"]["task_no"] is None             # not numbered yet


def test_patch_response_carries_task_no(db):
    _co, site, _folder, _prefix, items, caller = _day(db)
    aid, stable = items["Order timber"]
    action_items.assign_task_no(db, stable, site["id"])
    resp = lambda_org_api.patch_action_item(db, caller, str(aid), {"priority": "high"})
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    assert body["task_no"] == 1 and body["priority"] == "high"


def test_payloads_unchanged_before_migration_0093(db):
    co, _site, folder, prefix, items, caller = _day(db)
    db.execute("DROP TABLE action_task_numbers")          # rolled back with the fixture
    out = _shaped_items(db, folder, prefix, co["id"])
    assert {a["task_no"] for a in out.values()} == {None}
    assert out["Order timber"]["status"] == "open"
    resp = lambda_org_api.patch_action_item(
        db, caller, str(items["Book crane"][0]), {"priority": "low"})
    assert resp["statusCode"] == 200, resp["body"]
    assert json.loads(resp["body"])["task_no"] is None


def _live_items(db, caller):
    resp = lambda_org_api.list_live_items(
        db, caller, {"queryStringParameters": {"date": DATE}})
    assert resp["statusCode"] == 200, resp["body"]
    return {a["text"]: a for t in json.loads(resp["body"])["topics"]
            for a in t["action_items"]}


def test_live_items_rows_carry_task_no(db):
    _co, site, _folder, _prefix, items, caller = _day(db)
    action_items.assign_task_no(db, items["Order timber"][1], site["id"])
    out = _live_items(db, caller)
    assert out["Order timber"]["task_no"] == 1
    assert out["Book crane"]["task_no"] is None


def test_live_items_unchanged_before_migration_0093(db):
    _co, _site, _folder, _prefix, _items, caller = _day(db)
    db.execute("DROP TABLE action_task_numbers")          # rolled back with the fixture
    out = _live_items(db, caller)
    assert set(out) == {"Order timber", "Book crane"}
    assert {a["task_no"] for a in out.values()} == {None}
