"""Collapse the photos that already hang off more than one topic.

NOT RUN BY THIS BRANCH. The fix stops new duplicates; the rows already in prod
need one pass, and a prod write is the owner's to execute. Run --dry-run first;
it is the default, and --apply is the only thing that writes.

WHY A SCRIPT AND NOT A MIGRATION
--------------------------------
A migration runs inside the deploy, on every environment, with no one looking
at what it did. This DELETEs rows that took a model call to produce, and the
row it keeps is a judgement (which of six topics does a photo belong to) rather
than a mechanical transform. The deploy should not be the thing making that
call at 3am.

WHAT IT DOES
------------
For each (folder, date) that holds a photo bound more than once, it recomputes
the day exactly the way `photo_rebind.rebind_day_photos` now does -- same
matcher, same session spans, same tie-breaks -- and replaces that day's
`source='binding'` rows with the result. Keyframe and human rows are untouched,
because `replace_day_photo_bindings` is what does the writing and its DELETE is
bounded on `source = 'binding'`.

It is therefore NOT a "delete the extra rows" script. Deleting five of six
arbitrary rows would leave whichever one happened to survive, and on prod the
surviving row was often the wrong one -- ben_lin_2026-09-11_14-37-31.jpg is
under a 14:31-14:32 topic and a 14:37-14:37 one, and the six-minutes-away row
is the complaint. Recomputing picks the containing window.

MEASURED BEFORE WRITING THIS (prod, 2026-09-23)
-----------------------------------------------
    topic_photos            193 rows / 161 distinct photos
    bound more than once     22 photos (13.7%)
    worst                     6 topics, 6 source keys, spanning 7 minutes

Expected after: ~161 rows, 0 photos bound more than once, and no photo missing
from any screen -- the day view lists every photo regardless of binding, which
is what makes this safe to run at all.

USAGE
    python scripts/collapse_multibound_photos.py                 # dry run
    python scripts/collapse_multibound_photos.py --apply         # writes
    python scripts/collapse_multibound_photos.py --folder Ben_Lin --date 2026-09-11

Reads the same PG*/DATABASE_URL env `db.connection` reads, so it must run
somewhere that can reach Aurora (in-VPC, or through the RDS Data API wrapper
the operator prefers).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import photo_binding                      # noqa: E402
import photo_rebind                       # noqa: E402
from db.connection import get_connection   # noqa: E402
from repositories import topics            # noqa: E402

_MULTIBOUND = """
SELECT split_part(tp.s3_key, '/', 2)  AS folder,
       split_part(tp.s3_key, '/', 4)  AS day,
       count(DISTINCT tp.topic_id)    AS n,
       count(*)                       AS rows
  FROM topic_photos tp
 WHERE tp.source = 'binding'
 GROUP BY tp.s3_key
HAVING count(DISTINCT tp.topic_id) > 1
"""


def affected_days(conn):
    """{(folder, day): photos_bound_more_than_once}, worst first.

    Folder and day come out of the KEY (`users/{folder}/pictures/{date}/x.jpg`),
    not out of `topics`, for the same reason the binding scope does: the key is
    what the photo list was built from, and a topic's site/user can differ
    between two sessions of one day.
    """
    out = {}
    for r in conn.cursor().execute(_MULTIBOUND).fetchall():
        folder, day = r[0], r[1]
        out[(folder, day)] = out.get((folder, day), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def company_of(conn, folder):
    """The company that owns a folder's recordings, for the session spans.

    None is survivable: `session_local_span` then finds no rows, every span is
    unknown, and the session rule fails open -- the same answer a RealPTT day
    gives. The day still collapses to one topic per photo, which is the point.
    """
    row = conn.cursor().execute(
        "SELECT company_id FROM recordings WHERE s3_key LIKE %s LIMIT 1",
        (f"users/{folder}/%",)).fetchone()
    return row[0] if row else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it nothing is changed.")
    ap.add_argument("--folder", help="limit to one folder")
    ap.add_argument("--date", help="limit to one day (YYYY-MM-DD)")
    ap.add_argument("--bucket", default=os.environ.get("S3_BUCKET", ""),
                    help="data bucket, for re-listing each day's pictures")
    args = ap.parse_args()

    import boto3
    s3 = boto3.client("s3")

    with get_connection() as conn:
        days = affected_days(conn)
        if args.folder or args.date:
            days = {k: v for k, v in days.items()
                    if (not args.folder or k[0] == args.folder)
                    and (not args.date or k[1] == args.date)}
        print(f"{len(days)} day(s) hold a photo bound more than once")
        total_before = total_after = 0
        for (folder, day), n in days.items():
            photos = photo_binding.list_pictures(
                s3, args.bucket, f"users/{folder}/pictures/{day}/")
            before = conn.cursor().execute(
                "SELECT count(*) FROM topic_photos tp JOIN topics t ON t.id = tp.topic_id "
                "WHERE tp.source = 'binding' AND tp.s3_key LIKE %s",
                (f"users/{folder}/pictures/{day}/%",)).fetchone()[0]
            if args.apply:
                written = photo_rebind.rebind_day_photos(
                    conn, company_of(conn, folder), folder, day, photos)
            else:
                # Same computation, no write: resolve the day and count what a
                # rebind WOULD produce. `replace_day_photo_bindings` is the only
                # writer and it is not called here.
                day_topics = topics.list_day_topics_for_binding(conn, folder, day)
                sessions = {i: photo_rebind.session_of(t.get("source_s3_key"))
                            for i, t in enumerate(day_topics)}
                written = sum(len(v) for v in photo_binding.photos_for_topics(
                    photos, day_topics, topic_sessions=sessions).values())
            total_before += before
            total_after += written
            print(f"  {folder}/{day}: {n} multi-bound photo(s), "
                  f"{before} row(s) -> {written}")
        print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: "
              f"{total_before} binding row(s) -> {total_after}")
        if not args.apply:
            print("nothing was written. Re-run with --apply to write.")
            # An explicit rollback rather than relying on "we only SELECTed":
            # a dry run that commits is a dry run in name only.
            conn.rollback()


if __name__ == "__main__":
    main()
