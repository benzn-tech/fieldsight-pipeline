"""
Pure photo->topic binding + the S3 pictures lister, shared by
lambda_item_writer (extraction path) and lambda_ingest (report path, P4).

Lives in its own module because lambda_item_writer already imports
lambda_ingest -- lambda_ingest importing lambda_item_writer back would be
circular (precedent: chunking.py / match_request.py).

Rule (2026-07-23 prod-media-binding plan, user-approved): each photo binds to
the topic whose time_range window is NEAREST (distance 0 when the photo is
inside the window; the +/-5 min tolerance of the spec is subsumed -- nearest
also implements the never-orphan fallback). Ties -> lowest topic index. No
parseable window on ANY topic -> topic 0, so a photo is never orphaned when
the day has at least one topic. Per-topic cap PHOTOS_PER_TOPIC_CAP with a
deterministic cascade to the photo's next-nearest topic that still has
headroom; only when EVERY topic is at cap does a photo drop (warning-logged).

History: the previous rule (lambda_item_writer v1) was strict containment
`start <= photo <= end` against a U+2013-only regex -- proven to strand every
photo on Ben_UCPK/2026-07-23 (photos at 10:40, 12:15, 12:16 vs windows
10:39-10:39, 12:12-12:13, 12:13-12:14, 12:14-12:14: misses of 1-2 minutes,
and topic_photos held 0 rows across all of prod history). The retired
report-generator path was permissive: correlate_photos_with_transcripts used
+/-300 s proximity with related[:5].
"""
import logging
import re

from transcript_utils import extract_base_time_from_filename

logger = logging.getLogger()

PHOTOS_PER_TOPIC_CAP = 10   # was 5 (report-generator parity); raised so the
                            # cascade rarely engages on real field days
PHOTO_TOLERANCE_MIN = 5     # documented intent; subsumed by nearest-wins

# 'HH:MM <dash> HH:MM'. The dash is normalized: en dash (U+2013, what the LLM
# actually writes), em dash (U+2014) and the ASCII hyphen are all accepted --
# the prod failure was the WINDOW, not the dash (verified ascii()==8211), but
# a one-character prompt drift must not silently strand photos again.
_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})$")


def _hhmm_to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def parse_time_range(time_range):
    """'HH:MM – HH:MM' -> (start_minutes, end_minutes), or None if
    time_range is missing/unparseable (never raises -- callers treat 'no
    range' as 'this topic has no window', not an error)."""
    if not time_range:
        return None
    m = _TIME_RANGE_RE.match(time_range.strip())
    if not m:
        return None
    start_h, start_m, end_h, end_m = m.groups()
    return int(start_h) * 60 + int(start_m), int(end_h) * 60 + int(end_m)


def _distance(p_minutes, window):
    """Minutes from a photo to a topic window; 0 when inside it."""
    start, end = window
    if start <= p_minutes <= end:
        return 0
    return min(abs(p_minutes - start), abs(p_minutes - end))


def photos_for_topics(photo_objects, topics):
    """PURE. photo_objects: [{key, filename, hhmm}] -- hhmm ('HH:MM') is
    already derived by the caller (list_pictures) from the BUG-01-safe
    transcript_utils filename extractor. topics: the topic dicts of an
    extraction JSON or a daily report (each may carry 'time_range').

    Returns {topic_index: [matched photo_objects entries]} with a key for
    EVERY topic index (callers may still use .get(i, [])). A photo attaches
    to AT MOST one topic. See the module docstring for the rule.

    NOTE: the `topics` parameter name intentionally shadows the callers'
    `repositories.topics` import -- this function is pure and never touches
    that module; the name is kept to match the design's exact signature.
    """
    result = {i: [] for i in range(len(topics))}
    if not topics:
        return result

    windows = {}
    for i, t in enumerate(topics):
        parsed = parse_time_range(t.get("time_range"))
        if parsed is not None:
            windows[i] = parsed

    for p in photo_objects:
        hhmm = p.get("hhmm")
        if not hhmm:
            continue
        p_minutes = _hhmm_to_minutes(hhmm)
        if windows:
            # Nearest window first; ties -> lowest index. The full ordering
            # (not just the winner) is what lets an at-cap topic cascade to
            # the next-nearest one, so the cap re-orphans nothing while any
            # topic still has headroom.
            order = sorted(windows, key=lambda i: (_distance(p_minutes, windows[i]), i))
        else:
            order = [0]                      # no parseable windows: first topic
        target = next((i for i in order if len(result[i]) < PHOTOS_PER_TOPIC_CAP), None)
        if target is None:
            logger.warning("photo %s dropped: every topic at cap %d",
                           p.get("key"), PHOTOS_PER_TOPIC_CAP)
            continue
        result[target].append(p)
    return result


def list_pictures(s3_client, bucket, prefix):
    """List S3 pictures under prefix (paginated), deriving each photo's clock
    time (BUG-01-safe) via transcript_utils.extract_base_time_from_filename.
    A photo whose filename carries no parseable timestamp is skipped -- it can
    never time-correlate to a topic anyway.

    Ported from lambda_item_writer._list_pictures, parameterized on the S3
    client so both callers (and unit tests) inject their own without module
    monkeypatching."""
    photo_objects = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            base_time = extract_base_time_from_filename(filename)
            if base_time is None:
                continue
            photo_objects.append({
                "key": key, "filename": filename,
                "hhmm": base_time.strftime("%H:%M"),
            })
    return photo_objects
