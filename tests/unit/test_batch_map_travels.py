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

    The spec for this change listed four consumers and had two of them wrong: it named the
    org-api viewer, which does not call `normalize_transcript` at all, and omitted the
    meeting minutes, which does. A list maintained by hand was wrong the first time it was
    written, so this asserts the property instead.

    The check is that the `normalize_transcript` call is NESTED INSIDE a call to
    `rebase_turns_from_embedded_map`, not that the name appears nearby. An earlier version
    searched a window of source text, and deleting the rebase call while leaving its name in
    a neighbouring comment kept the suite green -- a guard that a comment satisfies.
    """
    import ast

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src")

    # `lambda_extract_session` rebases through the sidecar it fetches itself -- it is the
    # one consumer that pre-dates the embedded map and still reads pre-change artifacts.
    EXEMPT = {"lambda_extract_session.py": "fetches the sidecar directly",
              "transcript_utils.py": "defines normalize_transcript"}

    def _name(node):
        return getattr(node, "id", None) or getattr(node, "attr", None)

    offenders = []
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py") or name in EXEMPT:
            continue
        try:
            tree = ast.parse(open(os.path.join(src, name), encoding="utf-8").read())
        except SyntaxError:
            continue
        rebased = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) ==                     "rebase_turns_from_embedded_map":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and                             _name(inner.func) == "normalize_transcript":
                        rebased.add(id(inner))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "normalize_transcript"                     and id(node) not in rebased:
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, (
        "these resolve absolute times from a transcript without applying its batch map, so "
        "batched turns render early -- by the trimmed overlap, and by the whole of any "
        "VAD-dropped chunk the window bridged: " + ", ".join(offenders))


def test_the_other_shape_of_resolver_is_covered_too():
    """The invariant above can only see consumers that call `normalize_transcript`.

    The org-api transcript viewer has its own `file_time_sec + segment.start` arithmetic, so
    it was structurally invisible to that check and shipped unrebased -- rendering every
    post-gap segment up to a window early, and dropping whole files out of a topic window
    because their filename span under-states the wall clock they cover. Its arithmetic now
    lives in one named helper, and this asserts the bare form has not come back.
    """
    import ast

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src", "lambda_org_api.py")
    tree = ast.parse(open(src, encoding="utf-8").read())
    # The helper's own no-map fallback is the one sanctioned place for the bare form.
    # Both helpers own the bare form; everywhere else must go through them.
    names = {"_org_segment_abs_sec", "_org_transcript_file_end_sec"}
    helpers = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(helpers) == len(names), "a helper this invariant is about has been removed"
    inside = {ln for h in helpers
              for ln in range(h.lineno, (h.end_lineno or h.lineno) + 1)}

    bare = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            continue
        if node.lineno in inside:
            continue
        left = getattr(node.left, "id", None)
        # ANY right-hand side, not just `seg_*`. Naming the variable seen last time is
        # how this guard missed `file_time_sec + word_start` sitting thirty lines away.
        if left == "file_time_sec":
            bare.append(node.lineno)
    assert not bare, (
        f"lambda_org_api.py:{bare} adds a segment offset to the file time directly; a "
        f"batched transcript needs _org_segment_abs_sec so a bridged gap is accounted for")
