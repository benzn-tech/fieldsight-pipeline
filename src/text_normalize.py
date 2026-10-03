# src/text_normalize.py
"""Pure alias substitution + diff-candidate extraction for editable content
correction (spec §5.4 / §7). NO psycopg, NO I/O -- imported by both the in-VPC
re-embed path and the non-VPC embed lambda, and unit-tested in isolation.

normalize() is whole-word (regex \b boundaries, so 'Mackon' never rewrites
inside 'Mackonsson') and case-aware (the replacement adopts the surface casing
of the matched token: lower/Title/UPPER). Aliases are applied in the order the
caller supplies them (the caller sorts site-scoped before company-scoped, so
the more specific alias wins -- spec §7 scope precedence)."""
import re

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]+")


def _match_case(surface: str, replacement: str) -> str:
    if surface.isupper():
        return replacement.upper()
    if surface[:1].isupper() and surface[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def term_pattern(wrong):
    """The ONE whole-word, case-insensitive pattern used to both find and
    rewrite a term. Shared so a preview count can never disagree with what a
    later rewrite actually changes."""
    return re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)


def normalize(text, aliases):
    if not text or not aliases:
        return text
    out = text
    for a in aliases:
        wrong = (a.get("wrong_term") or "").strip()
        right = a.get("right_term") or ""
        if not wrong:
            continue
        pattern = term_pattern(wrong)
        out = pattern.sub(lambda m: _match_case(m.group(0), right), out)
    return out


def occurrences(text, wrong):
    """How many whole-word matches of `wrong` normalize() would rewrite in
    `text` (0 for empty text/term). Same pattern as normalize -- counts and
    rewrites stay in lockstep."""
    wrong = (wrong or "").strip()
    if not text or not wrong:
        return 0
    return len(term_pattern(wrong).findall(text))


def first_match_span(text, wrong):
    """(start, end) of the first whole-word match, or None -- lets a caller
    build a preview snippet around the exact text normalize() would touch."""
    wrong = (wrong or "").strip()
    if not text or not wrong:
        return None
    m = term_pattern(wrong).search(text)
    return m.span() if m else None


def _proper_nounish(tok: str) -> bool:
    # Capitalized or ALLCAPS multi-char token -- a plausible name/product.
    return len(tok) > 1 and (tok[0].isupper())


def diff_candidates(before, after):
    """Tokens present in `after` but not in `before` that look like proper
    nouns -- the D2 glossary candidates surfaced after an edit. De-duplicated,
    order-preserving."""
    before_tokens = set(_TOKEN_RE.findall(before or ""))
    seen, out = set(), []
    for tok in _TOKEN_RE.findall(after or ""):
        if tok in before_tokens or tok in seen or not _proper_nounish(tok):
            continue
        seen.add(tok)
        out.append(tok)
    return out


# How alike a misheard name and its correction must be spelled to be learned:
# "Tikaha" -> "TEKAHA" is 0.67, "Mackon" -> "Macon" 0.91; "Monday" -> "Friday"
# (a different day, not a mishearing) is 0.5 and is not learned.
LEARN_SIMILARITY = 0.6


def learned_pairs(before, after):
    """[(wrong, right)] an edit teaches the glossary (owner, 2026-10-02).

    Only the narrow case that is a mishearing and not a change of meaning: ONE
    word replaced by one or two words that look like a name (each capitalised)
    and are spelled like it -- "Tikaha" -> "TEKAHA", "Tikaha" -> "Te Kaha".
    Anything else (a rewritten sentence, a different day, a new fact) teaches
    nothing; the edit still saves. The learned row is visible and can be
    undone, because it applies across the site.
    """
    import difflib
    a = _TOKEN_RE.findall(before or "")
    b = _TOKEN_RE.findall(after or "")
    out = []
    sm = difflib.SequenceMatcher(a=[t.lower() for t in a], b=[t.lower() for t in b],
                                 autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or i2 - i1 != 1 or not 1 <= j2 - j1 <= 2:
            continue
        wrong, right_toks = a[i1], b[j1:j2]
        if not all(_proper_nounish(t) for t in right_toks):
            continue
        right = " ".join(right_toks)
        ratio = difflib.SequenceMatcher(
            a=wrong.lower(), b="".join(right_toks).lower()).ratio()
        if ratio < LEARN_SIMILARITY or wrong.lower() == right.lower():
            continue
        if (wrong, right) not in out:
            out.append((wrong, right))
    return out
