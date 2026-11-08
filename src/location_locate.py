"""Where the speaker was at a moment, from the day's location markers.

Pure -- no database, no psycopg -- so the generated-report worker, which has no
psycopg layer, can group by place (Track L Task 6). Moved here from
`repositories.location_markers`, which re-exports it.
"""

# A marker owns the time after it, but not forever -- one announcement must not
# claim the rest of the day. Past this, the location is UNKNOWN rather than
# assumed, because an unknown location is honest and a wrong one looks like
# evidence.
CARRY_MINUTES = 30


def _seconds(when, unknown_seconds):
    """'HH:MM:SS' or 'HH:MM' -> seconds of the day, or None."""
    parts = str(when or "").split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        return (int(parts[0]) * 3600 + int(parts[1]) * 60
                + (int(parts[2]) if len(parts) == 3 else unknown_seconds))
    except ValueError:
        return None


def locate(markers, when):
    """Where the speaker was at `when` ('HH:MM:SS', or 'HH:MM'), or None.

    None is a real answer and must stay distinguishable from a location: before
    the first announcement of the day nobody has said where they are, and
    CARRY_MINUTES after the last one he may be anywhere. Returning the nearest
    marker in either of those cases would be the unbounded-nearest rule that was
    deliberately removed from photo binding in 2026-07-24, rebuilt one layer up.

    Compared in SECONDS where they are known -- a marker's `at_s` (timed from
    its quote) and a photo's filename time. Two places can be named in one
    minute (prod, Ben_Lin_Test 2026-10-05: Level 1 at 11:02:05, Level 2 at
    11:02:14), and to the minute the 11:02:08 photograph was grouped under
    Level 2 while photo binding (photo_binding._stays) had it under Level 1.
    A time known only to the minute sits at the end of it, so minute-only data
    groups exactly as before.
    """
    t = _seconds(when, 59)
    if t is None:
        return None
    best = None
    for m in markers:
        at = _seconds(m.get("at_s"), 0) if m.get("at_s") else None
        if at is None:
            at = _seconds(str(m.get("at") or "")[:5], 0)
        if at is None or at > t:
            continue                      # not yet said
        if t // 60 - at // 60 > CARRY_MINUTES:
            continue                      # too long ago to still be true
        if best is None or at >= best[0]:
            best = (at, m.get("location"))
    return best[1] if best else None
