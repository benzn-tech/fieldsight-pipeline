"""content_hash.normalize / content_hash + the golden-fixture parity guard
(spec 2026-07-26 §2 / §7). This is the Python half of the cross-language
parity contract; the JS twin asserts the SAME golden file byte-for-byte.
"""
import json
import os
import unicodedata

from content_hash import content_hash, normalize

GOLDEN = os.path.join(os.path.dirname(__file__), "..", "..", "src",
                      "content_hash_golden.json")


# ---- normalize(): the exact, ordered, cross-language spec ----
def test_normalize_trims_and_collapses_internal_whitespace():
    assert normalize("  Loose   handrail  on level 3 ") == "loose handrail on level 3"


def test_normalize_collapses_tabs_and_newlines_to_single_space():
    assert normalize("Missing guardrail\tnear\nthe edge") == "missing guardrail near the edge"


def test_normalize_casefolds():
    assert normalize("LOOSE Handrail") == "loose handrail"


def test_normalize_keeps_punctuation():
    # punctuation is meaningful -- stripping it would widen collisions.
    assert normalize("Rebar @ 200mm c/c; check.") == "rebar @ 200mm c/c; check."


def test_normalize_none_is_empty():
    assert normalize(None) == ""


def test_normalize_is_nfc():
    # 'e' + U+0301 combining acute (decomposed) and U+00E9 precomposed hash
    # identically after NFC. Built from escapes so the source bytes are ASCII.
    decomposed = "café rebar"
    precomposed = "café rebar"
    # Rebuild both forms from a single base via unicodedata so they genuinely
    # differ on the wire regardless of how this source file was stored.
    _base = decomposed
    decomposed = unicodedata.normalize("NFD", _base)
    precomposed = unicodedata.normalize("NFC", _base)
    assert decomposed != precomposed                 # different code points on the wire
    assert normalize(decomposed) == normalize(precomposed)
    assert content_hash(decomposed) == content_hash(precomposed)


# ---- content_hash(): stability + distinctness ----
def test_hash_is_sha256_hex():
    h = content_hash("anything")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_empty_hash_is_canonical_sha256_of_empty():
    # sanity anchor: sha256 of the empty byte string.
    assert content_hash("") == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_hash_stable_across_simulated_reextraction():
    # Re-extraction is delete+reinsert: fresh uuids, SAME text. The key must
    # reproduce -- the whole point of hashing displayed text, not any uuid.
    original_text = "Loose handrail on level 3 near the stair core"
    before = content_hash(original_text)
    reextracted_text = original_text            # same extraction JSON re-ingested
    assert content_hash(reextracted_text) == before


def test_hash_stable_across_whitespace_and_case_variants():
    canonical = content_hash("Loose handrail on level 3")
    assert content_hash("  Loose   handrail on level 3  ") == canonical
    assert content_hash("LOOSE HANDRAIL ON LEVEL 3") == canonical


def test_each_of_the_three_sources_produces_a_hash():
    # #1 topic_flag -> finding.observation, #2 observation -> prose obs, #3
    # topic_quality -> topics.title. Each is just text; each yields a key.
    finding_observation = "Scaffold tie missing at bay 4"
    prose_observation = "No edge protection on the east slab"
    topic_title = "Concrete pour quality - level 2"
    hashes = {content_hash(finding_observation),
              content_hash(prose_observation),
              content_hash(topic_title)}
    assert len(hashes) == 3                      # three distinct, non-empty keys
    assert all(len(h) == 64 for h in hashes)


def test_distinct_texts_produce_distinct_hashes():
    assert content_hash("hazard A") != content_hash("hazard B")


# ---- the golden-fixture parity guard (the load-bearing anti-drift test) ----
def test_golden_fixtures_match_content_hash():
    data = json.load(open(GOLDEN, encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 6                       # a meaningful sample, not a token pair
    for c in cases:
        assert content_hash(c["input"]) == c["hash"], c["input"]


def test_golden_fixtures_are_ascii_latin_for_js_parity():
    # casefold() (Python) and toLowerCase() (JS) diverge on exotic scripts;
    # the fixtures stay ASCII/Latin so the JS twin matches byte-for-byte.
    data = json.load(open(GOLDEN, encoding="utf-8"))
    for c in data["cases"]:
        c["input"].encode("ascii")               # raises if a non-ASCII char sneaks in
