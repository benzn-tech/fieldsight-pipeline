"""Site decision loop: the P0 rules (pure; no I/O, no model calls).

RULES is keyed by event type. rule_id equals the event type. Card copy is
English (spec 6.3 / 6.5) and says only what the system can do; evidence is
copied from the event payload, never written here.
"""
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from decision_hub import (CardDraft, Event, format_date, is_safety,
                          next_working_day_end_nz, today_nz_of)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    bucket: Callable[[Event, date], str]
    late_after_days: Optional[int]
    purpose: str
    build_card: Callable[[Event], CardDraft]


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _parse_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


# --- todo.overdue -----------------------------------------------------------

def _build_overdue(event: Event) -> CardDraft:
    p = event.payload or {}
    text = _text(p.get("text"))
    due = _parse_date(p.get("deadline"))
    person = _text(p.get("responsible"))
    if due is not None:
        question = f"'{text}' was due {format_date(due)}, still open"
    else:
        question = f"'{text}' is past its date, still open"
    return CardDraft(
        kind="choose",
        question=question,
        consequence="It stays on the open to-do list until someone acts.",
        options=[
            {"id": "remind",
             "label": f"Remind {person}" if person else "Remind the owner",
             "recommended": True},
            {"id": "done", "label": "Mark done", "recommended": False},
            {"id": "new_date", "label": "Set a new date", "recommended": False},
            {"id": "not_needed", "label": "No longer needed", "recommended": False},
        ],
        evidence=[],
        urgency="normal",
        deadline_at=next_working_day_end_nz(today_nz_of(event)),
        timeout_default="nothing changes",
        recipient_roles=["pm", "site_manager"],
    )


# --- checklist.gap ----------------------------------------------------------

_GAP_KIND_WORDS = {"not_covered": "was not covered", "failed": "failed"}


def _build_gap(event: Event) -> CardDraft:
    p = event.payload or {}
    checklist = _text(p.get("checklist"))
    item = _text(p.get("item"))
    verb = _GAP_KIND_WORDS.get(_text(p.get("kind")), "is open")
    person = _text(p.get("responsible")) or _text(p.get("inspector"))
    quote = _text(p.get("quote"))
    safety = is_safety(checklist) or is_safety(item)

    question = f"'{item}' on the {checklist} checklist {verb}"
    consequence = "The item stays open on the checklist until it is rechecked or accepted."
    if safety:
        consequence += (" This is a safety item: accepting it needs a reason and "
                        "copies the company admin.")
    roles = ["pm", "site_manager"] + (["company_admin"] if safety else [])
    return CardDraft(
        kind="choose",
        question=question,
        consequence=consequence,
        options=[
            {"id": "remind",
             "label": (f"Remind {person} to recheck" if person
                       else "Remind the inspector to recheck"),
             "recommended": True},
            {"id": "accept", "label": "Accept with reason", "recommended": False,
             "reason_required": True},
            {"id": "reassign", "label": "Reassign", "recommended": False},
        ],
        evidence=[{"quote": quote, "at": None}] if quote else [],
        urgency="injury" if safety else "normal",
        deadline_at=next_working_day_end_nz(today_nz_of(event)),
        timeout_default="not accepted" if safety else "nothing changes",
        recipient_roles=roles,
    )


RULES = {
    "checklist.gap": Rule(
        rule_id="checklist.gap",
        bucket=lambda event, today_nz: "",
        late_after_days=7,
        purpose="gap",
        build_card=_build_gap,
    ),
    "todo.overdue": Rule(
        rule_id="todo.overdue",
        bucket=lambda event, today_nz: today_nz.isoformat(),
        late_after_days=None,
        purpose="overdue",
        build_card=_build_overdue,
    ),
}
