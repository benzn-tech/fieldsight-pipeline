"""Pure logic of the decision-card inbox (spec 2026-11-12-decision-card-inbox-design rev 2 §4, §5)."""
from datetime import datetime, timedelta, timezone

import decision_inbox
from decision_inbox import (DECISION_OPTIONS, card_view, digest_title, is_excluded,
                            order_and_cap, source_label, validate_answer)

UTC = timezone.utc
T0 = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)


# --- DECISION_OPTIONS ---------------------------------------------------------

def test_decision_options_table_exact():
    assert DECISION_OPTIONS == {("todo.overdue", "done"): "decided",
                                ("todo.overdue", "not_needed"): "declined",
                                ("checklist.gap", "accept"): "decided"}
    for non_decision in (("todo.overdue", "remind"), ("todo.overdue", "new_date"),
                         ("checklist.gap", "remind"), ("checklist.gap", "reassign")):
        assert non_decision not in DECISION_OPTIONS


# --- source_label ---------------------------------------------------------------

def test_source_label_overdue_is_deadline_plus_one_day():
    assert source_label("todo.overdue", {"deadline": "2026-11-10"}) == "Overdue since 11 Nov"
    assert source_label("todo.overdue", {"deadline": "2026-11-30T09:00:00+13:00"}) \
        == "Overdue since 1 Dec"
    assert source_label("todo.overdue", '{"deadline": "2026-12-31"}') == "Overdue since 1 Jan"


def test_source_label_overdue_malformed_or_absent():
    for payload in (None, {}, {"deadline": None}, {"deadline": ""}, {"deadline": "soon"},
                    {"deadline": "2026-13-40"}, "not json", [], 7):
        assert source_label("todo.overdue", payload) == "Overdue", payload


def test_source_label_gap_and_other():
    assert source_label("checklist.gap", {"deadline": "2026-11-10"}) == "Missing from the inspection"
    assert source_label("checklist.gap", None) == "Missing from the inspection"
    assert source_label("something.else", None) == "From the site"
    assert source_label("todo.overdue_backlog", {"deadline": "2026-11-10"}) == "From the site"


# --- digest_title ---------------------------------------------------------------

def test_digest_title_one():
    assert digest_title([("A", 1)]) == "1 overdue to-do more than 3 days old"


def test_digest_title_single_site_many():
    assert digest_title([("A", 4)]) == "4 overdue to-dos more than 3 days old"


def test_digest_title_multi_site_sorted():
    assert digest_title([("B", 2), ("A", 3)]) == "5 overdue to-dos more than 3 days old on A, B"


def test_digest_title_more_than_three_sites():
    got = digest_title([("E", 1), ("C", 1), ("A", 1), ("D", 1), ("B", 1)])
    assert got == "5 overdue to-dos more than 3 days old on A, B, C and 2 more sites"


def test_digest_title_zero_sites_left_out():
    assert digest_title([("B", 0), ("A", 2)]) == "2 overdue to-dos more than 3 days old"


def test_digest_title_empty_or_all_zero_is_none():
    assert digest_title([]) is None
    assert digest_title([("A", 0), ("B", 0)]) is None


# --- order_and_cap --------------------------------------------------------------

def _row(cid, urgency="normal", deadline=None, created=T0):
    return {"card_id": cid, "urgency": urgency, "deadline_at": deadline, "created_at": created}


def test_order_injury_first_then_deadline_nulls_last_then_created_then_id():
    rows = [
        _row("n-null-late", created=T0 + timedelta(hours=2)),
        _row("n-null-early", created=T0),
        _row("n-d2", deadline=T0 + timedelta(days=2)),
        _row("n-d1", deadline=T0 + timedelta(days=1)),
        _row("i-null", urgency="injury"),
        _row("b-tie", deadline=T0 + timedelta(days=1)),
    ]
    shown, more = order_and_cap(rows, cap=10)
    assert [r["card_id"] for r in shown] == [
        "i-null", "b-tie", "n-d1", "n-d2", "n-null-early", "n-null-late"]
    assert more == 0


def test_cap_three_and_more():
    rows = [_row(f"c{i}", created=T0 + timedelta(minutes=i)) for i in range(5)]
    shown, more = order_and_cap(rows)
    assert [r["card_id"] for r in shown] == ["c0", "c1", "c2"]
    assert more == 2


def test_injury_cards_never_cut():
    rows = ([_row(f"i{i}", urgency="injury", created=T0 + timedelta(minutes=i)) for i in range(4)]
            + [_row("n0"), _row("n1")])
    shown, more = order_and_cap(rows)
    assert [r["card_id"] for r in shown] == ["i0", "i1", "i2", "i3"]
    assert more == 2


def test_order_and_cap_empty():
    assert order_and_cap([]) == ([], 0)


# --- validate_answer --------------------------------------------------------------

_GAP = {"rule_id": "checklist.gap", "options": [
    {"id": "remind", "label": "Remind", "recommended": True},
    {"id": "accept", "label": "Accept with reason", "recommended": False,
     "reason_required": True},
    {"id": "reassign", "label": "Reassign", "recommended": False}]}


def test_validate_unknown_option():
    assert validate_answer(_GAP, "nope", None) is not None
    assert validate_answer(_GAP, None, None) is not None
    assert validate_answer(_GAP, 3, None) is not None
    assert validate_answer({"options": ["junk", {"label": "no id"}]}, "junk", None) is not None


def test_validate_reason_required():
    for reason in (None, "", "   \n "):
        assert validate_answer(_GAP, "accept", reason) is not None, repr(reason)
    assert validate_answer(_GAP, "accept", "  ok  ") is None
    assert validate_answer(_GAP, "remind", None) is None


def test_validate_reason_length_after_strip():
    assert validate_answer(_GAP, "accept", "x" * 500) is None
    assert validate_answer(_GAP, "accept", "  " + "x" * 500 + "  ") is None
    assert validate_answer(_GAP, "accept", "x" * 501) is not None
    assert validate_answer(_GAP, "remind", "x" * 501) is not None      # optional reason too


def test_validate_reason_not_text():
    assert validate_answer(_GAP, "remind", 5) is not None


def test_validate_messages_distinct():
    msgs = {validate_answer(_GAP, "nope", None), validate_answer(_GAP, "accept", None),
            validate_answer(_GAP, "accept", "x" * 501)}
    assert len(msgs) == 3 and None not in msgs


# --- is_excluded --------------------------------------------------------------------

def test_is_excluded():
    assert is_excluded({"rule_id": "hub.fault", "kind": "fault"})
    assert is_excluded({"rule_id": "todo.overdue_backlog", "kind": "fact"})
    assert is_excluded({"rule_id": "x.y", "kind": "fault"})
    assert is_excluded({"rule_id": "x.y", "kind": "rule"})
    assert not is_excluded({"rule_id": "todo.overdue", "kind": "choose"})
    assert not is_excluded({"rule_id": "checklist.gap", "kind": "choose"})
    assert not is_excluded({"rule_id": "checklist.gap", "kind": "fact"})


# --- card_view ----------------------------------------------------------------------

def _overdue_row(**kw):
    row = {"card_id": "11111111-1111-1111-1111-111111111111", "kind": "choose",
           "site_id": "22222222-2222-2222-2222-222222222222", "rule_id": "todo.overdue",
           "subject_ref": "stable-1", "created_at": datetime(2026, 11, 12, 1, 2, 3, tzinfo=UTC),
           "payload": {"text": "Order the beams", "deadline": "2026-11-10"}}
    row.update(kw)
    return row


_OPTS = [{"id": "remind", "label": "Remind Sam", "recommended": True},
         {"id": "done", "label": "Mark done", "recommended": False}]


def test_card_view_shape_overdue():
    v = card_view(_overdue_row(), "q?", _OPTS, [], "Site A", False)
    assert v == {
        "card_id": "11111111-1111-1111-1111-111111111111", "kind": "choose",
        "site_id": "22222222-2222-2222-2222-222222222222", "site_name": "Site A",
        "created_at": "2026-11-12T01:02:03+00:00",
        "source_label": "Overdue since 11 Nov", "sub_label": None,
        "question": "q?", "item": {"stable_id": "stable-1", "title": "Order the beams"},
        "options": [{"option_id": "remind", "label": "Remind Sam", "recommended": True},
                    {"option_id": "done", "label": "Mark done", "recommended": False}],
        "evidence": []}


def test_card_view_removed_overdue_item_is_none():
    v = card_view(_overdue_row(), "q", _OPTS, [], "Site A", True)
    assert v["item"] is None


def test_card_view_item_null_for_other_rules_and_payload_as_json_string():
    v = card_view(_overdue_row(rule_id="checklist.gap"), "q", _OPTS, [], "S", False)
    assert v["item"] is None and v["source_label"] == "Missing from the inspection"
    v = card_view(_overdue_row(payload='{"text": "T", "deadline": "2026-11-10"}'),
                  "q", _OPTS, [], "S", False)
    assert v["item"] == {"stable_id": "stable-1", "title": "T"}
    assert v["source_label"] == "Overdue since 11 Nov"


def test_card_view_naive_created_at_is_utc():
    v = card_view(_overdue_row(created_at=datetime(2026, 11, 12, 1, 2, 3)), "q", _OPTS, [], "S",
                  False)
    assert v["created_at"] == "2026-11-12T01:02:03+00:00"


def test_card_view_options_without_id_dropped_and_reason_required_passed():
    opts = [{"label": "no id", "recommended": True}, {"id": "", "label": "empty"},
            {"id": "accept", "label": "Accept with reason", "recommended": False,
             "reason_required": True}]
    v = card_view(_overdue_row(rule_id="checklist.gap"), "q", opts, [], "S", False)
    assert v["options"] == [{"option_id": "accept", "label": "Accept with reason",
                             "recommended": False, "reason_required": True}]


def test_card_view_no_options_gives_empty_list():
    v = card_view(_overdue_row(), "q", [], [], "S", False)
    assert v["options"] == []
    v = card_view(_overdue_row(), "q", [{"label": "x"}], [], "S", False)
    assert v["options"] == []


def test_card_view_evidence_mapped_and_capped_at_two():
    ev = [{"quote": "a", "at": None}, {"quote": "b", "at": "2026-11-12T01:00:00+00:00"},
          {"quote": "c", "at": None}]
    v = card_view(_overdue_row(rule_id="checklist.gap"), "q", _OPTS, ev, "S", False)
    assert v["evidence"] == [
        {"speaker_name": None, "said_at": None, "quote": "a", "audio": None},
        {"speaker_name": None, "said_at": "2026-11-12T01:00:00+00:00", "quote": "b",
         "audio": None}]


def test_module_has_no_io_imports():
    import ast
    tree = ast.parse(open(decision_inbox.__file__, encoding="utf-8").read())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"json", "datetime", "typing", "decision_rules"}, mods
