"""Cross-session status reconcile: the pure core (spec 2026-11-09 §5, §6, §12.2).

Open items recorded earlier are offered to the model as aliases C1..Cn (never their
stable_ids). The model returns hits: "this line of THIS transcript reports C3 done". Code --
not the prompt -- decides what each hit becomes:

  suggestion  the quote is verified and names exactly one candidate;
  pick card   the quote is verified and names something, but 2-4 candidates fit it -- the
              person chooses ("uncertain means ask, never pick", §12.2);
  dropped     everything else, under the name of the first guard that failed.

Guards, in order: shape -> evidence -> anchor -> twin -> one_to_one -> same_status.

Pure: no boto3, no psycopg, no env reads, no logging. The quote check is injected so every
rule is unit-testable. Quote and item text never leave this module except in the returned
suggestions and cards.
"""
import json
import re
from typing import NamedTuple

import action_pack_targets
import item_text_match
from status_suggestions import STATUSES

_KEEP_EVIDENCE = ("verified", "verified_fuzzy")
PICK_CAP = 4
_EPS = 1e-9

# --- numerals -------------------------------------------------------------------------------
# Rewritten only where they are a label, so a bare "one" ("that one's done") or "一起" stays a
# word and never becomes a token that matches "level 1".
_UNIT_NOUNS = ("unit", "level", "block", "floor", "stage", "lot", "house", "room", "apartment")
_EN_NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_EN_LABEL = re.compile(
    r"\b(%s)(\s+)(%s)\b" % ("|".join(_UNIT_NOUNS), "|".join(_EN_NUMBERS)), re.IGNORECASE)

# Single-digit numerals and digit-by-digit runs ("二零三号" -> "203号") before a measure word.
# Compound numerals ("十二楼", "一百零一号", "一千零二楼") are left alone, never half-rewritten:
# a run whose left neighbour is 十/百/千/万/零 is the tail of one. 两 is a count ("两栋" = two
# buildings), not a label, so it is not a numeral here.
_CN_MEASURE = ("单元", "楼", "层", "号", "栋", "期")
_CN_DIGITS = {ch: str(i) for i, ch in enumerate("零一二三四五六七八九")}
_CN_LABEL = re.compile(r"(?<![十百千万零])([零一二三四五六七八九]+)(?=%s)" % "|".join(_CN_MEASURE))


def normalise_numerals(text: str) -> str:
    """'unit four' -> 'unit 4', '三楼' -> '3楼'; a number word elsewhere is untouched."""
    t = _EN_LABEL.sub(lambda m: m.group(1) + m.group(2) + _EN_NUMBERS[m.group(3).lower()],
                      text or "")
    return _CN_LABEL.sub(lambda m: "".join(_CN_DIGITS[ch] for ch in m.group(1)), t)


def _toks(text):
    return set(item_text_match.tokens(normalise_numerals(text)))


# --- labels (label_conflict) ----------------------------------------------------------------
_EN_NUM_LABEL = re.compile(r"\b(%s)\s+(\d+)\b" % "|".join(_UNIT_NOUNS))
# 号 is a label only in an address ("3号楼", "5号房"); "11号" alone is usually a date.
_CN_NUM_LABEL = re.compile(
    r"(\d+)\s*(?:(单元|楼|层|栋|期)|号(?=楼|房|栋|单元|室|门|库))")
# Sides. north/south/east/west always count; the other words are everyday English ("I'm back",
# "right, ...", "nothing left") and count only before a place noun ("left wing", "back yard").
_COMPASS_EN = frozenset("north south east west".split())
_SIDES_EN = frozenset("upper lower front back left right".split())
_PLACE_NOUNS_EN = frozenset(
    "block wing side wall yard unit room bathroom bedroom door fence deck level floor stair "
    "stairs".split())
# A CJK side counts only directly before a place character ("北面", "西区"), never inside an
# ordinary word ("上午", "东西", "然后", "下单", "上传").
_SIDE_CJK = re.compile(r"([东南西北上下前后左右])(?=[面边侧区栋楼部门墙院])")


def _number_labels(text):
    """{unit noun or measure word: {number, ...}} after numeral normalisation."""
    t = normalise_numerals(text).lower()
    out = {}
    for noun, num in _EN_NUM_LABEL.findall(t):
        out.setdefault(noun, set()).add(str(int(num)))
    for num, noun in _CN_NUM_LABEL.findall(t):
        out.setdefault(noun or "号", set()).add(str(int(num)))
    return out


def _directions(text):
    t = (text or "").lower()
    words = re.findall(r"[a-z]+", t)
    out = {w for w in words if w in _COMPASS_EN}
    out |= {w for w, nxt in zip(words, words[1:]) if w in _SIDES_EN and nxt in _PLACE_NOUNS_EN}
    return out | set(_SIDE_CJK.findall(t))


def _label_conflict(quote, item_text):
    """True when the quote names a label that contradicts the item's: the same unit noun with
    a different number ("level 2" vs "Level 1 slab"), or a side the item lacks while the item
    names a side the quote lacks ("east bathroom" vs "west block"). A label merely absent from
    the quote is not a conflict."""
    q_nums, i_nums = _number_labels(quote), _number_labels(item_text)
    for noun, nums in q_nums.items():
        if noun in i_nums and not (i_nums[noun] & nums):
            return True
    q_dirs, i_dirs = _directions(quote), _directions(item_text)
    return bool(q_dirs - i_dirs) and bool(i_dirs - q_dirs)


# --- prompt ---------------------------------------------------------------------------------
_PROMPT = """You are reconciling a construction site's open work items against a new transcript.

The candidates below are open work items recorded EARLIER. For each one, decide whether \
someone in THIS transcript reports that this exact item is:
- finished ("done"),
- no longer needed or cancelled ("closed"),
- blocked ("blocked"),
- or started ("in_progress").

Plans, intentions, hearsay ("I heard..."), questions and conditionals are not statuses: \
"I'll order it tomorrow" is not done.
Answer only when the line clearly names the item. When two candidates differ only by \
location, unit, block or level, the line must name the distinguishing word.

Candidates:
{candidates}

Transcript (each line is [HH:MM:SS] speaker: text):
{transcript}

Return JSON: {{"hits": [{{"candidate": "C1", "status": "done | closed | blocked | in_progress", \
"at": "HH:MM:SS", "quote": "..."}}]}}
- "candidate" is the alias exactly as listed above.
- "quote" is the WHOLE transcript line it was said in, copied VERBATIM, errors included.
- "at" is the [HH:MM:SS] printed on that line, copied exactly, THE HOUR INCLUDED.
Zero hits is the expected answer for most transcripts: return {{"hits": []}}."""


def build_prompt(candidates: list, transcript_lines: str) -> str:
    """The reconcile prompt. Candidates are aliased C1..Cn in the order given; no stable_id
    is sent."""
    lines = "\n".join(f"C{i + 1}: {' '.join(str(c.get('text') or '').split())}"
                      for i, c in enumerate(candidates))
    return _PROMPT.format(candidates=lines, transcript=transcript_lines or "")


# --- parse ----------------------------------------------------------------------------------
def parse_hits(raw: str, n_candidates: int):
    """The model's hits as [{alias, status, at, quote}], or None when `raw` is not JSON of a
    recognisable shape. Accepts a bare list or {"hits": [...]}, and the alias under
    "candidate" (as the prompt asks) or "alias".

    Nothing is validated beyond that here: an alias outside C1..C{n_candidates}, a bad status
    or a non-object entry is passed through (the alias as None) so that judge counts it
    under `shape` rather than it vanishing uncounted."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("hits")
    if not isinstance(data, list):
        return None
    aliases = {f"C{i + 1}" for i in range(max(0, int(n_candidates)))}
    hits = []
    for entry in data:
        if not isinstance(entry, dict):
            hits.append({"alias": None, "status": None, "at": None, "quote": None})
            continue
        alias = entry.get("candidate", entry.get("alias"))
        alias = alias.strip() if isinstance(alias, str) else None
        hits.append({"alias": alias if alias in aliases else None,
                     "status": entry.get("status"), "at": entry.get("at"),
                     "quote": entry.get("quote")})
    return hits


# --- judge ----------------------------------------------------------------------------------
class Outcome(NamedTuple):
    suggestions: list
    picks: list
    counts: dict


def _alias_index(alias, n):
    if not isinstance(alias, str) or not re.fullmatch(r"C[1-9][0-9]*", alias):
        return None
    i = int(alias[1:]) - 1
    return i if i < n else None


def _shape_ok(h, n):
    return (isinstance(h, dict) and _alias_index(h.get("alias"), n) is not None
            and h.get("status") in STATUSES
            and isinstance(h.get("quote"), str) and h["quote"].strip()
            and isinstance(h.get("at"), str) and h["at"].strip())


def _at_key(at):
    """Sort key for the model's 'HH:MM:SS' (an unpadded hour too); anything else sorts by
    its text, after every parsed time."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2}):(\d{2})\s*", at)
    if m:
        return (0, int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)), "")
    return (1, 0, at)


def _order(indices, candidates, quote, overlaps, pick=None):
    """Card order, best first, at most PICK_CAP: more shared tokens first, then higher
    containment of the candidate's words in the quote. The model's pick leads only when its
    containment is within AMBIGUITY_MARGIN of the best -- ordering only, never a choice."""
    q = normalise_numerals(quote)
    score = {i: action_pack_targets.containment(normalise_numerals(candidates[i]["text"]), q)
             for i in indices}
    best = max(score.values(), default=0.0)

    def key(i):
        lead = 0 if (i == pick and best - score[i] <= action_pack_targets.AMBIGUITY_MARGIN
                     + _EPS) else 1
        return (lead, -overlaps[i], -score[i], i)
    return sorted(indices, key=key)[:PICK_CAP]


def _anchor_and_twin(pick, candidates, quote):
    """('ok', None) | ('drop', reason) | ('card', [candidate index, ...]).

    A card's indices are unordered and uncapped here: _anchor_card filters, orders and caps
    them once per anchor."""
    q = _toks(quote)
    ctoks = [_toks(c.get("text") or "") for c in candidates]
    overlaps = [len(q & t) for t in ctoks]
    top = max(overlaps)
    if top == 0:
        return "drop", "no_anchor"
    plausible = [i for i, o in enumerate(overlaps) if o > 0]

    # anchor: the pick is the unique argmax AND shares a token no other candidate has.
    leaders = [i for i, o in enumerate(overlaps) if o == top]
    others = set().union(*(ctoks[i] for i in range(len(candidates)) if i != pick))
    unique = (q & ctoks[pick]) - others
    if len(leaders) > 1 or pick not in leaders or not unique:
        return ("card", plausible) if len(plausible) >= 2 else ("drop", "anchor")

    # twin: against every other plausible candidate, the quote must name one of the pick's
    # distinguishing words and none of the other's ("west... no wait, the east one").
    offending = []
    for i in plausible:
        if i == pick:
            continue
        d_pick, d_other = ctoks[pick] - ctoks[i], ctoks[i] - ctoks[pick]
        if not (q & d_pick) or (q & d_other):
            offending.append(i)
    if offending:
        return "card", [pick] + offending
    return "ok", None


def judge(hits, candidates, check) -> Outcome:
    """Run every hit through the guards (spec §6; the first failure names the drop).

    Order: shape -> evidence -> anchor -> twin -> label_conflict -> one_to_one -> same_status.

    `check(quote, at)` is _check_status_quote's shape: {status, segment_key, offset_sec,
    quote}; it must not raise. Returns Outcome(suggestions, picks, counts):
      suggestion {stable_id, suggested_status, quote, segment_key, offset_sec}
      pick       {suggested_status, quote, segment_key, offset_sec, candidates: [stable_id]}
                 (2..4 stable_ids, best first)
      counts     {offered, kept, picks, dropped_by: {reason: n}}

    Every hit is accounted for once: offered == kept + picks + sum(dropped_by). Extra hits
    folded into one anchor's card count as `duplicate_card`; a hit that loses to a later hit
    on the same candidate counts as `superseded`."""
    hits = hits or []
    n = len(candidates)
    dropped = {}

    def drop(reason, k=1):
        dropped[reason] = dropped.get(reason, 0) + k

    # Per hit: shape, evidence, anchor, twin, label_conflict. What passes evidence and is not
    # dropped is either a suggestion-to-be ("ok") or a card ("card"), keyed by its anchor.
    passed = []         # (verdict, candidate indices, hit, check result, quote)
    for h in hits:
        if not _shape_ok(h, n):
            drop("shape")
            continue
        r = check(h["quote"], h["at"]) or {}
        evidence = r.get("status") or "unchecked"
        if evidence not in _KEEP_EVIDENCE:
            drop(evidence)
            continue
        quote = r.get("quote") or h["quote"]
        pick = _alias_index(h["alias"], n)
        verdict, detail = _anchor_and_twin(pick, candidates, quote)
        if verdict == "drop":
            drop(detail)
        elif verdict == "card":
            passed.append(("card", detail, h, r, quote))
        elif _label_conflict(quote, candidates[pick].get("text") or ""):
            drop("label_conflict")
        else:
            passed.append(("ok", [pick], h, r, quote))

    # one_to_one, part 1: one verified anchor -> at most one candidate and one status. Every
    # hit that passed evidence is grouped, cards included, before suggestion vs card is decided.
    groups = {}
    for p in passed:
        groups.setdefault((p[3].get("segment_key"), p[3].get("offset_sec")), []).append(p)
    picks, single = [], []
    for group in groups.values():
        if len({p[2]["status"] for p in group}) > 1:
            drop("conflict", len(group))
            continue
        union = list(dict.fromkeys(i for p in group for i in p[1]))
        if any(p[0] == "card" for p in group) or len(union) > 1:
            card = _anchor_card(group, union, candidates)
            if isinstance(card, str):
                drop(card, len(group))
            else:
                picks.append(card)
                if len(group) > 1:
                    drop("duplicate_card", len(group) - 1)
        else:
            single.extend(group)

    # one_to_one, part 2: one candidate -> its latest status.
    latest = {}
    for p in single:
        i = p[1][0]
        cur = latest.get(i)
        if cur is None or _at_key(p[2]["at"]) >= _at_key(cur[2]["at"]):
            latest[i] = p
    if len(single) > len(latest):
        drop("superseded", len(single) - len(latest))

    suggestions = []
    for i, p in sorted(latest.items()):
        _v, _ix, h, r, quote = p
        c = candidates[i]
        if c.get("status") == h["status"]:
            drop("same_status")
            continue
        suggestions.append({"stable_id": c["stable_id"], "suggested_status": h["status"],
                            "quote": quote, "segment_key": r.get("segment_key"),
                            "offset_sec": r.get("offset_sec")})

    counts = {"offered": len(hits), "kept": len(suggestions), "picks": len(picks),
              "dropped_by": dropped}
    return Outcome(suggestions, picks, counts)


def _anchor_card(group, union, candidates):
    """One card for an anchor, or the drop reason when fewer than 2 candidates survive.

    The earliest-`at` hit gives the card its quote and status. Candidates are the union over
    the anchor, best first against that quote; label_conflict and then same_status remove
    candidates one by one, and a card left with 1 is dropped (never a 1-candidate card)."""
    _v, _ix, h, r, quote = min(group, key=lambda p: _at_key(p[2]["at"]))
    status = h["status"]
    rows = [c.get("text") or "" for c in candidates]
    q = _toks(quote)
    overlaps = [len(q & _toks(t)) for t in rows]
    indices = [i for i in union if not _label_conflict(quote, rows[i])]
    if len(indices) < 2:
        return "label_conflict"
    indices = [i for i in indices if candidates[i].get("status") != status]
    if len(indices) < 2:
        return "same_status"
    indices = _order(indices, candidates, quote, overlaps, _alias_index(h["alias"],
                                                                         len(candidates)))
    return {"suggested_status": status, "quote": quote, "segment_key": r.get("segment_key"),
            "offset_sec": r.get("offset_sec"),
            "candidates": [candidates[i]["stable_id"] for i in indices]}
