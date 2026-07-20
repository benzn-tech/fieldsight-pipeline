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


def normalize(text, aliases):
    if not text or not aliases:
        return text
    out = text
    for a in aliases:
        wrong = (a.get("wrong_term") or "").strip()
        right = a.get("right_term") or ""
        if not wrong:
            continue
        pattern = re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE)
        out = pattern.sub(lambda m: _match_case(m.group(0), right), out)
    return out


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
