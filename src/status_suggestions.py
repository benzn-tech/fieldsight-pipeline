"""Status suggestions from speech: which proposals the extractor made survive.

Spec: docs/superpowers/specs/2026-10-27-action-status-suggestions-design.md.

The extraction call may say, per action item, that speech in THIS session reported it done,
no longer needed (closed), blocked or started. Code -- not the prompt -- decides what survives:
the shape, that the item was carried by identity from an earlier pass of the same session
(Part C: never by text), and that the quoted words are really in the transcript. Nothing here
writes a status; a person accepts or dismisses a suggestion.

Pure: no psycopg, no boto3, no LLM. The quote check is injected so every rule is unit-testable.
"""
from item_continuity import KINDS

FIELD = "status_suggestion"
STATUSES = ("done", "closed", "blocked", "in_progress")
_KEEP_EVIDENCE = ("verified", "verified_fuzzy")

INSTRUCTION = (
    "For an action item that continues an earlier item, you may add \"status_suggestion\": "
    "{\"status\": \"done | closed | blocked | in_progress\", \"at\": \"HH:MM:SS\", "
    "\"quote\": \"...\"} -- ONLY when someone in THIS transcript says that work is finished "
    "(done), no longer needed or cancelled (closed), blocked, or started (in_progress). An "
    "intention or a plan is not a status: \"I'll send it tomorrow\" is not done. `quote` is the "
    "WHOLE line it was said in, at least five words, copied VERBATIM, errors included. `at` is "
    "the [HH:MM:SS] printed on that line -- copy all three fields exactly as printed, THE HOUR "
    "INCLUDED. Otherwise omit \"status_suggestion\"."
)


def _children(topics):
    for topic in topics if isinstance(topics, list) else []:
        if not isinstance(topic, dict):
            continue
        for list_name in KINDS:
            children = topic.get(list_name)
            if not isinstance(children, list):
                continue
            for child in children:
                if isinstance(child, dict):
                    yield list_name, child


def strip_all(topics):
    """Remove every suggestion: the flag-off, no-prior, fresh-ids and group paths. Returns how
    many were removed."""
    n = 0
    for _ln, child in _children(topics):
        if child.pop(FIELD, None) is not None:
            n += 1
    return n


def _shape_ok(s):
    return (isinstance(s, dict) and s.get("status") in STATUSES
            and isinstance(s.get("quote"), str) and s["quote"].strip()
            and isinstance(s.get("at"), str) and s["at"].strip())


def filter_suggestions(topics, prior, check):
    """Keep a suggestion only where every rule passes; pop it everywhere else. Run AFTER
    item_continuity.resolve, which is what decides inheritance.

    Rules, in order (the first failure names the drop):
      shape        -- status one of STATUSES, non-empty quote and at
      wrong_kind   -- only action items carry a status
      not_carried  -- the child's item_id is not one the prior pass published (resolve gave it a
                      fresh uuid: no exact match, no accepted claim). Part C.3.
      <evidence>   -- check(quote, at) must return status verified / verified_fuzzy; weak,
                      unverified and unchecked drop under their own name.

    `check(quote, at)` returns the evidence_match.check_quote shape plus an optional
    `segment_key`; it must not raise (the caller wraps it). A kept suggestion is rewritten to
    {status, quote, at, segment_key, offset_sec, evidence}. Returns the counts that go in the
    artifact: {offered, kept, dropped_by: {reason: n}}. Never logs or returns quote text."""
    carried = {p.item_id for p in prior if p.list_name == "action_items"}
    counts = {"offered": 0, "kept": 0, "dropped_by": {}}

    def drop(child, reason):
        child.pop(FIELD, None)
        counts["dropped_by"][reason] = counts["dropped_by"].get(reason, 0) + 1

    for list_name, child in _children(topics):
        s = child.get(FIELD)
        if s is None:
            continue
        counts["offered"] += 1
        if not _shape_ok(s):
            drop(child, "shape")
        elif list_name != "action_items":
            drop(child, "wrong_kind")
        elif child.get("item_id") not in carried:
            drop(child, "not_carried")
        else:
            r = check(s["quote"], s["at"]) or {}
            status = r.get("status") or "unchecked"
            if status not in _KEEP_EVIDENCE:
                drop(child, status)
                continue
            child[FIELD] = {"status": s["status"], "quote": s["quote"].strip(),
                            "at": s["at"].strip(), "segment_key": r.get("segment_key"),
                            "offset_sec": r.get("offset_sec"), "evidence": status}
            counts["kept"] += 1
    return counts
