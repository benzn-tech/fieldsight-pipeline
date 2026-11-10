"""One tokeniser for "does this turn talk about this item?".

Shared by the site-scoped item history and the item -> transcript evidence
chain, so both answer the question with the same notion of "same words". Two
tokenisers would drift, and an item would be "supported" in one view and not in
the other.

Bilingual by construction. ASCII text becomes lower-cased word tokens with a
short English stop-word list dropped; CJK text becomes character bigrams (a
lone CJK character between non-CJK characters is a unigram). Chinese is never
ASCII-folded or stripped: a tokeniser that only sees [a-z0-9] scores every
Chinese item zero and makes it look unsupported, which is a language-correlated
bias in the number this feeds.

Pure: no AWS, no DB.
"""
import re
import unicodedata
from collections import defaultdict

from evidence_match import is_cjk, strip_cjk_spacing

RULE_VERSION = "lexical_v1"

# Deliberately small and explicit: articles, auxiliaries, prepositions,
# pronouns, and the commonest conjunctions. Anything domain-bearing ("slab",
# "level", "north") must survive.
STOP_WORDS = frozenset("""
a an the
is am are was were be been being do does did have has had
will would shall should can could may might must
of in on at to for from by with about into onto over under up down off out
as than then so if or and but not no
i me my we us our you your he him his she her it its they them their
this that these those there here
re ll ve
won isn aren wasn weren didn doesn hasn haven couldn shouldn wouldn
ok okay yeah yep just get got go going like um uh
""".split())
# "won isn ...": what a negated contraction leaves once `_CONTRACTION` strips
# its 't (where `_NEGATION` did not apply). "don" is NOT here: "don't" goes
# through `_NEGATION` to "do", and "Don" is a name. "mm" is NOT a filler here:
# it is the millimetre unit. "ok okay ...": spoken fillers, since turns are
# speech. "re ll ve" only catch contractions written without an apostrophe.
#
# Single letters and "right"/"left" are NOT stop words: site labels use them
# ("Block D", "Level M") and left/right tell twin locations apart.

_ASCII_WORD = re.compile(r"[a-z0-9]+")

# A contraction suffix attached by an apostrophe (' or U+2019), stripped before
# tokenising. Without this, `[a-z0-9]+` split "Tom's" into "tom" + "s" and
# "Tom's crane" matched "it's fine" on "s". n't is covered by 't: "won't" ->
# "won", "isn't" -> "isn", and those stems are stop words above.
_CONTRACTION = re.compile(r"(?<=[a-z0-9])['\u2019](?:s|t|re|ll|ve|d|m)\b")

# A negated contraction whose stem without "n't" is itself a stop word ("don't"
# -> "do", "isn't" -> "is") drops the whole n't; otherwise ("won't" -> "wo") the
# word is left for `_CONTRACTION`'s 't rule ("won").
_NEGATION = re.compile(r"\b([a-z0-9]+)n['\u2019]t\b")


def _strip_negation(m):
    return m.group(1) if m.group(1) in STOP_WORDS else m.group(0)


def _is_cjk_word_char(ch):
    # CJK punctuation (e.g. U+3002) sits inside the CJK class but carries no
    # meaning; only letter-like CJK characters form tokens.
    return is_cjk(ch) and ch.isalnum()


def tokens(text):
    """ASCII word tokens (stop words dropped) and CJK bigrams, in text order.

    Duplicates are kept; `overlap` ignores multiplicity.

    A lone CJK character becomes a unigram, which never equals any bigram, so
    "楼" in an item does not match "二楼" in a turn. That costs recall, never
    precision.
    """
    t = unicodedata.normalize("NFKC", text or "")
    t = strip_cjk_spacing(t).lower()
    t = _NEGATION.sub(_strip_negation, t)
    t = _CONTRACTION.sub("", t)
    out = []
    i, n = 0, len(t)
    while i < n:
        if _is_cjk_word_char(t[i]):
            j = i
            while j < n and _is_cjk_word_char(t[j]):
                j += 1
            run = t[i:j]
            if len(run) == 1:
                out.append(run)
            else:
                out.extend(run[k:k + 2] for k in range(len(run) - 1))
            i = j
        else:
            j = i
            while j < n and not _is_cjk_word_char(t[j]):
                j += 1
            out.extend(w for w in _ASCII_WORD.findall(t[i:j])
                       if w not in STOP_WORDS)
            i = j
    return out


def overlap(item_text, turn_text):
    """Containment of the item's tokens in the turn's.

    Returns (|T(item) & T(turn)| / |T(item)|, sorted matched tokens), with
    multiplicity ignored. An item with no tokens scores 0.0 -- nothing to
    support is not the same as fully supported.

    A tiny item (e.g. digits only, "2") can score 1.0 against any turn that
    mentions the number; callers apply a minimum item token count before
    trusting the score.
    """
    item = set(tokens(item_text))
    if not item:
        return 0.0, []
    matched = item & set(tokens(turn_text))
    return len(matched) / len(item), sorted(matched)


# ---------------------------------------------------------------------------------------
# Place values and the twin check (moved from scripts/item_support_eval/score.py so
# Lambda code can use them; the eval imports them from here)
# ---------------------------------------------------------------------------------------

_EN_CLASS = {"unit": "unit", "level": "level", "floor": "level", "lot": "lot",
             "room": "room", "block": "block"}
_EN_NUM = re.compile(r"\b(unit|level|lot|room|block|floor)s?\s*(?:no\.?|number|#)?\s*"
                     r"(\d+)([a-z]?)(?:st|nd|rd|th)?\b", re.IGNORECASE)
# A single capital letter (Block C). "I" is left out: "the unit I checked".
_EN_LETTER = re.compile(r"\b((?i:unit|level|lot|room|block|floor))\s+([A-HJ-Z])\b")
_EN_DIR = re.compile(r"\b(east|west|north|south)(?:ern)?\b", re.IGNORECASE)
_ZH_DIR = {"东": "east", "西": "west", "南": "south", "北": "north"}
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9}
_ZH_NUM = re.compile(r"([0-9]+|[一二三四五六七八九十两]+)\s*(号|楼|层|区|栋)")
_ZH_CLASS = {"楼": "level", "层": "level", "区": "block", "栋": "block", "号": "number"}


def cn_to_int(s):
    """Arabic digits, or a Chinese numeral up to 99 (十二, 二十, 两); None if unreadable."""
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        if "十" in tail:
            return None
        tens = _CN_DIGITS.get(head, None) if head else 1
        ones = _CN_DIGITS.get(tail, None) if tail else 0
        return None if tens is None or ones is None else tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def place_values(text):
    """{class: set of values} for the place tokens in `text`."""
    t = unicodedata.normalize("NFKC", text or "")
    out = defaultdict(set)
    for m in _EN_NUM.finditer(t):
        out[_EN_CLASS[m.group(1).lower()]].add(f"{int(m.group(2))}{m.group(3).lower()}")
    for m in _EN_LETTER.finditer(t):
        out[_EN_CLASS[m.group(1).lower()]].add(m.group(2).lower())
    for m in _EN_DIR.finditer(t):
        out["direction"].add(m.group(1).lower())
    for ch, val in _ZH_DIR.items():
        if ch in t:
            out["direction"].add(val)
    for m in _ZH_NUM.finditer(t):
        n = cn_to_int(m.group(1))
        out[_ZH_CLASS[m.group(2)]].add(str(n) if n is not None else m.group(1))
    return dict(out)


def twin_flags(item_text, turn_text):
    """Sorted class names where item and turn both carry a value and some pair differs."""
    a, b = place_values(item_text), place_values(turn_text)
    return sorted(c for c in a.keys() & b.keys()
                  if any(x != y for x in a[c] for y in b[c]))
