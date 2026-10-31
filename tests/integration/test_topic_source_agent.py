"""Integration: agent-created topics are labelled "agent", never nightly (action packs
spec §14 D2, §3.4), against a real Postgres.

Task 7's apply creates one synthetic topic per (pack, site): `source='agent'`,
`source_s3_key='agent://action_pack/<pack_id>'`. `upsert_topic` cannot set `source`
(it is passive provenance, not a kwarg), so the row is inserted directly here, the way
apply will. The day view / Tasks payload (render_report_shape) and /live-items must carry
`source`, and the agent row must not read as nightly: `is_live` False (unchanged rule:
only `extractions/` keys are live) with `source='agent'`, and `session_kind` never
`report`. A regular extraction topic on the same day is unchanged apart from the
additive `source` field. The `db` fixture rolls back after each test.
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
    co = companies.create_company(db, f"SRC-Co-{tag}")
    site = sites.create_site(db, co["id"], f"SRC-Site-{tag}")
    admin = users.upsert_user(db, f"sub-src-{tag}", f"src-{tag}@example.com",
                              company_id=co["id"], global_role="admin")
    caller = {"id": admin["id"], "company_id": co["id"], "global_role": "admin",
              "cognito_sub": f"sub-src-{tag}"}
    folder = f"SRC-{tag}"
    prefix = f"extractions/{folder}/{DATE}/"
    regular = topics.upsert_topic(db, site["id"], DATE, "Deliveries", user_id=admin["id"],
                                  source_s3_key=prefix + f"sid{tag}.json",
                                  action_items=[{"text": "Order timber"}])
    agent_key = f"agent://action_pack/{uuid.uuid4()}"
    agent_id = db.execute(
        "INSERT INTO topics (site_id, user_id, source_s3_key, report_date, title, source) "
        "VALUES (%s, %s, %s, %s, %s, 'agent') RETURNING id",
        (site["id"], admin["id"], agent_key, DATE, "Pack: chase deliveries")).fetchone()[0]
    agent_item = db.execute(
        "INSERT INTO action_items (topic_id, site_id, text, status) "
        "VALUES (%s, %s, %s, 'open') RETURNING id",
        (agent_id, site["id"], "Chase steel supplier")).fetchone()[0]
    return {"co": co, "site": site, "caller": caller, "folder": folder, "prefix": prefix,
            "regular": regular, "agent_id": agent_id, "agent_key": agent_key,
            "agent_item": agent_item}


def test_live_items_carries_source_and_agent_topic_is_not_nightly(db):
    d = _day(db)
    resp = lambda_org_api.list_live_items(
        db, d["caller"], {"queryStringParameters": {"date": DATE}})
    assert resp["statusCode"] == 200, resp["body"]
    by_title = {t["title"]: t for t in json.loads(resp["body"])["topics"]}
    agent = by_title["Pack: chase deliveries"]
    assert agent["source"] == "agent"
    assert agent["is_live"] is False
    assert [a["text"] for a in agent["action_items"]] == ["Chase steel supplier"]
    regular = by_title["Deliveries"]
    assert regular["source"] == "ai"                       # the column default
    assert regular["is_live"] is True                      # unchanged


def test_timeline_and_tasks_rows_carry_source(db):
    d = _day(db)
    # The day view reads by the recorder's S3 prefix; an agent:// key matches none, so
    # it is unioned in by key here (the same hook a merged meeting uses).
    rows = topics.list_topics_for_source_prefix(db, d["prefix"], merged_keys=[d["agent_key"]])
    shaped = lambda_org_api.render_report_shape(rows, {}, DATE, d["folder"], db,
                                                company_id=d["co"]["id"])
    by_title = {t["topic_title"]: t for t in shaped["topics"]}
    agent = by_title["Pack: chase deliveries"]
    assert agent["source"] == "agent"
    assert agent["session_kind"] != "report"               # never the nightly "Whole day"
    assert agent["session_id"] is None
    assert [(a["action"], a["source"]) for a in agent["action_items"]] == [
        ("Chase steel supplier", "agent")]
    regular = by_title["Deliveries"]
    assert regular["source"] == "ai"
    assert regular["session_kind"] == "extraction"
    assert [(a["action"], a["source"]) for a in regular["action_items"]] == [
        ("Order timber", "ai")]


def test_get_action_item_carries_topic_source(db):
    d = _day(db)
    row = action_items.get_action_item(db, d["agent_item"])
    assert row["topic_source"] == "agent"
    reg_item = db.execute("SELECT id FROM action_items WHERE topic_id=%s",
                          (d["regular"]["id"],)).fetchone()[0]
    assert action_items.get_action_item(db, reg_item)["topic_source"] == "ai"


def test_patch_response_carries_source(db):
    d = _day(db)
    resp = lambda_org_api.patch_action_item(db, d["caller"], str(d["agent_item"]),
                                            {"priority": "high"})
    assert resp["statusCode"] == 200, resp["body"]
    body = json.loads(resp["body"])
    assert body["source"] == "agent" and body["priority"] == "high"


def test_agent_topic_untouched_by_supersede_of_the_day(db):
    """§3.4 check (a): the re-extraction and nightly supersede paths key on source_s3_key
    (exact key, or an escaped `extractions/{folder}/{date}/` prefix) and never reach an
    agent:// topic of the same site, day and author."""
    d = _day(db)
    topics.supersede_topics_for_source(db, d["regular"]["source_s3_key"], "run-1")
    topics.supersede_topics_for_source_prefix(db, d["prefix"], "run-2")
    live = db.execute("SELECT superseded_at FROM topics WHERE id=%s",
                      (d["agent_id"],)).fetchone()[0]
    assert live is None
    assert db.execute("SELECT superseded_at IS NOT NULL FROM topics WHERE id=%s",
                      (d["regular"]["id"],)).fetchone()[0] is True
