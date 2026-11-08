"""Backfill: re-derive claim_type for one site's sessions (Track C, Task 8).

`claim_type` is derived at extraction time and again when the voiceprint writer lands a
session's names (`lambda_voiceprint_writer._recompute_claims`). Sessions whose names landed
before that hook shipped -- or whose hook run failed (it is best-effort and only logs) --
keep the claim_type they had at extraction time. This walks the site's live sessions and
runs the same `claim_type_store.recompute_session` over each.

DRY-RUN BY DEFAULT: every session is recomputed inside the connection's transaction and
then ROLLED BACK; `commit` is only ever called with `--apply` (one commit per session, so
a long run never holds one site-wide transaction). The printed counts are what an apply
would change.

USAGE
    python scripts/claim_type_backfill.py --site <uuid>                      # dry run
    python scripts/claim_type_backfill.py --site <uuid> --since 2026-10-01   # dry run
    python scripts/claim_type_backfill.py --site <uuid> --since 2026-10-01 --apply

Reads the same PG*/DATABASE_URL env `db.connection` reads, so it must run somewhere that
can reach the database. Prints ids and counts only -- never a name or any item text.
"""
import argparse
import datetime
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Re-derive claim_type for one site's sessions.")
    ap.add_argument("--site", required=True, type=uuid.UUID, help="site id (uuid)")
    ap.add_argument("--since", type=datetime.date.fromisoformat, default=None,
                    help="only sessions with report_date >= YYYY-MM-DD")
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without it nothing is changed.")
    return ap.parse_args(argv)


def _session_bases(conn, site_id, since):
    """The extraction-key stems (`.../{stem}.json`) of the site's live topics."""
    from deleted_predicates import visible_topics_predicate
    sql = ("SELECT DISTINCT t.source_s3_key FROM topics t "
           "WHERE t.site_id = %s AND t.source_s3_key LIKE '%%.json' AND "
           + visible_topics_predicate("t"))
    params = [str(site_id)]
    if since is not None:
        sql += " AND t.report_date >= %s"
        params.append(since)
    stems = set()
    for (key,) in conn.execute(sql, params).fetchall():
        stem = key.rsplit("/", 1)[-1][:-len(".json")]
        if stem:
            stems.add(stem)
    return sorted(stems)


def run(conn, *, site_id, since=None, apply=False) -> dict:
    from repositories import claim_type_store
    row = conn.execute("SELECT company_id::text FROM sites WHERE id = %s",
                       (str(site_id),)).fetchone()
    if row is None:
        raise SystemExit(f"no site {site_id}")
    company_id = row[0]
    bases = _session_bases(conn, site_id, since)
    conn.rollback()
    summary = {"site": str(site_id), "since": since.isoformat() if since else None,
               "apply": bool(apply), "sessions": len(bases), "failed": 0,
               "rows": 0, "changed": 0}
    for base in bases:
        try:
            counts = claim_type_store.recompute_session(
                conn, company_id=company_id, site_id=str(site_id), session_base=base)
        except Exception as exc:
            conn.rollback()
            summary["failed"] += 1
            print(f"failed: {base}: {type(exc).__name__}", file=sys.stderr)
            continue
        if apply:
            conn.commit()
        else:
            conn.rollback()
        summary["rows"] += counts["rows"]
        summary["changed"] += counts["changed"]
    return summary


def main(argv=None) -> int:
    args = parse_args(argv)
    from db.connection import get_connection
    conn = get_connection()
    try:
        summary = run(conn, site_id=args.site, since=args.since, apply=args.apply)
    finally:
        if not args.apply:
            conn.rollback()
        conn.close()
    print(json.dumps(summary, indent=2))
    if not args.apply:
        print("nothing was written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
