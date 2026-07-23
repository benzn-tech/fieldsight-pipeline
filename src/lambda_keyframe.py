"""Lambda: keyframe extractor (video-keyframe-to-photo plan, Task 3).

In-VPC (PsycopgLayer + PG env, S3 via the gateway endpoint -- zero non-S3 AWS
calls, BUG-36-clean). The VAD layer is attached ONLY for the static
/opt/bin/ffmpeg binary; onnxruntime/numpy in that layer are cp312 and are
NEVER imported here (runtime is the global python3.11).

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
from urllib.parse import unquote_plus

import boto3

from db.connection import get_connection
from keyframe_selection import (ffmpeg_frame_cmd, keyframe_filename,
                                keyframe_seconds, select_covering_recording)
from photo_binding import parse_time_range
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
        # Multiple frames per topic (2026-07-24 rule): keyframe_seconds is a
        # list; the emitter already gated, this double-check is free.
        for mid_s in keyframe_seconds(t.get("time_range")):
            pick = select_covering_recording(recordings, start_min * 60, end_min * 60, mid_s)
            if pick is None:
                continue
            key = pics_prefix + keyframe_filename(device, date, mid_s, session_base)
            expected.add(key)
            plans.append((t["topic_id"], mid_s, pick, key))

    # Stale cleanup: this session's previous keyframes no longer expected
    # (window shifted on a re-extraction). Scoped to *_kf_s{session}.jpg so
    # other sessions' keyframes and real user photos are never touched.
    sess_marker = f"_kf_s{session_base.rsplit('_', 1)[-1].replace('-', '')}.jpg"
    for page in s3().get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=pics_prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(sess_marker) and obj["Key"] not in expected:
                logger.info("deleting stale keyframe %s", obj["Key"])
                s3().delete_object(Bucket=S3_BUCKET, Key=obj["Key"])

    written = 0
    with get_connection() as conn:
        for topic_id, mid_s, (source_key, seek), key in plans:
            # Stale request (topic cascaded away between emit and processing):
            # skip instead of raising an FK error on insert.
            if conn.execute("SELECT 1 FROM topics WHERE id=%s", (topic_id,)).fetchone() is None:
                logger.info("topic %s superseded -- skipping keyframe", topic_id)
                continue
            if not _object_exists(key):
                if not _extract_and_upload(source_key, seek, key):
                    continue                       # ffmpeg failed: log-and-skip, never raise
            caption = f"Auto keyframe @ {mid_s // 3600:02d}:{(mid_s % 3600) // 60:02d}"
            if add_topic_photo_if_absent(conn, topic_id, key, caption):
                written += 1
    return {"skipped": False, "keyframes": written}


def _object_exists(key):
    try:
        s3().head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _extract_and_upload(source_key, seek, dest_key):
    tmp_dir = tempfile.mkdtemp(prefix="kf_")
    try:
        local = os.path.join(tmp_dir, "in" + os.path.splitext(source_key)[1])
        out = os.path.join(tmp_dir, "frame.jpg")
        s3().download_file(S3_BUCKET, source_key, local)
        r = subprocess.run(ffmpeg_frame_cmd(FFMPEG_PATH, local, seek, out),
                           capture_output=True, text=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(out):
            logger.warning("ffmpeg frame grab failed for %s: %s", source_key, (r.stderr or "")[:300])
            return False
        s3().upload_file(out, S3_BUCKET, dest_key, ExtraArgs={"ContentType": "image/jpeg"})
        return True
    except Exception as e:
        logger.warning("keyframe extract error for %s: %s", source_key, e)
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def lambda_handler(event, context):
    results = []
    for record in (event or {}).get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        req = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
        results.append(process_request(req))
    return {"results": results}
