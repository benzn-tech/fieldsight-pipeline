"""Translation between Notion's property envelopes and plain Python.

These exist because Notion's shapes are nested and forgiving in the worst way:
a value in the wrong shape is often accepted and ignored, so a bug here leaves
a table that looks merely stale rather than one that errors. `build_props`
therefore raises on an unknown key instead of dropping it.
"""

import datetime as dt

import pytest

import src.notion_client as nc


def test_parses_a_full_row():
    raw = {
        "id": "page-1",
        "properties": {
            "Device": {"title": [{"plain_text": "FS-07"}]},
            "Dispatched": {"date": {"start": "2026-08-01"}},
            "Due back": {"date": {"start": "2026-08-31"}},
            "Returned": {"checkbox": False},
            "Client": {"rich_text": [{"plain_text": "UC Property"}]},
            "Activated": {"date": None},
            "Notes": {"rich_text": []},
        },
    }
    row = nc.parse_row(raw)
    assert row == {
        "page_id": "page-1",
        "device": "FS-07",
        "dispatched": dt.date(2026, 8, 1),
        "due_back": dt.date(2026, 8, 31),
        "returned": False,
        "client": "UC Property",
        "activated": None,
        "notes": None,
    }


def test_a_row_with_everything_blank_parses_to_nones_not_errors():
    row = nc.parse_row({"id": "p", "properties": {"Device": {"title": []}}})
    assert row["device"] == ""
    assert row["dispatched"] is None
    assert row["returned"] is False
    assert row["client"] is None


def test_a_datetime_valued_date_is_still_a_date():
    raw = {"id": "p", "properties": {
        "Device": {"title": [{"plain_text": "FS-01"}]},
        "Dispatched": {"date": {"start": "2026-08-01T09:30:00.000+12:00"}},
    }}
    assert nc.parse_row(raw)["dispatched"] == dt.date(2026, 8, 1)


def test_rich_text_split_across_runs_is_rejoined():
    raw = {"id": "p", "properties": {
        "Device": {"title": [{"plain_text": "FS-"}, {"plain_text": "07"}]},
    }}
    assert nc.parse_row(raw)["device"] == "FS-07"


def test_a_ticked_checkbox_reads_true():
    raw = {"id": "p", "properties": {
        "Device": {"title": [{"plain_text": "FS-01"}]},
        "Returned": {"checkbox": True},
    }}
    assert nc.parse_row(raw)["returned"] is True


# --- writing ---


def test_builds_notion_shapes_for_a_write():
    props = nc.build_props({
        "last_seen": dt.date(2026, 8, 4),
        "app_version": "1.4.2",
        "actual_site": "UC PK",
        "status": "使用中",
    })
    assert props["Last seen"] == {"date": {"start": "2026-08-04"}}
    assert props["App version"] == {"rich_text": [{"text": {"content": "1.4.2"}}]}
    assert props["Actual site"] == {"rich_text": [{"text": {"content": "UC PK"}}]}
    assert props["Status"] == {"select": {"name": "使用中"}}


def test_a_none_clears_rather_than_writing_the_string_none():
    props = nc.build_props({"actual_site": None, "last_seen": None, "status": None})
    assert props["Actual site"] == {"rich_text": []}
    assert props["Last seen"] == {"date": None}
    assert props["Status"] == {"select": None}


def test_an_unknown_key_is_refused_loudly():
    """A typo'd key must not be silently dropped — that is a table that looks
    updated and is not."""
    with pytest.raises(KeyError):
        nc.build_props({"lastseen": dt.date(2026, 8, 4)})


def test_the_hand_edited_columns_have_no_writable_mapping_except_fill_if_empty():
    """Dispatched, Returned and Notes are the human's. There is deliberately no
    way to address them, so no future edit can overwrite them by accident."""
    for key in ("dispatched", "returned", "notes", "device"):
        with pytest.raises(KeyError):
            nc.build_props({key: "anything"})
