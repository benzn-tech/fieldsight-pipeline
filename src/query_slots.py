"""query_slots.py — the time slot Ask reads out of a question.

Ask retrieves by semantic distance alone, so "what happened yesterday" is a
query with almost no semantic content: the k nearest chunks come back from
whatever dates happen to be near it, and the model — which is never told what
day it is — summarises all of them. Two independent causes, one symptom.

This module supplies the half that is cheap and exact: the calendar range the
question names, resolved in the CALLER'S OWN ZONE.

WHY A ZONE AND NOT A DATE. The client knows its date, and sending it would be
one fewer moving part. It is still wrong: at 12:30 UTC it is already tomorrow
in Auckland and still today in Sydney, both are on daylight saving for part of
the year, and they do not switch on the same date. A date computed anywhere but
in the caller's zone is wrong for someone several weeks a year, and Australia is
a stated market. An IANA zone id is the only form of this that does not rot.

WHY RULES AND NOT A MODEL. Ask is capped by API Gateway's 29s ceiling and the
answer already spends p90 8.6s in synthesis; a second model hop to read a date
out of a sentence would be the most expensive way to learn something a regex
knows. It is also the safer half of the trade: a rule that does not recognise a
phrase returns nothing, and nothing means "do not filter" — the pre-existing
behaviour, byte for byte. A model asked the same question can return a
confident wrong range, and a wrong range is invisible: the answer looks fine and
is about the wrong week.

PURE. No boto3, no psycopg, no network. Import costs nothing.
"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = ["resolve_today", "time_range", "span", "has_time_anchor", "spoken_anchor"]


def resolve_today(tz_name, now=None):
    """The caller's local calendar day, or None when the zone is unusable.

    None is a real answer and not an error: it flows through `time_range` as
    "no anchor", which yields no range, which leaves the search unfiltered. A
    caller with a broken zone gets exactly what every caller gets today.

    Deliberately broad except: this runs on the request path, the zone string
    comes from a client, and every failure mode of ZoneInfo (missing tzdata,
    an offset string like "GMT+12", a non-string) has the same correct
    response.
    """
    if not isinstance(tz_name, str) or not tz_name.strip():
        return None
    try:
        tz = ZoneInfo(tz_name.strip())
    except Exception:
        return None
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(tz).date()


# An explicit date needs no anchor, so it is matched first and separately.
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# A window counted in units: "最近7天", "过去 3 天", "last 14 days", "past 2 weeks".
# Checked BEFORE the bare week/month words so "past 2 weeks" is two weeks and
# not last week.
_WINDOW = re.compile(
    r"(?:最近|过去|近)\s*(\d{1,3})\s*(天|周|星期|个月)"
    r"|(?:last|past|previous)\s+(\d{1,3})\s*(day|week|month)s?",
    re.IGNORECASE,
)

# Ordered most specific first. Each entry is (pattern, handler-name).
_PHRASES = (
    (re.compile(r"前天|\bday\s+before\s+yesterday\b", re.IGNORECASE), "day_before_yesterday"),
    (re.compile(r"昨天|昨日|\byesterday\b", re.IGNORECASE), "yesterday"),
    (re.compile(r"今天|今日|\btoday\b", re.IGNORECASE), "today"),
    (re.compile(r"上周|上星期|上个星期|\blast\s+week\b", re.IGNORECASE), "last_week"),
    (re.compile(r"这周|本周|这星期|本星期|这个星期|\bthis\s+week\b", re.IGNORECASE), "this_week"),
    (re.compile(r"上个月|上月|\blast\s+month\b", re.IGNORECASE), "last_month"),
    (re.compile(r"这个月|本月|这月|\bthis\s+month\b", re.IGNORECASE), "this_month"),
)

_UNIT_DAYS = {"天": 1, "day": 1, "周": 7, "星期": 7, "week": 7, "个月": 30, "month": 30}


def _iso(d):
    return d.isoformat()


def _monday_of(d):
    return d - timedelta(days=d.weekday())


def span(today, name):
    if name == "today":
        return today, today
    if name == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if name == "day_before_yesterday":
        d = today - timedelta(days=2)
        return d, d
    if name == "this_week":
        return _monday_of(today), today
    if name == "last_week":
        this_monday = _monday_of(today)
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if name == "this_month":
        return today.replace(day=1), today
    if name == "last_month":
        last_day = today.replace(day=1) - timedelta(days=1)
        return last_day.replace(day=1), last_day
    raise AssertionError(f"unhandled span {name!r}")   # unreachable; loud if it ever is


_span = span  # old private name, kept for existing callers


def _time_range_v1(question, today):
    """The original rules, unchanged since 86262261 -- see `time_range`.

    (date_from, date_to) as ISO strings, or (None, None).

    (None, None) is the common case and the safe one: it is what a question
    with no time expression returns, and what a question WITH one returns when
    the zone gave us no anchor to resolve it against. Both mean "search
    everything", which is what Ask does today.

    Inclusive on both ends, matching the SQL: report_date >= from AND <= to.
    Windows counted in units include today, so "最近 7 天" is today and the six
    days before it -- seven days, the way a person counts them.
    """
    text = question if isinstance(question, str) else ""
    if not text.strip():
        return None, None

    # 1. An explicit date. Needs no anchor, and beats a relative word in the
    #    same sentence -- "昨天提到的 2026-08-12 那次" is about the 12th.
    m = _ISO.search(text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            d = None                      # 2026-13-45 is not a date; fall through
        if d is not None:
            return _iso(d), _iso(d)

    # Everything below is relative, so it needs a day to be relative TO.
    if not isinstance(today, date):
        return None, None

    # 2. A counted window: "past 2 weeks" is two weeks, not last week, so this
    #    runs before the bare week/month phrases.
    m = _WINDOW.search(text)
    if m:
        count = m.group(1) or m.group(3)
        unit = (m.group(2) or m.group(4) or "").lower()
        days = _UNIT_DAYS.get(unit)
        if days and count:
            n = int(count)
            if n > 0:
                start = today - timedelta(days=n * days - 1)
                return _iso(start), _iso(today)

    # 3. Named periods, most specific first.
    for pattern, name in _PHRASES:
        if pattern.search(text):
            start, end = _span(today, name)
            return _iso(start), _iso(end)

    return None, None


# ==========================================================================
# "last <weekday>" (2026-10-16): the one spoken-date rule kept.
#
# WHY ONLY THIS ONE. Two review rounds showed that regexes cannot tell a past
# day from a future one ("we pour on Friday"), a deadline from an event ("due
# by 30 November", 11月30日前), a habit from an instance (逢周二), or a date
# from a label ("Rev 3 Mar"). Each new guard uncovered a new class of misread.
# A wrong range silently filters Ask to the wrong day, which is worse than no
# range. So general spoken dates are left to the Ask judge model, which knows
# today and writes ISO dates into tool arguments that the tool layer
# validates and caps. "last Tuesday" is kept because it is unambiguous: it
# always names a past day.
#
# OLD ANCHORS ARE IMMUTABLE. `time_range` runs the original parser
# (`_time_range_v1`, byte-identical in behaviour to 86262261) first, and this
# rule runs ONLY when that returned (None, None). "last week on Tuesday"
# stays last week. Pinned against a frozen copy of the old module in
# tests/unit/test_query_slots_old_anchors.py.
# ==========================================================================

# Full weekday names, or a 3-letter form, directly after "last". Not when it
# continues into a range ("last Monday to Friday", "last Mon-Fri"), a
# possessive or plural ("last Sunday's slab", "last Mondays"), a part of day in
# the plural (a habit), "of" ("the last Friday of the month"), or a street.
_LAST_WEEKDAY = re.compile(
    r"\blast\s+(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tue|wed|thu|fri|sat|sun)\b\.?"
    r"(?!['’]s?\b|s\b|\s*[-–—~]|\s+(?:to|till|until|through|thru|of)\b"
    r"|\s+(?:mornings|afternoons|evenings|nights)\b"
    r"|\s+(?:street|st|road|rd|avenue|ave|lane)\b)",
    re.IGNORECASE,
)
# "since last Monday" / "until last Friday" name a range or a bound, not the day.
_LAST_WEEKDAY_BLOCKED_BEFORE = re.compile(
    r"\b(?:since|until|till|from|before|after|by|due)\s+$", re.IGNORECASE)
_WD_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def spoken_anchor(question, today):
    """(date, (start, end)) when `time_range` anchored on "last <weekday>" for
    this question, else None. search_browse uses the span to treat exactly
    those characters -- and nothing else -- as non-content.

    The day is the most recent such weekday STRICTLY before today: said on a
    Monday, "last Monday" is a week ago, not this morning."""
    text = question if isinstance(question, str) else ""
    if not isinstance(today, date) or not text.strip():
        return None
    if _time_range_v1(text, today) != (None, None):
        return None
    for m in _LAST_WEEKDAY.finditer(text):
        if _LAST_WEEKDAY_BLOCKED_BEFORE.search(text[:m.start()]):
            continue
        wd = _WD_INDEX[m.group("wd").lower()[:3]]
        back = (today.weekday() - wd) % 7 or 7
        return today - timedelta(days=back), m.span()
    return None


def time_range(question, today):
    """(date_from, date_to) as ISO strings, or (None, None).

    The original rules (`_time_range_v1`) run first and their answer is final;
    "last <weekday>" only fills a gap they left. See the block comment above.
    """
    result = _time_range_v1(question, today)
    if result != (None, None):
        return result
    hit = spoken_anchor(question, today)
    if hit is None:
        return None, None
    return _iso(hit[0]), _iso(hit[0])


def has_time_anchor(question, today):
    """True iff `time_range` resolves the question to a range."""
    date_from, date_to = time_range(question, today)
    return date_from is not None or date_to is not None
