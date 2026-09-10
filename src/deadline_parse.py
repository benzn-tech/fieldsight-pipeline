"""deadline_parse.py — resolve the extractor's free-text deadline to a date.

The extraction prompt asks for "When, or null if not mentioned", so what
comes back is speech: "Tomorrow", "This Friday, 2026-07-25", "Immediately",
"Week after next Tuesday (2026-07-28 approx.)". `lambda_ingest._map_action_items`
stored that verbatim in `deadline_text` and wrote the `deadline` DATE column
only when the string already WAS an ISO date -- which on prod meant 18 open
items with a stated deadline and ZERO with a date.

This is the frontend's parser (`today-adapter.js:resolveDeadline`, shipped in
fix/timeline-buttons-and-deadline) moved server-side, so the date is derived
once at write time instead of re-derived by every reader -- and so anything
that needs to COMPARE dates (overdue, due this week, counting how many times
a promised date slipped) has a column to compare.

The one rule: NEVER GUESS. Anything unrecognised keeps its text and leaves
the date NULL. A wrong due date is worse than none -- it either fabricates
urgency or hides it, and nobody can tell which by looking.

Anchoring: every relative phrase resolves against the REPORT's date, never
the server's today. Reprocessing a recording from February must produce the
February answer.
"""
import re
from datetime import date, timedelta

_ISO_ANYWHERE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(20\d{2})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\b")
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b")
_WITHIN = re.compile(r"\b(?:within|in)\s+(\d+)\s*(day|week)s?\b")
# "Within one hour" is real prod text. At this column's DAY granularity that
# is the report's own date -- not a guess, just the honest rounding. Worded
# numbers are included because that is how people say it out loud.
_WITHIN_HOURS = re.compile(
    r"\b(?:within|in)\s+(?:\d+|an?|one|two|a\s+few|couple\s+of)\s*(?:hour|hr|minute|min)s?\b")
_WEEKDAY = re.compile(
    r"\b(sunday|monday|tuesday|wednesday|thursday|friday|saturday"
    r"|sun|mon|tues|tue|weds|wed|thurs|thur|thu|fri|sat)\b")

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]

# Sunday-first, matching the frontend's getUTCDay() so the two resolvers
# cannot disagree about which Friday is meant.
_WEEKDAY_INDEX = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tues": 2, "tue": 2,
    "wednesday": 3, "weds": 3, "wed": 3,
    "thursday": 4, "thurs": 4, "thur": 4, "thu": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}

# Strings the extractor emits for "no deadline" that are not absences at the
# type level but are at the meaning level. `null` as a literal STRING is real
# prod data.
_PLACEHOLDERS = {"null", "none", "n/a", "na", "tbc", "tbd", "-", "--"}

# Continuity markers. "Ongoing from next week" is real prod text, and it
# names a START and an open-ended state -- not a due point. Resolving it to
# next Wednesday would mark it overdue the day after, which is the opposite
# of what it says. Deliberately narrow: only words that make the whole phrase
# about duration rather than a deadline, checked before any relative parsing
# so the date inside them ("from next week") cannot be mistaken for one.
#: Phrases that name a day without spelling one, and the bare times that mean
#: the same. Added when the report's action ranking needed an order and found
#: this module returning NULL for the most urgent thing a site says: "EOD" is
#: not unresolvable, it is today. Before this, `deadline` was NULL for every one
#: of them, so the Today page could not call them overdue and a second parser
#: was written elsewhere to fill the gap -- which is how one rule became two
#: that disagreed.
#:
#: Still no guessing. "Urgent" is deliberately absent: it states a priority, not
#: a day, and turning it into one would fabricate the urgency this module exists
#: to avoid fabricating. `_CONTINUOUS` is still checked first, so "ongoing from
#: next week" stays NULL.
#: ASAP, "immediately" and "right away" are NOT here, and the test that says so
#: predates this addition: they state a priority, not a day, and inventing
#: today's date for one makes it overdue tomorrow and every day after --
#: forever, for something nobody ever put a date on. "End of day" is different:
#: it names a day, and an EOD item not done by tomorrow genuinely IS overdue.
_TODAY_WORDS = (
    "end of day", "close of business", "eod", "cob",
    "tonight", "this morning", "this afternoon", "this evening",
)
_END_OF_WEEK_WORDS = ("end of week", "eow", "end of the week", "this week",
                      "by the end of the week")

#: The month phrases the report's own ranker understood and this module did
#: not, so unifying the two parsers silently dropped them: "end of month" and
#: "this month" went from an order to unreadable.
#:
#: They resolve to the LAST day of the month, not to the old ranker's guess of
#: "+25 days". That number was never a date and could not go into a DATE column
#: honestly. Where a month phrase is imprecise, resolving it LATE is the safe
#: direction: too late merely delays a nudge, too early reports something as
#: overdue that was never owed.
_END_OF_MONTH_WORDS = ("end of month", "end of the month", "eom", "this month",
                       "by the end of the month")
_NEXT_MONTH_WORDS = ("next month",)

#: 15:00, 09.30, 9am -- a time with no day is a time TODAY.
#:
#: The separator is REQUIRED, or the meridiem is. Without that, `\d{1,2}` with
#: everything after it optional matched a bare number, so a deadline of "2026"
#: or "3" or "1.5" resolved to the anchor and was overdue the next morning.
#: A number on its own does not name a time.
_BARE_TIME = re.compile(
    r"^\s*(?:\d{1,2}[:.]\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))\s*$",
    re.IGNORECASE)
#: 2026-9-5 as readily as 2026-09-05.
_ISO_LOOSE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
#: 15/09/2026 is September. Nothing in this product is US-format, and reading it
#: the other way moves a deadline by months without anything failing.
_DAY_FIRST = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")


def _numeric_formats(lower):
    """`15/09/2026` and `2026-9-5`, or None.

    These run BEFORE the spelled-out forms, because the looser day/month
    patterns down there would read `15/09/2026` as a day-month pair. They are
    unambiguous: a string in one of these shapes names one date and nothing
    else, so there is no phrase they could be stealing.
    """
    m = _DAY_FIRST.search(lower)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    m = _ISO_LOOSE.search(lower)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def _weak_day_words(lower, anchor):
    """"EOD", "EOW", "15:00", "end of month" -- phrases that name a day only
    because nothing else in the sentence does.

    THE ORDER IS THE WHOLE POINT, and getting it wrong shipped a real
    regression. Run first, `_TODAY_WORDS` matched the "EOD" inside
    **"EOD Friday"** and returned today. Measured, anchored Wednesday
    2026-09-09: `EOD Friday` 2026-09-11 -> 2026-09-09, `COB Monday`
    2026-09-14 -> 2026-09-09, `End of day tomorrow` 2026-09-10 -> 2026-09-09.

    That is not an ordering nicety. `lambda_ingest` writes this value into
    `action_items.deadline`, so a Friday commitment shows as overdue on
    Thursday morning -- the exact failure this module exists to avoid, and the
    meeting prompt's own example is "'By Friday', 'EOW'", so the combination is
    not hypothetical.

    So these run LAST: after today/tomorrow/weekday/next week/within N, and
    after the spelled-out dates. Anything that names an actual day wins; these
    only speak when the answer would otherwise be None.
    """
    if _BARE_TIME.match(lower):
        return anchor.isoformat()
    for word in _TODAY_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", lower):
            return anchor.isoformat()
    for word in _END_OF_MONTH_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", lower):
            return _end_of_month(anchor).isoformat()
    for word in _NEXT_MONTH_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", lower):
            return _end_of_month(_end_of_month(anchor) + timedelta(days=1)).isoformat()
    for word in _END_OF_WEEK_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", lower):
            # Friday, or today when the week is already there or past it.
            ahead = max(0, 4 - anchor.weekday())
            return (anchor + timedelta(days=ahead)).isoformat()
    return None


def _end_of_month(anchor):
    """The last day of the anchor's month."""
    if anchor.month == 12:
        return date(anchor.year, 12, 31)
    return date(anchor.year, anchor.month + 1, 1) - timedelta(days=1)


_CONTINUOUS = re.compile(r"\b(ongoing|continuous(?:ly)?|throughout|as\s+required|"
                         r"as\s+needed|daily|weekly|每天|持续)\b", re.IGNORECASE)


def _parse_iso(s):
    m = _ISO_ANYWHERE.search(s)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None          # 2026-13-40 is refused, never clamped


def _parse_spelled(s, anchor):
    """"12 Feb", "Feb 12", "12th February 2027". The year is taken from the
    text when present, else from the report's own year -- a site conversation
    saying "3 Aug" means this year's August."""
    day = month_token = None
    m = _DAY_MONTH.search(s)
    if m and m.group(2)[:3].lower() in _MONTHS:
        day, month_token = int(m.group(1)), m.group(2)
    else:
        m = _MONTH_DAY.search(s)
        if m and m.group(1)[:3].lower() in _MONTHS:
            day, month_token = int(m.group(2)), m.group(1)
    if day is None:
        return None
    month = _MONTHS.index(month_token[:3].lower()) + 1
    ym = _YEAR.search(s)
    year = int(ym.group(1)) if ym else (anchor.year if anchor else None)
    if year is None:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None          # "32 Feb"


def resolve_deadline(free_text, report_date_iso):
    """Return 'YYYY-MM-DD', or None when the text states no resolvable date.

    `report_date_iso` anchors every relative phrase. Without it only absolute
    dates resolve -- falling back to the server's today would make a
    recording's deadlines drift each time it is reprocessed.
    """
    text = "" if free_text is None else str(free_text).strip()
    if not text or text.lower() in _PLACEHOLDERS:
        return None
    if _CONTINUOUS.search(text):
        return None

    anchor = None
    if report_date_iso:
        try:
            anchor = date.fromisoformat(str(report_date_iso)[:10])
        except ValueError:
            anchor = None

    # An explicit date in the text is authoritative and is tried FIRST: the
    # extractor often renders the phrase AND the date it resolved to --
    # "Week after next Tuesday (2026-07-28 approx.)" -- and re-deriving the
    # weekday from the phrase would land on a different day.
    iso = _parse_iso(text)
    if iso:
        return iso

    lower = text.lower()

    if anchor is None:
        # Nothing relative can be resolved without the report's date, and the
        # spelled-out forms need at least its year.
        return None

    # Numeric date FORMATS only. The weak day-words ("EOD", "EOW", a bare
    # time) are deliberately not here -- see _weak_day_words for what happened
    # the one night they were.
    numeric = _numeric_formats(lower)
    if numeric:
        return numeric

    if re.search(r"\btoday\b", lower) or _WITHIN_HOURS.search(lower):
        return anchor.isoformat()
    if re.search(r"\b(?:tmr|next\s+day)\b", lower):
        # Both were in the report ranker's vocabulary before the two parsers
        # were merged; "tmr" is what somebody types on a phone.
        return (anchor + timedelta(days=1)).isoformat()
    if re.search(r"\btomorrow\b", lower):
        return (anchor + timedelta(days=1)).isoformat()

    wd = _WEEKDAY.search(lower)
    if wd:
        target = _WEEKDAY_INDEX[wd.group(1)]
        # date.weekday() is Monday-0; shift to Sunday-0 to match the frontend.
        current = (anchor.weekday() + 1) % 7
        delta = (target - current) % 7
        if delta == 0:
            delta = 7        # strictly after the conversation, never same-day
        return (anchor + timedelta(days=delta)).isoformat()

    if re.search(r"\bnext\s+week\b", lower):
        return (anchor + timedelta(days=7)).isoformat()

    w = _WITHIN.search(lower)
    if w:
        n = int(w.group(1))
        days = n * 7 if w.group(2) == "week" else n
        return (anchor + timedelta(days=days)).isoformat()

    spelled = _parse_spelled(text, anchor)
    if spelled:
        return spelled

    # Last: the phrases that name a day only because nothing else did.
    return _weak_day_words(lower, anchor)
