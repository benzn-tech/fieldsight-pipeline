"""Turn audio into a voiceprint vector, and turns into names.

Two operations, invoked directly (never by an S3 event — nothing about this is triggered by
a file landing):

    {"op": "enrol", "voiceprint_id", "user_folder", "date", "source_filename",
     "start_sec", "end_sec", "correction_ref"?}
    {"op": "match", "session", "user_folder", "date", "company_id",
     "profiles": [{"person_key", "status", "embedding"}, ...],
     "turns": [{"source_filename", "start_sec", "end_sec"}, ...]}

Pure compute: **no database, no VPC.** This function runs on python3.12 because that is where
onnxruntime comes from (the VAD layer is cp312-only) and `PsycopgLayer` is cp311-only — one
function cannot carry both, and holding a connection here made every invocation raise
`ModuleNotFoundError` while the deploy stayed green. Profiles arrive in the event from
org-api, which is in-VPC and already owns the consent/withdrawn filters.

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

import batch_seal
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
    parts, rate, first = [], None, None
    for p in pieces:
        # A member is a VAD UNIT under audio_segments/, not the device's upload, and the
        # unit itself begins _off{T} into its chunk. Both conversions live in batch_seal
        # beside raw_key_for so there is one implementation of each.
        raw, lo, hi = batch_seal.raw_window_for_member(p["chunk_key"], p["start_sec"],
                                                       p["end_sec"])
        audio, sr = _read_wav(_get(raw))
        rate = sr
        first = first or raw
        parts.append(audio[int(lo * sr):int(hi * sr)])
    return first, np.concatenate(parts), rate


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

    # Returned, not stored. The in-VPC writer persists it into the column that already
    # requires consent, so the vector never lands in S3 — the biometric-residence defect
    # that relocated twice during review (first a cache, then a request artifact).
    return {"status": "embedded",
            "voiceprint_id": event["voiceprint_id"],
            "embedding": [float(x) for x in embed_audio(clip, sr)],
            "s3_key": key, "window": [start, end],
            "created_by": event.get("created_by"),
            "correction_ref": event.get("correction_ref")}


def _match(event):
    # From the caller, never from a database. org-api is in-VPC, holds psycopg, and owns
    # `profiles_for_matching` — the one query whose mistakes are invisible, because a
    # withdrawn profile that still matches is not a withdrawal. Duplicating that filter
    # here would be a second place for it to be forgotten.
    profiles = event.get("profiles") or []
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


WRITER_FUNCTION = os.environ.get("VOICEPRINT_WRITER_FUNCTION", "")


def invoke_writer(payload):
    """Hand the result to the in-VPC half.

    RequestResponse, not Event, and the reason is not latency: under async invocation the
    192-d vector would sit in Lambda's internal queue and any DLQ — durable biometric
    storage in a place nobody would think to sweep, which is this design's defect relocating
    for a fourth time. Synchronous also means a failure surfaces here rather than being
    retried invisibly by the service.
    """
    import boto3
    resp = boto3.client("lambda").invoke(
        FunctionName=WRITER_FUNCTION, InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode())
    if resp.get("FunctionError"):
        raise RuntimeError(f"voiceprint writer failed: "
                           f"{resp['Payload'].read()[:400]!r}")
    return resp


MAX_PROPAGATION_TURNS = int(os.environ.get("SPEAKER_PROPAGATION_MAX_TURNS", "300"))


def _propagate(folder, date, turns, reference, display_name, asserted_ref):
    """Correct one passage, name that person's other passages in the same session.

    The unit is a VOICE, not a turn. Two spec revisions died scoring turns against the
    corrected window directly, because `decide_name` refuses to confirm on a single
    candidate — deliberately, since confirming on one candidate means confirming on an
    absolute similarity. Clustering the session and letting the correction NAME A CLUSTER
    removes that: the reference selects a cluster instead of competing beside it.

    Everything here is capped at `tentative`. Gate A froze the CLUSTERING threshold (0.85,
    measured); the margin that would justify `confirmed` has never been measured, so an
    inferred name is a suggestion and says so.

    An empty turn list degrades to naming only the corrected turn — an older producer, or a
    session whose transcript is not ready, must not fail.
    """
    usable = [t for t in turns
              if float(t.get("end_sec", 0)) - float(t.get("start_sec", 0))
              >= vp.DEFAULT_MIN_TURN_S][:MAX_PROPAGATION_TURNS]
    if len(usable) < 2:
        return []

    vectors, refs = [], []
    for t in usable:
        try:
            _, clip, sr = _window_audio(folder, date, t["source_filename"],
                                        float(t["start_sec"]), float(t["end_sec"]))
        except Exception:
            # One unreadable turn must not lose the whole propagation. It simply goes
            # unnamed, which is the honest outcome for audio nobody could read.
            logger.warning("propagation: could not read %s", t.get("source_filename"))
            continue
        vectors.append(embed_audio(clip, sr))
        refs.append(f"{t['source_filename']}@{float(t['start_sec'])}")
    if len(vectors) < 2:
        return []

    labels = vp.cluster_turns(vectors)
    groups = {}
    for i, lab in enumerate(labels):
        groups.setdefault(lab, []).append(i)
    centroids, keys = [], []
    for lab, idxs in groups.items():
        keys.append(lab)
        centroids.append(sum(vectors[i] for i in idxs) / len(idxs))

    which, margin = vp.assign(reference, centroids)
    if which is None:
        return []
    if margin is not None and margin < vp.DEFAULT_MIN_MARGIN:
        # The corrected window sits between two voices — §2 measured 1 turn in 6 holding
        # two. Naming a cluster from it would spread one person's name over another's whole
        # session, so it names nothing and the asserted turn stands alone.
        logger.warning("propagation refused: reference is between clusters (margin %.3f)",
                       margin)
        return []

    out = []
    for i in groups[keys[which]]:
        if refs[i] == asserted_ref:
            continue
        out.append({"turn_ref": refs[i], "state": "tentative",
                    "cluster_ref": f"C{keys[which]}", "asserted": False,
                    "display_name": display_name})
    logger.info("propagation: %d turns in %d voices, named %d",
                len(vectors), len(groups), len(out))
    return out


def _from_request_artifact(bucket, key):
    """One correction, start to finish.

    The artifact is org-api's output: it holds the window the user marked and the profiles
    that survived the consent and withdrawn filters. This function has no database and no
    other way to obtain a profile, which is what keeps those filters in one place.
    """
    req = json.loads(_get(key))
    c = req.get("correction") or {}
    folder, date = req.get("user_folder"), req.get("date")
    if not folder or not date:
        # Deliberately not derived from `session_base`. A first version guessed, and the
        # guess produced `users/s1/audio//x.wav` — a key that cannot exist, which would have
        # surfaced as a NoSuchKey far from the missing field. The producer knows both; if it
        # ever stops sending them, this says so.
        raise ValueError(
            f"request artifact {key} carries no user_folder/date; the producer has both and "
            f"guessing them puts the read on a key that cannot exist")
    start, end = float(c["start_sec"]), float(c["end_sec"])
    s3_key, clip, sr = _window_audio(folder, date, c["source_filename"], start, end)
    v = embed_audio(clip, sr)

    profiles = req.get("profiles") or []
    rows = [{"person_key": p["person_key"], "score": vp.cosine(v, p["embedding"])}
            for p in profiles]
    d = vp.decide_name(vp.aggregate_scores(rows), duration_s=end - start)

    # The turn the user asserted. Its name is not an inference and is written as
    # `correction`; the writer refuses to give it a profile id unless it is marked asserted.
    turn_ref = f"{c['source_filename']}@{start}"
    results = [{"turn_ref": turn_ref, "state": "confirmed",
                "cluster_ref": None, "asserted": True,
                "display_name": c.get("display_name"),
                "score": d.margin}]
    results.extend(_propagate(folder, date, req.get("turns") or [], v,
                              c.get("display_name"), turn_ref))

    payload = {
        "op": "propagation",
        "company_id": req.get("company_id"),
        "session_base": req.get("session_base"),
        "correction_ref": req.get("request_id"),
        "cluster_threshold": vp.DEFAULT_CLUSTER_TAU,
        "results": results,
    }
    invoke_writer(payload)
    logger.info("correction applied: session=%s ref=%s audio=%s",
                req.get("session_base"), turn_ref, s3_key)
    return {"status": "applied", "turn_ref": turn_ref}


def lambda_handler(event, context):
    records = (event or {}).get("Records")
    if records:
        # An S3 event carries `Records`, not `op`. Without this branch the function dies in
        # 3 ms with `unknown op None` -- which is exactly what a real correction on TEST did
        # on 2026-08-14, after the trigger was wired and this entry point was not.
        out = None
        for r in records:
            b = r["s3"]["bucket"]["name"]
            k = r["s3"]["object"]["key"]
            out = _from_request_artifact(b, k)
        return out or {"status": "empty"}
    op = (event or {}).get("op")
    if op == "enrol":
        return _enrol(event)
    if op == "match":
        return _match(event)
    raise ValueError(f"unknown op {op!r} — expected 'enrol' or 'match'")
