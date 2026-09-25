"""The day-wide photo rebind, shared by lambda_item_writer (extraction path)
and lambda_ingest (report path).

Its own module for the same reason photo_binding.py is one: lambda_item_writer
already imports lambda_ingest, so lambda_ingest importing lambda_item_writer
back would be circular. `photo_binding` stays pure plus an S3 lister; this is
the half that touches Aurora, and keeping them apart is what lets the matcher
be tested without a database.

WHAT THIS FIXES (2026-09-23)
----------------------------
`photo_binding.photos_for_topics` has always bound a photo to at most one
topic. That guarantee is PER CALL. Both callers listed the photos of a whole
DAY (`users/{folder}/pictures/{date}/`) and matched them against ONE
extraction's topics, and idempotency is keyed on `source_s3_key` -- so session
B's run never cleared session A's bindings and both kept the same photo.

Measured on prod the day this was written: `topic_photos` held 193 rows over
161 distinct photos, and 22 of those photos (13.7%) hung off more than one
topic. Ben_UCPK2 produced SEVEN extraction artifacts for one afternoon, each of
which had independently claimed the whole day's pictures. The worst row,
Ben_Lin / 2026-09-11 / 14-37-31.jpg, sat under six topics from six source keys
spanning seven minutes.

It is not a side effect of the 2026-09-07 three-layer fix, though that fix made
it more frequent: raising PHOTOS_PER_TOPIC_CAP 10->60 and adding the bounded
carry-forward both widened what a single run claims. The duplicate is
structural and predates both -- day-scoped photos x session-scoped topics x
source-keyed idempotency.

Nothing about the matcher changed. It is called ONCE, for the whole day.
"""
import logging

import photo_binding
from repositories import recordings, topics

logger = logging.getLogger()


def session_of(source_s3_key):
    """The session an `extractions/{folder}/{date}/{session}.json` key names.

    None for a report key (`reports/{date}/{folder}/daily_report.json`) and for
    anything else -- and None is meaningful downstream rather than a hole: it
    means "this topic belongs to no session we can place in time", which
    photo_binding._eligible_windows treats as ALWAYS eligible, never as never.
    """
    if not source_s3_key or not source_s3_key.startswith("extractions/"):
        return None
    tail = source_s3_key.rsplit("/", 1)[-1]
    return tail[:-5] if tail.endswith(".json") else tail


def rebind_day_photos(conn, company_id, user_folder, date, photo_objects):
    """Rebuild one (folder, day)'s MACHINE photo bindings from scratch.

    Order, and every step is load-bearing:

      1. take a DAY-wide advisory lock. Two sessions of one day could not race
         before -- each cleared and wrote only its own source key -- and a
         day-wide replace can. Keyed on (folder, date), because that is the
         scope the replace covers, not on the extraction;
      2. read every topic the day has, both source shapes, deleted ones
         excluded with both arms;
      3. ask each SESSION (not each topic -- a session has many, and this is a
         query) for its span in the DEVICE'S LOCAL CLOCK. Never
         `recordings.session_span`, which answers in timestamptz: mixing it
         with a filename clock is the BUG-19/37 family and it fails silently,
         a span off by thirteen hours simply covering nothing;
      4. ONE matcher pass over the whole day;
      5. replace the day's `source='binding'` rows, leaving keyframe and human
         rows untouched.

    A day with NO topics still runs the replace, which then deletes the stale
    rows and inserts none. That is the correct answer for a day whose topics
    were all deleted, and it is the branch it would have been easy to skip --
    "wrote no rows" and "never ran" must not be the same outcome.

    Returns the number of binding rows written.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"photobind:{user_folder}:{date}",))
    day_topics = topics.list_day_topics_for_binding(conn, user_folder, date)

    topic_sessions, spans = {}, {}
    for i, t in enumerate(day_topics):
        session = session_of(t.get("source_s3_key"))
        topic_sessions[i] = session
        if session is not None and session not in spans:
            spans[session] = recordings.session_local_span(
                conn, company_id, user_folder, date, session)
    # A span of None is "unknown", and _eligible_windows must not read it as a
    # window covering nothing -- drop it, so that session always competes.
    spans = {k: v for k, v in spans.items() if v is not None}

    bound = photo_binding.photos_for_topics(
        photo_objects, day_topics,
        topic_sessions=topic_sessions, session_spans=spans or None)

    rows = []
    for i, photos in bound.items():
        for p in photos:
            rows.append({
                "topic_id": day_topics[i]["id"],
                "s3_key": p["key"],
                # video-keyframe plan (Task 4): a re-bound synthetic keyframe
                # (its filename carries the '_kf_' marker) keeps an "Auto
                # keyframe" caption so the UI can still tell it apart after a
                # re-run; real photos stay caption-less.
                "caption_text": ("Auto keyframe" if "_kf_" in p["filename"] else None),
            })
    removed = topics.replace_day_photo_bindings(conn, user_folder, date, rows)
    logger.info("photo rebind %s/%s: %d topic(s), %d photo(s) listed, "
                "%d bind(s) written, %s stale row(s) removed",
                user_folder, date, len(day_topics), len(photo_objects),
                len(rows), removed)
    return len(rows)
