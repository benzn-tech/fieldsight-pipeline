"""
Pure photo->topic binding + the S3 pictures lister, shared by
lambda_item_writer (extraction path) and lambda_ingest (report path, P4).

Lives in its own module because lambda_item_writer already imports
lambda_ingest -- lambda_ingest importing lambda_item_writer back would be
circular (precedent: chunking.py / match_request.py).

Rule (2026-07-24 correction, user-approved -- supersedes the P2 nearest-wins
plan below): a photo binds to a topic only if it falls INSIDE the topic's
time_range window, or overreaches either edge by AT MOST
PHOTO_TOLERANCE_MIN (2) minutes. Beyond that it is too far to be sure it
belongs to the event, so it binds to NOTHING -- the never-orphan fallback is
REMOVED. Among topics that qualify (distance <= PHOTO_TOLERANCE_MIN), nearest
wins; ties -> lowest topic index. A topic with no parseable time_range never
competes and always resolves to an empty list. Per-topic cap
PHOTOS_PER_TOPIC_CAP still applies, with a deterministic cascade to the
photo's next-nearest QUALIFYING topic that has headroom; only when every
qualifying topic is at cap does a photo drop (warning-logged). A dropped
photo (no qualifying topic at all, or all qualifying topics at cap) is
logged, never silently lost.

History: P2 (2026-07-23) briefly ran unbounded nearest-wins (every photo
bound to *some* topic, however far, to guarantee no orphans) with
PHOTO_TOLERANCE_MIN=5 documented but not enforced. That was superseded the
next day by the rule above once the user clarified that an unbounded bind is
worse than an orphan: a photo minutes-to-hours from every window should not
be silently attributed to the nearest one.

Before P2, the original rule (lambda_item_writer v1) was strict containment
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

import os

PHOTOS_PER_TOPIC_CAP = int(os.environ.get("PHOTOS_PER_TOPIC_CAP", "60"))
# Was 10, and before that 5. Raised because the cap changed meaning on
# 2026-09-07: while binding was the ONLY way a photo reached a screen, a
# capped photo was a LOST photo, and 10 was a data-loss threshold wearing a
# display-limit's clothes. The day view now lists every photo of the day
# regardless of binding, so the cap only decides how many thumbnails hang off
# one paragraph.
#
# Measured on the day that prompted it: Neil / 2026-09-02 is one topic and 53
# photos, of which 10 came back. 60 covers every real day in the bucket while
# still bounding a pathological one.

# How far past a topic's END a photo may still be attributed to it when NOTHING
# qualifies under PHOTO_TOLERANCE_MIN. See _carry_forward below -- this is the
# inspection case, and the bound is what separates it from the unbounded
# nearest-wins rule that was deliberately removed in 2026-07-24.
PHOTO_CARRY_FORWARD_MIN = int(os.environ.get("PHOTO_CARRY_FORWARD_MIN", "30"))
PHOTO_TOLERANCE_MIN = 2     # hard cap: a photo overreaching a topic window's
                            # edge by more than this many minutes does not
                            # qualify for that topic at all (enforced in
                            # photos_for_topics, not just documented)

# 'HH:MM <dash> HH:MM'. The dash is normalized: en dash (U+2013, what the LLM
# actually writes), em dash (U+2014) and the ASCII hyphen are all accepted --
# the prod failure was the WINDOW, not the dash (verified ascii()==8211), but
# a one-character prompt drift must not silently strand photos again.
_TIME_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})$")


def _carry_forward(p_minutes, windows):
    """The last thing said before this photo, if it was said recently enough.

    THIS RE-OPENS A DOOR THAT WAS DELIBERATELY CLOSED, so the difference
    matters. On 2026-07-24 the never-orphan fallback was removed by explicit
    decision: "an unbounded bind is worse than an orphan -- a photo
    minutes-to-hours from every window should not be silently attributed to the
    nearest one." That reasoning is still correct, and this is not that rule:

      * it is BOUNDED (PHOTO_CARRY_FORWARD_MIN), not nearest-at-any-distance;
      * it is DIRECTIONAL -- only a topic that had already STARTED can claim a
        later photo, because the claim being modelled is "he was still doing
        the thing he last described", not "this is the closest event";
      * and the premise changed. In July an unbound photo was INVISIBLE, so a
        wrong bind and no bind were both failures and the quieter one won.
        Since 2026-09-07 the day view lists every photo whether or not it
        binds, so an unbound photo is merely ungrouped. The cost of being
        wrong went down; the cost of binding nothing did not.

    Why it is needed, measured: 37% of topic time windows are a single instant
    and 76% are narrower than PHOTO_TOLERANCE_MIN. Neil / 2026-08-18 is one
    utterance at 14:32 followed by twelve silent minutes of photography -- 25
    photos, of which 6 bound. The inspection workflow (say where you are, then
    photograph in silence) produces exactly this shape every time.

    Deterministic on ties, and the ties are real: 2026-08-13 has three topics
    sharing one time_range, so "the last thing said" is genuinely ambiguous
    there. Latest end wins, then lowest index. That is a coin-toss dressed as a
    rule, and it is the reason this is a FALLBACK: the real answer is a
    location marker (spec 2026-09-07), which does not collide because people
    announce where they are far less often than they change subject.
    """
    best = None
    for i, (start, end) in windows.items():
        if start > p_minutes:
            continue                      # had not been said yet
        if p_minutes - end > PHOTO_CARRY_FORWARD_MIN:
            continue                      # too long ago to still be true
        key = (end, -i)
        if best is None or key > best[0]:
            best = (key, i)
    return [best[1]] if best else []


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
    to AT MOST one topic, or to none at all if no topic's window is within
    PHOTO_TOLERANCE_MIN minutes. See the module docstring for the rule.

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

    capped = 0
    carried_count = 0
    for p in photo_objects:
        hhmm = p.get("hhmm")
        if not hhmm:
            continue
        p_minutes = _hhmm_to_minutes(hhmm)
        # Qualifying candidates only: inside the window, or within
        # PHOTO_TOLERANCE_MIN minutes of an edge. Beyond that a topic does
        # not compete at all -- there is no "nearest of everything" fallback.
        qualifying = [i for i in windows if _distance(p_minutes, windows[i]) <= PHOTO_TOLERANCE_MIN]
        carried = False
        if not qualifying:
            # Nothing was being said when this was taken. Fall back to what was
            # last said BEFORE it, bounded -- the inspection case.
            qualifying = _carry_forward(p_minutes, windows)
            carried = bool(qualifying)
        if not qualifying:
            logger.info("photo %s dropped: no topic window within %d min and "
                        "nothing said in the %d min before it",
                        p.get("key"), PHOTO_TOLERANCE_MIN, PHOTO_CARRY_FORWARD_MIN)
            continue
        if carried:
            carried_count += 1
        # Nearest window first; ties -> lowest index. The full ordering (not
        # just the winner) is what lets an at-cap topic cascade to the next-
        # nearest QUALIFYING one, so the cap only drops a photo when every
        # qualifying topic is full.
        order = sorted(qualifying, key=lambda i: (_distance(p_minutes, windows[i]), i))
        target = next((i for i in order if len(result[i]) < PHOTOS_PER_TOPIC_CAP), None)
        if target is None:
            capped += 1
            continue
        result[target].append(p)
    # ONE line, not one per photo, and info rather than warning.
    #
    # It used to warn per photo, which was right while a capped photo was LOST:
    # binding was the only way a photo reached a screen. Since the day view
    # carries the whole day's list, the cap no longer decides whether a photo is
    # visible -- only how many hang off one paragraph -- and 43 warnings for a
    # single ordinary day (2026-09-02, measured) is the kind of noise that
    # teaches people to skip the channel.
    #
    # Still logged, and still unconditional in the sense that matters: "nothing
    # was capped" and "binding never ran" have to stay distinguishable, so the
    # count is emitted whenever there was anything to place.
    if capped:
        logger.info("photo binding: %d photo(s) past the per-topic cap of %d "
                    "(visible in the day list, not lost)", capped, PHOTOS_PER_TOPIC_CAP)
    if carried_count:
        # Counted separately from the ordinary binds, because these are the
        # weaker claim: they say "nothing was being said, so we attributed this
        # to the last thing that was". If that ever starts being wrong, the
        # number that moves is this one.
        logger.info("photo binding: %d photo(s) attributed to the last topic "
                    "that started before them (nothing within %d min)",
                    carried_count, PHOTO_TOLERANCE_MIN)
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
