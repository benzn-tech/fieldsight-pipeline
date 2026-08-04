"""Derive every device's state and alerts. Pure — no I/O, no clock.

The decision that makes this safe: each alert is evaluated only inside the
window where it CAN be true. A device sitting in stock, or already collected,
is legitimately dark, so the quiet alert simply does not apply to it. That is
what lets a quiet alert exist at all without becoming noise — and noise gets
muted, and a muted alert is worse than none, because you believe you are
covered.

The fill-if-empty rule is the other half: `client`, `due_back` and `activated`
are proposed only when the human left them blank. A value, once present, is
never proposed again — so no sync can overwrite what somebody typed.
"""

import datetime as dt

STATUS_UNACTIVATED = "未激活"
STATUS_IN_USE = "使用中"
STATUS_IN_STOCK = "在库"
STATUS_QUIET = "失联"
STATUS_UNCLAIMED = "未认领"

DEFAULT_TERM_DAYS = 30


def working_days_between(a, b):
    """NZ working days from a to b, weekends excluded.

    Public holidays are not modelled: the threshold is generous and
    configurable, so a holiday shifts the count by at most one.
    """
    if b <= a:
        return 0
    days = 0
    cur = a
    while cur < b:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _version_key(v):
    """Numeric, so 1.10.0 sorts above 1.9.0 — which string order gets backwards."""
    parts = []
    for chunk in str(v).replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def derive(ledger, notion, today, quiet_working_days, grace_days):
    """One entry per Notion row: its status, its alerts, and the properties to write."""
    by_tag = {r["asset_tag"]: r for r in ledger}

    versions = [r.get("app_version") for r in ledger if r.get("app_version")]
    newest = max(versions, key=_version_key) if versions else None

    out = []
    for row in notion:
        led = by_tag.get(row["device"], {})
        seen_at = led.get("last_seen_at")
        seen_date = seen_at.date() if isinstance(seen_at, dt.datetime) else None

        dispatched = row["dispatched"]
        returned = row["returned"]
        # Activation is the first sighting AFTER hand-over. A bench test before
        # dispatch does not mean the client ever switched it on.
        activated = (
            dispatched is not None and seen_date is not None and seen_date >= dispatched
        )

        alerts = []
        updates = {
            "last_seen": seen_date,
            "app_version": led.get("app_version"),
            "actual_site": led.get("actual_site"),
            "last_synced": today,
        }

        if returned or dispatched is None:
            status = STATUS_IN_STOCK
        elif not activated:
            status = STATUS_UNACTIVATED
        else:
            status = STATUS_IN_USE
        if row["device"].startswith("unclaimed:"):
            status = STATUS_UNCLAIMED

        # Handed over and never switched on.
        if dispatched is not None and not returned and not activated:
            if (today - dispatched).days > grace_days:
                alerts.append("never_activated")

        # Gone quiet — ONLY inside the in-use window.
        if activated and not returned and seen_date is not None:
            if working_days_between(seen_date, today) > quiet_working_days:
                alerts.append("quiet")
                status = STATUS_QUIET

        if not returned and row["due_back"] is not None and row["due_back"] < today:
            alerts.append("due_back")

        mine = led.get("app_version")
        if mine and newest and _version_key(mine) < _version_key(newest):
            alerts.append("outdated_version")

        # Working for someone other than the registered client: a flag on the
        # row, never a push. It is context, not a call to action.
        company = led.get("actual_company")
        if row["client"] and company and company != row["client"]:
            alerts.append("site_mismatch_flag")

        # Fill-if-empty: propose ONLY into blanks.
        if row["client"] is None and company:
            updates["client"] = company
        if row["due_back"] is None and dispatched is not None:
            updates["due_back"] = dispatched + dt.timedelta(days=DEFAULT_TERM_DAYS)
        if row["activated"] is None and activated:
            updates["activated"] = seen_date

        updates["status"] = status
        out.append({
            "page_id": row["page_id"],
            "device": row["device"],
            "status": status,
            "alerts": alerts,
            "updates": updates,
        })
    return out
