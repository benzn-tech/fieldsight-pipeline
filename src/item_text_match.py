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
don won isn aren wasn weren didn doesn hasn haven couldn shouldn wouldn
ok okay yeah yep just get got go going like um uh mm
""".split())
# "don won ...": what a negated contraction leaves once `_CONTRACTION` strips
# its 't. "ok okay ...": spoken fillers, since turns are speech. "re ll ve" only
# catch contractions written without an apostrophe.
#
# Single letters and "right"/"left" are NOT stop words: site labels use them
# ("Block D", "Level M") and left/right tell twin locations apart.

_ASCII_WORD = re.compile(r"[a-z0-9]+")

# A contraction suffix attached by an apostrophe (' or U+2019), stripped before
# tokenising. Without this, `[a-z0-9]+` split "Tom's" into "tom" + "s" and
# "Tom's crane" matched "it's fine" on "s". n't is covered by 't: "won't" ->
# "won", "isn't" -> "isn", and those stems are stop words above.
_CONTRACTION = re.compile(r"(?<=[a-z0-9])['\u2019](?:s|t|re|ll|ve|d|m)\b")


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
