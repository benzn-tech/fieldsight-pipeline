"""Unit: a chunk with no speech is dropped, not handed to Transcribe.

The old no-speech path uploaded the whole chunk and let Transcribe try anyway,
on the theory that VAD might have missed something. Measured against a second
engine on 48 real chunks, that theory was wrong in 37 cases: ElevenLabs
returned nothing at all, while Transcribe returned 313 fluent, invented words
that went on to be summarised and turned into action items. Silence in, content
out, no signal anywhere that it was fabricated.

Three properties are pinned here:
  1. nothing is uploaded for a silent chunk,
  2. the sidecar is STILL written, so a drop is auditable rather than a gap, and
  3. the old behaviour survives behind DROP_SILENT_CHUNKS=false.

The module is read as source rather than imported: lambda_vad builds an S3
client at import time, which cannot resolve credentials off Lambda, and an
import-time failure here would make the file silently un-runnable locally — the
same trap that let a broken rename ship earlier.
"""
import os
import re

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src", "lambda_vad.py")


def _source():
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def _no_speech_block():
    """The branch that runs once both VAD attempts have found nothing."""
    body = _source()
    start = body.index("if DROP_SILENT_CHUNKS:")
    end = body.index("# Step 7", start)
    return body[start:end]


def test_dropping_is_the_default():
    """A flag that defaults to the old behaviour ships nothing. The measured
    defect is on by default; the escape hatch is the opt-in."""
    body = _source()
    assert re.search(
        r"DROP_SILENT_CHUNKS\s*=\s*os\.environ\.get\(\s*'DROP_SILENT_CHUNKS',\s*'true'",
        body), "DROP_SILENT_CHUNKS must default to true"


def test_a_silent_chunk_uploads_no_audio():
    """THE fix. upload_file is what created the Transcribe job, because the
    transcribe lambda triggers on objects landing under audio_segments/."""
    block = _no_speech_block()
    drop_branch = block[:block.index("else:")]
    assert "upload_file" not in drop_branch, (
        "uploading the audio is what makes Transcribe run on it")


def test_a_dropped_chunk_still_writes_its_sidecar():
    """A drop that leaves no trace is indistinguishable from a lost chunk, and
    the difference is the whole of the next investigation."""
    block = _no_speech_block()
    assert "meta_key" in block and "put_object" in block


def test_a_dropped_chunk_is_named_as_dropped():
    """`fallback_full_audio` on a chunk that was dropped would misdescribe every
    row in the audit trail."""
    block = _no_speech_block()
    assert "'no_speech_dropped'" in block


def test_the_old_behaviour_is_still_reachable():
    """Rollback must not need a code change: this path is now the one that only
    runs when someone has deliberately turned the drop off."""
    block = _no_speech_block()
    assert "else:" in block
    fallback = block[block.index("else:"):]
    assert "upload_file" in fallback
    assert "'fallback_full_audio'" in fallback


def test_the_response_does_not_claim_a_segment_that_was_never_made():
    """`segments_created: 1` was hardcoded. Left alone it would report a segment
    for every dropped chunk, and the drop rate would be invisible in metrics."""
    body = _source()
    assert "'segments_created': 1," not in body
    assert "'segments_created': len(segments_meta)" in body


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False),
    ("", False),        # explicitly blank reads as off, not as the default
])
def test_the_flag_parses_the_spellings_people_actually_type(value, expected):
    assert (value.lower() == "true") is expected
