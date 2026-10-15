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
# Spoken dates: day + month, and weekday names (2026-10-16).
#
# PRECISION OVER RECALL. A wrong range silently filters Ask to the wrong day,
# which is strictly worse than no range (search everything). Every rule below
# therefore fires only on a narrow, closed shape and returns nothing when in
# doubt. A site sentence is full of words that look like dates and are not:
# "Level 3 May inspection", "march 3 crews", "dec 2 slab", "Monday Street",
# "due Friday", "上周二层浇筑", "9月3号楼".
#
# OLD ANCHORS ARE IMMUTABLE. `time_range` runs the original parser
# (`_time_range_v1`, byte-identical to 86262261) first, and these rules run
# ONLY when it returned (None, None). So any sentence that anchored before
# keeps exactly the range it had: "上周二" is still the whole of last week
# (上周 wins), "last week on Tuesday" is still last week, and "昨天9月3号楼"
# is still yesterday. Pinned against a frozen copy of the old module in
# tests/unit/test_query_slots_old_anchors.py.
# ==========================================================================

# --- Day + month ----------------------------------------------------------
# "3 September", "3rd of Sep", "September 3", "Sept 3rd", "Sep. 3", optionally
# with a year. The month word is only a month when it is glued to a day
# number, AND the date ends the noun phrase: what follows must be the end of
# the text, punctuation, or one of a few words that cannot continue a noun
# phrase. "3 Sep pour" is therefore NOT a date -- losing it is the price of
# never reading "the dec 2 slab" or "march 3 crews" as one.
_MONTH = (r"(?P<mon{n}>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
          r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?")
_ORD = r"(?:st|nd|rd|th)?"
_YEAR = r"(?:,?\s+(?P<y{n}>\d{{4}})\b)?"
_DATE_END = (r"(?=\s*$|\s*[^\w\s]|\s+(?:and|or|at|in|on|we|i|they|did|was|were|what"
             r"|when|about)\b)")
_DAY_MONTH_EN = re.compile(
    r"\b(?P<d1>\d{1,2})" + _ORD + r"\s+(?:of\s+)?" + _MONTH.format(n=1)
    + _YEAR.format(n=1) + _DATE_END
    + r"|\b" + _MONTH.format(n=2) + r"\s*(?P<d2>\d{1,2})" + _ORD + r"\b"
    + _YEAR.format(n=2) + _DATE_END,
    re.IGNORECASE,
)
# "9月3日", "2026年9月3日", "9月3号". 号 also numbers buildings, lines,
# machines and piles (9月3号楼 = "building 3 in September"), so a 号/日 that
# a word-forming character follows is not a date.
_DAY_MONTH_ZH = re.compile(
    r"(?:(?P<y3>\d{4})\s*年\s*)?(?<!\d)(?P<m3>\d{1,2})\s*月\s*(?P<d3>\d{1,2})\s*[日号號]"
    r"(?![楼樓线線机機桩樁栋棟层層门門座房室厅廳院区區段井位台车車口间間单單元])"
)
_MONTH_NUM = {k: i + 1 for i, k in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}

# --- English weekdays -------------------------------------------------------
# ONLY behind "on" / "last" / "this". A bare weekday is too often a deadline
# ("due Friday"), a habit ("Monday to Friday"), or a name ("Friday Lane").
# Sat/Sun are ordinary words ("sat down", "sun deck"), so their short forms
# need "last"/"this"; "on Sat scaffold" is not a date.
_WD_EN = re.compile(
    r"\b(?P<mod>on|last|this)\s+"
    r"(?:(?P<full>monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|(?P<abbr>mon|tues?|weds?|thu(?:rs?)?|fri)\.?"
    r"|(?<=(?:last|this)\s)(?P<ss>sat|sun)\.?"
    r")\b"
    # what may NOT follow: a possessive or plural, a range, a part of day in
    # the plural (a habit), a street name
    r"(?!['’]s?\b|s\b|\s*[-–—~]|\s+(?:to|till|until|through|thru)\b"
    r"|\s+(?:mornings|afternoons|evenings|nights)\b"
    r"|\s+(?:street|st|road|rd|avenue|ave|lane)\b|\s+as\s+usual\b)",
    re.IGNORECASE,
)
# What may NOT precede "on/last/this <weekday>": a deadline, a range start, a
# habit or a future. "due on Friday", "until this Friday", "every last ...".
_WD_EN_BLOCKED_BEFORE = re.compile(
    r"\b(?:due|by|until|till|before|from|to|every|each|next|coming|following|good)\s+$",
    re.IGNORECASE,
)
_WD_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# --- Chinese weekdays -------------------------------------------------------
# 周二 / 星期二 / 礼拜二, optionally behind 上(个) / 这(个) / 本. 上周X and 这周X
# never reach here (the old 上周/这周 phrases anchor them first, and that is
# final). The weekday character is also a numeral and 日/天 are nouns, so
# 周二层 / 周日报 / 周五金 / 周天气 are words, not days: the weekday anchors
# only when the text ends there, punctuation follows, or one of a short list
# of particles / pronouns / verbs / parts of day starts the next word. That
# list also excludes 到 / 至 (a range: 周一到周五) and 次 (一周一次).
# 每 / 每个 / 下 / 下个 before it (a habit, the future) -> no anchor, and
# neither end of a range anchors: 到 / 至 / ~ / - before the weekday as well.
_WD_ZH = re.compile(
    r"(?P<pre>上个|上個|上|这个|這個|这|這|本)?"
    r"(?<![每下两兩几幾一二三四五六七八九十到至~～\-—–])(?<!每个)(?<!每個)(?<!下个)(?<!下個)"
    r"(?:周|週|星期|礼拜|禮拜)(?P<zd>[一二三四五六日天])"
    r"(?=$|[\s，。,.!?！？、；;：:]"
    r"|的|了|吗|嗎|呢|吧|我|你|他|她|咱|大家|有|做|干|幹|发生|發生|开会|開會"
    r"|讨论|討論|说|說|谈|談|在|和|跟|与|與|那|上午|下午|早上|晚上|中午|都|是"
    r"|怎|什么|什麼|见|見|去|来|來)"
)
_ZH_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _plausible_year(y, today):
    """A 4-digit number after a date is a year only when it is 19xx/20xx and,
    with an anchor, within one year of it -- "Sept 3 2000 litres" is litres."""
    if not 1900 <= y <= 2099:
        return False
    return not isinstance(today, date) or abs(y - today.year) <= 1


def _spoken_day_month(text, today):
    """(date, span) for the first well-formed day+month, else None.

    Without a year: the most recent such day on or before `today` (else last
    year). An impossible date -- 31 Sep, 30 Feb, and 29 Feb with no year in a
    non-leap year (deliberately None rather than stepping back to the last
    leap year) -- is None. A year that is not plausible voids the anchor."""
    for pattern in (_DAY_MONTH_EN, _DAY_MONTH_ZH):
        m = pattern.search(text)
        if not m:
            continue
        g = m.groupdict()
        if g.get("m3"):
            month, day, year = int(g["m3"]), int(g["d3"]), g.get("y3")
        else:
            month = _MONTH_NUM[(g["mon1"] or g["mon2"]).lower()[:3]]
            day = int(g["d1"] or g["d2"])
            year = g.get("y1") or g.get("y2")
        if year:
            y = int(year)
            if not _plausible_year(y, today):
                return None
        elif isinstance(today, date):
            y = today.year if (month, day) <= (today.month, today.day) else today.year - 1
        else:
            return None
        try:
            return date(y, month, day), m.span()
        except ValueError:
            return None
    return None


def _spoken_weekday(text, today):
    """(date, span) for a weekday phrase, else None.

    "on X" / "last X" / bare Chinese 周X: the most recent X STRICTLY before
    today -- said on a Monday, "on Monday" is a week ago, not this morning.
    "this X" / 这(个)X / 本X: X of the current Mon-Sun week, only if <= today.
    上礼拜X / 上个周X (the 上 forms the old 上周/上星期 phrases do not cover): X
    of the previous calendar week."""
    m = _WD_ZH.search(text)
    if m:
        wd = _ZH_INDEX[m.group("zd")]
        pre = (m.group("pre") or "")[:1]
        mod = {"上": "prev", "这": "this", "這": "this", "本": "this"}.get(pre, "")
    else:
        m = None
        for cand in _WD_EN.finditer(text):
            if not _WD_EN_BLOCKED_BEFORE.search(text[:cand.start()]):
                m = cand
                break
        if m is None:
            return None
        word = m.group("full") or m.group("abbr") or m.group("ss")
        wd = _WD_INDEX[word.lower()[:3]]
        mod = "this" if m.group("mod").lower() == "this" else ""
    monday = _monday_of(today)
    if mod == "prev":
        return monday - timedelta(days=7) + timedelta(days=wd), m.span()
    if mod == "this":
        d = monday + timedelta(days=wd)
        return (d, m.span()) if d <= today else None
    back = (today.weekday() - wd) % 7 or 7
    return today - timedelta(days=back), m.span()


def spoken_anchor(question, today):
    """(date, (start, end)) when `time_range` anchored on a NEW spoken-date
    rule for this question, else None. search_browse uses the span to treat
    exactly those characters -- and nothing else -- as non-content."""
    text = question if isinstance(question, str) else ""
    if not text.strip() or _time_range_v1(text, today) != (None, None):
        return None
    hit = _spoken_day_month(text, today)
    if hit is None and isinstance(today, date):
        hit = _spoken_weekday(text, today)
    return hit


def time_range(question, today):
    """(date_from, date_to) as ISO strings, or (None, None).

    The original rules (`_time_range_v1`) run first and their answer is final;
    the spoken day+month and weekday rules only fill a gap they left. See the
    block comment above `_MONTH`.
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
