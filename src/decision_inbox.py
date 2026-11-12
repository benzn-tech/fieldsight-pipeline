"""Pure logic of the decision-card inbox: GET/POST /api/org/decision-cards.

Spec: docs/superpowers/specs/2026-11-12-decision-card-inbox-design.md (rev 2) §4, §5.
No I/O here: SQL lives in repositories/decision_cards.py and decision_ledger.py, the route
in lambda_org_api.py, and the removed-source rewording in decision_card_copy.redact.

Row contract (what the repository supplies): a decision_cards row as a dict with at least
card_id, kind, site_id, rule_id, subject_ref, urgency, deadline_at, created_at, options,
and `payload` -- the card's site_events.payload (via event_id), a dict or a JSON string,
None when the event has none.
"""
import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from decision_rules import _parse_date

# Options that settle the subject -> decision_ledger.kind. Every other option (remind,
# new_date, reassign, anything unknown) writes no ledger row (spec §5).
DECISION_OPTIONS: dict[tuple[str, str], str] = {
    ("todo.overdue", "done"): "decided",
    ("todo.overdue", "not_needed"): "declined",
    ("checklist.gap", "accept"): "decided",
}

# Never in the inbox: fault cards stay on the shadow page; backlog cards feed only the digest.
EXCLUDED_RULES = ("hub.fault", "todo.overdue_backlog")
EXCLUDED_KINDS = ("fault", "rule")

CAP = 3
REASON_MAX = 500
DIGEST_HREF = "#/tasks?filter=overdue-old"


def _payload(payload) -> dict:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _day_month(d: date) -> str:
    """'11 Nov' (no leading zero; portable: Windows strftime has no %-d)."""
    return f"{d.day} {d:%b}"


def source_label(rule_id: str, payload) -> str:
    """The one source-label table (spec §4)."""
    if rule_id == "todo.overdue":
        due = _parse_date(_payload(payload).get("deadline"))
        if due is None:
            return "Overdue"
        return f"Overdue since {_day_month(due + timedelta(days=1))}"
    if rule_id == "checklist.gap":
        return "Missing from the inspection"
    return "From the site"


def digest_title(per_site: list[tuple[str, int]]) -> Optional[str]:
    """Title of the digest over (site_name, live stale count) pairs; None when no site
    contributes. Sites with 0 are left out; the site clause appears only when more than one
    site contributes (names sorted, at most 3, then ' and N more sites')."""
    sites = [(name, n) for name, n in per_site if n and n > 0]
    total = sum(n for _, n in sites)
    if total <= 0:
        return None
    title = ("1 overdue to-do more than 3 days old" if total == 1
             else f"{total} overdue to-dos more than 3 days old")
    if len(sites) > 1:
        names = sorted(name for name, _ in sites)
        title += " on " + ", ".join(names[:3])
        if len(names) > 3:
            title += f" and {len(names) - 3} more sites"
    return title


_LAST = datetime.max.replace(tzinfo=timezone.utc)


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _order_key(row: dict):
    deadline = _aware(row.get("deadline_at"))
    created = _aware(row.get("created_at"))
    return (0 if row.get("urgency") == "injury" else 1,
            deadline is None, deadline if deadline is not None else _LAST,
            created is None, created if created is not None else _LAST,
            str(row.get("card_id")))


def order_and_cap(cards: list[dict], cap: int = CAP) -> tuple[list[dict], int]:
    """(shown, more): rows ordered injury first, deadline_at ascending (nulls last),
    created_at, card_id; the first `cap`, or every injury card when there are more than
    `cap` of them. `more` = ordered rows beyond those shown."""
    ordered = sorted(cards, key=_order_key)
    n = max(cap, sum(1 for r in ordered if r.get("urgency") == "injury"))
    shown = ordered[:n]
    return shown, len(ordered) - len(shown)


def is_excluded(row: dict) -> bool:
    return row.get("rule_id") in EXCLUDED_RULES or row.get("kind") in EXCLUDED_KINDS


def _iso_utc(value):
    # Same form as lambda_org_api._iso_utc (the shadow page), kept local: this module is pure.
    if not isinstance(value, datetime):
        return value
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat()


def card_view(row: dict, question: str, options: list, evidence: list, site_name: str,
              removed: bool) -> dict:
    """The inbox card (spec §4) from a row plus the redacted (question, options, evidence)
    of decision_card_copy.redact. Options without an id are dropped; a card left with no
    option has `options == []` and the caller skips it."""
    rule_id = row.get("rule_id")
    payload = _payload(row.get("payload"))
    item = None
    if rule_id == "todo.overdue" and not removed:
        item = {"stable_id": row.get("subject_ref"), "title": payload.get("text")}
    opts = []
    for o in options or []:
        if not isinstance(o, dict) or not o.get("id"):
            continue
        view = {"option_id": o["id"], "label": o.get("label"),
                "recommended": bool(o.get("recommended"))}
        if "reason_required" in o:
            view["reason_required"] = bool(o["reason_required"])
        opts.append(view)
    ev = [{"speaker_name": None, "said_at": _iso_utc(e.get("at")), "quote": e.get("quote"),
           "audio": None}
          for e in (evidence or []) if isinstance(e, dict)][:2]
    return {
        "card_id": str(row.get("card_id")), "kind": row.get("kind"),
        "site_id": str(row.get("site_id")), "site_name": site_name,
        "created_at": _iso_utc(row.get("created_at")),
        "source_label": source_label(rule_id, payload), "sub_label": None,
        "question": question, "item": item, "options": opts, "evidence": ev,
    }


def validate_answer(row: dict, option_id, reason) -> Optional[str]:
    """The 400 message for an answer to this card, or None when it is valid. Checked against
    the stored options (redaction never changes ids or reason_required)."""
    option = None
    if isinstance(option_id, str) and option_id:
        option = next((o for o in row.get("options") or []
                       if isinstance(o, dict) and o.get("id") == option_id), None)
    if option is None:
        return "option_id is not one of this card's options"
    if reason is not None and not isinstance(reason, str):
        return "reason must be text"
    text = (reason or "").strip()
    if option.get("reason_required") and not text:
        return "this option needs a reason"
    if len(text) > REASON_MAX:
        return f"reason is over {REASON_MAX} characters"
    return None
