"""A control character in model output must not silently disable a feature.

2026-09-08, prod. An extraction came back with

    "time_range": "13:37 \\x0e2\\x0813:37"

-- SHIFT OUT and BACKSPACE where the en dash belongs. `_TIME_RANGE_RE` correctly
refused it, `parse_time_range` returned None as designed, and the topic simply
got no time window, so no photo bound to it. Nothing raised. Nothing logged. The
guard did its job and the feature stopped working, and the only reason it was
found is that someone read the artifact.

Rare, and total when it lands: 123 prod artifacts, 281 topics, exactly 2 bad --
both in the SAME session, which therefore lost photo binding on every topic it
had.

Scrubbed in `extract_json` rather than in the parsers because there are TWO
`parse_time_range` implementations (photo_binding, chunking), and two copies of
one rule is how one gets fixed and the other does not.
"""
import json
import pytest

lu = pytest.importorskip("llm_utils")
pb = pytest.importorskip("photo_binding")
ck = pytest.importorskip("chunking")

REAL = "13:37 \x0e2\x0813:37"        # verbatim from the prod artifact
REAL2 = "13:38 \x080413:38"


def test_the_real_prod_value_reaches_the_parser_clean():
    """End to end through the choke point, with the exact bytes that broke it."""
    got = lu.extract_json(json.dumps({"topics": [{"time_range": REAL}]}))
    tr = got["topics"][0]["time_range"]
    assert tr == "13:37 213:37", repr(tr)
    assert not any(ord(c) < 0x20 for c in tr)


def test_prose_whitespace_survives():
    """Summaries legitimately contain newlines and tabs. Scrubbing those would
    trade a rare defect for a constant one."""
    text = "line one\nline two\twith a tab\r\n"
    assert lu.extract_json(json.dumps({"summary": text}))["summary"] == text


def test_every_extraction_tier_is_scrubbed():
    """extract_json has three fallbacks that return independently. A scrub wired
    into only the first would pass a naive test and still leak on real output,
    which usually arrives fenced or with prose around it."""
    payload = '{"time_range": "13:37 \\u000e2\\u000813:37"}'
    for raw in ("```json\n" + payload + "\n```",
                payload,
                "Here you go:\n" + payload + "\nthanks"):
        got = lu.extract_json(raw)
        assert got is not None, raw
        assert not any(ord(c) < 0x20 for c in got["time_range"]), raw


def test_nested_values_are_scrubbed_not_just_top_level():
    raw = json.dumps({"topics": [{"evidence": [{"quote": "a\x0eb"}]}]})
    assert lu.extract_json(raw)["topics"][0]["evidence"][0]["quote"] == "ab"


def test_both_parsers_refuse_the_raw_value_which_is_why_this_matters():
    """Pins the consequence, not the input: without the scrub BOTH consumers
    return None, and None here means "this topic owns no time", which is
    indistinguishable from a topic that genuinely had none."""
    assert pb.parse_time_range(REAL) is None
    assert ck.parse_time_range(REAL) is None
    assert pb.parse_time_range(REAL2) is None
