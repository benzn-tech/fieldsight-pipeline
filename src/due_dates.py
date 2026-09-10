"""due_dates.py -- a deadline as a number of days, for ordering.

Pure. No boto3, no psycopg, no network, no model.

THIS IS NOT A PARSER. `deadline_parse.resolve_deadline` is, and it already owns
the question: `lambda_ingest` calls it to write the `action_items.deadline`
column the Today page reads for overdue and due-this-week. This module converts
its answer into days from the report's date, which is what an ordering needs.

It was a second parser for about an hour, and the two disagreed in both
directions -- measured, anchored on 2026-09-10: this one sank "3 Aug", "By 15
September" and "Within 2 days" to the bottom as unreadable, while the real one
resolved them; the real one returned NULL for "EOD", "ASAP", "15:00" and
"15/09/2026", which this one placed. Fifteen phrases one way, six the other. The
Today page and the report would have told a site manager two different stories
about the same commitment, and neither would have failed.

So the vocabulary moved into `deadline_parse`, where the column comes from, and
what is left here is arithmetic.

WHY DAYS AND NOT A DATE. Ordering wants a number, and it wants negatives:
an action whose date has passed is more urgent than one coming up, and hiding
that by clamping would file it below a comfortable one.
"""
import re
from datetime import date, datetime

import deadline_parse

__all__ = ["days_until"]

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def days_until(text, report_date):
    """Days from `report_date` to what `text` says, or None if it says no day.

    `report_date` is a `date`, a `datetime`, or an ISO string -- the report's own
    date, because "Tomorrow" only means anything relative to when it was said.
    None is the honest answer for "when the crane arrives" and for "ongoing",
    and it matters: an unreadable deadline must not outrank a real one merely
    for containing characters.
    """
    anchor = _as_date(report_date)
    if anchor is None:
        return None
    resolved = deadline_parse.resolve_deadline(text, anchor.isoformat())
    if not resolved:
        return None
    try:
        return (date.fromisoformat(resolved) - anchor).days
    except ValueError:
        return None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = _ISO.search(str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
