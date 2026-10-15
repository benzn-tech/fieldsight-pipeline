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
})
TIME_WORDS_ZH = frozenset({
    "昨天", "今天", "今早", "上午", "下午", "本周", "这周", "上周", "本月",
    "这个月", "上个月", "上月",
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


def is_content_free(question, today=None):
    """True when the question is only a time phrase plus generic "what
    happened" words, so nothing in it can be matched against a topic.

    Chinese is not tested on lexical_terms()' 2-character shingles: a shingle
    straddling two words ("天我" in 昨天我们做了什么) is in no word list, so the
    sentence would never read as content-free. Each unspaced run has the known
    phrases cut out of it instead, and any character left over is content.

    A spoken date or weekday ("on 3 September", "9月3日", "on Tue", "周二") is
    non-content ONLY when time_range actually anchored on it for this question
    and this `today` (query_slots.spoken_anchor gives the exact span). There is
    deliberately no global list of month names, short weekday forms, ordinals
    or 月/日/号: "until Friday", "the dec 2 slab" and "9月3号楼" do not anchor,
    so every word in them stays content.
    """
    q = question if isinstance(question, str) else ""
    hit = query_slots.spoken_anchor(q, today)
    if hit is not None:
        start, end = hit[1]
        q = q[:start] + " " + q[end:]
    q = _ISO_DATE.sub(" ", q.lower())
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
