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

The support object carries ids, offsets and scores only -- never item or turn
text. Pure: no AWS, no DB.
"""
import logging
import re
from datetime import datetime, timedelta

from item_continuity import KINDS, normalise_children
from item_text_match import RULE_VERSION, overlap, tokens
from turn_name_overlay import TOLERANCE_SEC, _stem

logger = logging.getLogger(__name__)

# Paths tried, in order. Task 9 may drop one on the fit half.
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
_TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})")
# `time_range` is minute-granular, so its end minute is inclusive.
_END_SLACK = timedelta(minutes=1)
# Absorbs float noise in `best - second >= margin` (0.7 - 0.6 < 0.1 in binary).
_EPS = 1e-9
STATUSES = ("linked", "none_close", "ambiguous", "below_floor")


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
    """Indices of turns whose abs_start lies in [start, end + 1 min).

    Naive local datetimes on `session_date`, as `_parse_at` builds them, compared
    with the turns' naive local abs_start. A range whose end is before its start
    crosses midnight; the whole range takes the day nearest the session's middle
    turn, the same rule `_parse_at` applies to a quote anchor.
    """
    m = _TIME_RANGE.search(str(time_range or ""))
    if not m:
        return []
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    try:
        start = datetime.strptime(f"{session_date} {h1:02d}:{m1:02d}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{session_date} {h2:02d}:{m2:02d}", "%Y-%m-%d %H:%M")
    except ValueError:
        return []
    if end < start:
        end += timedelta(days=1)
    anchored = [t.get("abs_start") for t in turns if isinstance(t.get("abs_start"), datetime)]
    if anchored:
        mid = anchored[len(anchored) // 2]
        shift = min((timedelta(0), timedelta(days=1), timedelta(days=-1)),
                    key=lambda s: abs((start + s - mid).total_seconds()))
        start, end = start + shift, end + shift
    end = end + _END_SLACK
    return [i for i, t in enumerate(turns)
            if isinstance(t.get("abs_start"), datetime) and start <= t["abs_start"] < end]


def _decide(item_text, n_tokens, candidates, turns, floor, margin, nothing_close):
    """(support, linked?) for one path's candidates. Ties break on turn order."""
    scored = sorted(((round(overlap(item_text, turns[i].get("text") or "")[0], 4), i)
                     for i in candidates), key=lambda p: (-p[0], p[1]))
    best, best_i = scored[0] if scored else (0.0, None)
    second = scored[1][0] if len(scored) > 1 else 0.0
    if n_tokens < MIN_ITEM_TOKENS:
        return {"status": "below_floor", "best": best}, None
    if best_i is not None and best >= floor and best - second + _EPS >= margin:
        return {"status": "linked", "score": best}, best_i
    if best < nothing_close:
        return {"status": "none_close", "best": best}, None
    if best >= floor:
        return {"status": "ambiguous", "best": best, "second": second}, None
    return {"status": "below_floor", "best": best}, None


def _support_for(item_text, topic, turns, session_date, floor, margin, nothing_close):
    n_tokens = len(set(tokens(item_text)))
    quote_turns = None
    last = None
    for path in PATHS:
        if path == "quote":
            quote_turns = _evidence_turns(topic.get("evidence"), turns)
            candidates = sorted(quote_turns)
        elif path == "window":
            candidates = _window_turns(topic.get("time_range"), session_date, turns)
        else:
            continue
        support, idx = _decide(item_text, n_tokens, candidates, turns,
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
    if last is None:
        last = {"status": "none_close", "best": 0.0}
    last["rule_version"] = RULE_VERSION
    return last


def attach_support(result, turns, session_date, *, floor, margin, nothing_close):
    """Give every child of every topic a `support` object (mutates `result`).

    Plain-string decisions/questions are first turned into dicts (as
    `item_continuity.normalise_children` does) so they can carry the object.
    Returns counts per status plus `by_path` counts of links.
    """
    if floor is None or margin is None or nothing_close is None:
        raise ValueError("item_support thresholds are uncalibrated (None)")
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
                    counts["by_path"][s["candidate_path"]] = \
                        counts["by_path"].get(s["candidate_path"], 0) + 1
    logger.info("item_support: %s", counts)
    return counts
