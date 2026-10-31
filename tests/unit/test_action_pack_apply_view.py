"""Unit: action_pack_view's apply half (Task 7) -- body whitelist, effective fields, the
S10 transition table, field comparison and the live/snapshot view. Pure: no database."""
import datetime as dt

import pytest

import action_pack_view as v


# ------------------------------------------------------------------ body whitelist

@pytest.mark.parametrize("body", [{}, {"fields": {}}])
def test_empty_body_means_apply_as_proposed(body):
    assert v.validate_apply_body(body, "close_item") == ({}, None)


def test_malformed_body_is_400():
    assert v.validate_apply_body(None, "close_item")[1][1] == 400
    assert v.validate_apply_body({"fields": []}, "close_item")[1][1] == 400


def test_unknown_top_level_key_is_400():
    edits, err = v.validate_apply_body({"fields": {}, "force": True}, "close_item")
    assert edits is None and err == ("unknown fields", 400, {"fields": ["force"]})


@pytest.mark.parametrize("atype,field", [
    ("close_item", "text"), ("close_item", "deadline"), ("update_item", "text"),
    ("update_item", "site_id"), ("create_item", "status"), ("create_item", "site_id")])
def test_field_outside_the_types_whitelist_is_400(atype, field):
    edits, err = v.validate_apply_body({"fields": {field: "x"}}, atype)
    assert edits is None and err == ("field not editable", 400, {"fields": [field]})


def test_close_item_status_edit_must_be_done_or_closed():
    assert v.validate_apply_body({"fields": {"status": "closed"}}, "close_item") == (
        {"status": "closed"}, None)
    assert v.validate_apply_body({"fields": {"status": "open"}}, "close_item")[1][1] == 400


def test_allowed_edits_pass_through():
    body = {"fields": {"deadline": "2026-11-09", "responsible": "Ann Lee", "priority": "high",
                       "status": "blocked"}}
    assert v.validate_apply_body(body, "update_item") == (body["fields"], None)


# ------------------------------------------------------------------ effective fields

def test_close_defaults_to_done_and_edits_override():
    assert v.effective_fields({"type": "close_item", "proposed": None}, {}) == (
        {"status": "done"}, None)
    assert v.effective_fields({"type": "close_item", "proposed": {"status": "done"}},
                              {"status": "closed"}) == ({"status": "closed"}, None)


def test_update_merges_proposal_and_edit():
    action = {"type": "update_item", "proposed": {"deadline": "2026-11-09", "priority": "low"}}
    assert v.effective_fields(action, {"priority": "high"}) == (
        {"deadline": "2026-11-09", "priority": "high"}, None)


def test_proposal_with_a_field_apply_may_not_write_is_refused():
    fields, err = v.effective_fields({"type": "update_item",
                                      "proposed": {"text": "reworded", "priority": "low"}}, {})
    assert fields is None and err[1] == 409 and err[2] == {"code": "unsupported_field",
                                                           "fields": ["text"]}


def test_close_proposal_must_close():
    fields, err = v.effective_fields({"type": "close_item", "proposed": {"status": "open"}}, {})
    assert fields is None and err[1] == 409


def test_update_with_nothing_to_set_is_refused():
    fields, err = v.effective_fields({"type": "update_item", "proposed": {}}, {})
    assert fields is None and err[1] == 409


def test_create_drops_site_id_and_needs_text():
    action = {"type": "create_item",
              "proposed": {"text": "Book the crane", "site_id": "s1", "priority": "high"}}
    assert v.effective_fields(action, {"responsible": "Ann Lee"}) == (
        {"text": "Book the crane", "priority": "high", "responsible": "Ann Lee"}, None)
    fields, err = v.effective_fields({"type": "create_item", "proposed": {"text": "  "}}, {})
    assert fields is None and err[1] == 400


# ------------------------------------------------------------------ S10 transitions

@pytest.mark.parametrize("frm,to,ok", [
    ("open", "in_progress", True), ("open", "done", True), ("open", "closed", True),
    ("open", "blocked", True), ("in_progress", "open", True), ("in_progress", "done", True),
    ("blocked", "in_progress", True), ("blocked", "open", True),
    ("done", "open", True), ("closed", "open", True),
    ("done", "closed", False), ("closed", "done", False), ("done", "in_progress", False),
    ("closed", "blocked", False), ("in_progress", "in_progress", False)])
def test_transition_table(frm, to, ok):
    assert v.transition_allowed(frm, to) is ok


# ------------------------------------------------------------------ comparison / view

def test_same_value_normalises_dates_and_blank():
    assert v.same_value(dt.date(2026, 11, 9), "2026-11-09")
    assert v.same_value(None, None)
    assert not v.same_value(None, "2026-11-09")
    assert not v.same_value("open", "done")


def test_differs_from_snapshot_fails_closed_on_a_missing_key():
    live = {"status": "open", "deadline": None}
    assert v.differs_from_snapshot({"status": "done"}, {"status": "open"}, live) is False
    assert v.differs_from_snapshot({"status": "done"}, {"status": "blocked"}, live) is True
    assert v.differs_from_snapshot({"deadline": "2026-11-09"}, {"status": "open"}, live) is True
    assert v.differs_from_snapshot({"status": "done"}, None, live) is True


def test_item_view_shape():
    row = {"id": "x", "status": "open", "deadline": dt.date(2026, 11, 9), "responsible": "A",
           "priority": None, "text": "t", "updated_at": None, "company_id": "c"}
    assert v.item_view(row) == {"status": "open", "deadline": "2026-11-09", "responsible": "A",
                                "priority": None, "text": "t", "updated_at": None}
    assert v.item_view(None) is None
