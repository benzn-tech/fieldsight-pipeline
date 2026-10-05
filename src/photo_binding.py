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


def _clock_seconds(text, unknown_seconds=0):
    """'HH:MM:SS' or 'HH:MM' -> seconds of the day, or None. A time given to the
    minute takes `unknown_seconds` for its seconds."""
    parts = str(text or "").split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        sec = int(parts[2]) if len(parts) == 3 else unknown_seconds
    except ValueError:
        return None
    return h * 3600 + m * 60 + sec


def _marker_seconds(m):
    """When a marker was said: `at_s` (timed from the words, see
    lambda_extract_session.time_location_markers) when it has one, else the
    start of its `at` minute."""
    timed = _clock_seconds(m.get("at_s")) if m.get("at_s") else None
    if timed is not None:
        return timed
    return _clock_seconds(str(m.get("at") or "")[:5])


def _photo_seconds(p_minutes, hhmmss=None):
    """When a photograph was taken, to the second when known. A photo known only
    to the minute is placed at the END of it, so against markers known only to
    the minute the comparison is exactly the old minute one (said in that minute
    or before: it owns the photo)."""
    sec = _clock_seconds(hhmmss) if hhmmss else None
    return sec if sec is not None else p_minutes * 60 + 59


def _stays(markers):
    """[(start_s, location_key, last_mention_s, session, at_min)], in SECONDS of
    the day -- consecutive markers naming the same place are ONE stay ("Level
    1", then "Continue level one inspection" is still Level 1), so a stay starts
    at its first announcement. `session` is the recording it was said in, when
    known. `at_min` is the first announcement's `at` minute: the clock the
    topics' time ranges are on, so it is what picks the stay's topic.

    Seconds, because minutes cannot order two places named in one minute: on
    prod (Ben_Lin_Test 2026-10-05) "back to the level one" (11:02:05) and
    "moving up to level two" (11:02:15) were both 11:02, and the Level 1
    photograph taken between them (11:02:08) went to Level 2. Markers with no
    `at_s` sort at the start of their minute, in the order given."""
    timed = []
    for idx, m in enumerate(markers or []):
        at = _marker_seconds(m)
        key = " ".join(str(m.get("location") or "").lower().split())
        if at is None or not key:
            continue
        at_min = _clock_seconds(str(m.get("at") or "")[:5])
        timed.append((at, idx, key, m.get("session"), at // 60 if at_min is None else at_min // 60))
    out = []
    for at, _idx, key, session, at_min in sorted(timed):
        if out and out[-1][1] == key and at - out[-1][2] <= PHOTO_CARRY_FORWARD_MIN * 60:
            prev = out[-1]                                            # same stay, later mention
            out[-1] = (prev[0], key, at, prev[3] or session, prev[4])
        else:
            out.append((at, key, at, session, at_min))
    return out


def _current_stay(p_seconds, stays):
    """The stay a moment falls in, or None: the last one announced at or before
    it, unless its last mention is more than PHOTO_CARRY_FORWARD_MIN minutes
    old (counted in whole minutes, as it always was)."""
    current = None
    for stay in stays:
        if stay[0] <= p_seconds:
            current = stay
    if current is None or p_seconds // 60 - current[2] // 60 > PHOTO_CARRY_FORWARD_MIN:
        return None
    return current


def _stay_owner(at, windows):
    """The topic in progress when a place was first announced: its window holds
    the moment, preferring the one that STARTS at it (the announcement opens
    what follows), then one that goes on after it, then the earliest start --
    the activity already under way.

    Starts-at comes first because times are whole minutes: on prod (Ben_Lin_Test
    2026-10-02) "going up to the level two" at 12:23:59 was marked 12:24, the
    Level 1 topic ran 12:22-12:24 and the Level 2 topic 12:24-12:24; "goes on
    after it" ties at minute grain and the earliest start gave the Level 2
    photographs to Level 1."""
    holding = [i for i, (s, e) in windows.items() if s <= at <= e]
    if not holding:
        return None
    return min(holding, key=lambda i: (0 if windows[i][0] == at else 1,
                                       0 if windows[i][1] > at else 1, windows[i][0], i))


def _location_owner(p_seconds, stays, windows, all_windows=None, topic_sessions=None):
    """The topic that owns a photo by WHERE it was taken, or None.

    A LOCATION OWNS ITS PHOTOS (owner, 2026-10-01). Binding by clock alone gave
    an inspection's photos to whatever conversation overlapped them: on TEST,
    Ben_Lin_test2 2026-10-01, three Level 1 photos went to members of the
    public asking the way, because that chat was the topic at 13:28. The
    location markers had it right all along (Ground floor 13:24, Level 1
    13:26): a photo belongs to the topic that was under way when its place was
    announced, for as long as the stay lasts (until another place is named,
    or PHOTO_CARRY_FORWARD_MIN after the stay's last mention).

    A STAY ENDS WITH ITS RECORDING. A topic of ANOTHER session beginning after
    the stay's last mention and by the photograph means the person stopped and
    started again elsewhere: on prod (Ben_Lin_Test 2026-10-02) "level two" at
    12:24 would otherwise have claimed the Te Kaha room photographs at 12:44,
    from a later recording. A chat inside the SAME recording does not end it --
    that is the interruption the stay exists to see past.
    """
    current = _current_stay(p_seconds, stays)
    if current is None:
        return None
    if current[3] and topic_sessions:
        for i, (start, _end) in (all_windows or windows).items():
            other = topic_sessions.get(i)
            if other and other != current[3] and current[2] // 60 < start <= p_seconds // 60:
                return None
    return _stay_owner(current[4], windows)


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


def _eligible_windows(p_minutes, windows, topic_sessions, session_spans):
    """The windows this photo is allowed to be judged against.

    A PHOTO TAKEN WHILE SESSION A WAS RECORDING IS NOT SESSION B'S. That is
    the whole rule, and it only ever REMOVES candidates -- the tolerance and
    carry-forward rules below are unchanged for everything it lets through.

    It exists because the day-wide rebind puts every session's topics in one
    call for the first time, so "the nearest window" can now be a window from a
    recording that was not running when the shutter went. Prod, 2026-09-11:
    one photo at 14:37:31 sat under six topics from six different sessions
    spanning 14:31-14:39. Collapsing that to one is what the day-wide call
    does; choosing the RIGHT one is what this does.

    FAILS OPEN, deliberately, in both directions:

      * a photo inside no session's span is unrestricted -- photos taken
        between sessions are the inspection case this product exists for, and
        they bound yesterday;
      * a topic whose session span is unknown always competes.
        `session_span` returns None for RealPTT days, days predating migration
        0009 and lake-fed files -- "a real answer, not an error" in its own
        words -- and a topic we cannot place in time must not be silently
        excluded from every photo on the day.

    Returns None when nothing is restricted, so the caller can tell "no
    restriction" from "restricted to nothing".
    """
    if not session_spans or not topic_sessions:
        return None
    covering = {k for k, span in session_spans.items()
                if span and span[0] <= p_minutes <= span[1]}
    if not covering:
        return None                      # between sessions: no evidence, no rule
    allowed = {i for i in windows
               if topic_sessions.get(i) is None or topic_sessions.get(i) in covering}
    # Restricted to nothing is not an answer. If the spans disagree with every
    # topic the day has, the spans are the less trustworthy of the two (they
    # come from `recordings` rows that a lake-fed or RealPTT day may never have
    # written) and the ordinary rule stands.
    return allowed or None


def photos_for_topics(photo_objects, topics, *, topic_sessions=None,
                      session_spans=None, markers=None, explain=None):
    """PURE. photo_objects: [{key, filename, hhmm}] -- hhmm ('HH:MM') is
    already derived by the caller (list_pictures) from the BUG-01-safe
    transcript_utils filename extractor. topics: the topic dicts of an
    extraction JSON or a daily report (each may carry 'time_range').

    Returns {topic_index: [matched photo_objects entries]} with a key for
    EVERY topic index (callers may still use .get(i, [])). A photo attaches
    to AT MOST one topic, or to none at all if no topic's window is within
    PHOTO_TOLERANCE_MIN minutes. See the module docstring for the rule.

    THAT GUARANTEE IS PER CALL, which is the whole reason the caller changed.
    Called once per extraction over a day-wide photo list -- which is what
    `lambda_item_writer` did until 2026-09-23 -- it hands the same photo to a
    topic in every session of the day, and `delete_topics_for_source` cannot
    clean up after it because idempotency is keyed on the source key. Measured
    on prod the day it was fixed: 22 of 161 distinct bound photos (13.7%) held
    more than one row, the worst under six topics. Give this function the
    WHOLE DAY's topics and its existing guarantee becomes the global one.

    `topic_sessions` {topic_index: session_key or None} and `session_spans`
    {session_key: (start_min, end_min)} are optional and opt-in: omit both and
    the result is byte-identical to the signature that existed before. See
    _eligible_windows for what they buy and where they fail open.

    `markers` (the day's location markers, [{at, location}]) is opt-in the
    same way: with them, a photo taken during a location stay goes to the
    topic that stay belongs to, before any clock rule -- see _location_owner.

    `explain`, when a dict, is filled with why each photo went where it did:
    {photo key: "location" | "clock" | "carried" | "capped" | "unbound"} -- for
    the recording's trace (pipeline_trace), so "why is this photo here" has an
    answer after the fact.

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
    excluded_by_session = 0
    by_location = 0
    stays = _stays(markers)
    for p in photo_objects:
        hhmm = p.get("hhmm")
        if not hhmm:
            continue
        p_minutes = _hhmm_to_minutes(hhmm)
        allowed = _eligible_windows(p_minutes, windows, topic_sessions, session_spans)
        if allowed is None:
            eligible = windows
        else:
            eligible = {i: w for i, w in windows.items() if i in allowed}
            if len(eligible) < len(windows):
                excluded_by_session += 1
        # WHERE before WHEN: a photo taken during a location stay goes to the
        # topic that stay belongs to, if that topic may have it (same session,
        # under the cap). Otherwise the clock rules below decide, unchanged.
        owner = (_location_owner(_photo_seconds(p_minutes, p.get("hhmmss")), stays,
                                 eligible, windows, topic_sessions)
                 if stays else None)
        if owner is not None and len(result[owner]) < PHOTOS_PER_TOPIC_CAP:
            result[owner].append(p)
            by_location += 1
            if explain is not None:
                explain[p.get("key")] = "location"
            continue
        # Qualifying candidates only: inside the window, or within
        # PHOTO_TOLERANCE_MIN minutes of an edge. Beyond that a topic does
        # not compete at all -- there is no "nearest of everything" fallback.
        qualifying = [i for i in eligible if _distance(p_minutes, eligible[i]) <= PHOTO_TOLERANCE_MIN]
        carried = False
        if not qualifying:
            # Nothing was being said when this was taken. Fall back to what was
            # last said BEFORE it, bounded -- the inspection case.
            # `eligible`, not `windows`: the carry-forward is exactly where
            # the session rule earns its keep. A photo taken in the middle of
            # session B's silent stretch is 20 minutes after session A's last
            # word, which is inside the 30-minute carry-forward -- so without
            # this the rule would be enforced on the tolerance path and
            # bypassed on the fallback path, which is the one that fires for
            # the inspection shape this whole feature exists for.
            qualifying = _carry_forward(p_minutes, eligible)
            carried = bool(qualifying)
        if not qualifying:
            logger.info("photo %s dropped: no topic window within %d min and "
                        "nothing said in the %d min before it",
                        p.get("key"), PHOTO_TOLERANCE_MIN, PHOTO_CARRY_FORWARD_MIN)
            if explain is not None:
                explain[p.get("key")] = "unbound"
            continue
        if carried:
            carried_count += 1
        # Nearest window first; ties -> lowest index. NOT "began latest": the
        # clock cannot tell a walk that goes on from a chat that interrupts it
        # (TEST 2026-10-01 wants the earlier topic, prod 2026-10-02 the later
        # one) -- that is what location markers are for. The full ordering
        # (not just the winner) is what lets an at-cap topic cascade to the
        # next-nearest QUALIFYING one, so the cap only drops a photo when
        # every qualifying topic is full.
        order = sorted(qualifying, key=lambda i: (_distance(p_minutes, eligible[i]), i))
        target = next((i for i in order if len(result[i]) < PHOTOS_PER_TOPIC_CAP), None)
        if target is None:
            capped += 1
            if explain is not None:
                explain[p.get("key")] = "capped"
            continue
        result[target].append(p)
        if explain is not None:
            explain[p.get("key")] = "carried" if carried else "clock"
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
    if excluded_by_session:
        # The only trace this rule leaves. It removes candidates and never adds
        # one, so when it is wrong the symptom is a photo under a topic that
        # looks further away than one it was not allowed to have -- with no
        # error and nothing to grep for. This is the number that moves.
        logger.info("photo binding: %d photo(s) judged only against the "
                    "session that was recording when they were taken",
                    excluded_by_session)
    if by_location:
        # The stronger claim, counted on its own: "taken where this topic's
        # place had just been announced". If the location rule is ever wrong,
        # this is the number that moves.
        logger.info("photo binding: %d photo(s) bound by location (where the "
                    "topic's place had been announced)", by_location)
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
                "hhmmss": base_time.strftime("%H:%M:%S"),
            })
    return photo_objects


def photo_hhmm(filename):
    """'HH:MM' a photograph was taken, from its filename, or None."""
    t = photo_time(filename)
    return t[:5] if t else None


def photo_time(filename):
    """'HH:MM:SS' a photograph was taken, from its filename, or None."""
    try:
        t = extract_base_time_from_filename(filename)
    except Exception:
        return None
    return t.strftime("%H:%M:%S") if t else None


def place_at(markers, when):
    """The place being walked at `when` ('HH:MM:SS', or 'HH:MM') by the day's
    markers, or None -- the same stays photos are bound by (_stays), so a
    report places a photograph by the same answer the binding gave it. Pass
    the seconds (photo_time) when there are any: a minute can hold two places."""
    if not when:
        return None
    p = _clock_seconds(when, unknown_seconds=59)
    if p is None:
        return None
    current = _current_stay(p, _stays(markers))
    return current[1] if current else None


_NUMBER_WORDS = {"zero": "0", "ground": "0", "one": "1", "two": "2", "three": "3", "four": "4",
                 "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _place_words(text):
    words = [_NUMBER_WORDS.get(w, w) for w in _WORD_RE.findall((text or "").lower())]
    out = " ".join(words)
    # "Ground floor" and "Level 0" are one place in every customer's forms.
    return out.replace("0 floor", "level 0").replace("0 level", "level 0")


def names_place(line, place):
    """True when `line` names `place` ("Level one" names "level 1"; "Ground
    floor" names "level 0" and the other way round)."""
    if not place:
        return False
    return " %s " % _place_words(place) in " %s " % _place_words(line)
