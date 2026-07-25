"""Content-key normalization + hash for the durable safety/quality resolved
state (compliance_resolutions, migration 0025). See spec 2026-07-26 §2.

PURE module — no boto3, no psycopg, no I/O. This is deliberate: the SAME
normalization MUST run here (org-api write + read endpoints) AND in a
JavaScript twin in the aggregator. A one-character drift between the two
silently orphans every resolved mark with no error, so the algorithm below is
kept dead-simple and unambiguous, and a golden-fixture file
(content_hash_golden.json) pins input->hash pairs that BOTH this module's
unit test and the future JS test assert against.

normalize(text) — the exact, ordered, cross-language spec:
  1. Coerce None -> "" (a missing field hashes like an empty one).
  2. Unicode NFC normalize (composed form; so a pre-composed 'é' and a
     decomposed 'e'+combining-accent hash identically).
  3. Strip leading/trailing whitespace AND collapse every internal run of
     whitespace to a single U+0020 space. `" ".join(text.split())` does both
     in one step (JS twin: `text.trim().replace(/\\s+/g, ' ')`).
  4. casefold() for case-insensitive identity (JS twin: `.toLowerCase()`).
     NOTE the casefold-vs-lowercase divergence on exotic scripts (German ß:
     casefold->"ss", toLowerCase->"ß"). For the ASCII/Latin site data in
     practice they agree, and the golden fixtures are ASCII/Latin so the JS
     twin matches byte-for-byte. Do not feed exotic-script text through a
     cross-language parity path without extending the fixtures first.
  5. Punctuation is NOT stripped — it is meaningful and stripping it widens
     collisions.

content_hash(text) = sha256 hex of the UTF-8 bytes of normalize(text).
"""
import hashlib
import unicodedata


def normalize(text: str | None) -> str:
    if text is None:
        text = ""
    text = unicodedata.normalize("NFC", str(text))
    text = " ".join(text.split())          # trim + collapse internal whitespace
    return text.casefold()


def content_hash(text: str | None) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()
