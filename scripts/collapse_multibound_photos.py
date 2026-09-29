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

import json                              # noqa: E402

import photo_collapse                     # noqa: E402
from db.connection import get_connection   # noqa: E402


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
    with get_connection() as conn:
        summary = photo_collapse.run(conn, boto3.client("s3"), args.bucket,
                                     apply=args.apply, folder=args.folder, date=args.date)
    print(json.dumps(summary, indent=2, default=str))
    if not args.apply:
        print("nothing was written. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
