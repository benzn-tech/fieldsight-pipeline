"""How a local subtree responds when its imported parent moves.

Spec: fieldsight-ui/docs/superpowers/specs/2026-08-02-programme-foundation-design.md §6.4

Local subtasks are scheduled relative to their imported parent, so a revision
that moves the parent has to move them too. Two rules:

  * Shift only what has not started. A task with recorded progress keeps its
    real dates — those are a record of what happened, and rewriting them would
    assert work occurred on days it did not. This can leave a gap between a
    finished child and the shifted remainder. The gap is real, and closing it
    would be a lie about the schedule.

  * When the parent's duration changes materially, do not reshape the
    breakdown — flag it. The subtasks are already allocated to named people,
    and quietly re-planning them changes someone's week without telling them.
    The UI offers a re-plan the PM accepts.

Pure: no database, no clock.
"""
from datetime import date, timedelta

# Above this relative duration change, a proportional scale is no longer a
# reasonable guess at what the work should look like — compressing a
# four-week breakdown into two weeks is a different plan, not the same plan
# scaled. Expansion counts too: stretching a breakdown to fill twice the time
# is as much of an invention as compressing it.
_INVALIDATE_ABOVE = 0.20


def _d(v):
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _span(p):
    s, e = _d(p.get("start_date")), _d(p.get("end_date"))
    if s is None or e is None:
        return None
    return (e - s).days + 1


def rebase_children(parent_before, parent_after, children):
    """Returns {shift: [{id, start_date, end_date}], invalidated, reason}."""
    before_start = _d(parent_before.get("start_date"))
    after_start = _d(parent_after.get("start_date"))
    before_span, after_span = _span(parent_before), _span(parent_after)

    if before_start is None or after_start is None \
            or not before_span or not after_span:
        return {"shift": [], "invalidated": False, "reason": None}

    ratio = after_span / before_span

    # No children means nothing to invalidate. Flagging here would put a
    # warning on every imported task with no breakdown yet — which is most
    # of them, and the warning would stop meaning anything.
    if children and abs(ratio - 1.0) > _INVALIDATE_ABOVE:
        return {
            "shift": [],
            "invalidated": True,
            "reason": (f"the parent's duration changed from {before_span} to "
                       f"{after_span} days — the existing breakdown no longer "
                       f"fits and needs re-planning"),
        }

    shift = []
    for c in children or []:
        # Started or finished work keeps its real dates.
        if (c.get("progress_pct") or 0) > 0:
            continue
        cs, ce = _d(c.get("start_date")), _d(c.get("end_date"))
        if cs is None or ce is None:
            continue
        offset = (cs - before_start).days
        new_start = after_start + timedelta(days=round(offset * ratio))
        new_len = round(((ce - cs).days + 1) * ratio)
        new_end = new_start + timedelta(days=max(0, new_len - 1))

        # A child whose dates land exactly where they already are is not a
        # shift. Emitting it anyway would bump its row_version for nothing and
        # make the import diff report movement that did not happen.
        if new_start == cs and new_end == ce:
            continue

        shift.append({"id": c["id"],
                      "start_date": new_start.isoformat(),
                      "end_date": new_end.isoformat()})

    return {"shift": shift, "invalidated": False, "reason": None}
