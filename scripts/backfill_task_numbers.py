"""Number the action items that have no task number yet (action packs Task 1, spec §14 D1).

NOT RUN BY THIS BRANCH. Running it on prod is the owner's step, after migration 0093.
Aurora is not reachable from outside the VPC: on prod run it as the org-api internal task
`backfill_task_numbers` (docs/runbooks/2026-11-01-action-packs-rollout.md step 9). The logic
lives in src/task_numbers_backfill.py; this script is the same thing for anyone inside.
DRY RUN BY DEFAULT: it counts and writes nothing; --apply is the only thing that writes.

WHAT IT NUMBERS
---------------
Every LIVE stable_id (an action_items row on a topic that is not superseded, not deleted and
not under a deleted recording -- visible_topics_predicate) with no action_task_numbers row:

  * every item that existed before 0093, and
  * every item written in the deploy -> migrate window (sam deploy runs first; the writers
    skip numbering while the table is missing).

Per site, in the order the stable_id FIRST appeared (min(created_at) over every row that has
carried it), ties broken by stable_id, starting after the site's current max -- so a number
already handed out (by a writer after migrate) is never changed or reused. Items numbered
live after migrate therefore get lower numbers than older items numbered here; that is the
price of the deploy order, and it is the same for every site.

Superseded-only stable_ids are left alone: nothing can address them.

HOW
---
One site at a time, each in its own transaction under the same per-site lock the writers
take (repositories.action_items.lock_site_task_numbers), so it can run while writers run and
never holds more than one site's lock. Idempotent: a second run finds nothing to number.

USAGE
    python scripts/backfill_task_numbers.py                  # dry run
    python scripts/backfill_task_numbers.py --apply          # writes
    python scripts/backfill_task_numbers.py --site <uuid>    # one site (repeatable)

Reads the same PG*/DATABASE_URL env `db.connection` reads, so it must run somewhere that can
reach Aurora. Prints counts only.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from task_numbers_backfill import run_backfill   # noqa: E402,F401 (re-exported)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Number action items that have no task number.")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it nothing is changed.")
    ap.add_argument("--site", action="append", help="limit to one site id (repeatable)")
    args = ap.parse_args(argv)

    from db.connection import get_connection
    conn = get_connection(autocommit=True)
    try:
        summary = run_backfill(conn, apply=args.apply, site_ids=args.site)
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    if not args.apply:
        print("nothing was written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
