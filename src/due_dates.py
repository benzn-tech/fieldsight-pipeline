"""due_dates.py -- turning what a model wrote about "when" into something sortable.

Pure. No boto3, no psycopg, no network, no model.

THE PRODUCER DOES NOT WRITE DATES. The daily prompt asks for
`"deadline": "When (e.g. 'Tomorrow 08:00', 'EOD', '15:00'), or null"` and the
extraction schema says "When, or null". So a deadline arrives as free text:
`Tomorrow 08:00`, `EOD`, `Friday`, `This week`, `15/09/2026`, `2026-9-5`.

Sorting those as strings orders them by their first character. Measured on the
real thing before this module existed: `15/09/2026` first (because "1" < "2"),
October above September, `EOD` below October, and `Tomorrow 08:00` below
`Friday`. An action mentioned three times with no deadline sorted last. The
report owner asked for an ordered action list and would have got one ordered by
the alphabet -- silently, because nothing fails.

So a deadline becomes a NUMBER OF DAYS from the report's own date, or None when
it cannot honestly be read as one. None is not an error; it is the answer for
`EOD Wednesday if the crane turns up`, and an unreadable deadline must not be
allowed to outrank a real one just for containing characters.
"""
import re
from datetime import date, datetime, timedelta

__all__ = ["days_until", "WEEKDAYS"]

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

#: Phrases that mean a day without naming one. Values are days from the report
#: date. "EOD" is today, not "unknown" -- it is the most urgent thing a site
#: report says, and leaving it unparsed put it below a date in October.
_RELATIVE = {
    "today": 0, "eod": 0, "end of day": 0, "cob": 0, "close of business": 0,
    "now": 0, "immediate": 0, "immediately": 0, "asap": 0, "urgent": 0,
    "tonight": 0, "this morning": 0, "this afternoon": 0,
    "tomorrow": 1, "tmr": 1, "next day": 1,
    "this week": 3, "end of week": 4, "eow": 4, "friday this week": 4,
    "next week": 7, "next monday": 7,
    "this month": 20, "end of month": 25, "eom": 25, "next month": 30,
}

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
_TIME_ONLY = re.compile(r"^\s*\d{1,2}[:.]\d{2}\s*(am|pm)?\s*$", re.IGNORECASE)


def days_until(text, report_date):
    """Days from `report_date` to what `text` says, or None if it says no day.

    `report_date` is a `date`, a `datetime`, or an ISO string -- the report's own
    date, because "Tomorrow" only means anything relative to when it was said.

    Returns a number that may be negative: a deadline already past is more
    urgent than one coming up, and hiding that by clamping would put an overdue
    action below a comfortable one.
    """
    anchor = _as_date(report_date)
    if anchor is None:
        return None

    raw = str(text or "").strip()
    if not raw:
        return None
    low = " ".join(raw.lower().split())

    # A time with no day is today. "15:00" is this afternoon, not unknown.
    if _TIME_ONLY.match(low):
        return 0

    m = _ISO.search(low)
    if m:
        found = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if found:
            return (found - anchor).days

    m = _DMY.search(low)
    if m:
        # Day first. NZ writes 15/09/2026; nothing in this product is US-format,
        # and guessing wrong here silently moves a deadline by months.
        found = _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if found:
            return (found - anchor).days

    # Longest phrase first, so "next week" is not read as "week" and
    # "end of week" is not read as "end of day".
    for phrase in sorted(_RELATIVE, key=len, reverse=True):
        if re.search(r"\b" + re.escape(phrase) + r"\b", low):
            return _RELATIVE[phrase]

    for name, index in WEEKDAYS.items():
        if re.search(r"\b" + name + r"\b", low):
            ahead = (index - anchor.weekday()) % 7
            # A weekday named today means next week's one, not this instant:
            # "do it Friday", said on Friday, is not already overdue.
            return ahead or 7

    return None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    m = _ISO.search(str(value or ""))
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _safe_date(year, month, day):
    """None rather than raising on 2026-02-31 -- a model writes those."""
    try:
        return date(year, month, day)
    except ValueError:
        return None
