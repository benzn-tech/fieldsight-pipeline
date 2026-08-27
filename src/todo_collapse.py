"""One commitment, said in several recordings — collapsed at read time.

Spec: docs/superpowers/specs/2026-08-17-todo-dedupe-and-threading.md

On 2026-08-10 three separate recordings each produced a byte-identical
`Scaffolding -- inspect before Monday`, in three different topics. One man said
it once per recording; every extraction is faithful to its own audio. Nothing
upstream is broken, and the todo list shows the job three times.

**This collapses on READ and writes nothing.** `action_items` cascade-delete
with their topic, and `write_extraction_items` deletes and re-inserts every
topic for a source key on every pass — the live tier re-runs the same key on a
~90 s throttle while recording (BUG-43). A stored pointer between two todo rows
therefore spans two independently churning lifetimes: depending on its
`ON DELETE` clause it is a foreign-key error raised inside the prod write path
while somebody is recording, a merge that silently undoes itself within the
hour, or one key's rewrite destroying another key's rows. There is no durable
per-todo identity in this store — `id`, `topic_id` and `text` all churn — so
recomputing on every read is not a shortcut, it is the only shape that survives.

**The duplicates live in DIFFERENT topics.** Collapsing within one topic catches
none of them, which is the implementation a first reading reaches for. Callers
must hand this the whole day's items, not one topic's.

Pure apart from one env read (`enabled`), which is deliberately at CALL time:
this repo's standing failure is a switch whose middle segment goes missing and
silently takes the default, and a module-import read cannot be monkeypatched by
the tests that would catch it.
"""
import os

# Trailing punctuation only, and CJK marks alongside the Latin ones: these
# recordings are routinely Chinese, and a rule that only knows `.` would treat
# `检查脚手架。` and `检查脚手架` as two commitments.
_TRAILING = " \t.,;:!?-–—。，、；：！？…"

# Statuses that may collapse. A closed item and an open one with the same words
# are NOT the same commitment — one of them was dealt with, and merging them
# would hide the open one behind a tick.
_COLLAPSIBLE_STATUS = "open"


def _status(row):
    """A row with NO status is open.

    Database rows always carry one. The rows that do not are the ones read
    straight out of an extraction artifact — the shape the stop-recording email
    builds its list from — and a freshly extracted commitment is open by
    definition; nothing has had the chance to close it. Reading a missing field
    as "not open" would have quietly excluded that surface from the collapse
    while every test on the database side stayed green, which is the shape of
    half the defects in this repo's own list.
    """
    return (row.get("status") or _COLLAPSIBLE_STATUS)


def enabled():
    """`ENABLE_TODO_COLLAPSE`, default OFF, read on every call.

    Off is the safe direction and that is the point: a variable that is unset,
    misspelled, or dropped from one of the two workflows leaves the lists
    exactly as they are today rather than silently changing what a customer's
    todo list says.
    """
    return os.environ.get("ENABLE_TODO_COLLAPSE", "false").strip().lower() == "true"


def collapse_if_enabled(rows):
    """The only entry point read paths should use.

    Returns a copy either way, so a caller cannot come to depend on the
    identity of the list it was handed — the switch changing that would be a
    second, invisible behaviour change.
    """
    return collapse(rows) if enabled() else [dict(r) for r in (rows or [])]


def normalise(text):
    """The comparison key. Case, whitespace and trailing punctuation only.

    ⚠ It deliberately does NOT strip to `[a-z0-9]` or to ASCII. This codebase
    has shipped that three times and each time every CJK character collapsed to
    the empty string, which makes two unrelated Chinese todos compare EQUAL —
    the failure is a silent merge of different commitments, not a missed one
    (memory: fieldsight-ascii-norm-erased-chinese).

    `split()` handles every Unicode space without a regex; `lower()` is a no-op
    on Han characters rather than a mangling. Returns "" for text that is blank
    or entirely punctuation, and the caller treats "" as "do not collapse" —
    two todos with no words are not evidence of one todo said twice.
    """
    if text is None:
        return ""
    return " ".join(str(text).split()).lower().strip(_TRAILING)


def _order_key(row):
    """Deterministic survivor: earliest, then id.

    `created_at` alone is not enough. `occurred_at` is NULL on every extraction
    topic — no writer passes it — and `created_at` is reassigned on every
    re-extraction, so rows written in one pass share a timestamp to the
    microsecond. Without the id tiebreak the survivor flips between identical
    runs and the collapse stops being idempotent, while any count of it stays
    flat and reports nothing wrong.
    """
    return (str(row.get("created_at") or ""), str(row.get("id") or ""))


def collapse(rows):
    """Collapse repeated open commitments, newest information preserved.

    Returns NEW dicts — the same row list is handed to several consumers within
    one request, and mutating it would make the result depend on which consumer
    ran first.

    The survivor carries:
      * `mention_count`  — how many recordings carried this commitment (1 when
        it was said once, so an ordinary row does not read as "raised once");
      * `collapsed_ids`  — the rows it stands for, so a caller can say which,
        and so a count that looks wrong can be traced to actual rows.
    """
    rows = list(rows or [])
    groups = {}
    for row in rows:
        key = normalise(row.get("text"))
        if not key or _status(row) != _COLLAPSIBLE_STATUS:
            continue
        groups.setdefault(key, []).append(row)

    out = []
    seen_keys = set()
    for row in rows:
        key = normalise(row.get("text"))
        if not key or _status(row) != _COLLAPSIBLE_STATUS:
            out.append(dict(row))
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        members = sorted(groups[key], key=_order_key)
        survivor = dict(members[0])
        survivor["mention_count"] = len(members)
        survivor["collapsed_ids"] = [m.get("id") for m in members[1:]]
        out.append(survivor)
    return out


def collapsed_count(rows):
    """How many distinct open commitments these rows represent.

    The number the UI shows next to a collapsed list, and the one an aggregate
    has to agree with. A list that collapses while its counter still says three
    is worse than not collapsing: it tells the reader the feature is broken
    rather than that the work is duplicated.
    """
    return sum(1 for r in collapse(rows) if _status(r) == _COLLAPSIBLE_STATUS)
