"""Unit: the timeline payload carries `topics.source` (action packs spec §14 D2).

`render_report_shape` is a fixed allowlist: a column the day read selects but this
serializer does not name never reaches the day view or the Tasks page (which flattens
`topics[].action_items[]` off the same payload). An agent-created topic
(`source='agent'`, `source_s3_key='agent://action_pack/<id>'`) must be told apart from
an extracted one there, and must never read as report-sourced ("Whole day" / nightly).
"""
import lambda_org_api as org

SITE_ID = "11111111-1111-1111-1111-111111111111"


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
        "category": "progress", "title": "Level 3",
        "summary": "Walked level three.", "time_range": "08:00 - 08:15",
        "participants": [], "safety_observations": [], "findings": [], "photos": [],
        "source": "ai", "source_s3_key": "extractions/Ada_L/2026-11-01/sidabc.json",
        "action_items": [{"id": "a-1", "text": "Order timber", "responsible": None,
                          "deadline": None, "deadline_text": None, "priority": None,
                          "status": "open"}],
    }
    base.update(over)
    return base


def test_topic_and_its_action_items_carry_source():
    out = org.render_report_shape([_row()], {}, "2026-11-01", "Ada_L")
    t = out["topics"][0]
    assert t["source"] == "ai"
    assert t["action_items"][0]["source"] == "ai"


def test_agent_topic_is_agent_and_not_report_sourced():
    out = org.render_report_shape(
        [_row(source="agent", source_s3_key="agent://action_pack/p-1")],
        {}, "2026-11-01", "Ada_L")
    t = out["topics"][0]
    assert t["source"] == "agent"
    assert t["action_items"][0]["source"] == "agent"
    # "report" is the nightly / "Whole day" kind; an agent topic must never claim it.
    assert t["session_kind"] != "report"
    assert t["session_id"] is None
    # The raw key stays out of this payload.
    assert "source_s3_key" not in t
