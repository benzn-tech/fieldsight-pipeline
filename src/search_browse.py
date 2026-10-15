"""Date-browse detection for the web search box.

"What did we do yesterday?" has no topic in it: once the time phrase is
resolved to a date range (query_slots.time_range), nothing is left to match a
chunk against, and the query embeds far from every chunk. Ranking such a
question by similarity is meaningless, so search treats it as a browse of that
date range instead (see lambda_ask_agent._rag_search_list).

Pure functions only: no boto3, no I/O.
"""
import re

import query_slots
from lexical_terms import query_terms

# Words that only say WHEN. Latin terms are compared as-is; the Chinese entries
# are removed from each unspaced run as phrases (see is_content_free).
TIME_WORDS_EN = frozenset({
    "yesterday", "today", "tonight", "morning", "afternoon", "week", "weeks",
    "weekend", "month", "months", "last", "this", "past", "previous",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "day", "days",
    # Spoken dates (query_slots): month names and their 3/4-letter forms, the
    # weekday short forms, and "on". A day number or ordinal is NOT here: it
    # is only non-content inside a recognised date phrase, which
    # query_slots.mask_spoken_dates blanks out before tokenising. "Level 2" or
    # "grid 12" must stay a topic.
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar",
    "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs", "fri",
    "sat", "sun", "on",
})
TIME_WORDS_ZH = frozenset({
    "昨天", "今天", "今早", "上午", "下午", "本周", "这周", "上周", "本月",
    "这个月", "上个月", "上月",
    # Weekday words left over once mask_spoken_dates has run, e.g. a bare
    # "星期" or "礼拜" ("这个星期"). The dated forms (9月3日, 上周二) are
    # removed whole by the mask, digits included.
    "星期", "礼拜", "周末",
})

# Words that only say "tell me what happened": they name no topic.
GENERIC_WORDS_EN = frozenset({
    "happen", "happened", "happening", "did", "done", "doing", "work",
    "worked", "working", "talk", "talked", "discuss", "discussed", "anything",
    "everything", "going", "what", "we", "our", "us", "team",
})
GENERIC_WORDS_ZH = frozenset({
    "做了", "什么", "我们", "讨论", "发生", "工作",
    # Particles and the bare verb that glue the phrases above together.
    "做", "了", "的", "吗", "呢", "都", "有",
})

_NON_CONTENT_EN = TIME_WORDS_EN | GENERIC_WORDS_EN
# Longest first, so "这个月" is removed whole rather than leaving a stray "个".
_NON_CONTENT_ZH = sorted(TIME_WORDS_ZH | GENERIC_WORDS_ZH, key=len, reverse=True)

_ISO_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_UNSPACED_RUN = re.compile(r"[㐀-䶿一-鿿぀-ヿ豈-﫿]+")


def is_content_free(question):
    """True when the question is only a time phrase plus generic "what
    happened" words, so nothing in it can be matched against a topic.

    Chinese is not tested on lexical_terms()' 2-character shingles: a shingle
    straddling two words ("天我" in 昨天我们做了什么) is in no word list, so the
    sentence would never read as content-free. Each unspaced run has the known
    phrases cut out of it instead, and any character left over is content.
    """
    q = _ISO_DATE.sub(" ", (question or "").lower())
    # "3rd of Sep" / "9月3日" / "上周二": the day number, the ordinal and the
    # 月/日/号 characters are part of the date, not a topic.
    q = query_slots.mask_spoken_dates(q)
    latin = [t for t in query_terms(q) if t not in _NON_CONTENT_EN
             and not _UNSPACED_RUN.fullmatch(t)]
    if latin:
        return False
    for run in _UNSPACED_RUN.findall(q):
        for w in _NON_CONTENT_ZH:
            run = run.replace(w, "")
        if run:
            return False
    return True


def start_seconds(time_range):
    """Start of a topic's "HH:MM[:SS] - HH:MM" range in seconds, or None."""
    first = re.split(r"[-–—]", time_range or "")[0].strip() if isinstance(
        time_range, str) else ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", first)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
