"""The ONE read-time redaction both decision-card views serve (inbox spec rev 2 §4).

`redact(row, removed, due)` -> (question, options, evidence) in the stored shape
(options {id, label, recommended, reason_required?}; evidence {quote, at}). The shadow
view (`lambda_org_api._decision_card_views`) and the inbox map these; neither keeps its own
copy of the removed-source wording.
"""
from datetime import date

import decision_card_copy
import decision_rules
from decision_card_copy import redact


def _gap_row():
    return {
        "card_id": "c1", "rule_id": "checklist.gap",
        "question": "'Handrail' on the Scaffold checklist failed",
        "options": [
            {"id": "remind", "label": "Remind Sam to recheck", "recommended": True},
            {"id": "accept", "label": "Accept with reason", "recommended": False,
             "reason_required": True},
            {"id": "reassign", "label": "Reassign", "recommended": False},
        ],
        "evidence": [{"quote": "the handrail is loose", "at": None}],
    }


def _overdue_row():
    return {
        "card_id": "c2", "rule_id": "todo.overdue",
        "question": "'Order the beams' was due 10 Nov 2026, still open",
        "options": [
            {"id": "remind", "label": "Remind Sam", "recommended": True},
            {"id": "done", "label": "Mark done", "recommended": False},
            {"id": "new_date", "label": "Set a new date", "recommended": False},
            {"id": "not_needed", "label": "No longer needed", "recommended": False},
        ],
        "evidence": [],
    }


def test_redact_live_card_unchanged():
    row = _gap_row()
    question, options, evidence = redact(row, False, None)
    assert question == row["question"]
    assert options == [
        {"id": "remind", "label": "Remind Sam to recheck", "recommended": True},
        {"id": "accept", "label": "Accept with reason", "recommended": False,
         "reason_required": True},
        {"id": "reassign", "label": "Reassign", "recommended": False},
    ]
    assert evidence == [{"quote": "the handrail is loose", "at": None}]


def test_redact_drops_non_dict_options_and_evidence_and_normalises_bools():
    row = {"rule_id": "checklist.gap", "question": "q",
           "options": ["junk", {"id": "accept", "label": "A", "recommended": 1,
                                "reason_required": 0}],
           "evidence": [None, {"quote": "x", "at": "t", "extra": 1}]}
    _, options, evidence = redact(row, False, None)
    assert options == [{"id": "accept", "label": "A", "recommended": True,
                        "reason_required": False}]
    assert evidence == [{"quote": "x", "at": "t"}]


def test_redact_removed_gap_generic_label_no_evidence():
    row = _gap_row()
    question, options, evidence = redact(row, True, None)
    assert question == row["question"]          # the gap question names no person
    assert options[0] == {"id": "remind", "label": decision_rules.REMIND_INSPECTOR,
                          "recommended": True}
    assert options[0]["label"] == "Remind the inspector to recheck"
    assert evidence == []


def test_redact_removed_overdue_question_has_no_text():
    row = _overdue_row()
    question, options, evidence = redact(row, True, date(2026, 11, 10))
    assert question == decision_rules.removed_overdue_question(date(2026, 11, 10))
    assert "Order the beams" not in question
    assert options[0]["label"] == decision_rules.REMIND_OWNER == "Remind the owner"
    assert evidence == []
    q_none, _, _ = redact(row, True, None)
    assert q_none == "A to-do that was removed is past its date, still open"


def test_redact_non_remind_labels_untouched():
    for row in (_gap_row(), _overdue_row()):
        _, live, _ = redact(row, False, None)
        _, gone, _ = redact(row, True, None)
        assert [o for o in gone if o["id"] != "remind"] == [o for o in live if o["id"] != "remind"]


def test_redact_removed_other_rule_keeps_question_and_labels():
    row = {"rule_id": "todo.overdue_backlog", "question": "3 to-dos on A are more than 3 days overdue",
           "options": [{"id": "open_list", "label": "Open the list", "recommended": True}],
           "evidence": [{"quote": "q", "at": None}]}
    question, options, evidence = redact(row, True, None)
    assert question == row["question"]
    assert options == [{"id": "open_list", "label": "Open the list", "recommended": True}]
    assert evidence == []


def test_generic_remind_table_lives_in_the_module():
    assert decision_card_copy.GENERIC_REMIND == {
        "checklist.gap": decision_rules.REMIND_INSPECTOR,
        "todo.overdue": decision_rules.REMIND_OWNER}
