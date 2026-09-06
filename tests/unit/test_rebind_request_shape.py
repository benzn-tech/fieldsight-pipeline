"""The producer's contract: what a batched multi-speaker session must look like.

Not a test of the clustering — that needs audio and a GPU-less minute of ONNX. This
pins the shape of the request, which is where the two real failures lived: a payload
whose turns carry no `source_filename` groups nothing, and one whose labels all come
from a single call is correctly skipped.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "scripts"))
import rebind_verify  # noqa: E402


class _FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix=None):
                yield {"Contents": [{"Key": Prefix + k} for k in outer._objects]}
        return _P()

    def get_object(self, Bucket=None, Key=None):
        body = json.dumps(self._objects[Key.rsplit("/", 1)[-1]]).encode()

        class _B:
            def read(self_inner):
                return body
        return {"Body": _B()}


def _transcript(labels):
    """A minimal AWS-Transcribe-shaped doc with one word per label."""
    return {"results": {"transcripts": [{"transcript": " ".join(labels)}],
                        "items": [{"type": "pronunciation",
                                   "start_time": str(i * 4.0),
                                   "end_time": str(i * 4.0 + 3.5),
                                   "speaker_label": lab,
                                   "alternatives": [{"content": f"w{i}",
                                                     "confidence": "1.0"}]}
                                  for i, lab in enumerate(labels)]}}


def test_two_calls_two_labels_yields_four_pairs(monkeypatch):
    sid = "sid" + "a" * 32
    objects = {
        f"ben_2026-08-27_11-06-35_{sid}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
        f"ben_2026-08-27_11-08-03_{sid}_c0003_bn4_off0.0_to114.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
    }
    monkeypatch.setattr(rebind_verify.boto3, "client", lambda _s: _FakeS3(objects))
    turns = rebind_verify.build_turns("b", "Ben_UCPK2", "2026-08-27", sid)

    pairs = {(t["source_filename"], t["speaker_label"]) for t in turns}
    assert len(pairs) == 4, f"expected 4 (call,label) pairs, got {sorted(pairs)}"
    assert len({src for src, _ in pairs}) == 2
    assert all(t["source_filename"] and t["speaker_label"] for t in turns)
    assert all(isinstance(t["start_sec"], (int, float)) for t in turns)


def test_turns_from_another_session_are_not_collected(monkeypatch):
    """One day holds several sessions; grouping across them would be wrong."""
    mine, other = "sid" + "a" * 32, "sid" + "b" * 32
    objects = {
        f"ben_2026-08-27_11-06-35_{mine}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
        f"ben_2026-08-27_14-00-00_{other}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
    }
    monkeypatch.setattr(rebind_verify.boto3, "client", lambda _s: _FakeS3(objects))
    turns = rebind_verify.build_turns("b", "Ben_UCPK2", "2026-08-27", mine)
    assert {t["source_filename"] for t in turns} == {
        f"ben_2026-08-27_11-06-35_{mine}_c0000_bn3_off0.0_to86.0_srcwav.json"}
