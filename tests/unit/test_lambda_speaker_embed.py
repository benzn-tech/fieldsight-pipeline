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


def _wav_bytes(seconds=5.0, sr=16000, silent=False):
    """Audio with SIGNAL in it by default.

    Every fixture here used to be digital zero, which was harmless while nothing looked at
    loudness. It stops being harmless the moment a speech gate exists: silent fixtures make
    every gated test fail, and a suite that fails because its fixtures are silent cannot tell
    you whether the gate works.

    `silent=True` is kept for the tests that are ABOUT silence — the gate needs something to
    reject, and it must be asked for explicitly rather than inherited.
    """
    import io
    import wave
    n = int(seconds * sr)
    if silent:
        samples = np.zeros(n, dtype="<i2")
    else:
        # A tone, not noise: deterministic, so a test never fails on a lucky draw, and loud
        # enough to sit well above any plausible speech gate.
        t = np.arange(n, dtype=np.float64) / sr
        samples = (np.sin(2 * np.pi * 220.0 * t) * 8000).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
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


# ---- the S3 entry point -------------------------------------------------
#
# Found by a real correction on TEST, not by reading code. The trigger was wired, the
# artifact landed, and the function died in 3 ms with `unknown op None` -- because the
# handler is op-keyed and an S3 event carries `Records`, not `op`. The plan said this in
# writing and the wiring shipped without it, so the gap is pinned here rather than trusted.


def _s3_event(key, bucket="b"):
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def test_an_s3_event_is_understood_rather_than_rejected(stub_embedder, monkeypatch):
    import json as _json
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "user_folder": "u", "date": "2026-08-13",
           "correction": {"source_filename": "x_c0000.wav", "start_sec": 0.0,
                          "end_sec": 5.0, "display_name": "Ben L"},
           "profiles": []}
    key = "voiceprint_requests/c1/s1/r1.json"
    audio = "users/u/audio/2026-08-13/x_c0000.wav"
    s3 = FakeS3({key: _json.dumps(req).encode(), audio: _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda payload: sent.update(payload) or {})
    out = se.lambda_handler(_s3_event(key), None)
    assert out.get("status") != "error", out
    assert key in s3.gets, "the request artifact was never read"


def test_the_result_goes_to_the_writer_and_not_to_s3(stub_embedder, monkeypatch):
    """The vector never lands in durable storage — that defect relocated twice during review
    and only died when the storage turned out not to need to exist."""
    import json as _json
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "user_folder": "u", "date": "2026-08-13",
           "correction": {"source_filename": "x_c0000.wav", "start_sec": 0.0,
                          "end_sec": 5.0, "display_name": "Ben L"},
           "profiles": []}
    key = "voiceprint_requests/c1/s1/r1.json"
    s3 = FakeS3({key: _json.dumps(req).encode(),
                 "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda payload: sent.update(payload) or {})
    se.lambda_handler(_s3_event(key), None)
    assert sent.get("op") == "propagation"
    assert sent.get("company_id") == "c1"
    assert not hasattr(s3, "puts") or not getattr(s3, "puts", None), (
        "the embedder wrote to S3; its result belongs in the invoke payload")


def test_the_direct_op_form_still_works(stub_embedder, monkeypatch):
    """The S3 entry is an addition, not a replacement — the ops are how it is smoke-tested
    by hand, and that is how tonight's two defects were both found."""
    key = "users/u/audio/2026-08-13/x_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes()}))
    out = se.lambda_handler({"op": "match", "session": "s", "user_folder": "u",
                             "date": "2026-08-13", "profiles": [],
                             "turns": [{"source_filename": "x_c0000.wav",
                                        "start_sec": 0.0, "end_sec": 5.0}]}, None)
    assert "results" in out


def test_an_artifact_without_folder_or_date_fails_loudly(stub_embedder, monkeypatch):
    """Guessing them from session_base produced `users/s1/audio//x.wav` — a key that cannot
    exist, so the missing field would have surfaced as NoSuchKey somewhere else entirely."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "correction": {"source_filename": "x.wav", "start_sec": 0.0, "end_sec": 5.0,
                          "display_name": "Ben L"}, "profiles": []}
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _json.dumps(req).encode()}))
    with pytest.raises(ValueError, match="user_folder"):
        se.lambda_handler(_s3_event(key), None)


def test_the_name_the_user_typed_reaches_the_writer(stub_embedder, monkeypatch):
    """The hop that dropped it. The name travelled from the correction body all the way to a
    writer that had no column to put it in, and nothing raised — the row simply named nobody.

    Pinned here rather than trusted, because a closing mutation pass showed that deleting
    this one field from the payload left 171 tests green.
    """
    import json as _json
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "user_folder": "u", "date": "2026-08-13",
           "correction": {"source_filename": "x_c0000.wav", "start_sec": 0.0,
                          "end_sec": 5.0, "display_name": "Ben L"},
           "profiles": []}
    key = "voiceprint_requests/c1/s1/r1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes()}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda payload: sent.update(payload) or {})
    se.lambda_handler(_s3_event(key), None)
    assert sent["results"][0]["display_name"] == "Ben L", (
        "the name never left the embedder; the row would name nobody")


# ---- in-session propagation --------------------------------------------
#
# The headline promise: correct one passage and the rest of that person's speech follows.
# Until now the correction path wrote exactly ONE row -- the turn the user pointed at -- so
# "propagation: queued" was a claim about a mechanism that existed and was never called.
#
# The unit is a VOICE, not a turn: the session is clustered and the correction NAMES A
# CLUSTER. Two spec revisions died trying to score turns against the corrected window
# directly, because `decide_name` refuses to confirm on a single candidate.


def _prop_request(turns, ref_start=0.0, ref_end=5.0):
    return {"request_id": "r1", "session_base": "s1", "company_id": "c1",
            "user_folder": "u", "date": "2026-08-13",
            "correction": {"source_filename": "x_c0000.wav", "start_sec": ref_start,
                           "end_sec": ref_end, "display_name": "Ben L"},
            "turns": turns, "profiles": []}


def _two_voice_session(monkeypatch, sent):
    """Six turns, two voices, alternating. The reference is turn 0, so voice A is Ben."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 10),
              "end_sec": float(i * 10 + 5)} for i in range(6)]
    s3 = FakeS3({key: _json.dumps(_prop_request(turns)).encode(),
                 "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=70.0)})
    monkeypatch.setattr(se, "s3", lambda: s3)
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    a = np.ones(192, dtype=np.float32)
    b = np.concatenate([np.ones(96), -np.ones(96)]).astype(np.float32)
    seq = [a, a, b, a, b, a, b]          # reference embed first, then the six turns
    calls = {"n": 0}

    def embed(audio, sr):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    return key


def test_one_correction_names_every_turn_of_that_voice(monkeypatch):
    sent = {}
    key = _two_voice_session(monkeypatch, sent)
    se.lambda_handler(_s3_event(key), None)
    named = [r for r in sent["results"] if r.get("display_name") == "Ben L"]
    assert len(named) > 1, (
        "only the corrected turn was named; the propagation the response promises never ran")


def test_the_other_voice_is_not_named(monkeypatch):
    """The failure that matters. Naming the whole session would be worse than naming one
    turn, because it is a confident wrong answer about somebody who was in the room."""
    sent = {}
    key = _two_voice_session(monkeypatch, sent)
    se.lambda_handler(_s3_event(key), None)
    # The alternating fixture makes turns at 0/20/40 one voice and 10/30/50 the other.
    # A named turn from the second set means the two voices were merged.
    named = {r["turn_ref"] for r in sent["results"]}
    # Refs are normalised — the extension is dropped, because a correction carries the .wav
    # and the transcript carries the .json for the same turn.
    assert not (named & {"x_c0000@10.0", "x_c0000@30.0", "x_c0000@50.0"}), (
        f"the second voice was swept up with the first: {sorted(named)}")
    assert named >= {"x_c0000@20.0", "x_c0000@40.0"}, (
        f"the first voice was not fully named: {sorted(named)}")


def test_the_asserted_turn_is_marked_and_the_rest_are_not(monkeypatch):
    """Only one of these rows is a claim a person made. The writer refuses a profile id to
    any row not marked asserted, which is what stops the machine confirming its own
    profiles."""
    sent = {}
    key = _two_voice_session(monkeypatch, sent)
    se.lambda_handler(_s3_event(key), None)
    asserted = [r for r in sent["results"] if r.get("asserted")]
    assert len(asserted) == 1
    assert asserted[0]["turn_ref"] == "x_c0000@0.0"


def test_propagated_rows_are_capped_at_tentative(monkeypatch):
    """No calibrated margin exists yet — Gate A froze the CLUSTERING threshold, not the one
    that decides confirmed vs tentative. Until it does, an inferred name is a suggestion."""
    sent = {}
    key = _two_voice_session(monkeypatch, sent)
    se.lambda_handler(_s3_event(key), None)
    for r in sent["results"]:
        if not r.get("asserted"):
            assert r["state"] != "confirmed", "an inferred name was presented as certain"


def test_a_session_with_no_turns_still_names_the_corrected_one(stub_embedder,
                                                              monkeypatch):
    """The artifact may carry no turn list — an older producer, or a session whose transcript
    is not ready. That must degrade to today's behaviour, not fail."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    s3 = FakeS3({key: _json.dumps(_prop_request([])).encode(),
                 "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    assert len(sent["results"]) == 1 and sent["results"][0]["asserted"]


def test_the_reference_is_never_scored_against_a_centroid_holding_itself(monkeypatch):
    """The corrected turn is IN the clustering, so its label already says which voice it is.

    An earlier version scored the reference against every centroid — including the one that
    contained the corrected turn's own vector, which made it match itself. That inflation
    worked against the only refusal propagation has, and `leave_one_out_centroid` sat
    written, tested and never called. Reading the label removes the comparison instead of
    correcting it.

    Pinned by the case the old code got wrong: a corrected turn that is the ONLY member of
    its voice. Scoring by similarity, its own cluster was a perfect self-match; the honest
    answer is that this person spoke once and there is nothing to propagate.
    """
    import json as _json
    import math
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 10),
              "end_sec": float(i * 10 + 5)} for i in range(3)]
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(_prop_request(turns)).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=40.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})

    def unit(theta):
        v = np.zeros(192, dtype=np.float32)
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v
    # reference == turn0, alone; turns 1 and 2 are one other voice, far away.
    seq = [unit(0.0), unit(0.0), unit(2.4), unit(2.45)]
    calls = {"n": 0}

    def embed(audio, sr):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler(_s3_event(key), None)
    assert [r for r in sent["results"] if not r.get("asserted")] == [], (
        "a voice that spoke once propagated a name to somebody else's turns")


def test_a_corrected_window_that_is_not_one_of_the_turns_propagates_nothing(monkeypatch):
    """A window drawn across a boundary, or an older producer. Guessing which cluster it
    belongs to would be the two-voice failure by another route."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(20 + i * 10),
              "end_sec": float(25 + i * 10)} for i in range(3)]
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(_prop_request(turns)).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=60.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    monkeypatch.setattr(se, "embed_audio", lambda a, sr: np.ones(192, dtype=np.float32))
    se.lambda_handler(_s3_event(key), None)
    assert [r for r in sent["results"] if not r.get("asserted")] == []


def test_a_single_voice_session_is_named_but_never_confirmed(stub_embedder, monkeypatch):
    """k=1 -- a solo narration walk, the most common recording shape here. `assign` returns
    no margin because there is no other voice to beat, so the only thing that could make the
    name wrong (clustering having merged two people) has no evidence against it. Named, and
    permanently capped at tentative."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 10),
              "end_sec": float(i * 10 + 5)} for i in range(3)]
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(_prop_request(turns)).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=40.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    propagated = [r for r in sent["results"] if not r.get("asserted")]
    assert propagated, "a single-voice session named nothing"
    assert all(r["state"] == "tentative" for r in propagated)


def test_a_corrected_turn_sitting_between_two_voices_names_nothing(monkeypatch):
    """§2 measured 1 turn in 6 holding two voices. Naming a cluster from such a turn spreads
    one person's name across another person's whole session, so the margin gate refuses.

    The corrected turn is the one at 0.7 rad: complete linkage puts it with the turn at 0,
    but it is nearly as close to the other voice — leave-one-out margin ≈0.086, under the
    0.15 floor. Constructed against the real tau (0.85), not a convenient one.

    This test exists because a mutation pass found the refusal branch could be deleted with
    the whole suite still green — the previous version of it was swallowed by an edit.
    """
    import json as _json
    import math
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 10),
              "end_sec": float(i * 10 + 5)} for i in range(4)]
    req = _prop_request(turns, ref_start=10.0, ref_end=15.0)
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=60.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})

    def unit(theta):
        v = np.zeros(192, dtype=np.float32)
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v
    seq = [unit(0.7), unit(0.0), unit(0.7), unit(1.5), unit(1.55)]
    calls = {"n": 0}

    def embed(audio, sr):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler(_s3_event(key), None)
    assert [r for r in sent["results"] if not r.get("asserted")] == [], (
        "a turn sitting between two voices named a whole cluster")


def test_an_enrolment_reuses_the_vector_the_correction_already_produced(stub_embedder,
                                                                        monkeypatch):
    """The corrected window is embedded once, for the propagation decision. Enrolling the
    same window is then free — embedding it a second time would pay twice for one answer and
    invite the two results to differ.

    The vector travels in the invoke payload, never through S3: that is what stopped the
    biometric-residence defect the reviews chased through two homes.
    """
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    # 15 s, not 5: the homogeneity guard compares 5-second frames and needs at least two,
    # so a window under 10 s is "cannot tell" and is refused. A five-second fixture here was
    # not a shorter version of the real thing — it was a case that cannot be enrolled at all.
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "user_folder": "u", "date": "2026-08-13",
           "correction": {"source_filename": "x_c0000.wav", "start_sec": 0.0,
                          "end_sec": 15.0, "display_name": "Ben L"},
           "enrol": {"voiceprint_id": "vp-1"}, "turns": [], "profiles": []}
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=20.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    assert sent["enrol"]["voiceprint_id"] == "vp-1"
    assert len(sent["enrol"]["embedding"]) == 192
    # The stored vector IS the one the propagation decision used — not a second embedding of
    # the same window, which would pay twice for one answer and let the two results differ.
    # (Counting calls no longer works: the homogeneity guard embeds FRAMES of the window,
    # which is a different and necessary cost.)
    assert sent["enrol"]["window"] == [0.0, 15.0]
    assert sent["enrol"]["s3_key"].endswith("x_c0000.wav")


def test_no_enrolment_means_no_vector_in_the_payload(stub_embedder, monkeypatch):
    """Absent consent, nothing biometric leaves this function at all."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    req = {"request_id": "r1", "session_base": "s1", "company_id": "c1",
           "user_folder": "u", "date": "2026-08-13",
           "correction": {"source_filename": "x_c0000.wav", "start_sec": 0.0,
                          "end_sec": 5.0, "display_name": "Ben L"},
           "turns": [], "profiles": []}
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes()}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    # No enrolment was ASKED FOR here — no consent — which is a different thing from one
    # asked for and refused. Neither may carry a vector; only the second carries a reason.
    assert sent.get("enrol") is None


# ---- the guard the enrolment path was routing around --------------------
#
# `op=enrol` has run `window_is_homogeneous` since it was written, and this module's own
# docstring says the guard runs "before anything is stored". The enrolment carried by a
# correction did not run it — so a window holding two voices could be stored as one person's
# profile, permanently, and a profile cannot be un-poisoned.
#
# Worse: `_propagate` already decides the corrected turn sits between two voices and refuses
# to propagate for exactly that reason, and the enrolment stored the same vector anyway. The
# system disbelieved itself in one direction and acted in the other.


def _enrol_request(turns, ref_start=0.0, ref_end=15.0):
    r = _prop_request(turns, ref_start=ref_start, ref_end=ref_end)
    r["enrol"] = {"voiceprint_id": "vp-1"}
    return r


def test_a_window_holding_two_voices_is_not_enrolled(monkeypatch):
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(_enrol_request([])).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=20.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    # Frames pointing opposite ways: the window is not one voice.
    seq = [np.ones(192, dtype=np.float32), -np.ones(192, dtype=np.float32),
           np.ones(192, dtype=np.float32)]
    calls = {"n": 0}

    def embed(audio, sr):
        v = seq[calls["n"] % len(seq)]
        calls["n"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler(_s3_event(key), None)
    # A refused enrolment is no longer None: it carries the REASON, so the writer can put
    # it on the profile. What must never appear is an embedding — that is the thing whose
    # storage cannot be undone.
    assert not (sent.get("enrol") or {}).get("embedding"), (
        "a window that may hold two voices was stored as one person's profile, and a "
        "profile cannot be un-poisoned")


def test_a_window_too_short_to_judge_is_not_enrolled(stub_embedder, monkeypatch):
    """`window_is_homogeneous` returns None for 'cannot tell', which is not a pass. Treating
    them alike in the permissive direction is how a guard becomes decoration — the enrol op
    has said so in a comment since it was written."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    req = _enrol_request([], ref_start=0.0, ref_end=4.0)
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=10.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    assert not (sent.get("enrol") or {}).get("embedding")
    assert (sent.get("enrol") or {}).get("refused")


def test_the_turn_names_still_land_when_the_enrolment_is_refused(stub_embedder,
                                                                  monkeypatch):
    """Refusing to STORE a voice pattern is not a reason to lose the name the user typed.
    The two effects are separate obligations and they fail separately."""
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    req = _enrol_request([], ref_start=0.0, ref_end=4.0)
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=10.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    se.lambda_handler(_s3_event(key), None)
    assert sent["results"] and sent["results"][0]["asserted"]


def test_a_window_propagation_refused_is_not_enrolled_either(monkeypatch):
    """The system disbelieving itself in one direction and acting in the other.

    `_propagate` refuses when the corrected turn sits between two voices — that IS the
    system saying it does not believe this window is one person. Storing the same vector as
    somebody's profile on the strength of it would be incoherent, and permanent: a profile
    cannot be un-poisoned, only the contributing sample deleted.

    The window here is long enough to pass the homogeneity check, so only the propagation
    refusal can stop the enrolment — which is what makes this test about that branch.
    """
    import json as _json
    import math
    key = "voiceprint_requests/c1/s1/r1.json"
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 20),
              "end_sec": float(i * 20 + 15)} for i in range(4)]
    req = _enrol_request(turns, ref_start=20.0, ref_end=35.0)
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=90.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})

    def unit(theta):
        v = np.zeros(192, dtype=np.float32)
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v
    # reference (0.7) then the four turns; the corrected one sits between two voices, and
    # every homogeneity frame of the window is the same vector so the guard passes.
    order = [unit(0.7)] + [unit(0.0), unit(0.7), unit(1.5), unit(1.55)]
    state = {"i": 0, "frames": 0}

    def embed(audio, sr):
        # The guard embeds 5-second frames of a 15s window; feed it a constant so it passes.
        if len(audio) <= 5 * 16000 + 8:
            state["frames"] += 1
            return unit(0.7)
        v = order[min(state["i"], len(order) - 1)]
        state["i"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler(_s3_event(key), None)
    assert [r for r in sent["results"] if not r.get("asserted")] == [], (
        "propagation did not refuse; this test is no longer about what it says it is")
    # A refused enrolment is no longer None: it carries the REASON, so the writer can put
    # it on the profile. What must never appear is an embedding — that is the thing whose
    # storage cannot be undone.
    assert not (sent.get("enrol") or {}).get("embedding"), (
        "the window propagation would not trust was stored as somebody's voiceprint")


def test_someone_who_spoke_once_can_still_be_enrolled(stub_embedder, monkeypatch):
    """The most ordinary correction there is — a visitor says one thing and you name them.

    A first version refused it: every empty propagation was treated as a refusal, so "this
    person has only this turn" (which the propagation log calls a normal answer) blocked the
    enrolment, and the log claimed the window was untrustworthy. Less evidence should not
    become more suspicion when the evidence is about a different question.
    """
    import json as _json
    key = "voiceprint_requests/c1/s1/r1.json"
    # Three turns, all one other voice, plus the corrected one — so the corrected speaker's
    # cluster has a single member and propagation returns nothing.
    turns = [{"source_filename": "x_c0000.wav", "start_sec": float(i * 20),
              "end_sec": float(i * 20 + 15)} for i in range(4)]
    req = _enrol_request(turns, ref_start=0.0, ref_end=15.0)
    monkeypatch.setattr(se, "s3", lambda: FakeS3(
        {key: _json.dumps(req).encode(),
         "users/u/audio/2026-08-13/x_c0000.wav": _wav_bytes(seconds=90.0)}))
    sent = {}
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.update(p) or {})
    import math

    def unit(theta):
        v = np.zeros(192, dtype=np.float32)
        v[0], v[1] = math.cos(theta), math.sin(theta)
        return v
    order = [unit(0.0), unit(0.0), unit(2.4), unit(2.42), unit(2.44)]
    st = {"i": 0}

    def embed(audio, sr):
        if len(audio) <= 5 * 16000 + 8:      # homogeneity frames
            return unit(0.0)
        v = order[min(st["i"], len(order) - 1)]
        st["i"] += 1
        return v
    monkeypatch.setattr(se, "embed_audio", embed)
    se.lambda_handler(_s3_event(key), None)
    assert [r for r in sent["results"] if not r.get("asserted")] == [], (
        "propagation named something; this test is no longer about a lone speaker")
    assert sent.get("enrol"), (
        "a person who spoke once could not be enrolled — the most ordinary correction there "
        "is was permanently refused")


def test_a_vad_segments_offset_is_added_before_the_audio_is_cut(stub_embedder, monkeypatch):
    """`_raw_key` strips `_off{T}` to reach the device's upload, so a position measured
    inside the VAD unit is short by T once the audio is the whole chunk.

    The batched path documents this term and applies it; the non-batched path did not, and
    the failure is silent — the wrong seconds of audio embed perfectly well.
    """
    seen = {}

    def embed(audio, sr):
        seen.setdefault("first_nonzero", int(np.flatnonzero(audio)[0]) if audio.any() else None)
        seen["len"] = len(audio)
        return np.ones(192, dtype=np.float32)

    monkeypatch.setattr(se, "embed_audio", embed)

    sr = 16000
    import io as _io
    import wave as _wave
    samples = np.zeros(120 * sr, dtype="<i2")
    samples[100 * sr:104 * sr] = 1000       # the only sound is at 100s-104s
    buf = _io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(samples.tobytes())

    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "a" * 32 + "_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: buf.getvalue()}))

    # The unit starts 100s into the chunk; the turn is at 0-4s WITHIN the unit.
    se.lambda_handler({"op": "match", "session": "s", "user_folder": "Ben_UCPK2",
                       "date": "2026-08-11",
                       "turns": [{"source_filename":
                                  "x_sid" + "a" * 32 + "_c0000_off100.0_to160.0_srcwav.json",
                                  "start_sec": 0.0, "end_sec": 4.0}]}, None)

    assert seen.get("len") == 4 * sr
    assert seen.get("first_nonzero") == 0, \
        "the window was cut at 0s of the whole upload — silence, not the speaker"


# ---- Phase 5: matching a whole session against stored profiles ------------


def _match_artifact(mode="on", turns=None):
    return json.dumps({
        "op": "match", "session_base": "sid" + "c" * 32, "company_id": "co-1",
        "user_folder": "Ben_UCPK2", "date": "2026-08-11", "mode": mode,
        "turns": turns if turns is not None else [
            {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
             "start_sec": 0.0, "end_sec": 5.0}]})


def _writer(monkeypatch, profiles, seen):
    def invoke(payload):
        seen.append(payload)
        if payload["op"] == "profiles":
            return {"profiles": profiles}
        return {"written": len(payload.get("results") or [])}
    monkeypatch.setattr(se, "invoke_writer", invoke)


def _profile(pid="vp-1", name="Ben L", status="confirmed", vec=None):
    return {"person_key": pid, "display_name": name, "status": status,
            "embedding": vec if vec is not None else [1.0] * 192}


def _pool():
    """Two profiles, far apart. A ONE-profile pool names nobody by design — see
    test_one_enrolled_profile_names_nobody — so a fixture with one would silently make
    every other matcher test assert on the same empty result."""
    return [_profile("vp-1", "Ben L", vec=[1.0] * 192),
            _profile("vp-2", "Zoe", vec=[1.0] + [-1.0] * 191)]


def test_the_match_artifact_never_carries_vectors_and_fetches_them_instead(
        stub_embedder, monkeypatch):
    """The request bucket has a 7-day expiry and nothing else; a voiceprint is biometric
    data whose storage was consented to in one column. The vectors arrive in the RESULT of a
    synchronous invoke and exist only in memory."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    s3 = FakeS3({art: _match_artifact(), key: _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    seen = []
    _writer(monkeypatch, [_profile()], seen)

    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": art}}}]}, None)
    assert seen[0]["op"] == "profiles", "the vectors were not fetched from the in-VPC half"
    assert "embedding" not in json.dumps(json.loads(s3.objects[art]))


def test_shadow_computes_everything_and_writes_no_name(stub_embedder, monkeypatch):
    """The scores are the point of shadow mode — they are what a threshold is calibrated on
    — but no name may reach a transcript."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    monkeypatch.setattr(se, "s3",
                        lambda: FakeS3({art: _match_artifact(mode="shadow"),
                                        key: _wav_bytes()}))
    seen = []
    _writer(monkeypatch, _pool(), seen)

    out = se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                                 "object": {"key": art}}}]}, None)
    assert out["matched"] == 0
    assert out["wouldMatch"] >= 1, "shadow computed nothing, so it collected nothing"
    assert not any(p["op"] == "match_names" for p in seen), "shadow wrote a name"


def test_on_writes_the_names_it_found(stub_embedder, monkeypatch):
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: _match_artifact(mode="on"),
                                                  key: _wav_bytes()}))
    seen = []
    _writer(monkeypatch, _pool(), seen)

    out = se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                                 "object": {"key": art}}}]}, None)
    write = next(p for p in seen if p["op"] == "match_names")
    assert out["matched"] == len(write["results"]) >= 1
    row = write["results"][0]
    assert row["display_name"] == "Ben L", \
        "the transcript would show a uuid: decide_name returns the KEY it won on"
    assert row["person_key"] == "vp-1"


def test_a_company_with_no_consented_profile_says_so(stub_embedder, monkeypatch):
    """'nobody was recognised' and 'there was nobody to recognise' look identical
    downstream. On TEST the only profile was withdrawn during a withdrawal test, which is
    exactly how this would be misread as the matcher being broken."""
    art = "voiceprint_requests/co-1/s/match-1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: _match_artifact()}))
    seen = []
    _writer(monkeypatch, [], seen)

    out = se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                                 "object": {"key": art}}}]}, None)
    assert out["profiles"] == 0 and out["matched"] == 0
    assert not any(p["op"] == "match_names" for p in seen)


def test_a_correction_artifact_still_takes_the_correction_path(stub_embedder, monkeypatch):
    """One prefix, two requests, told apart by `op` inside the object — a second prefix
    would be a second hand-wired S3 notification (BUG-33)."""
    art = "voiceprint_requests/co-1/s/r1.json"
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    doc = json.dumps({"session_base": "sid" + "c" * 32, "company_id": "co-1",
                      "user_folder": "Ben_UCPK2", "date": "2026-08-11", "turns": [],
                      "correction": {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                                     "start_sec": 0.0, "end_sec": 5.0,
                                     "display_name": "Ben L"}})
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: doc, key: _wav_bytes()}))
    seen = []
    _writer(monkeypatch, [_profile()], seen)
    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": art}}}]}, None)
    assert any(p["op"] == "propagation" for p in seen), \
        "a correction was routed to the matcher"


# ---- long audio ----------------------------------------------------------
#
# A 108-second turn killed this function with Runtime.OutOfMemory at 1769 MB on
# the first real enrolment against whole-chunk transcription. ECAPA's convolutions
# run the full length of the input, and every earlier run — Phase 0, the parity
# fixtures, yesterday's live verification — used a window of a few seconds.


def test_a_long_turn_is_embedded_in_pieces_not_in_one_pass(monkeypatch):
    lengths = []

    def once(audio):
        lengths.append(len(audio))
        return np.ones(192, dtype=np.float32)

    monkeypatch.setattr(se, "_embed_once", once)
    se.embed_audio(np.zeros(108 * 16000, dtype=np.float32), 16000)
    assert len(lengths) > 1, "the whole 108 s went to the model in one tensor"
    assert max(lengths) <= se.MAX_EMBED_SECONDS * 16000


def test_short_audio_still_takes_the_single_pass_path(monkeypatch):
    """The parity fixtures compare against vectors recorded from one pass. Changing the
    arithmetic under them would make the comparison meaningless while it stayed green."""
    calls = []
    monkeypatch.setattr(se, "_embed_once",
                        lambda a: calls.append(len(a)) or np.ones(192, dtype=np.float32))
    se.embed_audio(np.zeros(5 * 16000, dtype=np.float32), 16000)
    assert calls == [5 * 16000]


def test_the_pieces_are_normalised_before_they_are_averaged(monkeypatch):
    """The scores this feeds are cosines, so a loud piece is not more of an opinion about
    who is speaking. Without normalising, one piece with a large norm decides the vector."""
    vecs = [np.array([100.0] + [0.0] * 191, dtype=np.float32),
            np.array([0.0, 1.0] + [0.0] * 190, dtype=np.float32)]
    seq = iter(vecs)
    monkeypatch.setattr(se, "_embed_once", lambda a: next(seq))
    # Long enough for two pieces at the measured cap, whatever that cap is set to.
    n = int(se.MAX_EMBED_SECONDS * 2 * 16000)
    out = se.embed_audio(np.zeros(n, dtype=np.float32), 16000)
    assert abs(out[0] - out[1]) < 1e-6, "the louder piece dominated the average"


def test_the_same_audio_is_not_downloaded_once_per_turn(stub_embedder, monkeypatch):
    """A session's turns are, by construction, all from the same handful of files. Fetching
    and decoding each one per turn cost ~176 ms and a few megabytes every time."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    turns = [{"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
              "start_sec": float(i), "end_sec": float(i) + 5} for i in range(6)]
    s3 = FakeS3({art: _match_artifact(turns=turns), key: _wav_bytes(seconds=30.0)})
    monkeypatch.setattr(se, "s3", lambda: s3)
    _writer(monkeypatch, [_profile()], [])

    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": art}}}]}, None)
    assert s3.gets.count(key) == 1, f"downloaded {s3.gets.count(key)} times for 6 turns"


def test_one_invocation_never_serves_another_its_audio(stub_embedder, monkeypatch):
    """A warm container must not carry one session's audio into the next: the cache is a
    within-run optimisation, and a stale hit would embed the wrong recording."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    s3 = FakeS3({art: _match_artifact(), key: _wav_bytes()})
    monkeypatch.setattr(se, "s3", lambda: s3)
    _writer(monkeypatch, [_profile()], [])
    ev = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": art}}}]}
    se.lambda_handler(ev, None)
    se.lambda_handler(ev, None)
    assert s3.gets.count(key) == 2, "the second invocation reused the first one's audio"


def test_one_enrolled_profile_names_nobody(stub_embedder, monkeypatch):
    """`decide_name` returns a name with a NULL margin when the pool holds one profile —
    "the closest of the only person I know". Written to a transcript it reads as an
    identification, and it lands on every turn including the other speakers': one enrolled
    name spread over everybody, which is worse than no name at all.

    Discrimination needs two profiles. That is not a tuning choice — with overlapping score
    distributions and no usable absolute threshold, one candidate cannot be told from a
    stranger."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: _match_artifact(mode="on"),
                                                  key: _wav_bytes()}))
    seen = []
    _writer(monkeypatch, [_profile()], seen)
    out = se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                                 "object": {"key": art}}}]}, None)
    assert out["matched"] == 0
    assert not any(p["op"] == "match_names" for p in seen)


def test_two_profiles_can_produce_a_name(stub_embedder, monkeypatch):
    """The counterpart: with a runner-up to beat, the same turn is nameable. Without this
    the test above would pass for a matcher that never names anything."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/match-1.json"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: _match_artifact(mode="on"),
                                                  key: _wav_bytes()}))
    seen = []
    near = [1.0] * 192
    far = [1.0] + [-1.0] * 191
    _writer(monkeypatch, [_profile("vp-1", "Ben L", vec=near),
                          _profile("vp-2", "Zoe", vec=far)], seen)
    out = se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                                 "object": {"key": art}}}]}, None)
    assert out["matched"] >= 1
    assert next(p for p in seen if p["op"] == "match_names")["results"][0][
        "display_name"] == "Ben L"


# ---- harvest: one gesture, a usable profile -------------------------------
#
# The human vouches for ONE turn. Every other cluster member is machine-assigned,
# and `_propagate` caps those at `tentative` on purpose. Storing them as samples
# promotes a suggestion to permanent biometric ground truth — worth doing, and
# only defensible while it is labelled as such.


def _cands(*specs):
    return [{"turn": {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                      "start_sec": s, "end_sec": e},
             "vector": np.ones(192, dtype=np.float32), "turn_ref": f"t@{s}"}
            for s, e in specs]


def _audio(monkeypatch, seconds=120.0):
    # The per-invocation audio cache is cleared by `lambda_handler`, and these tests call
    # `_admit_harvest` directly. Without this they read the previous test's audio — a short
    # clip, from which a long window slices to nothing, and every admission "fails" for a
    # reason that has nothing to do with the rule under test.
    se._AUDIO_CACHE.clear()
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=seconds)}))


def test_a_turn_under_the_enrolment_floor_is_not_even_fetched(stub_embedder, monkeypatch):
    """The floor is REDUNDANT with the homogeneity check — a window under 10 s yields fewer
    than two frames, so `window_is_homogeneous` returns None and refuses it regardless. What
    the floor buys is not correctness but cost: no S3 read and no ONNX pass (~98 ms per
    second of audio) for a turn that cannot be admitted.

    Asserting the outcome alone would pass with the floor removed, which is how this was
    found. So this asserts the SAVING."""
    se._AUDIO_CACHE.clear()
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    s3 = FakeS3({key: _wav_bytes(seconds=120.0)})
    monkeypatch.setattr(se, "s3", lambda: s3)
    embeds = []
    monkeypatch.setattr(se, "embed_audio", lambda a, sr: embeds.append(1) or
                        np.ones(192, dtype=np.float32))

    out = se._admit_harvest("Ben_UCPK2", "2026-08-11", _cands((0.0, 4.0)))
    assert out == []
    assert embeds == [], "a turn that cannot be admitted was embedded anyway"
    assert s3.gets == [], "a turn that cannot be admitted was fetched anyway"

    out = se._admit_harvest("Ben_UCPK2", "2026-08-11", _cands((10.0, 24.0)))
    assert len(out) == 1 and embeds, "a long enough turn was not considered"


def test_an_unjudgeable_window_is_refused_like_a_bad_one(stub_embedder, monkeypatch):
    """`None` means "could not check". Admitting it is how a guard becomes decoration —
    the anchor's own check refuses None too."""
    _audio(monkeypatch)
    monkeypatch.setattr(se.vp, "window_is_homogeneous", lambda frames: None)
    assert se._admit_harvest("Ben_UCPK2", "2026-08-11", _cands((0.0, 20.0))) == []


def test_a_window_with_two_voices_is_refused(stub_embedder, monkeypatch):
    _audio(monkeypatch)
    monkeypatch.setattr(se.vp, "window_is_homogeneous", lambda frames: False)
    assert se._admit_harvest("Ben_UCPK2", "2026-08-11", _cands((0.0, 20.0))) == []


def test_harvest_stops_at_the_sample_cap(stub_embedder, monkeypatch):
    _audio(monkeypatch)
    monkeypatch.setattr(se, "ENROL_MAX_SECONDS", 10_000.0)
    out = se._admit_harvest("Ben_UCPK2", "2026-08-11",
                            _cands(*[(float(i * 12), float(i * 12 + 11)) for i in range(9)]))
    assert len(out) == se.ENROL_MAX_SAMPLES


def test_harvest_stops_at_the_seconds_cap(stub_embedder, monkeypatch):
    _audio(monkeypatch)
    monkeypatch.setattr(se, "ENROL_MAX_SAMPLES", 100)
    monkeypatch.setattr(se, "ENROL_MAX_SECONDS", 25.0)
    out = se._admit_harvest("Ben_UCPK2", "2026-08-11",
                            _cands((0.0, 11.0), (12.0, 23.0), (24.0, 35.0), (36.0, 47.0)))
    assert 0 < len(out) <= 3


def test_nothing_is_harvested_when_the_anchor_itself_was_refused(stub_embedder,
                                                                 monkeypatch):
    """`_propagate` runs BEFORE the anchor's homogeneity and between-voices checks. Without
    the ordering, six samples get stored for a profile whose own corrected window was just
    judged to hold two voices — the exact condition one sample is refused for."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    art = "voiceprint_requests/co-1/s/r1.json"
    doc = json.dumps({"session_base": "sid" + "c" * 32, "company_id": "co-1",
                      "user_folder": "Ben_UCPK2", "date": "2026-08-11",
                      # The ANCHOR is 4 s — unjudgeable, so its own enrolment is
                      # refused — while the cluster it names holds long turns that would
                      # otherwise be admitted. Refusing everything globally would make this
                      # test pass with the ordering removed, which is how it was found.
                      "correction": {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                                     "start_sec": 0.0, "end_sec": 4.0,
                                     "display_name": "Ben L"},
                      "turns": [{"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                                 "start_sec": 0.0, "end_sec": 4.0},
                                {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                                 "start_sec": 10.0, "end_sec": 26.0},
                                {"source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                                 "start_sec": 30.0, "end_sec": 46.0}],
                      "enrol": {"voiceprint_id": "vp-1"}})
    monkeypatch.setattr(se, "s3", lambda: FakeS3({art: doc,
                                                  key: _wav_bytes(seconds=60.0)}))
    candidates_embedded = []
    real_frames = se._frames
    monkeypatch.setattr(se, "_frames",
                        lambda a, sr: candidates_embedded.append(1) or real_frames(a, sr))
    seen = []
    monkeypatch.setattr(se, "invoke_writer", lambda p: seen.append(p) or {"written": 0})
    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": art}}}]}, None)
    assert (seen[0]["enrol"] or {}).get("refused"),         "the anchor was not refused; the test proves nothing"
    assert not (seen[0]["enrol"] or {}).get("embedding")
    assert seen[0]["harvest"] == []
    # Two guards stand here on purpose — the admission pass is skipped AND the payload
    # refuses to carry it — so removing either alone leaves the outcome unchanged. That is
    # what belt-and-braces means, and it is also how a mutation can look uncaught. The cost
    # is the observable difference: a refused anchor must not pay for the cluster's audio.
    assert len(candidates_embedded) == 1, (
        "the cluster was embedded for a harvest that could never be stored")


def test_the_spread_op_writes_nothing(stub_embedder, monkeypatch):
    """It exists because the two ops that could otherwise produce this number both have side
    effects: `enrol` stores the sample when the window passes, and `match` never computes
    frames at all. A diagnostic that writes is not a diagnostic."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    se._AUDIO_CACHE.clear()
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=30.0)}))
    called = []
    monkeypatch.setattr(se, "invoke_writer", lambda p: called.append(p) or {})
    out = se.lambda_handler({"op": "spread", "user_folder": "Ben_UCPK2",
                             "date": "2026-08-11",
                             "source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                             "start_sec": 0.0, "end_sec": 20.0}, None)
    assert called == [], "a read-only probe invoked the writer"
    assert out["frames"] >= 2 and out["verdict"] == "homogeneous"
    assert out["spread"] is not None and out["limit"] == se.vp.DEFAULT_MAX_FRAME_SPREAD


def test_the_spread_op_reports_unjudgeable_rather_than_guessing(stub_embedder, monkeypatch):
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    se._AUDIO_CACHE.clear()
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=30.0)}))
    out = se.lambda_handler({"op": "spread", "user_folder": "Ben_UCPK2",
                             "date": "2026-08-11",
                             "source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                             "start_sec": 0.0, "end_sec": 4.0}, None)
    assert out["verdict"] == "unjudgeable" and out["spread"] is None


# ---- the tail of a long turn is not thrown away ---------------------------


def test_the_last_seconds_of_a_long_turn_reach_the_vector():
    """`range(..., len(audio) - cap + 1, ...)` walks only WHOLE pieces. A 113.6 s turn was
    embedded from its first 90 seconds and the remaining 23.6 s — 21 % of that person's
    speech — never entered the vector, silently, producing an ordinary-looking result.

    Asserted on coverage rather than on the output, because the output of a dropped tail is
    indistinguishable from the output of a shorter recording."""
    import lambda_speaker_embed as m
    seen = []

    class Rec:
        def run(self, _o, feed):
            seen.append(feed["wav"].shape[-1])
            return [np.ones((1, 192), dtype=np.float32)]

    m._session = Rec()
    try:
        sr = 16000
        m.embed_audio(np.zeros(int(113.6 * sr), dtype=np.float32), sr)
        covered = sum(seen)
        assert covered >= int(113.0 * sr), (
            f"covered {covered / sr:.1f}s of 113.6s — the tail was dropped")
    finally:
        m._session = None


def test_a_scrap_of_a_remainder_is_not_its_own_piece():
    """Below a second the remainder cannot characterise a voice, and averaged in with equal
    weight it drags the result toward whatever noise it holds."""
    import lambda_speaker_embed as m
    seen = []

    class Rec:
        def run(self, _o, feed):
            seen.append(feed["wav"].shape[-1])
            return [np.ones((1, 192), dtype=np.float32)]

    m._session = Rec()
    try:
        sr = 16000
        m.embed_audio(np.zeros(int(45.3 * sr), dtype=np.float32), sr)
        assert len(seen) == 1, f"a 0.3s scrap became its own forward pass: {seen}"
    finally:
        m._session = None


# ---- frames must contain speech before they are evidence ------------------
#
# The homogeneity guard's job is to refuse a window holding two people. The only
# window it has ever ACCEPTED on real audio was the quietest one measured — two
# frames at -67 and -57 dBFS, the noise floor, about 30 dB below this device's
# speech. The input it exists to refuse is the input it let through.


def test_a_silent_frame_is_not_evidence_about_a_speaker(monkeypatch):
    sr = 16000
    loud = np.sin(2 * np.pi * 220.0 * np.arange(5 * sr) / sr).astype(np.float32) * 0.5
    quiet = np.zeros(5 * sr, dtype=np.float32)
    assert len(se._frames(np.concatenate([loud, quiet]), sr)) == 1


def test_a_window_of_pure_silence_becomes_unjudgeable_not_homogeneous(monkeypatch):
    """Not merely refused — UNJUDGEABLE. Two frames of noise resemble each other, so an
    ungated pair scores near zero and reads as "one voice". `None` is refused at all four
    consumers; `True` is stored."""
    sr = 16000
    frames = se._frames(np.zeros(20 * sr, dtype=np.float32), sr)
    assert frames == []
    assert se.vp.window_is_homogeneous([se.embed_audio(f, sr) for f in frames]) is None


def test_the_gate_is_in_frames_so_the_diagnostic_sees_it_too(stub_embedder, monkeypatch):
    """Gating at the writing sites only would leave `op: "spread"` ungated — and that is the
    op used to check whether the gate works, so it would report "not gating" while gating
    everywhere that matters."""
    key = "users/Ben_UCPK2/audio/2026-08-11/x_sid" + "c" * 32 + "_c0000.wav"
    se._AUDIO_CACHE.clear()
    monkeypatch.setattr(se, "s3", lambda: FakeS3({key: _wav_bytes(seconds=30.0,
                                                                 silent=True)}))
    out = se.lambda_handler({"op": "spread", "user_folder": "Ben_UCPK2",
                             "date": "2026-08-11",
                             "source_filename": "x_sid" + "c" * 32 + "_c0000.wav",
                             "start_sec": 0.0, "end_sec": 20.0}, None)
    assert out["frames"] == 0
    assert out["verdict"] == "unjudgeable"


def test_a_quiet_but_real_frame_is_kept():
    """A floor, not a judgement: it removes frames that cannot be about anybody, not frames
    that are merely quiet. These devices already record 15 dB below normal."""
    sr = 16000
    # -40 dBFS: well under normal speech, well over the noise floor.
    quiet_speech = (np.sin(2 * np.pi * 220.0 * np.arange(10 * sr) / sr)
                    * 0.014).astype(np.float32)
    assert len(se._frames(quiet_speech, sr)) == 2


# ---- the end of the window is judged too ---------------------------------


def _tone(seconds, sr=16000, amp=0.5):
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    return (np.sin(2 * np.pi * 220.0 * t) * amp).astype(np.float32)


def test_the_last_seconds_of_a_window_are_not_left_unjudged():
    """Walking forward in whole 5 s steps left everything after the last one unlooked-at: a
    14.7 s window — the shape enrolment is meant to accept — was decided on its first 10 s
    and a THIRD of it was never examined.

    Asserted on coverage, because a window judged on two thirds of itself produces a verdict
    that looks exactly like one judged on all of it."""
    sr = 16000
    frames = se._frames(_tone(14.7, sr), sr)
    assert len(frames) == 3
    assert all(len(f) == int(se.FRAME_SECONDS * sr) for f in frames), \
        "a short frame does not embed comparably; the difference would read as two voices"


def test_a_window_that_divides_evenly_gains_no_extra_frame():
    sr = 16000
    assert len(se._frames(_tone(15.0, sr), sr)) == 3


def test_a_window_shorter_than_one_frame_yields_nothing():
    """Not one short frame — nothing. Fewer than two frames already means "cannot tell", and
    that is the answer a window this short deserves."""
    sr = 16000
    assert se._frames(_tone(3.0, sr), sr) == []


def test_the_tail_frame_is_still_gated_on_speech():
    """The end-anchored frame is a frame like any other: if the window ends in silence it
    must not sneak past the gate on account of where it sits."""
    sr = 16000
    audio = np.concatenate([_tone(12.0, sr), np.zeros(int(2.9 * sr), dtype=np.float32)])
    frames = se._frames(audio, sr)
    assert all(se._dbfs(f) >= se.FRAME_MIN_DBFS for f in frames)
