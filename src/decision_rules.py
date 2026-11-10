"""Site decision loop: the P0 rules (pure; no I/O, no model calls).

RULES is keyed by event type. rule_id equals the event type. Card copy is
English (spec 6.3 / 6.5) and says only what the system can do; evidence is
copied from the event payload, never written here.
"""
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional

from decision_hub import (CardDraft, Event, RunContext, format_date, is_safety,
                          next_working_day_end_nz, today_nz_of)


@dataclass(frozen=True)
class Rule:
    """A card rule sets build_card (the hub writes the card); a run-rule sets run, which the
    hub calls as run(conn, event, RunContext) inside the event's run and which returns an
    outcome in decision_hub.RUN_OUTCOMES or raises. Exactly one of the two is set."""
    rule_id: str
    bucket: Callable[[Event, date], str]
    late_after_days: Optional[int]
    purpose: str
    build_card: Optional[Callable[[Event], CardDraft]] = None
    run: Optional[Callable[[Any, Event, RunContext], str]] = None

    def __post_init__(self):
        if (self.build_card is None) == (self.run is None):
            raise ValueError(f"rule {self.rule_id}: set exactly one of run / build_card")


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


# --- todo.overdue_backlog ----------------------------------------------------

def _build_overdue_backlog(event: Event) -> CardDraft:
    """One digest per (company, site) per NZ day listing the site's stale overdue items
    (spec 6.5 rev 6, owner option B). The payload holds the total count and at most 50 items;
    the card only names the count and the site."""
    p = event.payload or {}
    site = _text(p.get("site_name")) or "this site"
    try:
        n = int(p.get("count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n == 1:
        question = f"1 to-do on {site} is more than 3 days overdue"
    else:
        question = f"{n} to-dos on {site} are more than 3 days overdue"
    return CardDraft(
        kind="fact",
        question=question,
        consequence="They stay on the open to-do list until someone acts.",
        options=[{"id": "open_list", "label": "Open the list", "recommended": True}],
        evidence=[],
        urgency="normal",
        deadline_at=None,
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


# --- nightly.due (run-rule) ---------------------------------------------------

def _run_nightly(conn, event: Event, ctx: RunContext) -> str:
    """The nightly re-match handler. Imported here, at call time, so this module stays pure
    at import (the replay judge and the unit tests import it without a database driver)."""
    import nightly_rematch
    return nightly_rematch.run(conn, event, ctx)


def _night_bucket(event: Event, today_nz: date) -> str:
    """The night the tick computed (the site's previous local date), not today."""
    return _text((event.payload or {}).get("night"))[:10]


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
        # occurred_at is the NZ midnight the item became overdue (spec 6.5 rev 6): carded on
        # its first three overdue NZ days, late (listed in the site digest) after that.
        late_after_days=2,
        purpose="overdue",
        build_card=_build_overdue,
    ),
    "todo.overdue_backlog": Rule(
        rule_id="todo.overdue_backlog",
        bucket=lambda event, today_nz: today_nz.isoformat(),
        late_after_days=1,
        purpose="overdue_backlog",
        build_card=_build_overdue_backlog,
    ),
    "nightly.due": Rule(
        rule_id="nightly.due",
        bucket=_night_bucket,
        late_after_days=1,
        purpose="nightly",
        run=_run_nightly,
    ),
}
