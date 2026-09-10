"""Unit: the shape the dashboard actually receives carries the reader's sections.

`render_report_shape` returns a fixed allowlist, and most days go through it:
the verbatim S3 pass-through is reached only when a day has NO Aurora topics at
all, and under the authority flip most days have them. So a frontend written
against `sections` would render them on meeting and lake-only days and get
`undefined` on every extraction day — the majority — which is a bug that looks
like "the new report works sometimes".

It builds them with the same `report_sections.build` the lake reports use.
Two mappings for one report is two definitions free to drift, and this codebase
has paid for that shape more than once.
"""
import pytest

import report_sections
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


def _sections(shape):
    return {s["title"]: s for s in shape.get("sections") or []}


def test_the_shape_carries_sections():
    shape = org.render_report_shape([_row()], None, "2026-07-14", "Ada_L")
    assert "sections" in shape, "the dashboard's own path must serve the reader's shape"
    assert isinstance(shape["sections"], list)


def test_every_section_uses_the_library_vocabulary():
    shape = org.render_report_shape([_row()], None, "2026-07-14", "Ada_L")
    for s in shape["sections"]:
        assert s["kind"] in report_sections.KINDS, s


def test_actions_reach_the_reader_through_this_path_too():
    rows = [_row(action_items=[
        # The DB row shape org-api serializes from, not the report shape.
        {"id": "a-1", "text": "Chase the steel delivery", "responsible": "Ada",
         "deadline_text": "2026-07-16", "deadline": None, "priority": "medium",
         "status": "open", "mention_count": 2},
    ])]
    shape = org.render_report_shape(rows, None, "2026-07-14", "Ada_L")
    actions = _sections(shape).get("Actions")
    assert actions, "an action item on a topic must become an Actions row"
    assert actions["rows"][0]["action"] == "Chase the steel delivery"
    assert actions["rows"][0]["due"] == "2026-07-16"


def test_no_on_site_panel_of_zeros_on_this_path():
    """There is no `recording_session` here. Rendering 0 recordings for a day
    that plainly had some is the same misleading zero the KPI comment above
    `render_report_shape` exists to remove."""
    shape = org.render_report_shape([_row()], None, "2026-07-14", "Ada_L")
    assert "On Site" not in _sections(shape)


def test_topics_are_still_served_beside_the_sections():
    """Sections are additional. Anything already reading `topics` keeps working
    while the UI moves across."""
    shape = org.render_report_shape([_row()], None, "2026-07-14", "Ada_L")
    assert shape["topics"] and shape["topics"][0]["topic_title"] == "Morning walk"


def test_the_collapse_count_is_what_ranks_it_not_a_recount():
    """org-api's 0-day collapse merges one commitment said in three recordings
    into one row and counts it. That count spans recordings; counting the text
    again here would only span topics inside one report, and would report 1 for
    something said three times."""
    rows = [_row(action_items=[
        {"id": "a-1", "text": "Order the brackets", "responsible": "Ada",
         "deadline_text": None, "deadline": None, "priority": "low",
         "status": "open", "mention_count": 3},
        {"id": "a-2", "text": "Sweep level two", "responsible": "Ada",
         "deadline_text": None, "deadline": None, "priority": "high",
         "status": "open", "mention_count": 1},
    ])]
    rows_out = _sections(org.render_report_shape(
        rows, None, "2026-07-14", "Ada_L"))["Actions"]["rows"]
    assert rows_out[0]["action"] == "Order the brackets"
    assert rows_out[0]["mentions"] == 3
