"""Site decision loop: the pure hub functions (P0, shadow).

No I/O here: no database, no S3, no model, no clock. The scan Lambda and the
replay judge both call these, so every function is a deterministic function of
its arguments. "Today" is always passed in, and is always the NZ date of the
event's received_at (never a UTC date, never datetime.now()).

Imports are stdlib + nz_time only; decision_rules is imported lazily inside
the functions that need it (it imports Event/CardDraft from here).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import nz_time

FAULT_AT = 3
STOP_AT = 5

# Case-insensitive substrings that make a checklist title or item a safety item
# (spec 6.5: a list in the rule, not a model call).
SAFETY_WORDS = ("safety", "h&s", "health and safety", "hazard")

# v1 recipient roles, in de-duplication priority order (first role wins).
ROLE_ORDER = ("pm", "site_manager", "company_admin", "ops")
COMPANY_ADMIN_GLOBAL_ROLES = ("admin", "gm", "regional_manager")

FAULT_RULE_ID = "hub.fault"


@dataclass(frozen=True)
class Event:
    """The site_events columns the hub reads. occurred_at / received_at are
    tz-aware datetimes."""
    event_id: str
    company_id: Any
    site_id: Any
    source: str
    type: str
    subject_ref: str
    occurred_at: datetime
    received_at: datetime
    payload: dict
    chain_id: Any
    chain_depth: int


@dataclass
class CardDraft:
    kind: str
    question: str
    consequence: str
    options: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    urgency: str = "normal"
    deadline_at: Optional[datetime] = None
    timeout_default: Optional[str] = None
    recipient_roles: list = field(default_factory=list)


def today_nz_of(event: Event) -> date:
    """The event's NZ day: always the NZ date of received_at."""
    return nz_time.to_nz(event.received_at).date()


def format_date(d: date) -> str:
    """'1 Nov 2026' (no leading zero; portable across Windows and Linux)."""
    return f"{d.day} {d:%b %Y}"


def match(event: Event):
    from decision_rules import RULES
    return RULES.get(event.type)


def dedupe_key(event: Event, today_nz: date) -> str:
    """(company, site, type, subject_ref, bucket). A type with no rule gets an
    empty bucket (emitters only emit ruled types; an unknown one must still
    dedupe rather than raise)."""
    rule = match(event)
    bucket = rule.bucket(event, today_nz) if rule is not None else ""
    return "|".join(str(p) for p in (
        event.company_id, event.site_id, event.type, event.subject_ref, bucket))


def card_key(event: Event, rule: "Rule") -> str:  # noqa: F821
    """Open-card identity: independent of event_id, payload, received_at and
    bucket, so a re-emitted event lands on the same open card."""
    return "|".join(str(p) for p in (
        event.company_id, event.site_id, rule.rule_id, event.subject_ref, rule.purpose))


def fault_card_key(company_id, site_id, subject_ref, purpose: str) -> str:
    """purpose 'fault' -> subject_ref = event_id; 'too_deep' -> chain_id."""
    return "|".join(str(p) for p in (
        company_id, site_id, FAULT_RULE_ID, subject_ref, purpose))


def is_late(event: Event, rule: "Rule", today_nz: date) -> bool:  # noqa: F821
    if rule.late_after_days is None:
        return False
    occurred = nz_time.to_nz(event.occurred_at).date()
    return (today_nz - occurred).days > rule.late_after_days


def too_deep(event: Event) -> bool:
    return event.chain_depth > 3


def backoff_seconds(attempts: int) -> int:
    return min(300 * 2 ** (attempts - 1), 3600)


def next_working_day_end_nz(today_nz: date) -> datetime:
    """23:59:59 NZ on the next day that is not a Sunday (Saturday works,
    spec 6.6), returned tz-aware in UTC."""
    d = today_nz + timedelta(days=1)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return nz_time.from_nz(datetime.combine(d, time(23, 59, 59))).replace(
        tzinfo=timezone.utc)


def is_safety(text) -> bool:
    if not text:
        return False
    low = str(text).lower()
    return any(w in low for w in SAFETY_WORDS)


def recipients(card: CardDraft, site_members: list, company_users: list,
               platform_admins: list) -> list:
    """[{"user_id","role"}] for the roles the card names. Order is fixed:
    pm, site_manager, company_admin, ops; input order within a role; a user
    appears once, under the first role that matched. platform_admins is taken
    as already filtered by the caller."""
    wanted = set(card.recipient_roles or ())
    candidates = {
        "pm": [r for r in site_members if r.get("site_role") == "pm"],
        "site_manager": [r for r in site_members if r.get("site_role") == "site_manager"],
        "company_admin": [r for r in company_users
                          if r.get("global_role") in COMPANY_ADMIN_GLOBAL_ROLES],
        "ops": list(platform_admins),
    }
    out, seen = [], set()
    for role in ROLE_ORDER:
        if role not in wanted:
            continue
        for row in candidates[role]:
            uid = str(row["id"])
            if uid in seen:
                continue
            seen.add(uid)
            out.append({"user_id": uid, "role": role})
    return out


_FAULT_OPTIONS = [{"id": "acknowledge", "label": "Acknowledge", "recommended": True}]


def fault_card(event: Event, attempts: int, stopped: bool) -> CardDraft:
    """The hub's own card for an event that keeps failing (FAULT_AT, bumped at
    STOP_AT). Not a rule card."""
    question = (f"Site event {event.type} ({event.subject_ref}) has failed "
                f"{attempts} times.")
    if stopped:
        consequence = (f"Retrying stopped after {STOP_AT} failures; this event "
                       "will not be processed until someone looks at it.")
    else:
        consequence = ("It is being retried automatically; nothing has been "
                       "done for this event yet.")
    return CardDraft(
        kind="fault", question=question, consequence=consequence,
        options=[dict(o) for o in _FAULT_OPTIONS], evidence=[],
        urgency="normal", deadline_at=None, timeout_default="nothing changes",
        recipient_roles=["company_admin", "ops"])


def too_deep_card(event: Event) -> CardDraft:
    """One fault card per chain whose depth went above 3."""
    return CardDraft(
        kind="fault",
        question=(f"A chain of site events went deeper than 3 steps "
                  f"(at {event.type}, {event.subject_ref})."),
        consequence="Events deeper than 3 steps are not processed.",
        options=[dict(o) for o in _FAULT_OPTIONS], evidence=[],
        urgency="normal", deadline_at=None, timeout_default="nothing changes",
        recipient_roles=["company_admin", "ops"])
