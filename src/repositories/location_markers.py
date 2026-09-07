"""The day's location markers, and the timeline derived from them.

A marker says where the speaker WAS from that moment until the next marker. It
is a state, not an event, and that distinction is the whole feature: a
conversation does not move him, only another announcement does. See
migration 0054 and AI/spec-location-is-a-state-not-an-event-2026-09-07.md.
"""
import json

from psycopg.rows import dict_row

# A marker owns the time after it, but not forever -- one announcement must not
# claim the rest of the day. Past this, the location is UNKNOWN rather than
# assumed, because an unknown location is honest and a wrong one looks like
# evidence.
CARRY_MINUTES = 30


def replace_for_day(conn, company_id, user_folder, date, markers):
    """Write the day's markers, replacing whatever was there.

    Replace rather than append: a re-extraction produces the day's complete set,
    and appending would accumulate duplicates every time a session is re-driven
    -- which happens routinely (finalize re-drives, the backlog probe's repairs,
    a manual invoke).
    """
    conn.cursor().execute(
        "INSERT INTO day_location_markers (company_id, user_folder, report_date, markers) "
        "VALUES (%s, %s, %s, %s::jsonb) "
        "ON CONFLICT (company_id, user_folder, report_date) DO UPDATE "
        "SET markers = EXCLUDED.markers, updated_at = now()",
        (str(company_id), user_folder, date, json.dumps(markers or [])),
    )


def for_day(conn, company_id, user_folder, date):
    """The day's markers, oldest first, or [] when there are none.

    company_id=None MEANS NO COMPANY RESTRICTION -- the same three-state
    convention the rest of this package uses, and for the same reason: a
    cross-company platform_admin reads a customer's folder while sitting in its
    own operator company, so a bare equality binds NULL and matches nothing.
    That exact shape returned an empty day twice on prod (#738/#740).
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT markers FROM day_location_markers "
        "WHERE (%s::uuid IS NULL OR company_id = %s) "
        "AND user_folder = %s AND report_date = %s "
        "ORDER BY updated_at DESC LIMIT 1",
        (str(company_id) if company_id else None,
         str(company_id) if company_id else None,
         user_folder, date),
    ).fetchone()
    if not row or not row["markers"]:
        return []
    markers = row["markers"]
    if isinstance(markers, str):          # psycopg returns jsonb as str on some paths
        markers = json.loads(markers)
    return [m for m in markers if isinstance(m, dict) and m.get("at") and m.get("location")]


def _minutes(hhmm):
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def locate(markers, hhmm):
    """Where the speaker was at `hhmm`, or None.

    None is a real answer and must stay distinguishable from a location: before
    the first announcement of the day nobody has said where they are, and
    CARRY_MINUTES after the last one he may be anywhere. Returning the nearest
    marker in either of those cases would be the unbounded-nearest rule that was
    deliberately removed from photo binding in 2026-07-24, rebuilt one layer up.
    """
    t = _minutes(hhmm)
    if t is None:
        return None
    best = None
    for m in markers:
        at = _minutes(m.get("at"))
        if at is None or at > t:
            continue                      # not yet said
        if t - at > CARRY_MINUTES:
            continue                      # too long ago to still be true
        if best is None or at >= best[0]:
            best = (at, m.get("location"))
    return best[1] if best else None
