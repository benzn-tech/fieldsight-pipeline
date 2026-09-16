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
import time

import batch_stitch
import chunking
import transcript_utils as tu

logger = logging.getLogger()

_DURATION_RE = re.compile(r"_to(\d+(?:\.\d+)?)_")
_DEFAULT_DURATION_SEC = 60.0

# Past this many transcript objects, the read phase (S3 list + get + normalize)
# can itself outlast whatever time the caller has left, independent of how the
# model call is bounded -- a request this large is refused before the first
# read, not discovered mid-read. Named so the error can point at it.
MAX_TRANSCRIPT_OBJECTS = 500


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


class WindowTooLarge(Exception):
    """The read phase (S3 list + get + normalize) would outlast the time actually
    left on the invocation. Raised instead of letting Lambda SIGKILL the function
    mid-read, which writes no result at all and leaves the poller spinning
    forever."""


def assemble(s3, bucket, picked, deadline=None):
    """One entry per speaker turn: {"at": datetime, "until": datetime, "line":
    "[HH:MM:SS - HH:MM:SS] ..."}. `until` is the turn's absolute end, falling
    back to its start when the transcript carries no end time -- callers that
    only ever knew "at" keep working unchanged.

    `deadline` (epoch seconds from time.time(), optional) bounds this phase.
    A `picked` list longer than MAX_TRANSCRIPT_OBJECTS is refused before the
    first S3 read -- the window is too large regardless of how fast reading it
    turns out to be -- and the deadline is also checked between objects so a
    slow read is caught rather than silently running past it."""
    if deadline is not None and len(picked) > MAX_TRANSCRIPT_OBJECTS:
        raise WindowTooLarge(
            "window too large: %d transcript objects exceeds the %d-object cap"
            % (len(picked), MAX_TRANSCRIPT_OBJECTS))
    turns = []
    for start, key in picked:
        if deadline is not None and time.time() > deadline:
            raise WindowTooLarge(
                "window too large: the read phase ran out of its time budget "
                "before finishing (%d of %d objects read)" % (len(turns), len(picked)))
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
            until = (turn or {}).get("abs_end") or at
            turns.append({"at": at, "until": until, "line": line})
    turns.sort(key=lambda t: t["at"])
    return turns


class UnplaceableExclusion(Exception):
    """An excluded topic whose time_range cannot be parsed. The stretch it covers
    cannot be cut out, so the request fails: generating from the unclipped
    transcript would put hidden speech in a customer's report."""


def excluded_spans(date, excluded_topics, win_from=None, win_to=None):
    """Wall-clock spans to remove. `excluded_topics` are the day's topics that are
    redacted or non-work -- the ones the report scope already refuses to show.

    `win_from`/`win_to` (optional): a topic whose time_range PARSES and lies
    wholly outside [win_from, win_to) is dropped here rather than turned into a
    span -- it cannot affect this request's transcript, so it should not be able
    to fail it either. A topic whose time_range does NOT parse still raises
    regardless of the window: it cannot be placed, so it cannot be proven to lie
    outside the window either. Omit both to get the unfiltered fail-closed
    behaviour every other caller relies on."""
    day = dt.datetime.strptime(date, "%Y-%m-%d")
    spans = []
    for t in excluded_topics or []:
        parsed = chunking.parse_time_range(t.get("time_range"))
        if not parsed:
            raise UnplaceableExclusion(
                "topic %s is excluded but its time_range %r cannot be placed"
                % (t.get("id"), t.get("time_range")))
        start_s, end_s = parsed
        span_start = day + dt.timedelta(seconds=start_s)
        span_end = day + dt.timedelta(seconds=end_s)
        if win_from is not None and win_to is not None and (
                span_end <= win_from or span_start >= win_to):
            continue
        spans.append((span_start, span_end))
    return spans


def drop_spans(turns, spans):
    """Every turn that OVERLAPS an excluded span goes, not just one whose start
    falls inside it -- a turn that begins before the span and runs into it would
    otherwise carry a sliver of hidden speech into the prompt. `until` falls back
    to `at` for a turn with no known end, which keeps the boundary rule below
    identical to the old start-only check for that case. Boundaries are included
    on both ends, as before."""
    if not spans:
        return list(turns)
    kept = []
    for t in turns:
        at = t["at"]
        until = t.get("until", at)
        if any(at <= e and until >= s for s, e in spans):
            continue
        kept.append(t)
    return kept
