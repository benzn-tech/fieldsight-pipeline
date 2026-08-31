"""metric_slots.py — which metric a question asks for, or nothing.

Ask retrieves text chunks and answers from them alone. "How long did I record
yesterday" has no textual answer anywhere: nobody says the duration in the
meeting, it is a number in a column. So the question needs a different route,
and this module is the switch that finds it.

RULES, NOT A CLASSIFIER, and the asymmetry is the entire argument. A rule that
MISSES returns None and the question falls through to RAG — which is what
happens today, so a miss costs nothing new. A classifier that MISFIRES answers a
different question than the one asked, with a number, confidently, and replaces
a good answer with a wrong-shaped one.

TWO GATES BEFORE ANY NOUN IS LOOKED AT, and both exist because a countable noun
on its own is not a metric question:

  * a QUANTITY interrogative must be present -- otherwise "the safety issues from
    yesterday" routes to a counter and the reader gets "3" when they asked what
    the issues were;
  * a request for the ITEMS AS WELL must be absent -- "how many safety issues and
    what were they" wants prose, and a counter answers half of it while looking
    like it answered all of it.

PURE: no boto3, no psycopg, no network. `lambda_ask_agent` imports it, and the
legacy hand-built prod bundle (a fixed file list, outside SAM) carries neither.

Spec: docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md
"""
import re

__all__ = ["detect"]

# Gate 1. English needs the interrogative spelled out; Chinese carries it in the
# measure word (几张 / 几次 / 多少), which is why the two lists look different.
_QUANTITY = re.compile(
    r"\bhow (long|much|many)\b"
    r"|\btotal (time|number|count)\b"
    r"|多少|多长|多久|几张|几次|几段|几个|几场",
    re.IGNORECASE,
)

# Gate 2a. A quantity word whose subject is something this module cannot count.
# "How much did the quality rework COST" is a money question and "how many PEOPLE
# were in the meeting" is an attendance question; both carry a quantity
# interrogative and a domain noun, and without this gate the first answers "3
# quality issues" and the second "1 recording" -- exactly the confident
# wrong-question answer the header says the design exists to prevent. Neither
# number is stored anywhere, so there is nothing to route them to.
_NOT_OURS = re.compile(
    r"\bcosts?\b|\bprice\b|\bbudget\b|\bspend\b|\bspent\b|\bdollars?\b"
    r"|\bpeople\b|\bpersons?\b|\battend\w*\b|\bwho\b|\bwhom\b"
    r"|多少钱|费用|成本|预算|多少人|几个人|几位|几名",
    re.IGNORECASE,
)

# Gate 2. Asking for the items as well as the number.
_ALSO_WANTS_ITEMS = re.compile(
    r"\band what (were|was|are|is)\b"
    r"|\bwhat (were|was) they\b"
    r"|\blist (them|these|those)\b"
    r"|都是什么|分别是|具体是什么|有哪些|是什么问题",
    re.IGNORECASE,
)

# Ordered: the DOMAIN words win. "How many safety issues did I record" is a
# findings question -- the domain is the subject and "record" is incidental, so a
# recording-shaped pattern must not get there first.
_METRICS = (
    ("count_findings_safety",  re.compile(r"\bsafety\b|安全", re.IGNORECASE)),
    ("count_findings_quality", re.compile(r"\bqa\b|\bquality\b|质量", re.IGNORECASE)),
    ("count_photos",           re.compile(r"\bphotos?\b|\bpictures?\b|\bimages?\b|照片|图片",
                                          re.IGNORECASE)),
    # `duration` before `count_sessions`: "how long did I record" contains both a
    # time word and a recording word, and it is a duration question.
    ("duration",               re.compile(r"\bhow long\b|\bhow much time\b|\bduration\b"
                                          r"|\btotal time\b|多长时间|多久|时长",
                                          re.IGNORECASE)),
    ("count_sessions",         re.compile(r"\brecordings?\b|\bsessions?\b|\bmeetings?\b"
                                          r"|录音|录了几|录制了几|几段|几次|几场",
                                          re.IGNORECASE)),
)


def detect(question):
    """The metric this question asks for, or None.

    None is the common answer and the safe one: it means "not mine", and the
    caller keeps doing exactly what it does today. Non-strings are None rather
    than an error -- this reads a field that arrives over HTTP.
    """
    text = question if isinstance(question, str) else ""
    if not text.strip():
        return None
    if not _QUANTITY.search(text):
        return None
    if _ALSO_WANTS_ITEMS.search(text):
        return None
    if _NOT_OURS.search(text):
        return None
    for name, pattern in _METRICS:
        if pattern.search(text):
            return name
    return None
