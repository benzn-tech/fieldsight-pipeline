"""Unit: the map the sealer wrote is the map the readers use.

Plan: docs/superpowers/plans/2026-08-13-batch-by-wall-clock.md phase 5.

Four consumers turn a batched transcript into absolute times, and until now only
`lambda_extract_session` corrected for the batch origin. The other three — the ask agent,
ingest and the meeting minutes — read `base_time + word.start`, which is early by the
trimmed overlap at every seam and, since windows may bridge a VAD-dropped chunk, early by
up to two minutes after one.

The fix is not four S3 fetches. Three missing IAM grants have already broken this feature
silently, each behind an `except`-and-log, so the map travels **inside** the transcript and
the readers hold no S3 client at all.

Every test here drives the REAL sealer into a recording S3 double and feeds what it actually
wrote to the REAL reader. A hand-built map asserted at the reader's key is how the
`transcripts/` vs `audio_segments/` mismatch survived a green suite while every batched TEST
session silently fell back to filename arithmetic.
"""
import io
import json
import os
import sys
import wave
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import batch_seal  # noqa: E402
import batch_stitch as bs  # noqa: E402

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
PREFIX = "audio_segments/Ben_UCPK/2026-08-13"
RATE = 16000
T0 = datetime(2026, 8, 13, 9, 0, 0)


def _wav(seconds=30.0, value=1000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * RATE))
    return buf.getvalue()


def _unit(index):
    """Chunk 4 sits at T0, so the window's first member anchors the timeline the
    assertions are written against."""
    hms = (T0 + timedelta(seconds=30 * (index - 4))).strftime("%H-%M-%S")
    return f"{PREFIX}/Benl1_2026-08-13_{hms}_sid{SID}_c{index:04d}_off0.0_to30.0_srcwav.wav"


class _Body:
    def __init__(self, d):
        self._d = d

    def read(self):
        return self._d


class RecordingS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append(Key)
        self.objects[Key] = Body

    def copy_object(self, **kw):
        pass


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item=None, ConditionExpression=None):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def query(self, KeyConditionExpression=None, ExpressionAttributeValues=None):
        pk = ExpressionAttributeValues[":pk"]
        sk = ExpressionAttributeValues.get(":sk", "")
        return {"Items": [v for (p, s), v in sorted(self.items.items())
                          if p == pk and s.startswith(sk)]}


@pytest.fixture
def sealed():
    """A real batch bridging a VAD-dropped chunk: 4, 6, 7 with 5 gone.

    Returns (transcript_json_with_embedded_map, map_doc).
    """
    objects = {}
    for i in (4, 6, 7):
        objects[_unit(i)] = _wav()
    s3 = RecordingS3(objects)
    by_index = {i: {"chunk_key": _unit(i)} for i in (4, 6, 7)}
    batch_key = batch_seal.seal_batch(s3, "b", SID, [4, 6, 7], by_index, 0, FakeTable())
    assert batch_key, "the fixture must actually seal something"

    map_key = bs.map_key_for_audio(batch_key)
    doc = json.loads(s3.objects[map_key])
    # This is the embed step Phase 5b performs in the transcriber. Written here as the one
    # line it is, so the reader below is fed exactly what production will hand it.
    transcript = {"results": {}, bs.EMBEDDED_MAP_KEY: doc}
    return transcript, doc


def _turns(*offsets):
    """Turns as `normalize_transcript` produces them: filename arithmetic already applied,
    which for a batch means every one of them is wrong past the first member."""
    base = T0
    return {"speaker_turns": [
        {"speaker": "spk_0", "text": "x", "start_sec": o, "end_sec": o + 1.0,
         "abs_start": base + timedelta(seconds=o), "abs_end": base + timedelta(seconds=o + 1),
         "abs_start_str": "", "abs_end_str": ""}
        for o in offsets]}


def test_the_reader_gets_its_times_from_what_the_writer_actually_wrote(sealed):
    """Every turn lands on the wall-clock time its audio was recorded at.

    Chunks 4, 6 and 7 start 0 s, 60 s and 90 s after the batch's first sample. Filename
    arithmetic would put the second member's words at +30 s, because it cannot know a chunk
    is missing.
    """
    transcript, doc = sealed
    kept = [m["kept_duration_sec"] for m in doc["members"]]
    normalized = _turns(0.0, kept[0] + 0.5, kept[0] + kept[1] + 0.5)
    out = bs.rebase_turns_from_embedded_map(normalized, transcript)
    got = [t["abs_start"] for t in out["speaker_turns"]]

    assert got[0] == T0
    assert got[1] == T0 + timedelta(seconds=60.5), \
        "the second member is 60 s along the wall clock, not 30 -- chunk 5 was dropped"
    assert got[2] == T0 + timedelta(seconds=90.5)


def test_the_rendered_strings_follow_the_rebased_times(sealed):
    transcript, doc = sealed
    kept = [m["kept_duration_sec"] for m in doc["members"]]
    out = bs.rebase_turns_from_embedded_map(_turns(kept[0] + 0.5), transcript)
    assert out["speaker_turns"][0]["abs_start_str"] == "09:01:00"


def test_no_time_ever_lands_in_the_bridged_gap(sealed):
    """The 30 s chunk 5 occupied is not addressable: nobody spoke then, as far as this
    batch knows, and a resolved time inside it would be a claim that they did."""
    transcript, doc = sealed
    total = sum(m["kept_duration_sec"] for m in doc["members"])
    out = bs.rebase_turns_from_embedded_map(
        _turns(*[i / 2 for i in range(int(total * 2))]), transcript)
    for turn in out["speaker_turns"]:
        offset = (turn["abs_start"] - T0).total_seconds()
        assert not (30.0 < offset < 60.0), f"resolved into the dropped chunk at +{offset}"


def test_a_transcript_with_no_embedded_map_is_untouched():
    """Per-chunk transcripts are the common case and must cost nothing. A batch transcript
    written before this change also lands here, and keeps working through the sidecar path
    that extract-session still has."""
    normalized = _turns(5.0)
    before = normalized["speaker_turns"][0]["abs_start"]
    out = bs.rebase_turns_from_embedded_map(normalized, {"results": {}})
    assert out["speaker_turns"][0]["abs_start"] == before


def test_an_empty_or_malformed_map_is_ignored_rather_than_raising():
    """A reader that throws on a bad map turns a bounded time error into no transcript."""
    normalized = _turns(5.0)
    before = normalized["speaker_turns"][0]["abs_start"]
    for junk in ({}, {"members": []}, None, "not-a-dict"):
        out = bs.rebase_turns_from_embedded_map(normalized, {bs.EMBEDDED_MAP_KEY: junk})
        assert out["speaker_turns"][0]["abs_start"] == before


def test_a_turn_with_no_time_is_not_invented_one(sealed):
    transcript, _ = sealed
    out = bs.rebase_turns_from_embedded_map(
        {"speaker_turns": [{"speaker": "s", "text": "x", "abs_start": None}]}, transcript)
    assert out["speaker_turns"][0]["abs_start"] is None


# ---- no consumer may resolve absolute times without the map ----

def test_every_normalize_transcript_caller_rebases():
    """The invariant, not the four instances.

    The spec for this change listed four consumers. It had two of them wrong: it named the
    org-api viewer, which does not call `normalize_transcript` at all, and it omitted the
    meeting minutes, which does. A list maintained by hand was wrong the first time it was
    written, so this asserts the property instead: every call site either rebases or is
    named here with a reason.
    """
    import ast

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src")

    # `lambda_extract_session` rebases through the sidecar it fetches itself -- it is the
    # one consumer that pre-dates the embedded map and still reads pre-change artifacts.
    EXEMPT = {"lambda_extract_session.py": "fetches the sidecar directly (and prefers it)",
              "transcript_utils.py": "defines normalize_transcript"}

    offenders = []
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py") or name in EXEMPT:
            continue
        path = os.path.join(src, name)
        text = open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(text)
        except SyntaxError:                       # not ours to police
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            # A real call, not the word appearing in a docstring -- which is what a regex
            # over these files reports, and there are five of those.
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname != "normalize_transcript":
                continue
            window = " ".join(lines[max(0, node.lineno - 6):node.lineno + 5])
            if "rebase_turns_from_embedded_map" not in window:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "these resolve absolute times from a transcript without applying its batch map, so "
        "batched turns render early -- by the trimmed overlap, and by the whole of any "
        "VAD-dropped chunk the window bridged: " + ", ".join(offenders))


# ---- turns that span a splice are flagged, not eyeballed (phase 6) ----

def test_a_turn_that_spans_the_splice_is_flagged(sealed):
    """A bridged gap splices audio that is not contiguous in time, and the provider does
    not know that. It can emit ONE turn whose text runs across the join — fusing utterances
    up to two minutes apart into a single interval covering time when nobody spoke. Photo
    binding and claim provenance consume that interval.

    After rebasing, such a turn's own start and end are each individually correct, which is
    exactly why nothing downstream can notice. So the rebase marks it.
    """
    transcript, doc = sealed
    kept = [m["kept_duration_sec"] for m in doc["members"]]
    out = bs.rebase_turns_from_embedded_map({"speaker_turns": [
        {"speaker": "spk_0", "text": "...across the join...",
         "start_sec": kept[0] - 1.0, "end_sec": kept[0] + 1.0,
         "abs_start": T0, "abs_end": T0}]}, transcript)
    assert out["speaker_turns"][0]["crosses_gap"] is True


def test_a_turn_crossing_a_merely_adjacent_boundary_is_not_flagged():
    """Every batch has member boundaries; only the discontinuous ones are suspect. Flagging
    all of them would make the signal mean "this is a batch", which nothing needs."""
    doc = bs.build_map(SID, [
        bs.member(4, "k4", T0.isoformat(), 0.0, 30.0, seam="first"),
        bs.member(5, "k5", (T0 + timedelta(seconds=30)).isoformat(), 0.0, 30.0,
                  seam="adjacent"),
    ], sealed_by="arrival")
    out = bs.rebase_turns_from_embedded_map({"speaker_turns": [
        {"speaker": "s", "text": "x", "start_sec": 29.0, "end_sec": 31.0,
         "abs_start": T0, "abs_end": T0}]}, {bs.EMBEDDED_MAP_KEY: doc})
    assert out["speaker_turns"][0].get("crosses_gap") is False


def test_a_turn_wholly_inside_one_member_is_not_flagged(sealed):
    transcript, doc = sealed
    kept = [m["kept_duration_sec"] for m in doc["members"]]
    out = bs.rebase_turns_from_embedded_map({"speaker_turns": [
        {"speaker": "s", "text": "x", "start_sec": kept[0] + 2.0, "end_sec": kept[0] + 4.0,
         "abs_start": T0, "abs_end": T0}]}, transcript)
    assert out["speaker_turns"][0].get("crosses_gap") is False


def test_the_count_is_reported_even_when_it_is_zero(sealed, caplog):
    """Zero is the positive evidence that the check ran at all.

    Every guard in this feature that only logged on failure became indistinguishable from a
    guard that was never reached — three times, each behind a missing IAM grant. A count
    line on every batched transcript is what tells those two apart.
    """
    import logging
    transcript, _ = sealed
    with caplog.at_level(logging.INFO):
        bs.rebase_turns_from_embedded_map({"speaker_turns": []}, transcript)
    assert any("batch_splice_turns=0" in r.getMessage() for r in caplog.records), \
        f"no count line: {[r.getMessage() for r in caplog.records]}"
