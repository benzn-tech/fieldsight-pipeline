"""Which unit of a multi-unit walk he is in (spec 2026-11-03).

A framing inspector walks Lot 150 to 154, or units 1 to 5, in one recording and
wants one report with a checklist per unit. The model hears "now in unit two"
and says so (an entered mark); this module decides, in code and the same way
every time, what a unit IS, whether the check is a walk at all, and which unit
owns each moment of it.

Three rules carry the design:
- A unit is a prefix AND a number. "Unit two", "U2" and "unit-2" are the same
  unit, so they are written one way ("Unit 2"); "the unit", "lot of studs" or
  "Kitchen" are not a unit. A letter written against the number is kept: "Unit
  2A" and "Unit 2B" are two townhouses. ASR writes numbers as words as often as digits, and inspectors say
  "lot one fifty" for 150, so both are read.
- One "room 102" in an ordinary check is not a walk, even in the opening. A
  walk is an opening that names two or more units or a count of two or more, or
  entering two different units. Anything less
  behaves exactly as checks did before.
- Time before the first unit, and time a mark cannot reach, is General
  (unit None). A unit's stretch ends at the next mark or at the end of the
  recorded segment -- it is not carried across a gap, because a finding is never
  guessed into a unit.

Pure: no database, no network, so every rule is tested to the word.
"""
import re

PREFIXES = ("unit", "lot", "room", "house", "townhouse", "apartment", "apt", "flat", "villa", "u")
_TITLE = {"u": "Unit"}        # "U2" is how site plans write a unit

# "u2" -> "u", "2"; "unit-2" -> "unit", "2". A number keeps ONE letter written
# against it ("2a" -> "2a": Unit 2A and Unit 2B are different townhouses), but
# not a word ("150th" -> "150", "th") and not a letter after a space ("2 a crack").
_TOKEN = re.compile(r"[a-z]+|\d+[a-z](?![a-z])|\d+")
_DIGITS = re.compile(r"(\d+)([a-z]?)")
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ZERO = ("oh", "zero", "o")            # "room one oh two"


def _below_100(tok, i):
    """(value, next index) for 1..99 in words at tok[i], or None."""
    w = tok[i] if i < len(tok) else None
    if w in _TEENS:
        return _TEENS[w], i + 1
    if w in _TENS:
        nxt = tok[i + 1] if i + 1 < len(tok) else None
        if nxt in _ONES:
            return _TENS[w] + _ONES[nxt], i + 2
        return _TENS[w], i + 1
    if w in _ONES:
        return _ONES[w], i + 1
    return None


def _number(tok, i):
    """The number at tok[i] as an int, or None: digits, or words up to
    "nine hundred ninety nine", including the spoken "one fifty" (150) and
    "one oh two" (102)."""
    if i >= len(tok):
        return None
    if tok[i].isdigit():
        return int(tok[i])
    nxt = tok[i + 1] if i + 1 < len(tok) else None
    if tok[i] in _ONES:
        hundreds = _ONES[tok[i]] * 100
        if nxt == "hundred":
            j = i + 2
            if j < len(tok) and tok[j] == "and":
                j += 1
            rest = _below_100(tok, j)
            return hundreds + (rest[0] if rest else 0)
        if nxt in _ZERO and i + 2 < len(tok) and tok[i + 2] in _ONES:
            return hundreds + _ONES[tok[i + 2]]
        if nxt in _TEENS or nxt in _TENS:          # "one fifty", "one fifty four"
            return hundreds + _below_100(tok, i + 1)[0]
    got = _below_100(tok, i)
    return got[0] if got else None


def normalize_unit(text):
    """"Unit 2", "Lot 150", "Room 102" -- the prefix in title case, then the
    number in digits -- or None when the text has no prefix followed by a
    number. The first such pair wins."""
    tok = _TOKEN.findall((text or "").lower()) if isinstance(text, str) else []
    for i, w in enumerate(tok):
        if w not in PREFIXES:
            continue
        nxt = tok[i + 1] if i + 1 < len(tok) else ""
        d = _DIGITS.fullmatch(nxt)
        n = f"{int(d.group(1))}{d.group(2).upper()}" if d else _number(tok, i + 1)
        if n is not None:
            return f"{_TITLE.get(w, w.title())} {n}"
    return None


def _count(value):
    """A unit count the model gave: a positive int, or None. bool is not a count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and value > 0 else None


def opening_units(names, count):
    """{"names": [...], "count": int|None} from the opening. The model expands a
    range ("Lot 150 to 154") into names itself. Names are normalized, deduped and
    kept in the order he said them; when there are names they ARE the walk, so
    the count is theirs, not the model's."""
    out = []
    for n in names or []:
        u = normalize_unit(n)
        if u and u not in out:
            out.append(u)
    return {"names": out, "count": len(out) if out else _count(count)}


def _entered_unit(mark):
    """The unit a mark entered, or None. "Kitchen" is not a unit (no number),
    exactly as in the opening list -- it opens nothing and counts for nothing."""
    u = mark.get("unit") if isinstance(mark, dict) else None
    return normalize_unit(u) if isinstance(u, str) else None


def is_multi_unit(window):
    """A walk: the opening names two or more units, or counts two or more, or he
    entered two or more different units. One "room 102" -- in the opening or
    spoken along the way -- is an ordinary check (R8)."""
    opening = window.get("units") or {}
    if len(opening.get("names") or []) >= 2:
        return True
    c = _count(opening.get("count"))
    if c is not None and c >= 2:
        return True
    seen = {_entered_unit(m) for m in window.get("entered") or []}
    seen.discard(None)
    return len(seen) >= 2


def _hhmmss(mark):
    """A mark's time as "HH:MM:SS": `at_s` when the model gave seconds, else
    `at` ("HH:MM") on the minute."""
    if mark.get("at_s"):
        return str(mark["at_s"])
    return str(mark["at"]) + ":00" if mark.get("at") else None


def _seconds_form(t):
    t = str(t)
    return t + ":00" if len(t) == 5 else t


def unit_stretches(window):
    """[{"unit", "from", "to"}] over the window's recorded segments, in time order.

    Each entered mark opens its unit's stretch; it closes at the next mark or at
    the end of its segment. A segment's time before its first mark is General
    (unit None) -- the unit he was in before a gap is not assumed. A mark belongs
    to at most one segment; one outside every segment opens nothing, except a
    mark known only to the minute that a segment starts later in that minute.
    "to" None is a window still recording."""
    segments = window.get("segments") or [{"from": window.get("start_at"),
                                           "to": window.get("end_at")}]
    segments = [s for s in segments if s.get("from")]

    def _within(t, s):
        return t >= s["from"] and (s.get("to") is None or t < s["to"])

    # Each mark belongs to AT MOST ONE segment, so no unit reaches across a gap.
    placed = []                                   # (segment index, time, unit)
    for m in window.get("entered") or []:
        if not isinstance(m, dict):
            continue
        u, t = _entered_unit(m), _hhmmss(m)
        if not (u and t):
            continue
        k = next((k for k, s in enumerate(segments) if _within(t, s)), None)
        if k is None and not m.get("at_s"):
            # Known only to the minute ("10:41" -> "10:41:00"), inside no
            # segment: it was spoken in the segment that starts later in that
            # same minute. A mark with seconds is never moved.
            k = next((k for k, s in enumerate(segments)
                      if t < s["from"] and t[:5] == s["from"][:5]), None)
            if k is not None:
                t = segments[k]["from"]
        if k is not None:
            placed.append((k, t, u))
    placed.sort(key=lambda x: (x[0], x[1]))      # stable: same second keeps model order

    out = []
    for k, seg in enumerate(segments):
        start, end = seg["from"], seg.get("to")
        inside = [(t, u) for j, t, u in placed if j == k]
        cursor, unit = start, None
        for t, u in inside:
            if t > cursor:
                out.append({"unit": unit, "from": cursor, "to": t})
            cursor, unit = t, u
        if end is None or end > cursor:
            out.append({"unit": unit, "from": cursor, "to": end})
    return out


def unit_at(stretches, hhmmss):
    """The unit that owns a moment ("HH:MM:SS" or "HH:MM"), or None: General,
    or outside every stretch."""
    t = _seconds_form(hhmmss)
    for s in stretches or []:
        if s["from"] <= t and (s["to"] is None or t < s["to"]):
            return s["unit"]
    return None
