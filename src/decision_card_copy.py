"""The served copy of a decision card: ONE read-time redaction for every view.

Both the shadow review (`lambda_org_api._decision_card_views`) and the decision-card inbox
call `redact`; neither keeps its own copy of the removed-source wording (inbox spec
2026-11-12-decision-card-inbox-design rev 2 §4). Stored rows are never touched.

Pure: no I/O. Whether a card's source is removed (and its due date) is decided by the
caller (`lambda_org_api._removed_source_cards`, fail closed).
"""
from datetime import date
from typing import Optional

import decision_rules

# The person-free remind label per rule, served in place of "Remind <name>..." when a card's
# source is removed. The same wording the rules use when no person is known.
GENERIC_REMIND = {"checklist.gap": decision_rules.REMIND_INSPECTOR,
                  "todo.overdue": decision_rules.REMIND_OWNER}


def redact(row: dict, removed: bool, due: Optional[date]) -> tuple[str, list[dict], list[dict]]:
    """(question, options, evidence) to serve for a stored card row.

    Options keep the stored shape `{id, label, recommended[, reason_required]}` (bools
    normalised, non-dict entries dropped); evidence `{quote, at}` (non-dict dropped). Each
    view maps these to its own field names.

    A removed source serves no evidence, a person-free remind label, and for todo.overdue a
    question without the item's text (`decision_rules.removed_overdue_question(due)`)."""
    rule_id = row.get("rule_id")
    question = row.get("question")
    if removed and rule_id == "todo.overdue":
        question = decision_rules.removed_overdue_question(due)
    generic_remind = GENERIC_REMIND.get(rule_id) if removed else None
    options = []
    for o in row.get("options") or []:
        if not isinstance(o, dict):
            continue
        label = o.get("label")
        if generic_remind is not None and o.get("id") == "remind":
            label = generic_remind
        view = {"id": o.get("id"), "label": label, "recommended": bool(o.get("recommended"))}
        if "reason_required" in o:
            view["reason_required"] = bool(o["reason_required"])
        options.append(view)
    evidence = [] if removed else [
        {"quote": e.get("quote"), "at": e.get("at")}
        for e in (row.get("evidence") or []) if isinstance(e, dict)]
    return question, options, evidence
