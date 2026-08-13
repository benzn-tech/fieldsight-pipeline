"""Turn audio into a voiceprint vector, and turns into names.

Two operations, invoked directly (never by an S3 event — nothing about this is triggered by
a file landing):

    {"op": "enrol", "voiceprint_id", "user_folder", "date", "source_filename",
     "start_sec", "end_sec", "correction_ref"?}
    {"op": "match", "session", "user_folder", "date", "site_id"?,
     "turns": [{"source_filename", "start_sec", "end_sec"}, ...]}

They fail in opposite directions, and the code is shaped around that asymmetry:

  * `enrol` WRITES to a profile. A window holding two voices poisons it permanently — a
    profile cannot be un-poisoned, only the contributing sample deleted — so the
    homogeneity guard runs before anything is stored, and a window it *cannot judge* is
    refused rather than accepted.
  * `match` NAMES turns. A wrong confident name costs more than a missing one, so the
    duration floor is applied before the model is even asked, and everything ambiguous
    degrades to `tentative` or `unknown`.

The model is ONNX, downloaded from S3 at cold start into /tmp — the same place and for the
same reason as the Silero VAD model. BUG-02: loading that one from a Lambda Layer shipped a
DIFFERENT VERSION whose output was silently wrong, and a voiceprint model that drifts moves
every threshold in `voiceprint_utils` with no error anywhere. `onnxruntime` and `numpy` come
from the existing VAD layer, which already carries both and no torch.

Audio is always read from `users/{folder}/audio/{date}/` — the raw upload. The Phase 0
numbers are raw-audio numbers and do not transfer to the normalised copy under
`audio_segments/`.

Spec: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md
Plan: docs/superpowers/plans/2026-08-11-speaker-identity-implementation.md (phase 3)
"""
import io
import json
import logging
import os
import wave

import numpy as np

import batch_stitch
import voiceprint_utils as vp

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
MODEL_S3_KEY = os.environ.get("ECAPA_MODEL_S3_KEY", "models/ecapa_tdnn.onnx")
MODEL_LOCAL = "/tmp/ecapa_tdnn.onnx"
FRAME_SECONDS = float(os.environ.get("VOICEPRINT_FRAME_SECONDS", "5.0"))
EXPECTED_RATE = 16000

_s3 = None
_session = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _ensure_model():
    """Download the model and its external weights once per container.

    The export writes a small graph plus an 83 MB `.data` sidecar; onnxruntime resolves the
    sidecar by name from the graph's own directory, so both must land in /tmp together and
    keep their filenames.
    """
    global _session
    if _session is not None:
        return _session
    import onnxruntime as ort
    if not os.path.exists(MODEL_LOCAL):
        s3().download_file(S3_BUCKET, MODEL_S3_KEY, MODEL_LOCAL)
        sidecar = MODEL_S3_KEY + ".data"
        try:
            s3().download_file(S3_BUCKET, sidecar, MODEL_LOCAL + ".data")
        except Exception:
            # A graph small enough to hold its own weights is legitimate; a missing sidecar
            # for a graph that needs one fails loudly at InferenceSession, which is the
            # right place for it to fail.
            logger.info("no external weights at %s — assuming a self-contained graph",
                        sidecar)
    _session = ort.InferenceSession(MODEL_LOCAL, providers=["CPUExecutionProvider"])
    return _session


def embed_audio(audio, sample_rate):
    """One 192-d vector for this audio. Stubbed in tests; the model's fidelity is pinned by
    test_voiceprint_onnx_parity against committed reference vectors."""
    sess = _ensure_model()
    out = sess.run(None, {"wav": np.asarray(audio, dtype=np.float32)[None, :],
                          "wav_lens": np.array([1.0], dtype=np.float32)})
    return np.asarray(out[0]).ravel()


def _raw_key(user_folder, date, source_filename):
    """The device's own upload. Never `audio_segments/` — see the module docstring."""
    stem = source_filename.split("_off")[0]
    if stem.endswith(".json") or stem.endswith(".wav"):
        stem = stem.rsplit(".", 1)[0]
    return f"users/{user_folder}/audio/{date}/{stem}.wav"


def _read_wav(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getframerate() != EXPECTED_RATE:
            raise ValueError(
                f"expected {EXPECTED_RATE} Hz audio, got {w.getframerate()} — the model is "
                f"16 kHz and degrades silently at any other rate")
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit PCM")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, EXPECTED_RATE


def _get(key):
    """Raises on a denial rather than returning nothing.

    `except ClientError: pass` turned a missing IAM prefix into a 200-with-an-empty-result
    before, and without ListBucket a missing key answers 403 rather than 404, so "not
    allowed" and "not there" are indistinguishable from inside. Both must be loud."""
    return s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _fetch(user_folder, date, source_filename):
    key = _raw_key(user_folder, date, source_filename)
    audio, sr = _read_wav(_get(key))
    return key, audio, sr


def _window_audio(user_folder, date, source_filename, start, end):
    """The audio of one turn, always taken from the device's own upload.

    A batched turn does not point at a chunk. Its `source_filename` is the stitched object
    under `audio_segments/` and its offsets are batch-relative — so the naive read builds a
    key that does not exist, and a merely-corrected filename would still cut the wrong
    seconds. Both features' tests passed while disagreeing about this, because the speaker
    tests only ever fed per-chunk names.

    Reading the stitched copy instead is not the fix: every threshold in `voiceprint_utils`
    was measured on raw audio and none of them transfer to the normalised one. So the
    coordinates come back through the batch map, which exists for exactly this, and the raw
    rule survives. A window spanning a seam is concatenated from both chunks rather than
    silently truncated to the first.
    """
    if not batch_stitch.is_batched(source_filename):
        key, audio, sr = _fetch(user_folder, date, source_filename)
        return key, audio[int(start * sr):int(end * sr)], sr

    batch_key = f"audio_segments/{user_folder}/{date}/{source_filename}"
    doc = json.loads(_get(batch_stitch.map_key_for_audio(batch_key)))
    pieces = batch_stitch.locate_in_members(doc, start, end)
    parts, rate = [], None
    for p in pieces:
        audio, sr = _read_wav(_get(p["chunk_key"]))
        rate = sr
        parts.append(audio[int(p["start_sec"] * sr):int(p["end_sec"] * sr)])
    return pieces[0]["chunk_key"], np.concatenate(parts), rate


def load_profiles(company_id, site_id=None):
    """Every profile this company may match against, one row per SAMPLE.

    Separated so tests can supply rows without a database, and so the in-VPC connection is
    not made at import time."""
    from db.connection import get_connection
    from repositories import voiceprints
    with get_connection() as conn:
        rows = voiceprints.profiles_for_matching(conn, company_id, site_id=site_id)
    return [{"person_key": r["user_id"] or r["id"],
             "display_name": r.get("display_name"),
             "status": r.get("status"),
             "embedding": r["embedding"]} for r in rows]


def store_sample(voiceprint_id, company_id, embedding, s3_key, window, created_by=None,
                 correction_ref=None):
    from db.connection import get_connection
    from repositories import voiceprints
    with get_connection() as conn:
        return voiceprints.add_sample(
            conn, company_id, voiceprint_id, embedding, source="correction",
            s3_key=s3_key, window=window, created_by=created_by,
            correction_ref=correction_ref)


def _frames(audio, sr):
    step = int(FRAME_SECONDS * sr)
    return [audio[i:i + step] for i in range(0, len(audio) - step + 1, step)]


def _enrol(event):
    start, end = float(event["start_sec"]), float(event["end_sec"])
    key, clip, sr = _window_audio(event["user_folder"], event["date"],
                                  event["source_filename"], start, end)

    verdict = vp.window_is_homogeneous([embed_audio(f, sr) for f in _frames(clip, sr)])
    if verdict is not True:
        # None ("could not check") is refused alongside False. Treating them alike in the
        # permissive direction is how a guard becomes decoration.
        reason = ("window is not homogeneous — it may hold more than one voice"
                  if verdict is False else
                  "window too short to check homogeneity — refusing rather than assuming")
        logger.warning("enrol refused for %s [%s-%s]: %s", key, start, end, reason)
        return {"status": "refused", "reason": reason, "s3_key": key}

    store_sample(voiceprint_id=event["voiceprint_id"],
                 company_id=event.get("company_id"),
                 embedding=embed_audio(clip, sr),
                 s3_key=key, window=(start, end),
                 created_by=event.get("created_by"),
                 correction_ref=event.get("correction_ref"))
    return {"status": "stored", "s3_key": key, "window": [start, end]}


def _match(event):
    profiles = load_profiles(event.get("company_id"), event.get("site_id"))
    by_key = {}
    for p in profiles:
        by_key.setdefault(p["person_key"], p)

    results = []
    for turn in event.get("turns") or []:
        start, end = float(turn["start_sec"]), float(turn["end_sec"])
        duration = end - start
        if duration < vp.DEFAULT_MIN_TURN_S:
            # Refused before the model is asked: a turn this short is not weak evidence,
            # it is unusable evidence, and embedding it would be paid for as well.
            d = vp.decide_name({}, duration_s=duration)
            results.append({"turn": turn, "status": d.status, "name": None,
                            "reason": d.reason})
            continue
        key, clip, sr = _window_audio(event["user_folder"], event["date"],
                                      turn["source_filename"], start, end)
        v = embed_audio(clip, sr)
        rows = [{"person_key": p["person_key"],
                 "score": vp.cosine(v, p["embedding"])} for p in profiles]
        d = vp.decide_name(vp.aggregate_scores(rows), duration_s=duration)
        status = d.status
        if status == "confirmed" and by_key.get(d.name, {}).get("status") == "tentative":
            # A profile that has not earned confirmation cannot hand one out.
            status = "tentative"
        results.append({"turn": turn, "status": status, "name": d.name,
                        "margin": d.margin, "reason": d.reason})
    return {"session": event.get("session"), "results": results}


def lambda_handler(event, context):
    op = (event or {}).get("op")
    if op == "enrol":
        return _enrol(event)
    if op == "match":
        return _match(event)
    raise ValueError(f"unknown op {op!r} — expected 'enrol' or 'match'")
