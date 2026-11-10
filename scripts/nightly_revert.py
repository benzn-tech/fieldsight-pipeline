"""Revert nightly re-match links for one site (spec 2026-11-10-nightly-rematch-agent §5 Revert).

Deletes the nightly J2 links of one site written since a date -- only `event_links` rows with
`link_type = 'supported_by'` and `method LIKE 'nightly\\_%'` (the underscore escaped, so a
method like `nightlyX...` never matches) -- and then re-derives claim_type for the affected
sessions. Never touches a lexical or any other non-nightly link, a non-supported_by row, or a
`decision_records` row: each record stands for one paid model call and stays.

claim_type after the delete: `claim_type_store.recompute_session` re-derives a session's rows
that still hold a link (or whose basis says none_close), but a row whose only link was the
deleted nightly one is in neither arm and would keep a basis naming a link that no longer
exists. Such rows (basis `link_id` = a deleted link, no supported_by link left) are first
reset to what `claim_type.derive` gives an item with no support (claim_type NULL). The
pre-nightly support status (e.g. none_close -> inferred) is not in the DB: it lives only in
the extraction JSON on S3, which this script does not re-read, so it is not restored. A row
that was none_close before its nightly link therefore shows claim_type NULL until the session
is re-extracted: NULL means unknown, never a claim the evidence does not back.

Reverted items are NOT re-asked by the nightly J2 pass: their nightly_support records stay
(each stands for one paid call), and J2 never re-asks an item that has a record since its
row was written. Only a re-extraction (a new row) makes them askable again.

DRY-RUN BY DEFAULT: without `--apply` the connection is read-only (the server refuses any
write) and only the counts an apply would delete are printed. With `--apply` the delete, the
resets and every session's recompute run in ONE transaction, committed at the end; any error
rolls all of it back.

USAGE
    python scripts/nightly_revert.py --site <uuid> --since 2026-11-01                 # dry run
    python scripts/nightly_revert.py --site <uuid> --since 2026-11-01 --rule-version V
    python scripts/nightly_revert.py --site <uuid> --since 2026-11-01 --apply

`--since` is a site-local (NZ, through nz_time) calendar date: links created at or after its
midnight. Reads the same PG*/DATABASE_URL env `db.connection` reads. Prints ids and counts
only -- never item or turn text.
"""
import argparse
import datetime
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# psycopg parameter style: the literal % of the LIKE pattern is doubled.
_NIGHTLY = (r"e.link_type = 'supported_by' AND e.method LIKE 'nightly\_%%' ESCAPE '\' "
            r"AND e.site_id = %(site)s AND e.created_at >= %(since)s "
            r"AND (%(rv)s::text IS NULL OR e.rule_version = %(rv)s::text)")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Revert nightly re-match links for one site.")
    ap.add_argument("--site", required=True, type=uuid.UUID, help="site id (uuid); required")
    ap.add_argument("--since", required=True, type=datetime.date.fromisoformat,
                    help="links created on or after this site-local date (YYYY-MM-DD)")
    ap.add_argument("--rule-version", default=None,
                    help="only links of this rule_version (default: every nightly version)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete. Without it nothing is written.")
    return ap.parse_args(argv)


def _since_utc(since):
    import nz_time
    start = nz_time.from_nz(datetime.datetime.combine(since, datetime.time()))
    return start.replace(tzinfo=datetime.timezone.utc)


def _params(site_id, since, rule_version):
    if not site_id:
        raise ValueError("site_id is required")
    return {"site": str(site_id), "since": _since_utc(since), "rv": rule_version}


def _stem(key):
    name = (key or "").rsplit("/", 1)[-1]
    return name[:-len(".json")] if name.endswith(".json") else None


def _site_company(conn, site):
    """The site's company id; ValueError for an unknown site (plan and apply alike)."""
    owner = conn.execute("SELECT company_id::text FROM sites WHERE id = %s",
                         (site,)).fetchone()
    if owner is None:
        raise ValueError(f"no site {site}")
    return owner[0]


def plan(conn, *, site_id, since, rule_version=None) -> dict:
    """What an apply would delete: `{links, sessions, by_rule_version}`. Reads only.
    An unknown site raises ValueError, as apply does."""
    p = _params(site_id, since, rule_version)
    _site_company(conn, p["site"])
    rows = conn.execute(
        f"SELECT e.rule_version, e.source_s3_key FROM event_links e WHERE {_NIGHTLY}",
        p).fetchall()
    by_rv = {}
    for rv, _ in rows:
        by_rv[rv] = by_rv.get(rv, 0) + 1
    sessions = {_stem(k) for _, k in rows} - {None}
    return {"site": p["site"], "since": since.isoformat(), "rule_version": rule_version,
            "links": len(rows), "sessions": len(sessions), "by_rule_version": by_rv}


def apply(conn, *, site_id, since, rule_version=None) -> dict:
    """Delete the nightly links in range, reset rows left pointing at a deleted link, and
    recompute claim_type for every affected session. Never commits: the caller owns the
    transaction. Returns `{deleted, reset, sessions, rows, changed}`."""
    import claim_type
    from repositories import claim_type_store
    from repositories.event_links import KIND_SRC_COLUMN, KIND_TABLE

    p = _params(site_id, since, rule_version)
    company_id = _site_company(conn, p["site"])

    deleted = conn.execute(
        f"DELETE FROM event_links e WHERE {_NIGHTLY} RETURNING e.id::text, e.src_kind, "
        "COALESCE(e.src_action_item_id, e.src_finding_id, e.src_decision_id, "
        "e.src_question_id)::text, e.source_s3_key", p).fetchall()

    ct, basis = claim_type.derive({"status": None}, None, None)
    reset = 0
    by_kind = {}
    for link_id, kind, row_id, _ in deleted:
        by_kind.setdefault(kind, []).append((row_id, link_id))
    for kind, pairs in by_kind.items():
        table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
        for (row_id,) in conn.execute(
                f"SELECT c.id::text FROM {table} c "
                f"JOIN unnest(%s::uuid[], %s::text[]) AS d(row_id, link_id) "
                f"ON d.row_id = c.id AND c.claim_basis->>'link_id' = d.link_id "
                f"WHERE NOT EXISTS (SELECT 1 FROM event_links e WHERE e.{col} = c.id "
                f"AND e.link_type = 'supported_by')",
                ([r for r, _ in pairs], [lnk for _, lnk in pairs])).fetchall():
            claim_type_store.write(conn, kind, row_id, ct, basis)
            reset += 1

    sessions = sorted({_stem(k) for *_, k in deleted} - {None})
    out = {"site": p["site"], "deleted": len(deleted), "reset": reset,
           "sessions": len(sessions), "rows": 0, "changed": 0}
    for base in sessions:
        counts = claim_type_store.recompute_session(
            conn, company_id=company_id, site_id=p["site"], session_base=base)
        out["rows"] += counts["rows"]
        out["changed"] += counts["changed"]
    return out


def _connect(apply_mode):
    from db.connection import get_connection
    conn = get_connection()
    if not apply_mode:
        # Every transaction on this connection is READ ONLY: the server refuses a write.
        conn.read_only = True
    return conn


def main(argv=None) -> int:
    args = parse_args(argv)
    conn = _connect(args.apply)
    try:
        if args.apply:
            summary = apply(conn, site_id=args.site, since=args.since,
                            rule_version=args.rule_version)
            conn.commit()
        else:
            summary = plan(conn, site_id=args.site, since=args.since,
                           rule_version=args.rule_version)
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(json.dumps(summary, indent=2))
    if not args.apply:
        print("dry run: nothing was written. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
