"""metric_render.py -- a measured number, written as a sentence.

Pure. No boto3, no psycopg, no network, no model. That last one is the point:
the whole metric route exists because a model asked "how long did I record
yesterday" answers "about two hours" with the fluency of a fact. Handing the
number to a model to phrase would put the fabrication back one step later.

THE LANGUAGE FOLLOWS THE QUESTION. A person who asks in Chinese and is answered
"1 hour 17 minutes" has been given a worse answer than the RAG path would have
produced, because the model there follows the question's language for free. The
test is CJK characters in the question -- not a locale header, which is the
browser's language and not the asker's.

THE CAVEATS ARE PRINTED ONLY WHEN THEY ARE NOT ZERO. `unlabelled` is 0 on 189
of 189 findings live, so "and 0 unclassified" would appear on every answer
forever, and a caveat that always appears stops being read. What remains is a
number that is short arriving with the reason attached.

Spec: docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md
"""
import re

__all__ = ["render", "is_cjk", "duration_phrase"]

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def is_cjk(question) -> bool:
    return bool(_CJK.search(question or ""))


def duration_phrase(seconds, zh=False) -> str:
    """Seconds as a person says them.

    Rounded to the minute above an hour and to the second below a minute: "1
    hour 17 minutes" is what someone wants to hear, "4620 seconds" is what the
    column holds, and "1.28 hours" is neither.
    """
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0 分钟" if zh else "no time"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if zh:
        if h:
            return f"{h} 小时 {m} 分钟" if m else f"{h} 小时"
        if m:
            return f"{m} 分钟"
        return f"{s} 秒"
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if not parts:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts)


def _when(date_from, date_to, zh):
    if not date_from:
        return ""
    if date_to and date_to != date_from:
        return f"{date_from} 到 {date_to}" if zh else f"between {date_from} and {date_to}"
    return f"{date_from}" if zh else f"on {date_from}"


# Every zero kind gets its OWN sentence, because "0" has more than one cause and
# two of them read identically to a person. `no_rows_for_that_day` is the one
# this table exists for: topics are there and `recordings` rows are not, which is
# 15.4% of days with topics, and answering it with "you recorded nothing" is the
# misleading zero `lambda_org_api` was changed to stop producing.
_ZERO = {
    "nothing_visible": (
        "You do not have access to any project data yet, so there is nothing to count.",
        "你还没有任何项目的访问权限，所以没有可统计的内容。"),
    # Deliberately says nothing about LENGTH: this kind is reachable for every
    # recordings metric, and "the length cannot be measured" is a duration
    # sentence that came out under a photo question on the first TEST run.
    "no_rows_for_that_day": (
        "There are notes {when}, but no recording data was registered for it.",
        "{when}有记录内容，但没有登记录音数据。"),
    "none_of_that_kind": (
        "No {noun} {when}.",
        "{when}没有{noun}。"),
    "nothing_recorded": (
        "Nothing was recorded {when}.",
        "{when}没有录音。"),
    "no_topics": (
        "Nothing reached the system {when}.",
        "{when}没有任何内容进入系统。"),
    "none_in_domain": (
        "No {noun} were raised {when}.",
        "{when}没有{noun}。"),
}

# (plural, chinese, singular). English counts agree with their number -- "1
# recordings on 2026-08-13" was the first sentence this route produced on TEST
# for a real day, and a number that cannot agree with its own noun reads as
# machine output rather than an answer. Chinese has no such agreement.
_NOUN = {
    "count_findings_safety": ("safety issues", "安全问题", "safety issue"),
    "count_findings_quality": ("quality issues", "质量问题", "quality issue"),
    "count_sessions": ("recordings", "录音", "recording"),
    "count_photos": ("photos", "照片", "photo"),
    "duration": ("recording time", "录音时长", "recording time"),
}

# Chinese counts need a measure word between the number and the noun. Without
# one, "一共 1 录音" is what TEST produced -- grammatical nonsense from a
# template that assumed the English shape.
_MEASURE = {
    "count_sessions": "段",
    "count_photos": "张",
    "count_findings_safety": "个",
    "count_findings_quality": "个",
}


def _noun(metric, zh, n=0):
    forms = _NOUN.get(metric, ("items", "条目", "item"))
    if zh:
        return forms[1]
    return forms[2] if n == 1 else forms[0]

# Written as "the number is short and here is what it is missing", never as a
# denominator. `from_fallback` is deliberately absent: which table an item came
# out of is provenance for us, not information for the person who asked.
_NOTE = {
    "unmeasured": ("{n} recording(s) had no measurable length, so the total is short.",
                   "有 {n} 段录音无法测出长度，所以总时长偏短。"),
    "unattributed": ("{n} recording(s) are not assigned to a project and are not counted.",
                     "有 {n} 段录音没有归到项目下，未计入。"),
    "null_author": ("{n} item(s) sit on notes with no recorded author.",
                    "有 {n} 条挂在没有记录作者的内容上。"),
    "unlabelled": ("{n} unclassified item(s) were not counted in either domain.",
                   "有 {n} 条未分类，两个类别都没有计入。"),
}
_NOTE_ORDER = ("unmeasured", "unattributed", "null_author", "unlabelled")


def render(question, result) -> str:
    """The sentence for one metric result. `result` is what rag-search's metric
    mode returns.

    A result carrying an error renders as an error and NEVER as a zero: "0" and
    "the count could not be run" are different answers, and a service failure
    that reads as an honest zero is how a broken feature looks healthy.
    """
    zh = is_cjk(question)
    i = 1 if zh else 0
    metric = (result or {}).get("metric")
    when = _when(result.get("from"), result.get("to"), zh)
    noun = _noun(metric, zh)

    if result.get("error") or "value" not in result:
        return ("统计没能完成，请再试一次。" if zh
                else "That count could not be completed. Please try again.")

    value = int(result.get("value") or 0)
    notes = result.get("notes") or {}
    zero_kind = notes.get("zero_kind")

    if zero_kind:
        head = _ZERO.get(zero_kind, _ZERO["nothing_recorded"])[i]
        head = head.format(when=when, noun=noun)
    elif metric == "duration":
        phrase = duration_phrase(value, zh=zh)
        head = (f"{when}你一共录了 {phrase}。" if zh
                else f"You recorded {phrase} {when}.".replace("  ", " ").strip())
    else:
        head = (f"{when}一共 {value} {_MEASURE.get(metric, '个')}{noun}。" if zh
                else f"{value} {_noun(metric, zh, value)} {when}.".replace("  ", " ").strip())

    tail = [
        _NOTE[k][i].format(n=notes[k])
        for k in _NOTE_ORDER
        if notes.get(k)
    ]
    return " ".join([head] + tail) if tail else head
