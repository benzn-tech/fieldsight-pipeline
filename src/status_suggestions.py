"""Status suggestions from speech: which proposals the extractor made survive.

Spec: docs/superpowers/specs/2026-10-27-action-status-suggestions-design.md.

The extraction call may say, per action item, that speech in THIS session reported it done,
no longer needed (closed), blocked or started. Code -- not the prompt -- decides what survives:
the shape, that the item was carried by identity from an earlier pass of the same session
(Part C: never by text), and that the quoted words are really in the transcript. Nothing here
writes a status; a person accepts or dismisses a suggestion.

Pure: no psycopg, no boto3, no LLM. The quote check is injected so every rule is unit-testable.
"""
import re

from evidence_match import is_cjk, normalise, token_count
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

# The linker call's variant (continuity linker spec L3): there the model links, rather than
# declares, continuation. Only the opening is reworded; the rest is INSTRUCTION verbatim.
_OPENING = "For an action item that continues an earlier item"
assert INSTRUCTION.startswith(_OPENING)
LINKER_INSTRUCTION = "For an action item you link to an earlier item" + INSTRUCTION[len(_OPENING):]


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


# Display trim (focus_quote). The model quotes the WHOLE line, and the whole line is what is
# verified -- but under whole-chunk transcription a line can be 90 seconds of talk covering
# several items, and the chip then shows one wall of text under every suggestion it fed.
_SHORT_ENOUGH_TOKENS = 25   # a quote this short is shown as quoted
_FOCUS_CAP_TOKENS = 40      # the focused excerpt stops growing here
_FRAGMENT_TOKENS = 6        # a lone hit this short ("The scaffold.") takes the next sentence too
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s*")
_STOP = frozenset(
    "the and for get got with that this from have has had will would can could need needs "
    "into onto out off all any are was were been being not but just yeah okay also some".split())


def _item_tokens(text):
    """Content words (latin, 3+ letters, not a stopword) plus CJK bigrams. A CJK single
    character is too common to mean anything ("的", "了")."""
    n = normalise(text)
    latin = {w for w in re.findall(r"[a-z]{3,}", n) if w not in _STOP}
    cjk = "".join(ch for ch in n if is_cjk(ch))
    return latin, {cjk[i:i + 2] for i in range(len(cjk) - 1)}


def _overlap(sentence, item_latin, item_cjk):
    latin, cjk = _item_tokens(sentence)
    hits = sum(1 for w in latin
               if any(w.startswith(t[:5]) or t.startswith(w[:5]) for t in item_latin))
    return hits + len(cjk & item_cjk)


def focus_quote(quote, item_text):
    """The part of a verified `quote` that is about this item, for display.

    Picks the sentence sharing the most content words with the item text (a crude stem: a
    shared 5-letter prefix, so "scaffolds" meets "scaffold"), widens it over adjacent sentences
    that also mention the item, and stops at _FOCUS_CAP_TOKENS. Returns the quote unchanged when
    it is already short, is one sentence, or no sentence mentions the item -- ASR that misheard
    the item's words gets the whole line, never a guess. Every sentence returned is a verbatim
    piece of the verified quote, so the excerpt is no less "heard" than the whole."""
    q = (quote or "").strip()
    if token_count(q) <= _SHORT_ENOUGH_TOKENS:
        return q
    sentences = [s.strip() for s in _SENTENCE_END.split(q) if s.strip()]
    if len(sentences) < 2:
        return q
    item_latin, item_cjk = _item_tokens(item_text)
    scores = [_overlap(s, item_latin, item_cjk) for s in sentences]
    best = max(range(len(scores)), key=lambda i: (scores[i], -i))
    if scores[best] == 0:
        return q

    def fits(a, b):
        return token_count(" ".join(sentences[a:b + 1])) <= _FOCUS_CAP_TOKENS
    lo = hi = best
    while hi + 1 < len(sentences) and scores[hi + 1] > 0 and fits(lo, hi + 1):
        hi += 1
    while lo > 0 and scores[lo - 1] > 0 and fits(lo - 1, hi):
        lo -= 1
    if lo == hi and token_count(sentences[lo]) < _FRAGMENT_TOKENS and hi + 1 < len(sentences):
        hi += 1      # "The scaffold inspection. We don't need it any more." -- the status is next
    return " ".join(sentences[lo:hi + 1])


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
    {status, quote, at, segment_key, offset_sec, evidence}, `quote` narrowed by focus_quote
    after it was verified. Returns the counts that go in the
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
            # Verified on the whole line; shown as the part about this item (focus_quote).
            child[FIELD] = {"status": s["status"],
                            "quote": focus_quote(s["quote"], child.get(KINDS[list_name].text_field)),
                            "at": s["at"].strip(), "segment_key": r.get("segment_key"),
                            "offset_sec": r.get("offset_sec"), "evidence": status}
            counts["kept"] += 1
    return counts
