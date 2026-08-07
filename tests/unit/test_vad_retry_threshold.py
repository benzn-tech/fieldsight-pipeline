"""Unit: the VAD retry must be more permissive than the first attempt.

The retry existed to catch speech the first pass was unsure about, and it was
written as a constant — 0.25 — which was more permissive only while
VAD_THRESHOLD kept its 0.5 default. Both environments were later configured to
0.2, and "retry at 0.25" became a TIGHTER test that could not possibly succeed.

Nothing noticed, because the code still fell through to a fallback that sends
the whole chunk to Transcribe. On one real meeting, 45 of 129 chunks were
recorded with speech_duration_sec = 0 while their transcripts came back full of
ordinary sentences ("That's gonna manage my job Christchurch or Queenstown").
Transcribe was being asked to find words in audio the pipeline had labelled
silent, and the one- and two-word fragments came from that.

The rule is pinned as a RELATION, not a number: any future threshold change
keeps the retry below it automatically, which is the part that was missing.
"""
import os

import pytest

# Read the module rather than import it: importing lambda_vad pulls in boto3's
# credential chain, which fails on a developer machine and would have made this
# file silently un-runnable outside CI — the same "green locally, meaningless
# locally" trap that let a rename ship broken earlier today.
SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src", "lambda_vad.py")


def _source():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def test_the_retry_is_derived_from_the_configured_threshold():
    body = _source()
    assert "retry_threshold = round(VAD_THRESHOLD / 2" in body, (
        "the retry must follow the configured threshold, not a constant")


def test_no_hardcoded_retry_threshold_survives():
    """`threshold=0.25` next to a retry is exactly the shape that broke: correct
    against one default, silently inverted against another."""
    body = _source()
    assert "threshold=0.25" not in body, "a literal retry threshold reintroduces the bug"


@pytest.mark.parametrize("configured", [0.5, 0.4, 0.25, 0.2, 0.1, 0.05])
def test_the_retry_is_always_more_permissive(configured):
    """The property, at every setting anyone might pick — including the two
    that have actually been deployed (0.5 and 0.2)."""
    retry = round(configured / 2, 4)
    assert retry < configured, f"retry {retry} must be below {configured}"
    assert retry > 0, "a retry at zero would accept anything, including silence"


def test_the_fallback_no_longer_names_a_threshold_it_does_not_use():
    """The old log said "still no speech at 0.25" whatever the retry actually
    ran at. A log that states the wrong number sends the next person to the
    wrong place — this one cost an evening."""
    body = _source()
    assert "Still no speech at 0.25" not in body
