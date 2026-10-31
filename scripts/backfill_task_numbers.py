"""Number the action items that have no task number yet (action packs Task 1, spec §14 D1).

NOT RUN BY THIS BRANCH. Running it on prod is the owner's step, after migration 0093.
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

from deleted_predicates import visible_topics_predicate   # noqa: E402
from repositories.action_items import lock_site_task_numbers   # noqa: E402

# The unnumbered live stable_ids, with the site of their live row and their first appearance.
# `%(sites)s` is NULL for "every site". Always executed WITH parameters: the deletion
# predicate carries a literal '%%'.
_CANDIDATES = (
    "WITH live AS ("
    " SELECT DISTINCT ON (a.stable_id) a.stable_id, a.site_id"
    " FROM action_items a JOIN topics t ON t.id = a.topic_id"
    f" WHERE {visible_topics_predicate('t')}"
    " AND (%(sites)s::uuid[] IS NULL OR a.site_id = ANY(%(sites)s::uuid[]))"
    " ORDER BY a.stable_id, a.created_at DESC"
    "), cand AS ("
    " SELECT l.stable_id, l.site_id,"
    " (SELECT min(a2.created_at) FROM action_items a2 WHERE a2.stable_id = l.stable_id)"
    " AS first_at"
    " FROM live l WHERE NOT EXISTS"
    " (SELECT 1 FROM action_task_numbers n WHERE n.stable_id = l.stable_id)"
    ")"
)

_COUNT_BY_SITE = _CANDIDATES + " SELECT site_id, count(*) FROM cand GROUP BY site_id ORDER BY site_id"

_INSERT_FOR_SITE = _CANDIDATES + (
    " INSERT INTO action_task_numbers (stable_id, site_id, task_no)"
    " SELECT c.stable_id, c.site_id,"
    " (SELECT COALESCE(max(n.task_no), 0) FROM action_task_numbers n"
    "  WHERE n.site_id = %(site)s)"
    " + row_number() OVER (ORDER BY c.first_at, c.stable_id)"
    " FROM cand c WHERE c.site_id = %(site)s"
    " ON CONFLICT (stable_id) DO NOTHING"
)


def run_backfill(conn, *, apply: bool, site_ids=None) -> dict:
    """Counts (and with `apply`, numbers) the unnumbered live stable_ids.

    Returns {"sites": sites with something to number, "to_number": stable_ids found,
    "numbered": rows written (0 on a dry run)}. Each site's write is its own
    `conn.transaction()` -- a transaction on an autocommit connection (main), a savepoint
    inside a caller's open one (the tests)."""
    sites = [str(s) for s in site_ids] if site_ids else None
    per_site = conn.execute(_COUNT_BY_SITE, {"sites": sites}).fetchall()
    numbered = 0
    if apply:
        for site_id, _n in per_site:
            with conn.transaction():
                lock_site_task_numbers(conn, site_id)
                numbered += conn.execute(
                    _INSERT_FOR_SITE, {"sites": [str(site_id)], "site": site_id}).rowcount
    return {"sites": len(per_site), "to_number": sum(n for _s, n in per_site),
            "numbered": numbered}


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
