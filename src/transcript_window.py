"""A stretch of a day's recorded speech, assembled from the transcript objects.

Selection is by OVERLAP, not by start: a recording that straddles the start of the
window carries the beginning of the conversation, and dropping it loses exactly the
part a reader needs. Nothing here reads Aurora -- the worker that uses it is
non-VPC and must not import psycopg.
"""
import datetime as dt
import json
import logging
import re

import batch_stitch
import transcript_utils as tu

logger = logging.getLogger()

_DURATION_RE = re.compile(r"_to(\d+(?:\.\d+)?)_")
_DEFAULT_DURATION_SEC = 60.0


def _duration(name):
    meta = tu.extract_vad_metadata_from_filename(name)
    dur = meta.get("segment_duration") or 0.0
    if dur > 0:
        return float(dur)
    m = _DURATION_RE.search(name)
    return float(m.group(1)) if m else _DEFAULT_DURATION_SEC


def select_keys(s3, bucket, folder, date, win_from, win_to):
    """Keys whose audio overlaps [win_from, win_to), oldest first."""
    prefix = "transcripts/%s/%s/" % (folder, date)
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]

    picked = []
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        start = tu.compute_segment_base_time(name)
        if start is None:
            logger.info("window: no time in %s -- skipped", name)
            continue
        end = start + dt.timedelta(seconds=_duration(name))
        if end > win_from and start < win_to:
            picked.append((start, key))
    picked.sort()
    return picked


def assemble(s3, bucket, picked):
    """One entry per speaker turn: {"at": datetime, "line": "[HH:MM:SS - HH:MM:SS] ..."}."""
    turns = []
    for start, key in picked:
        name = key.rsplit("/", 1)[-1]
        body = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
        # Every normalize_transcript() caller must rebase through the embedded batch map
        # (test_batch_map_travels.py) -- a batched transcript's word times count from the
        # concatenated file's first sample, not from anything filename arithmetic can recover.
        norm = batch_stitch.rebase_turns_from_embedded_map(
            tu.normalize_transcript(body, name), body)
        if not norm:
            logger.info("window: %s did not normalise -- skipped", name)
            continue
        lines = tu.format_turns_for_prompt(norm, use_absolute_time=True)
        for i, line in enumerate(lines):
            if not line or not line.strip():
                continue
            turn = (norm.get("speaker_turns") or [])[i] if i < len(norm.get("speaker_turns") or []) else None
            at = (turn or {}).get("abs_start") or start
            turns.append({"at": at, "line": line})
    turns.sort(key=lambda t: t["at"])
    return turns
