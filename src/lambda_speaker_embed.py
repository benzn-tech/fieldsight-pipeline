"""Turn audio into a voiceprint vector, and turns into names.

**In production this function is driven by an S3 event.** org-api is in-VPC with no NAT and
cannot invoke outward (BUG-36), so it hands work over by writing an artifact under
`voiceprint_requests/` and a hand-wired notification (BUG-33 — the template carries no S3
event for this function) brings it here. `Records` in the event means that path;
`_from_request_artifact` and `_from_match_artifact` read the `op` inside the object.

The header used to say the opposite — "never by an S3 event, nothing about this is triggered
by a file landing" — which was true until 2026-08-14 and then described the one entry point
that matters.

Three direct ops remain, used by scripts and tests:

    {"op": "enrol", "voiceprint_id", "user_folder", "date", "source_filename",
     "start_sec", "end_sec", "correction_ref"?}
    {"op": "match", "session", "user_folder", "date", "company_id",
     "turns": [{"source_filename", "start_sec", "end_sec"}, ...]}
    {"op": "spread", "user_folder", "date", "source_filename", "start_sec", "end_sec"}

`op=enrol`'s result is NOT the writer's `op=enrol` input despite the shared name: it carries
no `company_id`, which the writer requires. Nothing wires the pair, and it would fail loudly
if anything did.

Pure compute: **no database, no VPC.** This function runs on python3.12 because that is where
onnxruntime comes from (the VAD layer is cp312-only) and `PsycopgLayer` is cp311-only — one
function cannot carry both, and holding a connection here made every invocation raise
`ModuleNotFoundError` while the deploy stayed green. Nothing it needs comes from a database:
the artifact carries the window and the session's turns, and NO voice vectors — carrying them
was the biometric-residence defect's fifth home.

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
import transcript_utils
import batch_stitch
import turn_name_overlay
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


# Measured on the deployed function (1769 MB), embedding one window of each length with
# the cap disabled:
#
#     20 s -> 537 MB      40 s -> 802 MB      60 s -> 1158 MB      80 s -> 1486 MB
#
# ~15.8 MB per second of audio over a ~220 MB baseline, which puts the ceiling near 98 s and
# explains the 108 s window that killed it. 20.0 was a guess made before this was measured;
# 45 s uses about half the available memory and gives each piece more than twice the context.
MAX_EMBED_SECONDS = float(os.environ.get("VOICEPRINT_MAX_EMBED_SECONDS", "45.0"))

# Harvest: turning ONE naming gesture into a usable profile.
#
# 10 s, not the 3 s matching floor and not the 5 s a first draft picked.
# `window_is_homogeneous` returns None for fewer than two frames and frames are cut at
# FRAME_SECONDS = 5.0, so a 5-9.99 s window is UNJUDGEABLE — and "could not check" is
# refused alongside "no", or the guard becomes decoration. A 5 s floor would have admitted
# nothing at all, silently. `lambda_org_api` already documented this: "a window under ten
# seconds cannot be judged homogeneous and is not enrolled".
ENROL_MIN_TURN_S = float(os.environ.get("VOICEPRINT_ENROL_MIN_TURN_S", "10.0"))
ENROL_MAX_SAMPLES = int(os.environ.get("VOICEPRINT_ENROL_MAX_SAMPLES", "6"))
ENROL_MAX_SECONDS = float(os.environ.get("VOICEPRINT_ENROL_MAX_SECONDS", "60.0"))


def sr_floor(sample_rate):
    """The shortest remainder worth its own forward pass — one second.

    Below this the piece is a scrap: too short to characterise a voice, and averaged in with
    equal weight it moves the result toward whatever noise it happens to contain.
    """
    return int(sample_rate)


def _embed_once(audio):
    sess = _ensure_model()
    out = sess.run(None, {"wav": np.asarray(audio, dtype=np.float32)[None, :],
                          "wav_lens": np.array([1.0], dtype=np.float32)})
    return np.asarray(out[0]).ravel()


def embed_audio(audio, sample_rate):
    """One 192-d vector for this audio. Stubbed in tests; the model's fidelity is pinned by
    test_voiceprint_onnx_parity against committed reference vectors.

    **Long audio is embedded in pieces and averaged, not in one pass.** ECAPA's convolutions
    run the full length of the input, so the activations grow with it: a 108-second turn
    killed this function with `Runtime.OutOfMemory` at 1769 MB after 40 seconds, on the
    first real enrolment attempted against whole-chunk transcription. Every earlier run —
    Phase 0, the parity fixtures, yesterday's live verification — used a window of a few
    seconds, so nothing had ever asked the model for a long one.

    A 108-second turn is not unusual. `TRANSCRIBE_WHOLE_CHUNK` produces turns the length of
    the chunk, and the matcher embeds EVERY turn in a session, so the ceiling is reached
    immediately rather than rarely.

    Averaging unit vectors is the ordinary treatment for an utterance longer than the model
    was trained on, and it is what `window_is_homogeneous` already does with the same
    frames. Audio at or under the cap takes the single-pass path untouched, so the parity
    fixtures still compare like with like.
    """
    audio = np.asarray(audio, dtype=np.float32)
    cap = int(MAX_EMBED_SECONDS * sample_rate)
    if len(audio) <= cap:
        return _embed_once(audio)

    # `range(..., len(audio) - cap + 1, ...)` walks only WHOLE pieces, so everything after
    # the last full one is discarded: a 113.6 s turn was embedded from its first 90 seconds
    # and the remaining 23.6 s — 21 % of the person's speech — never entered the vector.
    # Silently, and the vector looked entirely ordinary.
    #
    # The remainder is kept unless it is too short to be worth a forward pass on its own; a
    # sub-second scrap is noise, not evidence, and would drag the average toward whatever
    # happened to be in it.
    starts = list(range(0, max(1, len(audio) - cap + 1), cap))
    tail = starts[-1] + cap
    if len(audio) - tail >= sr_floor(sample_rate):
        starts.append(tail)
    vecs = []
    for i in starts:
        v = _embed_once(audio[i:i + cap])
        n = np.linalg.norm(v)
        if n:
            # Normalised BEFORE averaging: the scores this feeds are cosines, so a piece
            # with a larger norm is not more of an opinion about who is speaking.
            vecs.append(v / n)
    if not vecs:
        return _embed_once(audio[:cap])
    mean = np.mean(vecs, axis=0)
    n = np.linalg.norm(mean)
    return mean / n if n else mean


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


# Decoded audio for the invocation in flight, keyed by S3 key. Cleared at the top of every
# handler call so a warm container never carries one session's audio into the next.
#
# Every turn used to re-download and re-parse the same object: a session of 100 turns from
# one batch fetched the same few chunks 100 times, ~176 ms and a few megabytes each. The
# turns in a match run are, by construction, all from the same handful of files.
_AUDIO_CACHE = {}
_AUDIO_CACHE_MAX = 8   # ~2 MB each as float32; the bound exists so a long session cannot
                       # accumulate the whole recording in memory beside the model


def _read_cached(key):
    hit = _AUDIO_CACHE.get(key)
    if hit is not None:
        return hit
    value = _read_wav(_get(key))
    if len(_AUDIO_CACHE) >= _AUDIO_CACHE_MAX:
        _AUDIO_CACHE.pop(next(iter(_AUDIO_CACHE)))
    _AUDIO_CACHE[key] = value
    return value


def _fetch(user_folder, date, source_filename):
    key = _raw_key(user_folder, date, source_filename)
    audio, sr = _read_cached(key)
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
        # The SAME missing term `batch_seal.raw_window_for_member` documents, on the branch
        # that does not go through it. `_raw_key` strips `_off{T}` to reach the device's
        # upload, so a position measured inside the VAD unit is short by T once the audio
        # is the whole chunk. Wrong audio, no error, and every voiceprint number computed
        # on it looks ordinary.
        offsets = transcript_utils.extract_vad_offsets_from_filename(source_filename)
        unit_start = float((offsets or [None])[0] or 0.0)
        start, end = start + unit_start, end + unit_start
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
        audio, sr = _read_cached(raw)
        rate = sr
        first = first or raw
        parts.append(audio[int(lo * sr):int(hi * sr)])
    return first, np.concatenate(parts), rate


# A frame quieter than this carries no speech worth comparing. The devices record speech at
# a median of -36 dBFS (measured), and the one window the homogeneity guard has ever accepted
# sat at -67/-57 — the noise floor, roughly 30 dB below speech. -55 sits between the two with
# room on both sides, and it is a floor rather than a judgement: it removes frames that
# cannot be about anybody, not frames that are merely quiet.
FRAME_MIN_DBFS = float(os.environ.get("VOICEPRINT_FRAME_MIN_DBFS", "-55.0"))

# The homogeneity limit, made settable WITHOUT a redeploy — and deliberately not moved.
#
# 0.35 was measured on read speech. On site audio it has refused every window ever tried, so
# no voiceprint can be created and everything downstream of enrolment — matching, automatic
# naming, harvest — has never run against real data. An evening of measurement produced a
# candidate number and then three reasons the measurement could not support it (see
# docs/superpowers/specs/2026-08-17-homogeneity-threshold-measured.md), so the constant stays
# where it is until somebody measures it properly.
#
# What this variable buys is the ability to unblock the path on TEST and find out whether the
# rest of it works, without shipping a number nobody can defend. PROD leaves it unset and gets
# `DEFAULT_MAX_FRAME_SPREAD` exactly as before.
#
# A guard that can be loosened from outside must SAY when it has been, or the next person
# reads a successful enrolment as evidence the audio was clean. `_limit_note()` is appended to
# every enrolment log line, accepted and refused alike — a guard that only speaks when it
# refuses leaves "it passed" and "it was switched off" looking identical.
def _read_limit(raw):
    """Parse the override, or fall back to the default LOUDLY rather than dying.

    This module also serves `match`, `propagation` and the S3-triggered correction path, so a
    bare `float(raw)` at import time turns a typo in one repo variable into
    Runtime.ImportModuleError on EVERY invocation of the speaker feature — while the deploy
    stays green and nothing reports it. The template constrains the parameter too; this is the
    half that survives a console edit or a direct `sam deploy`.

    Out-of-range is refused for the same reason it is refused in the template: cosine distance
    runs to 2.0 and the check is `spread <= limit`, so a limit of 2 admits any window at all —
    the guard switched off rather than loosened.
    """
    if raw is None:
        return vp.DEFAULT_MAX_FRAME_SPREAD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.error("VOICEPRINT_MAX_FRAME_SPREAD=%r is not a number; using the default "
                     "%.2f", raw, vp.DEFAULT_MAX_FRAME_SPREAD)
        return vp.DEFAULT_MAX_FRAME_SPREAD
    if not 0.0 < value <= 1.0:
        logger.error("VOICEPRINT_MAX_FRAME_SPREAD=%r is outside (0, 1.0]; using the default "
                     "%.2f. Above 1.0 the guard admits everything.",
                     raw, vp.DEFAULT_MAX_FRAME_SPREAD)
        return vp.DEFAULT_MAX_FRAME_SPREAD
    return value


MAX_FRAME_SPREAD = _read_limit(os.environ.get("VOICEPRINT_MAX_FRAME_SPREAD"))


def _admitted_limit():
    """The limit to STORE against a sample, or None when it was the ordinary one.

    None rather than always the number, so that the non-NULL rows in the database are exactly
    the set worth re-examining. A column that is populated on every row answers "what was the
    limit" and not the question anybody will actually ask, which is "which of these came in
    under a guard somebody had loosened".
    """
    return None if MAX_FRAME_SPREAD == vp.DEFAULT_MAX_FRAME_SPREAD else MAX_FRAME_SPREAD


def _limit_note():
    """Empty when the guard is at its default, loud when it is not."""
    if MAX_FRAME_SPREAD == vp.DEFAULT_MAX_FRAME_SPREAD:
        return ""
    return (" [HOMOGENEITY LIMIT OVERRIDDEN: %.3f, default %.3f — enrolments admitted under "
            "this limit are not evidence the window held one voice]"
            % (MAX_FRAME_SPREAD, vp.DEFAULT_MAX_FRAME_SPREAD))


def _dbfs(frame):
    arr = np.asarray(frame, dtype=np.float32)
    if arr.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr * arr)))
    return 20.0 * float(np.log10(rms)) if rms > 1e-9 else -120.0


def _frames(audio, sr):
    """Frames with speech in them, for the homogeneity comparison.

    The gate lives HERE and not in any caller, because four call sites build frames
    independently — `op=enrol`, `_admit_harvest`, `_from_request_artifact` and `op=spread` —
    and a gate applied at the writing sites only would leave the DIAGNOSTIC ungated. That is
    the one used to check whether the gate works, so it would report "not gating" while
    gating everywhere that matters.

    It also cannot live in `voiceprint_utils`: that module is pure numpy by contract, and
    `window_is_homogeneous` receives embeddings rather than audio, so it could not gate
    anything even in principle.

    **This does not unblock enrolment and is not meant to.** Dropping frames can only lower a
    maximum over pairs or leave it alone; the windows currently refused are refused on pairs
    of speech-bearing frames that no gate removes. What it stops is the failure that cannot
    be undone: a window of near-silence being judged "one voice" and stored as somebody's
    voiceprint. Too few surviving frames already means "cannot tell" at every consumer.
    """
    step = int(FRAME_SECONDS * sr)
    if len(audio) < step:
        return []
    starts = list(range(0, len(audio) - step + 1, step))
    # The window's END, as a full-length frame anchored backwards. Walking forward in whole
    # steps leaves everything after the last one unjudged: a 14.7 s window was decided on its
    # first 10 s and the remaining 4.7 s — a third of it — was never looked at. A partial
    # frame is not the answer, because a 4.7 s clip and a 5 s clip do not embed comparably
    # and the difference would read as two voices; overlapping the previous frame keeps every
    # frame the same length.
    if starts[-1] + step < len(audio):
        starts.append(len(audio) - step)
    cut = [audio[i:i + step] for i in starts]
    return [f for f in cut if _dbfs(f) >= FRAME_MIN_DBFS]


def _enrol(event):
    start, end = float(event["start_sec"]), float(event["end_sec"])
    key, clip, sr = _window_audio(event["user_folder"], event["date"],
                                  event["source_filename"], start, end)

    verdict = vp.window_is_homogeneous([embed_audio(f, sr) for f in _frames(clip, sr)],
                                       MAX_FRAME_SPREAD)
    if verdict is not True:
        # None ("could not check") is refused alongside False. Treating them alike in the
        # permissive direction is how a guard becomes decoration.
        reason = ("window is not homogeneous — it may hold more than one voice"
                  if verdict is False else
                  "window too short to check homogeneity — refusing rather than assuming")
        logger.warning("enrol refused for %s [%s-%s]: %s%s", key, start, end, reason,
                       _limit_note())
        return {"status": "refused", "reason": reason, "s3_key": key}

    # A line on the accepting path too. 1078 uploads once produced zero log lines because
    # only the failure branch spoke, and "it passed" and "it never ran" looked the same.
    logger.info("enrol accepted for %s [%s-%s]%s", key, start, end, _limit_note())

    # Returned, not stored. The in-VPC writer persists it into the column that already
    # requires consent, so the vector never lands in S3 — the biometric-residence defect
    # that relocated twice during review (first a cache, then a request artifact).
    return {"status": "embedded",
            "voiceprint_id": event["voiceprint_id"],
            "embedding": [float(x) for x in embed_audio(clip, sr)],
            "s3_key": key, "window": [start, end],
            "created_by": event.get("created_by"),
            "correction_ref": event.get("correction_ref"),
            "admitted_max_spread": _admitted_limit()}


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
            # (see below) a profile that has not earned confirmation cannot hand one out —
            # and since harvest can build a profile entirely from inference, `tentative` is
            # where such a profile stays until a person vouches for one of its windows.
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
    # The WRITER'S answer, not boto3's envelope. Returning `resp` meant every caller read a
    # dict of StatusCode/ExecutedVersion/Payload and found none of the keys it wanted:
    # `.get("profiles")` was always None, so the matcher's early return fired on every run
    # and logged "no consented profiles for this company" — which reads as "nobody has
    # enrolled yet", not as a broken hop. The whole match path was inert in production while
    # every test passed, because every test stubs this function.
    body = resp["Payload"].read()
    return json.loads(body) if body else {}


MAX_PROPAGATION_TURNS = int(os.environ.get("SPEAKER_PROPAGATION_MAX_TURNS", "300"))


def _propagate(folder, date, turns, reference, display_name, asserted_ref,
               why=None, harvest=None):
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

    # THREE parallel lists, appended together. `vectors` and `refs` used to skip unreadable
    # turns while the harvest path indexed `usable` with the same subscript — so after a
    # single skip, `usable[i]` was a different turn from `vectors[i]`, and harvest ran the
    # homogeneity guard on one window's audio while storing another window's embedding under
    # its s3_key. A profile cannot be un-poisoned, and the provenance recorded would have
    # pointed at audio that never produced the vector.
    kept, vectors, refs = [], [], []
    for t in usable:
        try:
            _, clip, sr = _window_audio(folder, date, t["source_filename"],
                                        float(t["start_sec"]), float(t["end_sec"]))
        except Exception:
            # One unreadable turn must not lose the whole propagation. It simply goes
            # unnamed, which is the honest outcome for audio nobody could read.
            logger.warning("propagation: could not read %s", t.get("source_filename"))
            continue
        kept.append(t)
        vectors.append(embed_audio(clip, sr))
        refs.append(turn_name_overlay.turn_ref(t["source_filename"], t["start_sec"]))
    if len(vectors) < 2:
        return []

    labels = vp.cluster_turns(vectors)
    groups = {}
    for i, lab in enumerate(labels):
        groups.setdefault(lab, []).append(i)

    # The corrected turn is IN this clustering, so its label already says which voice it is.
    # An earlier version instead scored the reference against every centroid — which meant
    # comparing it against a centroid that contained itself, and `leave_one_out_centroid`
    # sat written, tested and uncalled. Reading the label removes the self-comparison
    # entirely rather than correcting for it.
    self_i = next((i for i, r in enumerate(refs) if r == asserted_ref), None)
    if self_i is None:
        # Nothing to propagate from, and guessing a cluster for it would be the two-voice
        # failure by another route. But say WHICH reason: a turn under the duration floor is
        # filtered out of `usable` above and then arrives here looking identical to a window
        # that genuinely is not part of the session. The first is ordinary — roughly a fifth
        # of turns are under 3 s — and the second means the producer and consumer disagree.
        # One log line for both would send tomorrow's debugging at the wrong one.
        short = any(turn_name_overlay.turn_ref(t["source_filename"], t["start_sec"])
                    == asserted_ref for t in turns)
        logger.info("propagation: %s",
                    "the corrected turn is under the %.1fs floor" % vp.DEFAULT_MIN_TURN_S
                    if short else "corrected turn not among the session's turns")
        return []

    own = labels[self_i]
    members = groups[own]
    if len(members) < 2:
        # This voice spoke once. There is nothing to propagate, and that is the answer —
        # not a reason to look for a different cluster to name, and NOT a reason to refuse
        # an enrolment: naming somebody who said one thing is the most ordinary correction
        # there is.
        logger.info("propagation: the corrected voice has only this turn")
        return []

    v_self = vectors[self_i]
    own_score = vp.cosine(
        vp.leave_one_out_centroid([vectors[i] for i in members], members.index(self_i)),
        v_self)
    others = [vp.cosine(sum(vectors[i] for i in idxs) / len(idxs), v_self)
              for lab, idxs in groups.items() if lab != own]
    if others:
        margin = own_score - max(others)
        if margin < vp.DEFAULT_MIN_MARGIN:
            # The corrected window sits between two voices — §2 measured 1 turn in 6 holding
            # two. Naming a cluster from it spreads one person's name over another's whole
            # session.
            logger.warning("propagation refused: the corrected turn is between voices "
                           "(margin %.3f)", margin)
            if why is not None:
                why.append("between-voices")
            return []

    if harvest is not None:
        # The cluster, ordered by how well each member agrees with the rest of it. This work
        # is already done and was previously discarded: one correction stored ONE sample —
        # the window the user pointed at — which is often shorter than 10 s and makes a weak
        # profile. Selection happens here; whether any of it is STORED is the caller's
        # decision, taken after the anchor's own refusal checks.
        by_agreement = sorted(
            (i for i in members if refs[i] != asserted_ref),
            key=lambda i: vp.cosine(
                vp.leave_one_out_centroid([vectors[j] for j in members],
                                          members.index(i)),
                vectors[i]),
            reverse=True)
        for i in by_agreement:
            harvest.append({"turn": kept[i], "vector": vectors[i],
                            "turn_ref": refs[i]})

    out = []
    for i in members:
        if refs[i] == asserted_ref:
            continue
        out.append({"turn_ref": refs[i], "state": "tentative",
                    "cluster_ref": f"C{own}", "asserted": False,
                    "display_name": display_name})
    logger.info("propagation: %d turns in %d voices, named %d",
                len(vectors), len(groups), len(out))
    return out


def _admit_harvest(folder, date, candidates):
    """Which cluster members may become enrolment samples.

    **This is a machine-fed path and it is labelled as one.** The human vouched for ONE turn;
    every other member of the cluster was assigned by `cluster_turns`, and `_propagate` caps
    those at `tentative` on purpose — "the margin that would justify `confirmed` has never
    been measured, so an inferred name is a suggestion". Storing them as samples promotes a
    suggestion to permanent biometric ground truth, so the writer records them under a
    distinct source and a profile built only from them can never be promoted.

    What makes it defensible rather than merely convenient: complete-linkage clustering at
    tau = 0.85 means every member agrees with EVERY other at or above that threshold,
    including the anchor the human vouched for — and 0.85 is the one number in this system
    that was measured and frozen (Gate A, two Phase 0 sessions). It is a real bar. It is
    simply not a human one.

    Each admitted turn is re-fetched and re-framed: `_propagate` computes one whole-turn
    embedding and drops the clip, and homogeneity needs FRAMES. Raw audio is cached per
    invocation so the S3 cost is small; the ONNX cost is not (~98 ms per second of audio,
    measured), which is what the caps are for.
    """
    admitted, seconds = [], 0.0
    for c in candidates:
        if len(admitted) >= ENROL_MAX_SAMPLES or seconds >= ENROL_MAX_SECONDS:
            break
        t = c["turn"]
        start, end = float(t["start_sec"]), float(t["end_sec"])
        duration = end - start
        if duration < ENROL_MIN_TURN_S:
            # Redundant with the homogeneity check and kept deliberately: a window under
            # 10 s yields fewer than two frames, so `window_is_homogeneous` would return
            # None and refuse it anyway. What this saves is the S3 read and the ONNX pass
            # (~98 ms per second of audio, measured) for a turn that cannot possibly be
            # admitted — and it says the rule out loud instead of leaving it as an emergent
            # property of two constants that could drift apart.
            continue
        key, clip, sr = _window_audio(folder, date, t["source_filename"], start, end)
        frames = [embed_audio(f, sr) for f in _frames(clip, sr)]
        spread = vp.frame_spread(frames)
        verdict = vp.window_is_homogeneous(frames, MAX_FRAME_SPREAD)
        if verdict is not True:
            # None ("could not check") refused alongside False, exactly as the anchor's own
            # check does. Admitting the unjudgeable is how a guard becomes decoration.
            logger.info("harvest: %s not admitted (%s, frames=%d spread=%s)%s",
                        c["turn_ref"],
                        "not homogeneous" if verdict is False else "unjudgeable",
                        len(frames), "n/a" if spread is None else "%.3f" % spread,
                        _limit_note())
            continue
        logger.info("harvest: %s admitted (frames=%d spread=%s)%s", c["turn_ref"],
                    len(frames), "n/a" if spread is None else "%.3f" % spread,
                    _limit_note())
        admitted.append({"embedding": [float(x) for x in c["vector"]],
                         "s3_key": key, "window": [start, end],
                         "admitted_max_spread": _admitted_limit()})
        seconds += duration
    logger.info("harvest: %d of %d cluster members admitted (%.1fs)",
                len(admitted), len(candidates), seconds)
    return admitted


def _from_request_artifact(bucket, key):
    """One correction, start to finish.

    The artifact is org-api's output: the window the user marked and the session's turns.

    It carries NO voice vectors. It used to carry every consented profile in the company,
    192 dimensions each, into an S3 object with no lifecycle rule — so a withdrawn person's
    voiceprint stayed in the bucket after the withdrawal returned 200. They were never
    needed: propagation names the cluster the corrected turn belongs to and scores against
    no stored profile. The only thing those vectors produced was a similarity score on the
    one row whose name the user had typed himself.
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

    # The turn the user asserted. Its name is not an inference and is written as
    # `correction`; the writer refuses to give it a profile id unless it is marked asserted.
    turn_ref = turn_name_overlay.turn_ref(c["source_filename"], start)
    results = [{"turn_ref": turn_ref, "state": "confirmed",
                "cluster_ref": None, "asserted": True,
                "display_name": c.get("display_name"),
                # Carried so a withdrawal can reach this name even when the enrolment was
                # REFUSED and no sample exists to chain through. Only the asserted row gets
                # it, and only the writer decides what to do with it — a propagated row with
                # a profile id would count as an independent human confirmation.
                "voiceprint_id": (req.get("enrol") or {}).get("voiceprint_id")}]
    why = []
    candidates = []
    propagated = _propagate(folder, date, req.get("turns") or [], v,
                            c.get("display_name"), turn_ref, why=why,
                            harvest=candidates)
    refusal = why[0] if why else None
    results.extend(propagated)

    # The vector is already computed for the propagation decision, so enrolling the same
    # window costs nothing more — and embedding it twice would pay twice for one answer and
    # let the two results differ. It travels in the invoke payload and never through S3,
    # which is what stopped the biometric-residence defect the reviews chased twice.
    # Enrolment runs the same guard `op=enrol` has always run, and this module's docstring
    # has always claimed: a window that may hold two voices, or that is too short to judge,
    # is not stored. A profile cannot be un-poisoned — only the contributing sample deleted —
    # and until now the correction-carried path skipped the check entirely.
    #
    # It also refuses whenever PROPAGATION refused. `_propagate` returning nothing because
    # the corrected turn sits between two voices is the system saying it does not believe the
    # window is one person; storing that same vector as somebody's profile would be
    # disbelieving it in one direction and acting on it in the other.
    enrol = req.get("enrol")
    if enrol:
        frames = [embed_audio(f, sr) for f in _frames(clip, sr)]
        spread = vp.frame_spread(frames)
        verdict = vp.window_is_homogeneous(frames, MAX_FRAME_SPREAD)
        if verdict is not True:
            # The DISTANCE, not just the verdict. Three windows have now been refused on
            # TEST and every log line said only "not homogeneous" — which cannot be argued
            # with, and cannot tell "this window really holds two people" apart from "0.35
            # was measured on read speech and site audio does not look like that".
            logger.warning("enrolment refused: %s (frames=%d spread=%s limit=%.2f)%s",
                           "window is not homogeneous" if verdict is False
                           else "window too short to check homogeneity",
                           len(frames),
                           "n/a" if spread is None else "%.3f" % spread,
                           MAX_FRAME_SPREAD, _limit_note())
            # Carried to the writer rather than only logged. The DB half is the only one
            # that can attach this to the profile, and without it a refusal ends in
            # CloudWatch while the person who made the correction is told "requested".
            enrol = {"voiceprint_id": enrol["voiceprint_id"],
                     "refused": ("this window does not hold one voice"
                                 if verdict is False
                                 else "this window has too little speech to judge")}
        elif refusal == "between-voices":
            # Propagation judged this window to hold more than one voice. Storing it as
            # somebody's profile would be disbelieving that in one direction and acting on
            # it in the other.
            #
            # ONLY that verdict. A first version treated every empty propagation as a
            # refusal, which swept in three benign ones — too few usable turns, the corrected
            # window not among them, and "this person spoke once". The last is the most
            # ordinary enrolment there is (a visitor says one thing and you name them), and
            # it was permanently refused with a log line claiming the window was untrustworthy.
            # The note belongs on THIS line most of all: reaching it means the homogeneity
            # guard passed — possibly only because it was loosened — and propagation then
            # disagreed. Without it the two disagreeing judgements look equally weighted.
            logger.warning("enrolment refused: the corrected window holds more than one "
                           "voice%s", _limit_note())
            enrol = {"voiceprint_id": enrol["voiceprint_id"],
                     "refused": "the corrected passage holds more than one voice"}
        else:
            logger.info("enrolment accepted (frames=%d spread=%s limit=%.2f)%s", len(frames),
                        "n/a" if spread is None else "%.3f" % spread, MAX_FRAME_SPREAD,
                        _limit_note())
    # AFTER the anchor's own checks, never before. `_propagate` runs first, so without this
    # ordering six samples would be stored for a profile whose own corrected window had just
    # been judged to hold two voices — the exact condition one sample is refused for.
    # A refused anchor is still an `enrol` dict now — it carries the reason — so harvest
    # must ask whether it was ACCEPTED, not whether it exists.
    harvested = (_admit_harvest(folder, date, candidates)
                 if enrol and not enrol.get("refused") else [])

    payload = {
        "op": "propagation",
        "company_id": req.get("company_id"),
        "session_base": req.get("session_base"),
        "correction_ref": req.get("request_id"),
        "cluster_threshold": vp.DEFAULT_CLUSTER_TAU,
        "results": results,
        # Carried across untouched. org-api writes it into the artifact and the writer reads
        # it out of the event; this hop is the only thing between them and it did not speak
        # the key, so `_inherit_labels` got None on every call and returned 0. The feature
        # and the rejection guard it feeds were unreachable code, and nothing failed — each
        # end's tests exercised its own half.
        "label_map": req.get("label_map"),
        "enrol": (enrol if (enrol or {}).get("refused") else
                  ({"voiceprint_id": enrol["voiceprint_id"],
                    "embedding": [float(x) for x in v],
                    "s3_key": s3_key, "window": [start, end],
                    "created_by": req.get("requested_by"),
                    "admitted_max_spread": _admitted_limit()} if enrol else None)),
        # A LIST, and a separate key. Not the same act as the anchor: the anchor is what a
        # person vouched for, these are what the clustering suggested, and the two
        # populations must stay separable forever — for audit, for measurement, and so a bad
        # batch can be deleted without touching what somebody asserted.
        # No second `if enrol` here. `harvested` is already empty when the anchor was
        # refused, so a guard in this expression could never change an outcome — and a
        # guard no test can falsify is one nobody will notice going wrong.
        "harvest": [dict(h, voiceprint_id=enrol["voiceprint_id"],
                         created_by=req.get("requested_by")) for h in harvested],
    }
    invoke_writer(payload)
    logger.info("correction applied: session=%s ref=%s audio=%s",
                req.get("session_base"), turn_ref, s3_key)
    return {"status": "applied", "turn_ref": turn_ref}


def _from_match_artifact(bucket, key):
    """Phase 5: name a whole session against the profiles the company already holds.

    Same prefix and same trigger as a correction, told apart by `op` inside the object. A
    second prefix would be a second hand-wired S3 notification (BUG-33), and the two
    artifacts differ only in what they ask for.

    The artifact carries **no voice vectors**. It cannot: it sits in a bucket with a 7-day
    expiry and no other protection, while a voiceprint is biometric data whose storage the
    subject consented to in one specific column. The vectors are fetched here, synchronously,
    from the in-VPC writer, and exist only in this function's memory for the length of the
    run. That is the fifth and last home this defect gets.

    org-api cannot do this itself — it is in-VPC with no NAT, so any outward invoke
    black-holes (BUG-36). Non-VPC calling in-VPC is the permitted direction.
    """
    req = json.loads(_get(key))
    company_id = req.get("company_id")
    folder, date = req.get("user_folder"), req.get("date")
    session = req.get("session_base")
    if not company_id or not folder or not date or not session:
        raise ValueError(
            f"match artifact {key} is missing company_id/user_folder/date/session_base; the "
            f"producer has all four and guessing any of them reads a key that cannot exist")

    profiles = invoke_writer({"op": "profiles", "company_id": company_id,
                              "site_id": req.get("site_id")}).get("profiles") or []
    if not profiles:
        # Not an error, and worth a line rather than a silent zero: "nobody was recognised"
        # and "there was nobody to recognise" look identical downstream, and on TEST the
        # only profile was withdrawn during a withdrawal test, which is exactly how this
        # would have been misread as the matcher not working.
        logger.warning("match: no consented profiles for company %s; nothing to match "
                       "against (session %s)", company_id, session)
        # But label inheritance does not need a profile. It spreads names this session
        # ALREADY holds — from a correction somebody made — to the turns too short to embed,
        # and it rides inside the writer's `match_names` op. Returning here skipped it, so
        # "match a session I have no voiceprints for" silently did nothing at all, in exactly
        # the state the system is in today: no company has a profile, so this branch is the
        # ONLY one that runs. Two reasons, and it took a review to notice the first: the
        # product sends no consent, so an ordinary correction never requests enrolment at
        # all; and when an API call does request it, the homogeneity guard has so far refused
        # every window of real audio.
        if req.get("label_map") and (req.get("mode") or "").lower() == "on":
            written = invoke_writer({"op": "match_names", "company_id": company_id,
                                     "session_base": session,
                                     "label_map": req.get("label_map"),
                                     "results": []}).get("written", 0)
            logger.info("match: no profiles, but inheritance named %d turns in %s",
                        written, session)
            return {"session": session, "matched": written, "profiles": 0,
                    "inheritedOnly": True, "mode": req.get("mode")}
        return {"session": session, "matched": 0, "profiles": 0, "mode": req.get("mode")}

    out = _match({"session": session, "user_folder": folder, "date": date,
                  "profiles": profiles, "turns": req.get("turns") or []})

    by_key = {p["person_key"]: p for p in profiles}
    named = []
    skipped_no_runner_up = 0
    for r in out["results"]:
        if r.get("status") not in ("confirmed", "tentative") or not r.get("name"):
            continue
        if r.get("margin") is None:
            # No runner-up: `decide_name` returns a name with a null margin when the pool
            # holds ONE profile, meaning "the closest of the only person I know". Written to
            # a transcript that reads as an identification, and it would land on every turn
            # in the meeting including the other speakers' — the one enrolled name spread
            # over everybody, which is worse than no name at all.
            #
            # Matching needs at least two profiles before it can say anything. That is not a
            # tuning choice: with overlapping score distributions and no usable absolute
            # threshold, one candidate cannot be told from a stranger.
            skipped_no_runner_up += 1
            continue
        turn = r["turn"]
        named.append({
            "turn_ref": turn_name_overlay.turn_ref(turn["source_filename"],
                                                   float(turn["start_sec"])),
            "status": r["status"],
            "person_key": r["name"],
            # `decide_name` returns the KEY it won on, which is the profile id. The name a
            # reader sees has to come from the profile, or the transcript shows a uuid.
            "display_name": (by_key.get(r["name"]) or {}).get("display_name"),
            "margin": r.get("margin"),
        })

    if skipped_no_runner_up:
        # Loud, because the symptom is indistinguishable from "the matcher did nothing".
        logger.warning(
            "match: %d turn(s) had a nearest profile but no runner-up to beat — the company "
            "has %d matchable profile(s) and discrimination needs at least 2",
            skipped_no_runner_up, len(profiles))

    mode = (req.get("mode") or "").lower()
    if mode != "on":
        # `shadow` computes everything and writes nothing. The scores are the point of the
        # mode — they are what a threshold gets calibrated on — so they go to the log rather
        # than being discarded, but no name reaches a transcript.
        logger.info("match(shadow): session=%s would name %d of %d turns: %s",
                    session, len(named), len(out["results"]),
                    [(n["display_name"], n["status"], n["margin"]) for n in named])
        return {"session": session, "matched": 0, "wouldMatch": len(named),
                "profiles": len(profiles), "mode": mode or "off"}

    # No invoke when there is nothing to do at all: an empty write is a database round trip
    # that reports success, and "wrote 0 rows" then looks like "the writer is fine".
    #
    # `label_map` counts as something to do. Label inheritance lives INSIDE the writer's
    # `match_names` op, so an earlier version of this guard — `if named:` — meant that a run
    # which matched nothing also inherited nothing, and matching nothing is not unusual: with
    # a single profile every turn is skipped for having no runner-up, which this function
    # logs as an ordinary outcome. The session could be holding correction names from a
    # previous gesture that the label map is entitled to spread to the short turns, and they
    # were silently left unnamed because a DIFFERENT feature had found no candidates.
    written = 0
    if named or req.get("label_map"):
        written = invoke_writer({"op": "match_names", "company_id": company_id,
                                 "session_base": session,
                                 # Both artifacts carry it and both writer branches read it;
                                 # this hop dropped it on the floor in both directions.
                                 "label_map": req.get("label_map"),
                                 "results": named}).get("written", 0)
    logger.info("match: session=%s named %d of %d turns against %d profiles",
                session, written, len(out["results"]), len(profiles))
    return {"session": session, "matched": written, "profiles": len(profiles), "mode": mode}


def _spread(event):
    """The frame spread of one window, and nothing else. Read-only.

    Exists because the two ops that CAN produce this number both have side effects: `enrol`
    stores the sample when the window passes, and `match` never computes frames at all. So
    measuring the threshold with either meant either writing junk rows or not measuring.

    Whether 0.35 suits site audio is an open question — three enrolments have been refused,
    one of them a 14.7 s window inside a single chunk, which is the shape enrolment is meant
    to accept. This is how that question gets data instead of opinions.
    """
    start, end = float(event["start_sec"]), float(event["end_sec"])
    key, clip, sr = _window_audio(event["user_folder"], event["date"],
                                  event["source_filename"], start, end)
    raw = _frames(clip, sr)
    frames = [embed_audio(f, sr) for f in raw]
    spread = vp.frame_spread(frames)
    verdict = vp.window_is_homogeneous(frames, MAX_FRAME_SPREAD)
    # Per-frame loudness, because the leading explanation for a wide spread is that the
    # frames are not all speech. `_frames` cuts every 5 s blind — there is no VAD anywhere
    # in this path — so a frame that is mostly silence gets embedded like any other, and a
    # silence-vs-speech pair is enormously far apart for reasons that have nothing to do
    # with how many people are in the room. The devices are also known to record quietly
    # (median -36 dBFS against a normal -20 to -12), which makes the effect likelier here
    # than the measurement that set 0.35 would have seen.
    levels = []
    for f in raw:
        arr = np.asarray(f, dtype=np.float32)
        rms = float(np.sqrt(np.mean(arr * arr))) if arr.size else 0.0
        levels.append(round(20.0 * float(np.log10(rms)), 1) if rms > 1e-9 else -120.0)
    return {"s3_key": key, "seconds": round(end - start, 2), "frames": len(frames),
            # Every candidate statistic, computed here where the vectors already are. They
            # are NOT returned: a frame embedding is biometric data and this is a diagnostic
            # path, which is exactly the journey this defect has made four times. Scalars
            # leave; vectors do not.
            "candidates": vp.frame_statistics(frames),
            "spread": None if spread is None else round(float(spread), 4),
            # The limit IN FORCE, not the compiled-in default. A diagnostic that
            # reports a limit the function is not using is worse than reporting none.
            "limit": MAX_FRAME_SPREAD,
            "default_limit": vp.DEFAULT_MAX_FRAME_SPREAD,
            "frame_dbfs": levels,
            "verdict": "homogeneous" if verdict is True
                       else ("mixed" if verdict is False else "unjudgeable")}


def lambda_handler(event, context):
    _AUDIO_CACHE.clear()
    records = (event or {}).get("Records")
    if records:
        # An S3 event carries `Records`, not `op`. Without this branch the function dies in
        # 3 ms with `unknown op None` -- which is exactly what a real correction on TEST did
        # on 2026-08-14, after the trigger was wired and this entry point was not.
        out = None
        for r in records:
            b = r["s3"]["bucket"]["name"]
            k = r["s3"]["object"]["key"]
            doc = json.loads(_get(k))
            # One prefix, two requests. The op is inside the object rather than in the key
            # so a new kind of request never needs a new hand-wired S3 notification.
            out = (_from_match_artifact(b, k) if doc.get("op") == "match"
                   else _from_request_artifact(b, k))
        return out or {"status": "empty"}
    op = (event or {}).get("op")
    if op == "enrol":
        return _enrol(event)
    if op == "match":
        return _match(event)
    if op == "spread":
        return _spread(event)
    raise ValueError(f"unknown op {op!r} — expected 'enrol', 'match' or 'spread'")
