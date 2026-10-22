"""Unit: a day whose report doc has prose but no topic left to show.

`_aurora_shape` only calls `render_report_shape` with at least one row, but the
renderer itself drops every topic carrying a `scope='deleted'` redaction. When
that is every topic of the day, `rows` is empty by the time the prose is
alias-normalized and the header fields are filled -- and each of those read
`rows[0]`, so GET /timeline answered 500 (IndexError) instead of the day.

These pin the RENDERER only, which is a pure function and must not crash for any
caller: with no row there is no site to resolve aliases against, so the prose is
left as the doc has it and the header carries no site rather than a guessed one.
Whether that shape is SERVED is the handler's decision, and for /timeline it is
not -- prose over zero topics is the pre-deletion doc, so `_aurora_shape` turns
it into "no Aurora topics" and the deleted-sources guard answers 404. That is
pinned in test_lambda_org_api.py
(test_a_day_whose_every_topic_is_deleted_is_a_404_not_a_crash_or_the_old_prose).
"""
import pytest

org = pytest.importorskip("lambda_org_api")

SITE_ID = "11111111-1111-1111-1111-111111111111"


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
        "category": "safety", "title": "Morning walk", "summary": "Walked the site.",
        "time_range": "08:00 – 08:15", "participants": ["Ada L"],
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


DOC = {
    "executive_summary": ["Pour on level 3 went ahead."],
    "safety_observations": [{"observation": "Edge protection missing", "risk_level": "high"}],
}


def test_every_topic_deleted_still_renders_the_prose(monkeypatch):
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: {"t-1": {"id": "r-1", "scope": "deleted"}})
    looked_up = []
    monkeypatch.setattr(org, "_prose_alias_pairs",
                        lambda conn, site_id: looked_up.append(site_id) or [])

    shape = org.render_report_shape([_row()], DOC, "2026-10-21", "Ada_L", conn=object())

    assert shape["topics"] == []
    assert shape["executive_summary"] == DOC["executive_summary"]
    assert shape["safety_observations"] == DOC["safety_observations"]
    assert looked_up == [], "no row means no site to load aliases for"
    assert shape["site"] is None
    assert shape["site_id"] is None
    assert shape["user_name"] == "Ada L"


def test_every_topic_deleted_without_prose_still_renders(monkeypatch):
    """The header reads `rows[0]` too, with or without prose: the session-report
    preview renders with doc={} and reaches it the same way."""
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: {"t-1": {"id": "r-1", "scope": "deleted"}})

    shape = org.render_report_shape([_row()], {}, "2026-10-21", "Ada_L", conn=object())

    assert shape["topics"] == []
    assert shape["site"] is None and shape["site_id"] is None
    assert shape["user_name"] == "Ada L"
