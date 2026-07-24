"""Lambda: keyframe extractor (video-keyframe-to-photo plan, Task 3).

In-VPC (PsycopgLayer + PG env, S3 via the gateway endpoint -- zero non-S3 AWS
calls, BUG-36-clean). A runtime-agnostic ffmpeg layer (sitesync-vad-layer,
CompatibleRuntimes [python3.11, python3.12]) is attached ONLY for the static
/opt/bin/ffmpeg binary; the layer's Python packages are NEVER imported here.
The function stays on the python3.11 runtime: PsycopgLayer is cp311-only and
the ffmpeg layer is 3.11-compatible, so both layers load on one 3.11 function.
(The cp312-only fieldsight-vad-layer is NOT used here -- a 3.11 function + that
layer is rejected by CreateFunction, stack rollback.) The keyframe code itself
is stdlib subprocess/json + photo_binding.

Trigger: S3 ObjectCreated on keyframe_requests/*.json (wire-s3-events.sh,
BUG-33), written post-commit by lambda_item_writer. For each gated topic in
the request: find the covering video via the VAD metadata sidecars, grab ONE
frame per computed keyframe instant (-ss before -i, BUG-04), upload each into
the pictures/ prefix (filename parses to the frame's mid time, so item-writer
re-runs re-bind it), and insert the topic_photos row if absent. A topic with
a long window yields multiple frames (keyframe_selection.keyframe_seconds);
the loop below is per-frame.

Graceful failure (mirrors lambda_vad's preview-failure handling): an ffmpeg
non-zero exit warns and skips that frame; other frames/topics in the same
request still process; the handler never raises.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from urllib.parse import unquote_plus

import boto3

from psycopg.rows import dict_row

from db.connection import get_connection
from keyframe_selection import (ffmpeg_frame_cmd, keyframe_filename,
                                keyframe_seconds, select_covering_recording)
from photo_binding import parse_time_range
from repositories import keyframes
from repositories.topics import add_topic_photo_if_absent
from transcript_utils import (extract_base_time_from_filename,
                              extract_device_from_filename)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
FFMPEG_PATH = "/opt/bin/ffmpeg" if os.path.exists("/opt/bin/ffmpeg") else "ffmpeg"

_s3_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def load_video_recordings(s3c, bucket, user_folder, date):
    """Coverage intervals from the VAD metadata sidecars (the reliable
    per-file duration source -- recordings-table rows are absent for
    RealPTT-pulled files). Keeps source_type == 'video' only. Never parses
    times inline (BUG-01/09/11)."""
    recs = []
    prefix = f"audio_segments/{user_folder}/{date}/"
    for page in s3c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith("_vad_metadata.json"):
                continue
            # M-3: guard EACH sidecar. A single truncated/corrupt
            # _vad_metadata.json (bad JSON, or missing source_key) must not
            # raise out to the handler -- that would fail the whole request,
            # trigger S3's retries, and permanently drop it (no DLQ). Warn and
            # skip just that sidecar; the module's "never raises" contract holds.
            try:
                meta = json.loads(s3c.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                if meta.get("source_type") != "video":
                    continue
                basename = meta["source_key"].rsplit("/", 1)[-1]
                base = extract_base_time_from_filename(basename)
                if base is None:
                    continue
                recs.append({
                    "source_key": meta["source_key"],
                    "base_s": base.hour * 3600 + base.minute * 60 + base.second,
                    "duration_s": float(meta.get("total_duration_sec", 0)),
                })
            except Exception as e:
                logger.warning("skipping unreadable VAD sidecar %s: %s", obj["Key"], e)
                continue
    return recs


def process_request(req):
    user_folder, date = req["user_folder"], req["date"]
    session_base, topics = req["session_base"], req["topics"]
    device = extract_device_from_filename(session_base)

    recordings = load_video_recordings(s3(), S3_BUCKET, user_folder, date)
    if not recordings:
        return {"skipped": True, "reason": "no video coverage"}

    # Plan every frame first (the expected set drives the stale-file cleanup).
    pics_prefix = f"users/{user_folder}/pictures/{date}/"
    plans, expected = [], set()
    for t in topics:
        parsed = parse_time_range(t.get("time_range"))
        if parsed is None:
            continue
        start_min, end_min = parsed
        duration_min = end_min - start_min
        # Multiple frames per topic (2026-07-24 rule): keyframe_seconds is a
        # list; the emitter already gated, this double-check is free. frame_index
        # + n_frames + duration_min ride the plan tuple into the 'generated'
        # telemetry (Q7) at zero extra cost -- all three are already in hand.
        mids = keyframe_seconds(t.get("time_range"))
        n_frames = len(mids)
        for frame_index, mid_s in enumerate(mids):
            pick = select_covering_recording(recordings, start_min * 60, end_min * 60, mid_s)
            if pick is None:
                continue
            key = pics_prefix + keyframe_filename(device, date, mid_s, session_base)
            expected.add(key)
            plans.append((t["topic_id"], mid_s, pick, key, frame_index, n_frames, duration_min))

    sess_marker = f"_kf_s{session_base.rsplit('_', 1)[-1].replace('-', '')}.jpg"

    written = 0
    # M-4: download each original ONCE per invocation (keyed by source_key) and
    # seek to every requested frame off the local file. A topic can ask for up
    # to KEYFRAME_MAX_FRAMES grabs off one source_key; the old per-frame
    # re-download blew the (now 600s) timeout budget and, on a mid-run timeout,
    # left S3 uploads with zero committed DB bindings.
    downloaded = {}
    tmp_dir = tempfile.mkdtemp(prefix="kf_")
    try:
        with get_connection() as conn:
            # Q7 tombstone skip -- BEFORE head_object, ffmpeg, insert, and BEFORE
            # the cleanup below. A reviewer-deleted keyframe must never
            # regenerate: drop tombstoned keys from the plan (never plan the
            # frame at all) AND from the expected set. Removing them from
            # `expected` is what turns the existing stale S3 sweep and the M-2
            # row cleanup into the tombstone's garbage collector -- any lingering
            # object/row for a tombstoned key is then actively deleted below.
            tombstoned = keyframes.tombstoned_subset(conn, list(expected))
            if tombstoned:
                logger.info("skipping %d tombstoned keyframe(s)", len(tombstoned))
                plans = [p for p in plans if p[3] not in tombstoned]
                expected -= tombstoned
            # NEW-3: how many still-to-process frames need each original. The
            # local file is evicted the instant its count hits 0, so many
            # source_keys (~200 MB each) in one request cannot pile up past the
            # 2048 MB ephemeral disk. Computed AFTER the tombstone filter so a
            # shared original's count reflects only frames actually processed.
            remaining = Counter(pick[0] for _, _, pick, _, _, _, _ in plans)
            # Stale cleanup: this session's previous keyframes no longer
            # expected (window shifted on a re-extraction). Scoped to
            # *_kf_s{session}.jpg so other sessions' keyframes and real user
            # photos are never touched.
            for page in s3().get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=pics_prefix):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(sess_marker) and obj["Key"] not in expected:
                        logger.info("deleting stale keyframe %s", obj["Key"])
                        s3().delete_object(Bucket=S3_BUCKET, Key=obj["Key"])

            # M-2 (crash-consistency): remove dangling topic_photos rows via a
            # DB-side predicate over (pics_prefix, session marker, expected set)
            # -- NOT via the keys the S3 listing returned this run. If a prior
            # run deleted the S3 objects but crashed/timed out before COMMIT,
            # those keys are gone from S3 so a retry's listing can no longer
            # rediscover them; this predicate still matches the orphaned rows and
            # self-heals on retry. Runs unconditionally (idempotent), in the SAME
            # connection. Underscores in the prefix/marker are escaped so LIKE
            # treats them literally (Ben_UCPK, _kf_s...).
            conn.execute(
                "DELETE FROM topic_photos "
                "WHERE s3_key LIKE %s ESCAPE '\\' "
                "AND s3_key LIKE %s ESCAPE '\\' "
                "AND s3_key <> ALL(%s::text[])",
                (_like_escape(pics_prefix) + "%",
                 "%" + _like_escape(sess_marker),
                 list(expected)),
            )

            for topic_id, mid_s, (source_key, seek), key, frame_index, n_frames, duration_min in plans:
                try:
                    # Stale request (topic cascaded away between emit and
                    # processing): skip instead of raising an FK error on insert.
                    # Widened from `SELECT 1` to also fetch the structural signal
                    # the 'generated' telemetry needs, in ONE round trip. dict_row
                    # -> attribute-free dict access; None still means superseded.
                    trow = conn.cursor(row_factory=dict_row).execute(
                        "SELECT t.time_range, t.category, t.work_class, t.site_id, "
                        "s.company_id FROM topics t LEFT JOIN sites s ON s.id = t.site_id "
                        "WHERE t.id=%s", (topic_id,)).fetchone()
                    if trow is None:
                        logger.info("topic %s superseded -- skipping keyframe", topic_id)
                        continue
                    if not _object_exists(key):
                        # NEW-2/NEW-3: isolate the download. A missing/deleted
                        # original (S3 404) or a full ephemeral disk (OSError)
                        # must skip THIS frame|topic with a warning and let the
                        # other topics in the request proceed -- the handler must
                        # NEVER raise on a bad/absent video (module contract). The
                        # dead source_key is cached so sibling frames of the same
                        # video short-circuit here instead of re-downloading.
                        try:
                            local = _download_original(downloaded, tmp_dir, source_key)
                        except Exception as e:
                            logger.warning("keyframe download failed for %s (topic %s): %s",
                                           source_key, topic_id, e)
                            continue
                        if not _extract_and_upload(local, seek, key):
                            continue               # ffmpeg failed: log-and-skip, never raise
                        # A NEW JPEG was produced THIS run -> append-only
                        # 'generated' event. NOT on the _object_exists fast path
                        # (S3-event retries / unchanged re-runs must add nothing).
                        _record_generated_event(conn, trow, duration_min, frame_index, n_frames)
                    caption = f"Auto keyframe @ {mid_s // 3600:02d}:{(mid_s % 3600) // 60:02d}"
                    if add_topic_photo_if_absent(conn, topic_id, key, caption):
                        written += 1
                finally:
                    remaining[source_key] -= 1
                    if remaining[source_key] <= 0:
                        _evict_original(downloaded, source_key)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return {"skipped": False, "keyframes": written}


def _record_generated_event(conn, trow, duration_min, frame_index, n_frames):
    """Append one 'generated' telemetry row for a frame just produced (Q7).
    Never-raise contract (module docstring): telemetry must NEVER cost a frame or
    trip S3's retry-then-drop behaviour, so a failed write warns and is swallowed
    -- the upload + DB bind already happened and must stand."""
    try:
        keyframes.record_event(
            conn, "generated",
            company_id=trow.get("company_id"), site_id=trow.get("site_id"),
            topic_category=trow.get("category"), work_class=trow.get("work_class"),
            duration_min=duration_min, n_frames_generated=n_frames,
            frame_index=frame_index)
    except Exception as e:
        logger.warning("keyframe 'generated' telemetry write failed: %s", e)


def _object_exists(key):
    try:
        s3().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


class KeyframeDownloadError(Exception):
    """A previously-attempted original download for this source_key already
    failed; sibling frames re-raise this instead of retrying the doomed pull."""


def _like_escape(s):
    """Escape LIKE metacharacters so a literal S3-path fragment (which contains
    underscores -- Ben_UCPK, _kf_s...) matches only itself under ESCAPE '\\'."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _download_original(cache, tmp_dir, source_key):
    """Download the original video ONCE per invocation (M-4). Cached by
    source_key so a topic's N frames all seek into the same local file instead
    of re-pulling a ~200 MB original per frame.

    NEW-2/NEW-3: on a failed pull (missing/deleted original -> S3 404, or a full
    ephemeral disk -> OSError) a None sentinel is cached for this source_key and
    the error re-raised, so the caller skips this frame AND every sibling frame
    of the same dead video short-circuits to the same skip without re-attempting
    the doomed (and expensive) download. _evict_original / the tmp_dir finally
    handle the on-disk cleanup."""
    if source_key in cache:
        local = cache[source_key]
        if local is None:
            raise KeyframeDownloadError(source_key)
        return local
    dest = os.path.join(tmp_dir, f"in{len(cache)}" + os.path.splitext(source_key)[1])
    try:
        s3().download_file(S3_BUCKET, source_key, dest)
    except Exception:
        cache[source_key] = None            # remember the dead source_key
        raise
    cache[source_key] = dest
    return dest


def _evict_original(cache, source_key):
    """Free a downloaded original's local file once its last planned frame is
    done (NEW-3). Pops the cache entry (a path, or the None failure sentinel).
    Best-effort: process_request's tmp_dir finally still sweeps anything left."""
    local = cache.pop(source_key, None)
    if local:
        try:
            os.remove(local)
        except OSError:
            pass


def _extract_and_upload(local_input, seek, dest_key):
    """Grab ONE frame from an already-downloaded original and upload it. Never
    raises: an ffmpeg non-zero exit / missing output warns and returns False so
    the caller skips just this frame. The output name is unique per call (the
    same original is seeked to many frames within one invocation)."""
    out = os.path.join(os.path.dirname(local_input), uuid.uuid4().hex + ".jpg")
    try:
        r = subprocess.run(ffmpeg_frame_cmd(FFMPEG_PATH, local_input, seek, out),
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(out):
            logger.warning("ffmpeg frame grab failed for %s: %s", local_input, (r.stderr or "")[:300])
            return False
        s3().upload_file(out, S3_BUCKET, dest_key, ExtraArgs={"ContentType": "image/jpeg"})
        return True
    except Exception as e:
        logger.warning("keyframe extract error for %s: %s", local_input, e)
        return False


def lambda_handler(event, context):
    results = []
    for record in (event or {}).get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        req = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
        results.append(process_request(req))
    return {"results": results}
