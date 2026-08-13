"""Unit: the function that turns audio into a voiceprint vector, and back into a name.

Two operations, and they fail in opposite directions:

  * `enrol` writes to a profile. Writing a window that holds two voices poisons it
    permanently — a profile cannot be un-poisoned, only the contributing sample deleted —
    so the homogeneity guard runs BEFORE anything is stored, and a window it cannot judge
    is refused rather than accepted.
  * `match` names turns. A wrong confident name is worse than a missing one, so the
    duration floor is applied before the model is even asked, and everything ambiguous
    degrades to tentative or unknown.

The embedder is stubbed throughout: this file is about the wiring, and the model's own
fidelity is pinned separately by test_voiceprint_onnx_parity.py against committed reference
vectors. Nothing here downloads 84 MB.
"""
import json
import sys
import types

import numpy as np
import pytest

se = pytest.importorskip("lambda_speaker_embed")
import voiceprint_utils as vp  # noqa: E402


class FakeS3:
    """Records what was read, and can be told to deny a key the way S3 really does."""

    def __init__(self, objects=None, denied=()):
        self.objects = objects or {}
        self.denied = set(denied)
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key in self.denied:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        if Key not in self.objects:
            raise KeyError(Key)

        class B:
            def __init__(self, d):
                self._d = d

            def read(self):
                return self._d
        return {"Body": B(self.objects[Key])}


def _wav_bytes(seconds=5.0, sr=16000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.zeros(int(seconds * sr), dtype="<i2")).tobytes())
    return buf.getvalue()


@pytest.fixture
def stub_embedder(monkeypatch):
    """One vector per call, cycling, so two frames can be made to differ on demand."""
    calls = {"n": 0, "vectors": None}

    def embed(audio, sr):
        calls["n"] += 1
        vecs = calls["vectors"]
        if vecs is None:
            return np.ones(192, dtype=np.float32)
        return np.asarray(vecs[(calls["n"] - 1) % len(vecs)], dtype=np.float32)

    monkeypatch.setattr(se, "embed_audio", embed)
    return calls


# ---- shared rules -------------------------------------------------------


def test_audio_is_read_from_the_raw_upload_never_from_audio_segments(stub_embedder,
                                                                    monkeypatch):
    """The Phase 0 numbers are raw-audio numbers. `audio_segments/` holds the normalised
    copy, and a score measured on one does not transfer to the other."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_c0000.wav"
    s3 = FakeS3({key: _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    se.lambda_handler({"op": "match", "session": "s", "user_folder": "Ben_UCPK2",
                       "date": "2026-08-11",
                       "turns": [{"source_filename": "x_c0000.wav",
                                  "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert s3.gets == [key], f"read {s3.gets}, must be the raw upload only"


def test_an_access_denied_raises_rather_than_returning_an_empty_result(stub_embedder,
                                                                      monkeypatch):
    """The standing trap: `except ClientError: pass` turned a missing IAM prefix into a
    200-with-nothing before. A denial must be loud."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({}, denied={key}))
    with pytest.raises(Exception) as exc:
        se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                           "date": "2026-08-11",
                           "turns": [{"source_filename": "x_c0000.wav",
                                      "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert "AccessDenied" in str(exc.value) or "ClientError" in type(exc.value).__name__


def test_a_sample_rate_that_is_not_16k_raises(stub_embedder, monkeypatch):
    """The model is 16 kHz. At another rate it degrades silently rather than failing."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(sr=8000)}))
    with pytest.raises(ValueError, match="16"):
        se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                           "date": "2026-08-11",
                           "turns": [{"source_filename": "x_c0000.wav",
                                      "start_sec": 0.0, "end_sec": 5.0}]}, None)


# ---- match --------------------------------------------------------------


def test_a_turn_under_the_floor_is_never_embedded_at_all(stub_embedder, monkeypatch):
    """Not merely unnamed — not paid for either. The one Phase 0 miss was a 2.1 s turn."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes()}))
    out = se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                             "date": "2026-08-11",
                             "turns": [{"source_filename": "x_c0000.wav",
                                        "start_sec": 0.0, "end_sec": 1.0}]}, None)
    assert out["results"][0]["status"] == "unknown"
    assert stub_embedder["n"] == 0, "a turn below the floor must not reach the model"


def test_two_samples_of_one_person_are_one_candidate(stub_embedder, monkeypatch):
    """The aggregation that stops a person beating himself — Ben's two enrolments sit
    ~0.08 apart, which is below the margin."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes()}))
    v_ben, v_other = np.ones(192), np.concatenate([np.ones(96), -np.ones(96)])
    out = se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                             "date": "2026-08-11",
                             "profiles": [
                                 {"person_key": "ben", "display_name": "Ben",
                                  "status": "confirmed", "embedding": v_ben * 0.9},
                                 {"person_key": "ben", "display_name": "Ben",
                                  "status": "confirmed", "embedding": v_ben},
                                 {"person_key": "zoe", "display_name": "Zoe",
                                  "status": "confirmed", "embedding": v_other}],
                             "turns": [{"source_filename": "x_c0000.wav",
                                        "start_sec": 0.0, "end_sec": 5.0}]}, None)
    r = out["results"][0]
    assert r["status"] == "confirmed" and r["name"] == "ben"


def test_a_tentative_profile_can_only_produce_a_tentative_name(stub_embedder, monkeypatch):
    """`status` travels with the vector so a profile that has not earned confirmation
    cannot hand out a confirmed name."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes()}))
    out = se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                             "date": "2026-08-11",
                             "profiles": [
                                 {"person_key": "ben", "display_name": "Ben",
                                  "status": "tentative", "embedding": np.ones(192)},
                                 {"person_key": "zoe", "display_name": "Zoe",
                                  "status": "confirmed",
                                  "embedding": np.concatenate([np.ones(96),
                                                               -np.ones(96)])}],
                             "turns": [{"source_filename": "x_c0000.wav",
                                        "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert out["results"][0]["status"] == "tentative"


# ---- enrol --------------------------------------------------------------


def test_an_inhomogeneous_window_is_refused_and_nothing_is_stored(stub_embedder,
                                                                  monkeypatch):
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=15.0)}))
    # two frames pointing opposite ways -> spread 2.0, far above the threshold
    stub_embedder["vectors"] = [np.ones(192), -np.ones(192), np.ones(192)]
    out = se.lambda_handler({"op": "enrol", "voiceprint_id": "vp1", "user_folder": "u",
                             "date": "2026-08-11", "source_filename": "x_c0000.wav",
                             "start_sec": 0.0, "end_sec": 15.0}, None)
    assert out["status"] == "refused"
    assert "homogene" in out["reason"].lower()
    assert "embedding" not in out, "a window that may hold two voices must not be embedded"


def test_a_window_too_short_to_judge_is_refused_not_accepted(stub_embedder, monkeypatch):
    """`window_is_homogeneous` returns None for "cannot tell". None is not a pass — that
    conflation is how a guard becomes decoration."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=4.0)}))
    out = se.lambda_handler({"op": "enrol", "voiceprint_id": "vp1", "user_folder": "u",
                             "date": "2026-08-11", "source_filename": "x_c0000.wav",
                             "start_sec": 0.0, "end_sec": 4.0}, None)
    assert out["status"] == "refused"
    assert "embedding" not in out


def test_a_homogeneous_window_is_stored_with_its_provenance(stub_embedder, monkeypatch):
    """Every vector keeps a pointer back to the audio and the correction that made it —
    withdrawal has to enumerate what a profile justified, and that history cannot be
    added later."""
    key = "users/u/audio/2026-08-11/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=15.0)}))
    out = se.lambda_handler({"op": "enrol", "voiceprint_id": "vp1", "user_folder": "u",
                             "date": "2026-08-11", "source_filename": "x_c0000.wav",
                             "start_sec": 0.0, "end_sec": 15.0,
                             "correction_ref": "corr-7"}, None)
    assert out["status"] == "embedded"
    assert out["s3_key"] == key
    assert out["correction_ref"] == "corr-7"
    assert out["window"] == [0.0, 15.0]


def test_an_unknown_op_is_rejected_rather_than_silently_doing_nothing(monkeypatch):
    with pytest.raises(ValueError):
        se.lambda_handler({"op": "sing"}, None)


# ---- batched turns -------------------------------------------------------
#
# TEST has TEST_BATCH_TRANSCRIPTION=true, so every turn from a newly recorded session
# points at a stitched object under audio_segments/ with batch-relative offsets. Reading
# the raw upload is not optional -- the Phase 0 thresholds are raw-audio numbers -- so the
# coordinates come back through the batch map. Both filename shapes are fed here on
# purpose: a suite that only knows one of them is how two green features came to disagree.


def _batch_map_bytes():
    import json
    import batch_stitch as bs
    ms = [bs.member(0, "audio_segments/u/2026-08-13/x_c0000_off0.0_to30.0_srcwav.wav",
                    "2026-08-13T09:00:00", 1.5, 30.0),
          bs.member(1, "audio_segments/u/2026-08-13/x_c0001_off0.0_to30.0_srcwav.wav",
                    "2026-08-13T09:00:30", 0.5, 30.0)]
    return json.dumps(bs.build_map("sid1", ms, sealed_by="session_close")).encode()


BATCH_NAME = "x_c0000_bn2_off0.0_to60.0_srcwav.wav"
MAP_KEY = "audio_segments/u/2026-08-13/x_c0000_bn2_off0.0_to60.0_srcwav_batch_map.json"


def test_a_batched_turn_reads_the_raw_chunk_not_the_stitched_object(stub_embedder,
                                                                    monkeypatch):
    raw = "users/u/audio/2026-08-13/x_c0001.wav"
    s3 = FakeS3({MAP_KEY: _batch_map_bytes(), raw: _wav_bytes(seconds=30.0)})
    monkeypatch.setattr(se, "s3", lambda: s3)
    se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                       "date": "2026-08-13", "company_id": "c1",
                       "turns": [{"source_filename": BATCH_NAME,
                                  "start_sec": 35.0, "end_sec": 40.0}]}, None)
    assert raw in s3.gets, f"never read the raw chunk; read {s3.gets}"
    assert not any(g.endswith("_srcwav.wav") for g in s3.gets), (
        "read the stitched object under audio_segments/ — the Phase 0 thresholds do not "
        "transfer to it")


def test_a_per_chunk_turn_costs_no_map_fetch(stub_embedder, monkeypatch):
    """The old shape must keep working and keep costing nothing."""
    key = "users/u/audio/2026-08-13/x_c0000.wav"
    s3 = FakeS3({key: _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                       "date": "2026-08-13", "company_id": "c1",
                       "turns": [{"source_filename": "x_c0000.wav",
                                  "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert s3.gets == [key]


def _ramp_wav(seconds=30.0, sr=16000):
    """Every sample encodes its own index, so the audio that comes back says WHERE it was
    cut from. A wav of zeros cannot tell a right offset from a wrong one."""
    import io as _io
    import wave as _wave
    n = int(seconds * sr)
    data = (np.arange(n, dtype=np.int64) % 30000).astype("<i2")
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return buf.getvalue()


def test_the_batch_offset_is_applied_to_the_audio_that_is_embedded(stub_embedder,
                                                                   monkeypatch):
    """Not merely the right file and the right LENGTH -- the right seconds inside it.

    This test used to assert only that five seconds came back, which a version that
    dropped or double-counted the 0.5 s head trim would also satisfy while embedding a
    different person's syllables. The audio now carries its own position.

    Batch 35 s lands in member 1 (whose kept audio starts at batch 30 s) at 5 s in, and
    member 1's kept audio begins 0.5 s into its chunk -- so the first sample embedded is
    chunk sample 5.5 s.
    """
    raw = "users/u/audio/2026-08-13/x_c0001.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({MAP_KEY: _batch_map_bytes(),
                                                  raw: _ramp_wav(seconds=30.0)}))
    seen = {}

    def embed(audio, sr):
        seen["audio"] = np.asarray(audio)
        return np.ones(192, dtype=np.float32)
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                       "date": "2026-08-13", "company_id": "c1",
                       "turns": [{"source_filename": BATCH_NAME,
                                  "start_sec": 35.0, "end_sec": 40.0}]}, None)
    got = seen["audio"]
    assert len(got) == pytest.approx(5.0 * 16000, abs=16), f"embedded {len(got)} samples"
    first = round(float(got[0]) * 32768.0)
    assert first == pytest.approx(int(5.5 * 16000) % 30000, abs=2), (
        f"the window starts at chunk sample {first}, expected {int(5.5 * 16000)} — the "
        f"head trim was dropped or counted twice")


# ---- pure compute: no database, no VPC ----------------------------------
#
# The function runs on python3.12 because that is where onnxruntime comes from
# (fieldsight-vad-layer is cp312-only), and PsycopgLayer is cp311-only. One function cannot
# have both, so the deployed function raised ModuleNotFoundError on EVERY invocation while
# deploying green -- no test caught it, because they all stubbed load_profiles.
#
# The cure is not a second psycopg layer. It is that this function has no business holding
# a database connection: org-api is in-VPC, already has psycopg, and already owns the
# consent and withdrawn filters that must not be duplicated. It passes what it read.


def test_the_function_imports_no_database_module():
    """The defect, pinned. Import-level, because that is where it actually fired."""
    import ast
    import pathlib
    src = pathlib.Path(se.__file__).read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    forbidden = names & {"psycopg", "db", "repositories"}
    assert not forbidden, (
        f"{sorted(forbidden)} is imported; psycopg cannot be installed alongside the cp312 "
        f"VAD layer, so this is the ModuleNotFoundError that made the deployed function "
        f"100% non-functional while every test passed")


def test_profiles_come_from_the_event_and_there_is_no_other_way_to_get_them():
    """The consent and withdrawn filters live in profiles_for_matching, which is the one
    query whose mistakes are invisible -- a withdrawn profile that still matches is not a
    withdrawal. This function must not acquire a second way to reach profiles."""
    assert not hasattr(se, "load_profiles"), (
        "load_profiles still exists; a caller could reach profiles without the filters")


def test_a_match_scores_against_the_profiles_in_the_payload(stub_embedder, monkeypatch):
    key = "users/u/audio/2026-08-13/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes()}))
    out = se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                             "date": "2026-08-13",
                             "profiles": [
                                 {"person_key": "ben", "status": "confirmed",
                                  "embedding": list(np.ones(192))},
                                 {"person_key": "zoe", "status": "confirmed",
                                  "embedding": list(np.concatenate([np.ones(96),
                                                                    -np.ones(96)]))}],
                             "turns": [{"source_filename": "x_c0000.wav",
                                        "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert out["results"][0]["name"] == "ben"


def test_an_enrolment_returns_the_vector_instead_of_storing_it(stub_embedder, monkeypatch):
    """The writer stores it, in VPC, in the column that already requires consent. Returning
    it keeps the vector out of S3 entirely -- the biometric-residence defect that moved
    twice during review."""
    key = "users/u/audio/2026-08-13/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=15.0)}))
    out = se.lambda_handler({"op": "enrol", "voiceprint_id": "vp1", "user_folder": "u",
                             "date": "2026-08-13", "source_filename": "x_c0000.wav",
                             "start_sec": 0.0, "end_sec": 15.0,
                             "correction_ref": "corr-7"}, None)
    assert out["status"] == "embedded"
    assert len(out["embedding"]) == 192
    assert out["s3_key"] == key and out["correction_ref"] == "corr-7"
