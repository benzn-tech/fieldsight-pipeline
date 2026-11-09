"""Which transcript turn did this final item come from?

Run by the extractor on the final tier, after `verify_evidence`. Each child item
(action, finding, decision, question) of every topic gets a `support` object
that either names ONE turn or says why it names none. Precision first: a wrong
link sends a reader to audio that does not say what the item claims, which is
worse than sending them nowhere. So the rule refuses on ties, refuses on tiny
items, and refuses to run at all until its thresholds are calibrated.

Candidate turns come from, in order (only the paths listed in `PATHS`):
  1. "quote"  -- the turns a topic evidence quote with status verified /
                 verified_fuzzy resolved to (source stem + offset within
                 `turn_name_overlay.TOLERANCE_SEC` of the turn's start_sec);
  2. "window" -- if 1 gives no link, every turn whose abs_start lies within the
                 topic's `time_range` ("HH:MM – HH:MM") on the session date.

Scores are `item_text_match.overlap` (containment of the item's tokens in the
turn's), so this agrees with the item-history view about what "same words" is.

A path links its best candidate only if:
  * the item has at least MIN_ITEM_TOKENS distinct tokens -- a smaller item is
    refused as `below_floor` whatever it scores;
  * best >= floor, and best beats the runner-up by >= margin, where the
    runner-up is taken over the UNION of both paths' candidates (a cited twin
    must not win over an uncited exact turn in the window);
  * every digit token and direction word (east/west/north/south/left/right and
    the CJK six) of the item is in that turn -- else `below_floor` with
    "guard": "distinguishing_token".
A path that does not link falls through to the next; the last path's result is
reported.

The support object carries ids, offsets and scores only -- never item or turn
text. Pure: no AWS, no DB.
"""
import logging
import unicodedata
from datetime import datetime

import item_locator
from item_continuity import KINDS, normalise_children
from item_text_match import RULE_VERSION, overlap, tokens
# Re-exported: the eval scripts read `item_support._TIME_RANGE`.
from topic_window import _END_SLACK, _TIME_RANGE, _topic_window  # noqa: F401
from turn_name_overlay import TOLERANCE_SEC, _stem

logger = logging.getLogger(__name__)

# Lexical paths tried, in order. Task 9 may drop one on the fit half. The "locator" path
# is not listed here: it is chosen by `attach_support(locator=True)` and runs alone.
PATHS = ("quote", "window")

# Uncalibrated until Task 9 fits them; attach_support refuses None.
FLOOR = None
MARGIN = None
NOTHING_CLOSE = None

# An item needs at least this many distinct tokens to be linkable. A one-token
# item ("2", "crane") scores 1.0 against any turn that mentions it.
MIN_ITEM_TOKENS = 2

_QUOTE_STATUSES = ("verified", "verified_fuzzy")
_STATUS_RANK = {"verified": 2, "verified_fuzzy": 1}
# Absorbs float noise in `best - second >= margin` (0.7 - 0.6 < 0.1 in binary).
_EPS = 1e-9
STATUSES = ("linked", "none_close", "ambiguous", "below_floor")
_KNOWN_PATHS = ("quote", "window")
# Every path `attach_support` can report a link under, lexical or locator.
KNOWN_PATHS = _KNOWN_PATHS + ("locator",)
# Twin items differ in one of these; the linked turn must contain the item's.
_ASCII_DIRECTIONS = frozenset({"east", "west", "north", "south", "left", "right"})
_CJK_DIRECTIONS = "东西南北左右"  # east west south north left right
# Chinese numerals ("三号楼" / "四号楼"): the CJK twin of the digit-token rule.
_CJK_NUMERALS = "〇零一二三四五六七八九十百两"


def _evidence_turns(evidence, turns):
    """{turn index: citing status} for the topic's verified quotes, one entry per turn.

    Deduplicated by turn: two quotes resolving to one turn must not make that turn
    its own runner-up. `verified` beats `verified_fuzzy` as the reported status.
    """
    out = {}
    for ev in evidence or []:
        if not isinstance(ev, dict) or ev.get("status") not in _QUOTE_STATUSES:
            continue
        key, off = ev.get("segment_key"), ev.get("offset_sec")
        if not key or off is None:
            continue
        try:
            off = float(off)
        except (TypeError, ValueError):
            continue
        stem = _stem(str(key).rsplit("/", 1)[-1])
        best_i, best_d = None, None
        for i, t in enumerate(turns):
            if _stem(t.get("source_filename")) != stem or t.get("start_sec") is None:
                continue
            d = abs(off - float(t["start_sec"]))
            if d <= TOLERANCE_SEC and (best_d is None or d < best_d):
                best_i, best_d = i, d
        if best_i is None:
            continue
        status = ev["status"]
        prev = out.get(best_i)
        if prev is None or _STATUS_RANK[status] > _STATUS_RANK[prev]:
            out[best_i] = status
    return out


def _window_turns(time_range, session_date, turns):
    """Indices of turns whose abs_start lies in [start, end + 1 min) (see `_topic_window`)."""
    window = _topic_window(time_range, session_date, turns)
    if window is None:
        return []
    start, end = window
    return [i for i, t in enumerate(turns)
            if isinstance(t.get("abs_start"), datetime) and start <= t["abs_start"] < end]


def _distinguishing_ok(item_text, turn_text):
    """Every digit token, direction word and Chinese numeral in the item must also be in
    the turn.

    Twin items ("unit 3 ..." / "unit 4 ...", "east block ..." / "west block ...")
    differ in exactly one such token, and containment scoring alone still gives
    the wrong twin 0.75-0.8. CJK directions and numerals are checked character by
    character on the normalised text, because tokenising puts them inside bigrams.
    """
    item_toks, turn_toks = set(tokens(item_text)), set(tokens(turn_text))
    for tok in item_toks:
        if (any(ch.isdigit() for ch in tok) or tok in _ASCII_DIRECTIONS) \
                and tok not in turn_toks:
            return False
    item_n, turn_n = _norm(item_text), _norm(turn_text)
    return all(ch in turn_n for ch in _CJK_DIRECTIONS + _CJK_NUMERALS if ch in item_n)


def _norm(text):
    return unicodedata.normalize("NFKC", text or "")


def _score_all(item_text, idxs, turns):
    return {i: round(overlap(item_text, turns[i].get("text") or "")[0], 4) for i in idxs}


def _decide(item_text, n_tokens, candidates, scores, turns, floor, margin, nothing_close):
    """(support, linked turn index or None) for one path.

    `best` is over this path's candidates; the runner-up is over `scores`, the
    union of every path's candidates, so a cited twin cannot win just because
    the right turn was only in the window. Ties break on turn order.
    """
    ranked = sorted(((scores[i], i) for i in candidates), key=lambda p: (-p[0], p[1]))
    best, best_i = ranked[0] if ranked else (0.0, None)
    second = max((s for i, s in scores.items() if i != best_i), default=0.0)
    if n_tokens < MIN_ITEM_TOKENS:
        return {"status": "below_floor", "best": best}, None
    if best_i is not None and best >= floor and best - second + _EPS >= margin:
        if not _distinguishing_ok(item_text, turns[best_i].get("text") or ""):
            return {"status": "below_floor", "best": best,
                    "guard": "distinguishing_token"}, None
        return {"status": "linked", "score": best}, best_i
    if best < nothing_close:
        return {"status": "none_close", "best": best}, None
    if best >= floor:
        return {"status": "ambiguous", "best": best, "second": second}, None
    return {"status": "below_floor", "best": best}, None


def _support_for(item_text, topic, turns, session_date, floor, margin, nothing_close):
    n_tokens = len(set(tokens(item_text)))
    # Both paths' candidates are computed whatever PATHS says: the runner-up pool
    # is their union even when a path is not tried for the link itself.
    quote_turns = _evidence_turns(topic.get("evidence"), turns)
    by_path = {"quote": sorted(quote_turns),
               "window": _window_turns(topic.get("time_range"), session_date, turns)}
    scores = _score_all(item_text, set(by_path["quote"]) | set(by_path["window"]), turns)
    last = None
    for path in PATHS:
        support, idx = _decide(item_text, n_tokens, by_path[path], scores, turns,
                               floor, margin, nothing_close)
        if idx is not None:
            t = turns[idx]
            return {"status": "linked",
                    "source_stem": _stem(t.get("source_filename")),
                    "start_sec": t.get("start_sec"),
                    "end_sec": t.get("end_sec"),
                    "speaker_label": t.get("speaker"),
                    "quote_status": quote_turns.get(idx) if path == "quote" else None,
                    "score": support["score"],
                    "candidate_path": path,
                    "rule_version": RULE_VERSION}
        last = support
    last["rule_version"] = RULE_VERSION
    return last


def attach_support(result, turns, session_date, *, floor, margin, nothing_close,
                   shown=None, locator=False):
    """Give every child of every topic a `support` object (mutates `result`).

    Plain-string decisions/questions are first turned into dicts (as
    `item_continuity.normalise_children` does) so they can carry the object.
    Returns counts per status plus `by_path` counts of links.

    `locator=True` runs the locator path alone: each child's `support` is
    `item_locator.resolve` of its `said_at` against the turns whose indices are in
    `shown` (the rendered ones; None means none). The lexical thresholds are not
    consulted, so None is fine there. `locator=False` is the lexical rule, which
    refuses to run uncalibrated.
    """
    if locator:
        return _attach_locator(result, turns, session_date, set(shown or ()))
    if floor is None or margin is None or nothing_close is None:
        raise ValueError("item_support thresholds are uncalibrated (None)")
    if not PATHS or any(p not in _KNOWN_PATHS for p in PATHS):
        raise ValueError(f"item_support PATHS must be a non-empty subset of {_KNOWN_PATHS}")
    turns = list(turns or [])
    topics = result.get("topics") if isinstance(result, dict) else None
    normalise_children(topics)
    counts = {s: 0 for s in STATUSES}
    counts["by_path"] = {p: 0 for p in PATHS}
    for topic in topics if isinstance(topics, list) else []:
        if not isinstance(topic, dict):
            continue
        for list_name, kind in KINDS.items():
            for child in topic.get(list_name) or []:
                if not isinstance(child, dict):
                    continue
                text = child.get(kind.text_field)
                text = text if isinstance(text, str) else ""
                s = _support_for(text, topic, turns, session_date,
                                 floor, margin, nothing_close)
                child["support"] = s
                counts[s["status"]] += 1
                if s["status"] == "linked":
                    counts["by_path"][s["candidate_path"]] += 1
    logger.info("item_support: %s", counts)
    return counts


def _attach_locator(result, turns, session_date, shown):
    """The locator path of `attach_support`: `support` from `item_locator.resolve`.

    Leaves `said_at` on the children; the extractor strips it from the artifact."""
    turns = list(turns or [])
    topics = result.get("topics") if isinstance(result, dict) else None
    normalise_children(topics)
    counts = {s: 0 for s in item_locator.STATUSES}
    counts["by_path"] = {"locator": 0}
    for topic in topics if isinstance(topics, list) else []:
        if not isinstance(topic, dict):
            continue
        for list_name in KINDS:
            for child in topic.get(list_name) or []:
                if not isinstance(child, dict):
                    continue
                s = item_locator.resolve(child, topic, turns, shown, session_date)
                child["support"] = s
                counts[s["status"]] = counts.get(s["status"], 0) + 1
                if s["status"] == "linked":
                    counts["by_path"]["locator"] += 1
    logger.info("item_support: %s", counts)
    return counts
