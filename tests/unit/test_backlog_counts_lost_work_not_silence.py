"""Unit: the loss alarm counts lost work, not sessions where nobody spoke.

Measured 2026-09-09 against the ten sessions this alarm was actually reporting
on prod. NINE of them held transcripts containing "[background noise]", an empty
string, or nothing but the device saying "Recording started." — sessions the
pipeline skipped *correctly*. Exactly one was a real loss: two speakers, 200+
characters, "...they don't meet the requirements".

An alarm that is ninety percent noise gets ignored inside a week, and this
codebase has already paid for that lesson once — counting the 42 silent requests
alongside the 11 real ones would have parked the number at 42 forever.

The old test asked "does a transcript object exist". This asks the question that
was meant: did a person say anything. The fixtures below are the verbatim
transcript texts from that measurement, not invented examples.
"""
import json

import pytest

bl = pytest.importorskip(
    "lambda_extraction_backlog",
    reason="requires the backlog lambda's dependencies (installed in CI)")


class _Body:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return self._p


class _S3:
    """Returns whatever transcript text it was constructed with."""

    def __init__(self, text=None, raises=False, malformed=False):
        self.text = text
        self.raises = raises
        self.malformed = malformed

    def get_object(self, Bucket=None, Key=None):
        if self.raises:
            raise RuntimeError("S3 said no")
        if self.malformed:
            return {"Body": _Body(b"not json at all")}
        doc = {"results": {"transcripts": [{"transcript": self.text}]}}
        return {"Body": _Body(json.dumps(doc).encode())}


# Verbatim from the ten prod sessions the alarm was reporting.
SILENT = [
    "[background noise]",
    "",
    "   ",
    "Recording started. Recording started. Recording started",
]
REAL = ("I wouldn't know how he was. Because I They don't meet the requirements "
        "for the bracing so we need to go back")


@pytest.mark.parametrize("text", SILENT)
def test_a_session_where_nobody_spoke_is_not_lost_work(text):
    assert bl._has_real_speech(_S3(text), "k") is False


def test_the_one_real_loss_is_still_reported():
    """The whole point. Nine false alarms are worth removing only if the tenth
    still comes through."""
    assert bl._has_real_speech(_S3(REAL), "k") is True


def test_a_short_utterance_that_is_not_a_known_marker_counts_as_speech():
    """Brief is not the same as empty. Guessing that a short sentence is
    worthless is how the one real loss in ten would be thrown away, and a worker
    saying "the slab cracked" is four words."""
    assert bl._has_real_speech(_S3("The slab cracked."), "k") is True


def test_an_unreadable_transcript_counts_as_speech():
    """FAILS OPEN, deliberately. A transient S3 error must produce a false alarm
    rather than silently hide a genuinely lost session — the expensive mistake
    on this alarm is under-reporting, not over-reporting."""
    assert bl._has_real_speech(_S3(raises=True), "k") is True
    assert bl._has_real_speech(_S3(malformed=True), "k") is True


def test_the_markers_are_matched_case_insensitively():
    """The recogniser is not consistent about capitalisation and the device
    announcement text has changed spelling at least once."""
    assert bl._has_real_speech(_S3("[Background Noise]"), "k") is False
    assert bl._has_real_speech(_S3("RECORDING STARTED."), "k") is False


def test_a_long_transcript_is_never_dismissed_by_a_marker():
    """A real conversation that happens to contain a marker somewhere must not
    be discarded because of it — the length check runs first for that reason."""
    long_with_marker = REAL + " [background noise] " + REAL
    assert bl._has_real_speech(_S3(long_with_marker), "k") is True
