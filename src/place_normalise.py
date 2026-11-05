"""Which place an inspector named, written one way (Track L, Task 2).

Track L groups a day's topics and photos under the places he says out loud.
The same floor comes back as "Level one", "L1", "first floor" or "一楼", and a
group keyed on the raw words would split one floor into four. This module
decides, in code and the same way every time, what place a phrase names; later
tasks key their groups on `place_key`.

Rules that carry the design. The first is the one the rest serve: two
different places must never share a key. Splitting one place into two keys is
a nuisance an alias can fix; merging two places hides a finding under the
wrong place, and nobody sees it.
- A place is built from parts: a level, a block (block/wing/stair/core), a zone
  (zone/area/grid), a room or unit, and qualifiers. The parts are written in a
  fixed order (Block, Level, Zone, Area, Grid, Stair, Core, Room, then other
  words, then compass words), so "level two, zone four" and "zone four, level
  two" are one place: "Level 2 Zone 4".
- A qualifier is a narrower place, not a spelling. "Level 1 east", "Level 2
  slab" and "Level 2 corridor" are each their own group, apart from "Level 1".
  So every word left over after the parts are read is kept, in the order he
  said it, except a short list of filler words ("the", "on", "now"). That can
  split one place over a stray word; the opposite choice -- dropping unknown
  words -- would silently merge "Level 2 kitchen" into "Level 2", and a merge
  nobody sees is worse than a split somebody can alias away.
- Ground is level 0 ("ground floor" == "Level 0"), as in
  photo_binding._place_words. "first floor" is Level 1. A basement is below
  ground: "basement" is "Basement" (one level down), "basement two" is
  "Basement 2"; "Level -1", "level minus one", "地下一层" and "负一楼" are
  "Basement" too. "level ground" is flat ground, not a level, and a number
  followed by a length ("floor 3 metres from the wall") is a measurement.
- Rooms and units are read by units.normalize_unit ("Unit two", "U2",
  "unit-2" -> "Unit 2"), which belongs to multi-unit walks and is not changed
  here. What it lacks is added around it: "rm", dotted codes and lettered
  codes ("G02", "L2"), which it would misread ("Room 1") or miss. A dotted
  code is the plain code only when exactly two digits follow the dot ("1.01"
  is room 101); "1.1", "10.1" and "11.0" keep their dot, because flattening
  them would make "1.10" and "11.0" one room. A code right after a room word
  is the room's ("Room L2" is not level 2). A lone letter right after a part
  is that part's code letter ("Level 2 A" is not "Level 2").
- A phrase that gives one part two values ("level 2 to level 3", "room 101
  and room 102"), or a part followed by a connector and a second code ("level
  2 and 3", "zone A to B", "zone A/B", "level 2-3", "二楼到3"), names two places, so it is kind
  "other", not either one. So is any phrase with a measurement in it: what is
  left after removing "2 m" would spell the very place the measurement ruled
  out ("basement 2 m from the wall" is not Basement 2).
- Anything with no part -- a farm or site name, a landmark, "inside", a bare
  "level" -- is kind "other", valued as content_hash.normalize gives it. It is
  never guessed into a level.

`kind` of a multi-part place is its most specific part: room, then zone, then
block, then level. `value` of a structured place is its label casefolded; the
key is "kind:value", so an "other" can never collide with a structured place.

Pure: no database, no network.
"""
import re
import unicodedata

from content_hash import normalize as _casefold_collapse
from units import PREFIXES, normalize_unit

_TOKEN = re.compile(r"[一-鿿]+|[a-z0-9]+(?:\.[a-z0-9]+)*")

_LEVEL_WORDS = ("level", "lvl", "l", "floor", "storey", "story")
_FLOOR_NOUNS = ("floor", "level", "storey", "story")        # "first floor", "ground level"
_ROOM_PREFIXES = PREFIXES + ("rm",)          # normalize_unit's prefixes, plus "rm"
_ROOM_TITLE = {"u": "Unit", "rm": "Room"}
# word -> (slot, title). Zone-like and block-like parts take a code after them.
_CODED = {"zone": ("zone", "Zone"), "area": ("area", "Area"), "grid": ("grid", "Grid"),
          "gridline": ("grid", "Grid"), "block": ("block", "Block"), "wing": ("wing", "Wing"),
          "core": ("core", "Core")}
_STAIR = ("stair", "stairs", "stairwell", "staircase")
_SLOTS = ("block", "wing", "level", "zone", "area", "grid", "stair", "core", "room")
_KIND_BY_SLOT = (("room", "room"), ("zone", "zone"), ("area", "zone"), ("grid", "zone"),
                 ("block", "block"), ("wing", "block"), ("stair", "block"), ("core", "block"),
                 ("level", "level"))
_COMPASS = ("north", "south", "east", "west", "northeast", "northwest", "southeast",
            "southwest")
_FILLER = frozenset(("the", "a", "an", "on", "in", "at", "of", "to", "now", "here", "this",
                     "is", "we", "re", "are", "i", "m", "just", "so", "um", "uh", "okay",
                     "ok", "and", "near", "by", "next", "beside", "around"))
_MINUS = re.compile(r"(?<![a-z0-9])-(?=\d)", re.I)            # "Level -1", not "unit-2"
# "level 2-3", "room 101-102": a dash between two numbers is a range. A dash
# after a letter ("grid C-4", "unit-2") or before a leading zero ("Room 1-01")
# is inside one code, and is left alone.
_RANGE_DASH = re.compile(r"(?<=\d)-(?=[1-9])")
_DECIMAL = re.compile(r"\d+(?:\.\d+)?")
_SPLIT = re.compile(r"([a-z]+)(\d[a-z0-9.]*)")              # "l1", "u2", "rm101", "lvl2"

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
             "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
             "twelfth": 12}
_ORDINAL_DIGITS = re.compile(r"(\d+)(?:st|nd|rd|th)")
# Words normalize_unit reads as part of a room number ("room one oh two").
_ROOM_NUMBER_WORDS = frozenset(_ONES) | frozenset(_TEENS) | frozenset(_TENS) | {
    "sixty", "seventy", "eighty", "ninety", "hundred", "oh", "o"}

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9}
_CN_LEVEL = re.compile(r"(地下|负|負)?([一二两三四五六七八九十]+|\d+)\s*[楼层樓層]")
# A number followed by one of these is a measurement, not a level or a code:
# "floor 3 metres from the wall" names no floor.
# "level 2 and 3", "zone A to B": a connector then a second code names two places.
_CONNECT = frozenset(("and", "to", "through", "thru", "到", "和", "至"))
_MEASURE = frozenset(("m", "mm", "cm", "metre", "metres", "meter", "meters", "millimetre",
                      "millimetres", "millimeter", "millimeters", "centimetre", "centimetres"))
# "1.01" is room 101 written with the floor split off; "1.1" or "10.1" is not.
_DOTTED_FLOOR = re.compile(r"([a-z]*[1-9]\d*|[a-z]+)\.(\d\d)")


def _cn_number(s):
    """一..九十九 as an int ("十一" = 11, "二十" = 20), or None."""
    if s.isdigit():
        return int(s)
    if "十" not in s:
        return _CN_DIGITS.get(s) if len(s) == 1 else None
    tens, _, ones = s.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    t = _CN_DIGITS.get(tens, 1 if not tens else None)
    o = _CN_DIGITS.get(ones, 0 if not ones else None)
    return None if t is None or o is None else t * 10 + o


def _cn_levels(text):
    """"二楼" -> " level 2 ", "地下一层"/"负一楼" -> " basement 1 ": a Chinese
    floor is read by the same level rule as the English."""
    def sub(m):
        n = _cn_number(m.group(2))
        if n is None:
            return m.group(0)
        return f" basement {n} " if m.group(1) else f" level {n} "
    return _CN_LEVEL.sub(sub, text)


def _tokens(text):
    out = []
    text = _cn_levels(unicodedata.normalize("NFC", text))
    text = _RANGE_DASH.sub(" to ", text).replace("/", " and ").replace("&", " and ")
    text = _MINUS.sub(" minus ", text)
    for t in _TOKEN.findall(text.casefold()):
        m = _SPLIT.fullmatch(t)
        # A token right after a room prefix is the room's code: "Room L2" is
        # not level 2, "Room U2" is not unit 2.
        after_room = bool(out) and out[-1] in _ROOM_PREFIXES
        if m and not after_room and (m.group(1) in _LEVEL_WORDS or m.group(1) in _ROOM_PREFIXES):
            out.extend(m.groups())
        else:
            out.append(t)
    return out


def _number(tok, i, ordinal=False):
    """(n, next index) for the number at tok[i] -- digits, or words up to
    fifty-nine; with `ordinal`, "first"/"2nd" too -- or None."""
    w = tok[i] if i < len(tok) else ""
    if w.isdigit():
        return int(w), i + 1
    if ordinal:
        m = _ORDINAL_DIGITS.fullmatch(w)
        if m:
            return int(m.group(1)), i + 1
        if w in _ORDINALS:
            return _ORDINALS[w], i + 1
    if w in _ONES:
        return _ONES[w], i + 1
    if w in _TEENS:
        return _TEENS[w], i + 1
    if w in _TENS:
        nxt = tok[i + 1] if i + 1 < len(tok) else ""
        if nxt in _ONES and _ONES[nxt]:
            return _TENS[w] + _ONES[nxt], i + 2
        return _TENS[w], i + 1
    return None


def _codelike(w):
    """True when `w` could be a second code or number ("3", "B", "C4", "three")."""
    return ((len(w) == 1 and w.isascii() and w.isalpha()) or any(c.isdigit() for c in w)
            or w in _ONES or w in _TEENS or w in _TENS or w in _ORDINALS)


def _has_measurement(tok):
    """True when a number is followed by a length ("basement 2 m from the
    wall"). Such a phrase is a distance, and reading a place out of what is
    left of it would spell the very place the measurement ruled out."""
    return any(w in _MEASURE and i and (_DECIMAL.fullmatch(tok[i - 1]) or tok[i - 1] in _ONES
                                         or tok[i - 1] in _TEENS or tok[i - 1] in _TENS)
               for i, w in enumerate(tok))


def _measured(tok, got):
    """`got` (value, next index), or None when the number is a measurement ("3 m")."""
    return None if got and got[1] < len(tok) and tok[got[1]] in _MEASURE else got


def _code(tok, i):
    """(code, next index) for the code after "zone"/"block"/"grid": a letter,
    a number, or a letter-number code ("C4"), written in capitals -- or None."""
    w = tok[i] if i < len(tok) else ""
    if w.isascii() and w.replace(".", "").isalnum() and (len(w) == 1 or any(c.isdigit() for c in w)):
        got = (w.upper(), i + 1)
        # A bare letter is never a number, so it is never a measurement:
        # "zone A m" is still Zone A.
        return _measured(tok, got) if any(c.isdigit() for c in w) else got
    got = _measured(tok, _number(tok, i))
    return (str(got[0]), got[1]) if got else None


def _level(tok, i):
    """(level number, next index) for a level starting at tok[i], or None.
    Basements are negative."""
    w = tok[i]
    if w in _LEVEL_WORDS:
        # "level G" is the ground floor; "the level ground" is flat ground,
        # so "level ground" names no level.
        nxt = tok[i + 1] if i + 1 < len(tok) else ""
        if nxt == "g":
            return 0, i + 2
        if nxt == "minus":
            got = _measured(tok, _number(tok, i + 2))
            return (-got[0], got[1]) if got and got[0] > 0 else None
        return _measured(tok, _number(tok, i + 1, ordinal=True))
    if w == "basement":
        j = i + 2 if i + 1 < len(tok) and tok[i + 1] in _FLOOR_NOUNS else i + 1
        got = _measured(tok, _number(tok, j))
        return (-got[0], got[1]) if got and got[0] > 0 else (-1, i + 1)
    nxt = tok[i + 1] if i + 1 < len(tok) else ""
    if nxt in _FLOOR_NOUNS:
        if w == "ground":
            return 0, i + 2
        got = _number(tok, i, ordinal=True)
        if got and got[1] == i + 1:
            return got[0], i + 2
    return None


def _room(tok, i):
    """(label, next index) for a room/unit at tok[i], or None. Dotted and
    lettered codes are read here; plain numbers go to normalize_unit."""
    p = tok[i]
    if p not in _ROOM_PREFIXES or i + 1 >= len(tok):
        return None
    code = tok[i + 1]
    title = _ROOM_TITLE.get(p, p.title())
    if code.isascii() and ("." in code or re.fullmatch(r"[a-z]+\d+", code)):
        if not any(c.isdigit() for c in code):
            return None
        m = _DOTTED_FLOOR.fullmatch(code)
        return f"{title} {(m.group(1) + m.group(2) if m else code).upper()}", i + 2
    j = i + 1
    if not code[:1].isdigit():
        while j < len(tok) and tok[j] in _ROOM_NUMBER_WORDS:
            j += 1
        if j < len(tok) and tok[j - 1] == "hundred" and tok[j] == "and":
            j += 1
            while j < len(tok) and tok[j] in _ROOM_NUMBER_WORDS:
                j += 1
    else:
        j += 1
    label = normalize_unit(" ".join(["room" if p == "rm" else p] + tok[i + 1:j]))
    return (label, j) if label else None


def _parts(tok):
    """({slot: label piece}, qualifier words, compass words, conflict) read
    from tokens; conflict is True when one slot was given two values."""
    slots, words, compass = {}, [], []
    conflict = False

    def put(slot, label, j):
        """Fill `slot`; returns the next index. A lone letter right after the
        part is the part's own code letter ("Level 2 A"), not the word "a",
        and it stays on that part so "Level 2 A Zone 4" != "Level 2 Zone 4 A"."""
        nonlocal conflict
        if j < len(tok) and len(tok[j]) == 1 and tok[j].isascii() and tok[j].isalpha():
            label, j = f"{label} {tok[j].upper()}", j + 1
        if j + 1 < len(tok) and tok[j] in _CONNECT and _codelike(tok[j + 1]):
            conflict = True
        if slots.setdefault(slot, label) != label:
            conflict = True
        return j

    i = 0
    while i < len(tok):
        w = tok[i]
        lvl = _level(tok, i)
        if lvl is not None:
            n, j = lvl
            i = put("level", "Level %d" % n if n >= 0 else ("Basement" if n == -1 else "Basement %d" % -n), j)
            continue
        room = _room(tok, i)
        if room:
            i = put("room", room[0], room[1])
            continue
        if w in _STAIR:
            j = i + 1
            label = "Stair"
            if j < len(tok) and tok[j] == "core":
                label, j = "Stair core", j + 1
            else:
                got = _code(tok, j)
                if got:
                    label, j = f"Stair {got[0]}", got[1]
            i = put("stair", label, j)
            continue
        if w in _CODED:
            j = i + 2 if w == "grid" and i + 1 < len(tok) and tok[i + 1] == "line" else i + 1
            got = _code(tok, j)
            if got:
                slot, title = _CODED[w]
                i = put(slot, f"{title} {got[0]}", got[1])
                continue
        if w in _COMPASS:
            compass.append(w)
        elif w not in _FILLER:
            words.append(w)
        i += 1
    return slots, words, compass, conflict


def _require_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a place needs some text")


def normalise(text):
    """{"kind", "value", "label", "raw"} for one spoken place.

    kind is "level" | "room" | "zone" | "block" | "other". Raises ValueError on
    None or blank text: no words is not a place, and an empty group key would
    gather everything nobody placed."""
    _require_text(text)
    tok = _tokens(text)
    slots, words, compass, conflict = _parts(tok)
    conflict = conflict or _has_measurement(tok)
    # Two different values for one part ("level 2 to level 3") is two places,
    # not one; keeping either would merge it into a place it is not.
    if not slots or conflict:
        value = _casefold_collapse(text)
        return {"kind": "other", "value": value, "label": " ".join(text.split()), "raw": text}
    label = " ".join([slots[s] for s in _SLOTS if s in slots] + words + compass)
    kind = next(k for s, k in _KIND_BY_SLOT if s in slots)
    return {"kind": kind, "value": label.casefold(), "label": label, "raw": text}


def _strip_prefix(s):
    s = s.strip()
    return s[len("place:"):] if s.casefold().startswith("place:") else s


def _resolve(text, aliases):
    """The place text an alias pins `text` to, or `text` itself. One hop: an
    alias's right-hand side is not looked up again, so a loop cannot form."""
    _require_text(text)
    if not aliases:
        return text
    table = {_casefold_collapse(_strip_prefix(k)): _strip_prefix(v)
             for k, v in aliases.items() if isinstance(k, str) and isinstance(v, str)}
    return table.get(_casefold_collapse(_strip_prefix(text))) or text


def resolve(text, aliases=None):
    """The place text `aliases` pins `text` to, or `text` itself -- the text
    `place_key` normalises. Callers that need a group's label to agree with its
    key normalise this. Raises ValueError on None or blank text, as
    `normalise` does."""
    return _resolve(text, aliases)


def place_key(text, aliases=None):
    """The key groups are keyed on: "kind:value".

    `aliases` is a plain dict the caller builds from name_aliases rows with
    kind='other' whose wrong_term/right_term carry a "place:" prefix, e.g.
    {"the new block": "Block C"} (a "place:" prefix on either side is
    tolerated). Keys are matched casefolded and whitespace-collapsed; the
    right-hand side is then normalised like any spoken place."""
    got = normalise(_resolve(text, aliases))
    return f"{got['kind']}:{got['value']}"


def same_place(a, b, aliases=None):
    """True when two spoken places are one group."""
    return place_key(a, aliases) == place_key(b, aliases)
