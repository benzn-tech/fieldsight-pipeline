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

__all__ = ["resolve_today", "time_range", "span", "has_time_anchor", "mask_spoken_dates"]


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

# --- Spoken dates -----------------------------------------------------------
# Day + month, as people say them: "3 September", "3rd of Sep", "Sept 3rd",
# "Sep. 3", "9月3日", "9月3号", optionally with a year. A month word on its own
# is never an anchor -- only a month word glued to a day number is.
_MONTH = (r"(?P<mon{n}>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may{may}|june?|july?"
          r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_ORD = r"(?:st|nd|rd|th)?"
# "3 May" is a date but "level 3 may need propping" is not: after a day
# number, "may" counts only when no word follows it.
_MAY_AFTER_DAY = r"(?!\s+[a-z])"
_DAY_MONTH = re.compile(
    # "3 September", "3rd of Sep", "3 September 2026"
    r"\b(?P<d1>\d{1,2})" + _ORD + r"\s+(?:of\s+)?" + _MONTH.format(n=1, may=_MAY_AFTER_DAY)
    + r"(?:,?\s+(?P<y1>\d{4})\b)?"
    # "September 3", "Sept 3rd", "Sep. 3", "September 3, 2026"
    + r"|\b" + _MONTH.format(n=2, may="") + r"\s*(?P<d2>\d{1,2})" + _ORD + r"\b"
    + r"(?:,?\s+(?P<y2>\d{4})\b)?"
    # "9月3日", "2026年9月3日", "9月3号"
    + r"|(?:(?P<y3>\d{4})\s*年\s*)?(?P<m3>\d{1,2})\s*月\s*(?P<d3>\d{1,2})\s*[日号號]",
    re.IGNORECASE,
)
_MONTH_NUM = {k: i + 1 for i, k in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

# Weekdays. Full names may stand alone; the short forms ("Tue", "Sat", "Sun")
# are ordinary English words often enough ("we sat down", "sun exposure") that
# they count only behind "on" / "last" / "this". "next"/"coming" name a future
# day, which no record can be about, and "every" names no particular day, so
# they yield no anchor at all rather than being misread as the bare weekday
# that follows them.
_WD_FULL = r"(?P<full>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
_WD_ABBR = r"(?P<abbr>mon|tues?|weds?|thu(?:rs?)?|fri|sat|sun)\b\.?"
_WEEKDAY_EN = re.compile(
    r"\b(?:(?P<mod>on|last|this|next|coming|every)\s+)?" + _WD_FULL
    + r"|\b(?P<mod2>on|last|this|next|coming|every)\s+" + _WD_ABBR,
    re.IGNORECASE,
)
_WD_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Chinese: 周二 / 星期二 / 礼拜二, optionally behind 上(个) (previous calendar
# week), 这(个)/本 (this week) or 下(个) (next week -> no anchor). The
# lookbehind keeps 每周三次 ("three times a week") and 两周 ... from reading as
# Wednesday; the lookahead keeps 周三次 / 星期一个 ... out for the same reason.
_WEEKDAY_ZH = re.compile(
    r"(?P<pre>上个?|这个?|這個?|本|下个?)?(?<![每一两二三四五六七八九十几])"
    r"(?:周|週|星期|礼拜|禮拜)(?P<zd>[一二三四五六日天])(?![次个個天年遍回])"
)
_ZH_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _day_month(m, today):
    """(date or None, matched) for a _DAY_MONTH match. A date with a year needs
    no anchor; without one it is the most recent such day on or before
    `today`. An impossible date (31 Sep) is None -- no anchor, never a guess."""
    if m.group("m3"):
        month, day, year = int(m.group("m3")), int(m.group("d3")), m.group("y3")
    else:
        mon = (m.group("mon1") or m.group("mon2")).lower()[:3]
        month = _MONTH_NUM[mon]
        day = int(m.group("d1") or m.group("d2"))
        year = m.group("y1") or m.group("y2")
    if year:
        y = int(year)
    elif isinstance(today, date):
        y = today.year if (month, day) <= (today.month, today.day) else today.year - 1
    else:
        return None
    try:
        return date(y, month, day)
    except ValueError:
        return None


def _weekday(text, today):
    """The day a weekday phrase names, or None. Callers reach this only after
    every more specific rule has missed."""
    m = _WEEKDAY_ZH.search(text)
    if m:
        wd = _ZH_INDEX[m.group("zd")]
        pre = (m.group("pre") or "")[:1]
        if pre == "上":
            return _monday_of(today) - timedelta(days=7) + timedelta(days=wd)
        mod = {"这": "this", "這": "this", "本": "this", "下": "next"}.get(pre, "")
    else:
        m = _WEEKDAY_EN.search(text)
        if not m:
            return None
        wd = _WD_INDEX[(m.group("full") or m.group("abbr")).lower()[:3]]
        mod = (m.group("mod") or m.group("mod2") or "").lower()
    if mod in ("next", "coming", "every"):
        return None
    if mod == "this":
        d = _monday_of(today) + timedelta(days=wd)
        return d if d <= today else None
    # Bare, "on", "last": the most recent such weekday STRICTLY before today --
    # "Monday" said on a Monday means a week ago, not this morning.
    back = (today.weekday() - wd) % 7 or 7
    return today - timedelta(days=back)


def mask_spoken_dates(text):
    """`text` with every day+month(+year) and weekday phrase blanked out --
    for search_browse, so the day number and ordinal inside "on the 3rd of
    Sep" read as part of the date rather than as content."""
    text = text if isinstance(text, str) else ""
    for pattern in (_DAY_MONTH, _WEEKDAY_ZH, _WEEKDAY_EN):
        text = pattern.sub(" ", text)
    return text


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


def time_range(question, today):
    """(date_from, date_to) as ISO strings, or (None, None).

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

    # 1b. A spoken day + month. With a year it needs no anchor; without one it
    #     is relative to today and _day_month returns None when there is none.
    #     An impossible date is no anchor from this rule and falls through.
    m = _DAY_MONTH.search(text)
    if m:
        d = _day_month(m, today)
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

    # 2b. 上周二 / 这周二 contain 上周 / 这周, but name ONE day: they are the
    #     more specific phrase, so they are read before the week phrases below.
    #     (English has no such compound -- "last week on Tuesday" stays last
    #     week, see 4.)
    m = _WEEKDAY_ZH.search(text)
    if m and m.group("pre"):
        d = _weekday(text, today)
        return (_iso(d), _iso(d)) if d is not None else (None, None)

    # 3. Named periods, most specific first.
    for pattern, name in _PHRASES:
        if pattern.search(text):
            start, end = _span(today, name)
            return _iso(start), _iso(end)

    # 4. A weekday name, last: it never narrows a phrase matched above, so
    #    "last week on Tuesday" keeps the whole of last week and "yesterday,
    #    Tuesday" keeps yesterday.
    d = _weekday(text, today)
    if d is not None:
        return _iso(d), _iso(d)

    return None, None


def has_time_anchor(question, today):
    """True iff `time_range` resolves the question to a range."""
    date_from, date_to = time_range(question, today)
    return date_from is not None or date_to is not None
