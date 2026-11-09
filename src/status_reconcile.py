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
# Compound numerals with 十/百 ("十二楼") are left alone, never half-rewritten. 两 is a count
# ("两栋" = two buildings), not a label, so it is not a numeral here.
_CN_DIGITS = {ch: str(i) for i, ch in enumerate("零一二三四五六七八九")}
_CN_LABEL = re.compile(r"(?<![十百])([零一二三四五六七八九]+)(?=单元|楼|层|号|栋|期)")


def normalise_numerals(text: str) -> str:
    """'unit four' -> 'unit 4', '三楼' -> '3楼'; a number word elsewhere is untouched."""
    t = _EN_LABEL.sub(lambda m: m.group(1) + m.group(2) + _EN_NUMBERS[m.group(3).lower()],
                      text or "")
    return _CN_LABEL.sub(lambda m: "".join(_CN_DIGITS[ch] for ch in m.group(1)), t)


def _toks(text):
    return set(item_text_match.tokens(normalise_numerals(text)))


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
    """('ok', None) | ('drop', reason) | ('card', [candidate index, ...])."""
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
        card = _order(plausible, candidates, quote, overlaps, pick)
        return ("card", card) if len(card) >= 2 else ("drop", "anchor")

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
        rest = _order(offending, candidates, quote, overlaps)[:PICK_CAP - 1]
        return "card", [pick] + rest
    return "ok", None


def judge(hits, candidates, check) -> Outcome:
    """Run every hit through the guards (spec §6; the first failure names the drop).

    `check(quote, at)` is _check_status_quote's shape: {status, segment_key, offset_sec,
    quote}; it must not raise. Returns Outcome(suggestions, picks, counts):
      suggestion {stable_id, suggested_status, quote, segment_key, offset_sec}
      pick       {suggested_status, quote, segment_key, offset_sec, candidates: [stable_id]}
                 (2..4 stable_ids, best first)
      counts     {offered, kept, picks, dropped_by: {reason: n}}
    A hit that loses to a later hit on the same candidate counts as `superseded`."""
    hits = hits or []
    n = len(candidates)
    dropped = {}
    picks, seen_cards = [], set()

    def drop(reason, k=1):
        dropped[reason] = dropped.get(reason, 0) + k

    def add_card(status, quote, r, indices):
        sids = [candidates[i]["stable_id"] for i in indices]
        key = (r.get("segment_key"), r.get("offset_sec"), status, tuple(sids))
        if key in seen_cards:
            return
        seen_cards.add(key)
        picks.append({"suggested_status": status, "quote": quote,
                      "segment_key": r.get("segment_key"), "offset_sec": r.get("offset_sec"),
                      "candidates": sids})

    survivors = []      # (candidate index, hit, check result)
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
            add_card(h["status"], quote, r, detail)
        else:
            survivors.append((pick, h, r))

    # one_to_one, part 1: one verified anchor -> at most one candidate and one status.
    groups = {}
    for s in survivors:
        groups.setdefault((s[2].get("segment_key"), s[2].get("offset_sec")), []).append(s)
    single = []
    for group in groups.values():
        statuses = {h["status"] for _i, h, _r in group}
        indices = list(dict.fromkeys(i for i, _h, _r in group))
        if len(statuses) > 1:
            drop("conflict", len(group))
        elif len(indices) > 1:
            first = min(group, key=lambda s: _at_key(s[1]["at"]))
            add_card(first[1]["status"], first[2].get("quote") or first[1]["quote"], first[2],
                     indices[:PICK_CAP])
        else:
            single.extend(group)

    # one_to_one, part 2: one candidate -> its latest status.
    latest = {}
    for s in single:
        cur = latest.get(s[0])
        if cur is None or _at_key(s[1]["at"]) >= _at_key(cur[1]["at"]):
            latest[s[0]] = s
    drop_superseded = len(single) - len(latest)
    if drop_superseded:
        drop("superseded", drop_superseded)

    suggestions = []
    for i, h, r in sorted(latest.values(), key=lambda s: s[0]):
        c = candidates[i]
        if c.get("status") == h["status"]:
            drop("same_status")
            continue
        suggestions.append({"stable_id": c["stable_id"], "suggested_status": h["status"],
                            "quote": r.get("quote") or h["quote"],
                            "segment_key": r.get("segment_key"),
                            "offset_sec": r.get("offset_sec")})

    counts = {"offered": len(hits), "kept": len(suggestions), "picks": len(picks),
              "dropped_by": dropped}
    return Outcome(suggestions, picks, counts)
