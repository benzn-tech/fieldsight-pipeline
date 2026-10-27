"""Reassign one orphan folder's lake_objects rows to a real account -- in the INDEX only.

OWNER DECISION 2026-10-27 (A): folder `Ben_Lin` has no users row. Its rows go to account
`Ben_Lin_test2` (company FieldSight-platform). S3 objects are not moved; keys are not rewritten.

  * rows with company = the target's company           -> user_id = target, reason 'operator'
  * rows with company NULL and reason 'unresolved'     -> company = target's company,
                                                          user_id = target, reason 'operator'
  * rows of any OTHER company (the 9 Southbase rows)   -> untouched, not counted as a change
  * rows with company NULL for another reason (e.g. split_day) -> untouched, reported

`reason = 'operator'` records that a person decided this. migration 0087 has no CHECK on
reason (only on source, which is left alone), lake_owner.REASONS is not read by any code
path, and repositories.lake_objects skips every later upsert onto an 'operator' row, so a
backfill re-run or a drain cannot put the resolver's user back. That pin ships with this
branch: until it is deployed, do NOT run a lake backfill after --apply (or re-run this
script after it; it is idempotent).

MODES (a dry run is the default and writes nothing, in a READ ONLY transaction):
  python scripts/prod_lake_reassign_folder.py --database fieldsight --allow-prod \
      --from-folder Ben_Lin --to-folder Ben_Lin_test2 --expect-company FieldSight-platform \
      --manifest-out DRY.json
  ... same ... --apply --manifest DRY.json        # re-checks nothing drifted, then writes;
                                                   # the manifest's `before` rows ARE the undo
  ... --database fieldsight --allow-prod --rollback DRY.json [--apply]
"""
import argparse
import sys

try:
    from scripts import lake_ops_common as common
except ImportError:  # run as `python scripts/prod_lake_reassign_folder.py`
    import lake_ops_common as common

OPERATOR = "operator"
_COLS = ("s3_key, company_id::text AS company_id, site_id::text AS site_id, "
         "user_id::text AS user_id, session_base, folder, day::text AS day, kind, size_bytes, "
         "source, reason, object_modified_at::text AS object_modified_at, "
         "created_at::text AS created_at, updated_at::text AS updated_at, "
         "deleted_at::text AS deleted_at")
PAGE = 200


# ---- pure selection logic (unit-tested) ------------------------------------------------

def plan_reassign(rows, target_user_id, target_company_id):
    """Split the folder's live rows into what changes and what does not.

    Returns {"claim": [...], "set_user": [...], "pin_only": [...], "unchanged": [...],
             "other_company": [...], "null_other_reason": [...]} of row dicts.
    change set = claim + set_user + pin_only."""
    out = {k: [] for k in ("claim", "set_user", "pin_only", "unchanged", "other_company",
                           "null_other_reason")}
    for r in rows:
        company, user, reason = r.get("company_id"), r.get("user_id"), r.get("reason")
        if r.get("deleted_at"):
            continue
        if company is None:
            out["claim" if reason == "unresolved" else "null_other_reason"].append(r)
        elif company != target_company_id:
            out["other_company"].append(r)
        elif user != target_user_id:
            out["set_user"].append(r)
        elif reason != OPERATOR:
            out["pin_only"].append(r)
        else:
            out["unchanged"].append(r)
    return out


def change_set(plan):
    return plan["claim"] + plan["set_user"] + plan["pin_only"]


def fingerprint(rows):
    """What must not have moved between the dry run and --apply."""
    return sorted((r["s3_key"], r.get("company_id"), r.get("user_id"), r.get("reason"),
                   r.get("updated_at"), r.get("deleted_at")) for r in rows)


def summarise(plan):
    out = {}
    for name, rows in plan.items():
        by = {}
        for r in rows:
            k = f"company={r.get('company_id')} reason={r.get('reason')}"
            b = by.setdefault(k, {"rows": 0, "bytes": 0, "min_day": None, "max_day": None})
            b["rows"] += 1
            b["bytes"] += int(r.get("size_bytes") or 0)
            d = r.get("day")
            if d:
                b["min_day"] = d if b["min_day"] is None else min(b["min_day"], d)
                b["max_day"] = d if b["max_day"] is None else max(b["max_day"], d)
        out[name] = {"rows": len(rows), "bytes": sum(int(r.get("size_bytes") or 0) for r in rows),
                     "by": by}
    return out


# ---- database steps -----------------------------------------------------------------------

def target_account(db, folder):
    rows = db.query("SELECT u.id::text AS id, u.company_id::text AS company_id, c.name AS company "
                    "FROM users u LEFT JOIN companies c ON c.id = u.company_id "
                    "WHERE u.folder_name = :f", f=folder)
    if len(rows) != 1:
        raise SystemExit(f"expected exactly one users row with folder_name={folder!r}, got {len(rows)}")
    return rows[0]


def folder_rows(db, folder, lock=False):
    rows, after = [], ""
    while True:
        page = db.query(f"SELECT {_COLS} FROM lake_objects WHERE folder = :f AND s3_key > :a "
                        f"ORDER BY s3_key LIMIT {PAGE}" + (" FOR UPDATE" if lock else ""),
                        f=folder, a=after)
        rows += page
        if len(page) < PAGE:
            return rows
        after = page[-1]["s3_key"]


def load_state(db, args):
    if db.query("SELECT 1 AS x FROM users WHERE folder_name = :f", f=args.from_folder):
        raise SystemExit(f"{args.from_folder!r} has a users row; this script is for orphan folders")
    tgt = target_account(db, args.to_folder)
    if tgt["company"] != args.expect_company:
        raise SystemExit(f"{args.to_folder} is in {tgt['company']!r}, not {args.expect_company!r}")
    return tgt


def run_dry(db, args, log):
    tgt = load_state(db, args)
    rows = folder_rows(db, args.from_folder)
    plan = plan_reassign(rows, tgt["id"], tgt["company_id"])
    stray = db.query("SELECT count(*) AS n FROM lake_objects WHERE s3_key ~ :rx "
                     "AND folder IS DISTINCT FROM :f",
                     rx=common.folder_segment_regex([args.from_folder]), f=args.from_folder)[0]["n"]
    manifest = {"op": "reassign_folder", "created_at": common.now_iso(), "database": args.database,
                "from_folder": args.from_folder, "to_folder": args.to_folder,
                "target": tgt, "summary": summarise(plan),
                "rows_with_folder_segment_but_other_folder_column": stray,
                "before": change_set(plan),
                "fingerprint": fingerprint(change_set(plan))}
    log("dry_run", target=tgt["id"], company=tgt["company"], live_rows=len(rows),
        changes=len(manifest["before"]))
    return manifest


def run_apply(db, args, log):
    m = common.read_json(args.manifest)
    if (m.get("op"), m.get("database"), m.get("from_folder"), m.get("to_folder")) != \
            ("reassign_folder", args.database, args.from_folder, args.to_folder):
        raise SystemExit("manifest does not match these arguments")
    tgt = load_state(db, args)
    if tgt["id"] != m["target"]["id"] or tgt["company_id"] != m["target"]["company_id"]:
        raise SystemExit("target account changed since the dry run")
    rows = folder_rows(db, args.from_folder, lock=True)
    todo = change_set(plan_reassign(rows, tgt["id"], tgt["company_id"]))
    if [list(x) for x in fingerprint(todo)] != [list(x) for x in m["fingerprint"]]:
        raise SystemExit("rows changed since the dry run; nothing written. Run a new dry run.")
    keys = [r["s3_key"] for r in todo]
    done = 0
    for i in range(0, len(keys), PAGE):
        chunk = keys[i:i + PAGE]
        n = db.execute("UPDATE lake_objects SET company_id = CAST(:c AS uuid), "
                       "user_id = CAST(:u AS uuid), reason = :r, updated_at = now() "
                       "WHERE folder = :f AND s3_key IN "
                       "(SELECT jsonb_array_elements_text(CAST(:keys AS jsonb)))",
                       c=tgt["company_id"], u=tgt["id"], r=OPERATOR, f=args.from_folder, keys=chunk)
        log("update_chunk", first=chunk[0], last=chunk[-1], expected=len(chunk), updated=n)
        if n != len(chunk):
            raise SystemExit(f"updated {n} rows, expected {len(chunk)}; rolled back")
        done += n
    db.commit()
    log("committed", updated=done)
    return {"updated": done}


def run_rollback(db, args, log):
    m = common.read_json(args.rollback)
    if m.get("op") != "reassign_folder" or m.get("database") != args.database:
        raise SystemExit("snapshot does not match --database")
    before = [{"s3_key": r["s3_key"], "company_id": r.get("company_id"),
               "user_id": r.get("user_id"), "reason": r.get("reason")} for r in m["before"]]
    restorable = 0
    for i in range(0, len(before), PAGE):
        chunk = before[i:i + PAGE]
        restorable += db.query("SELECT count(*) AS n FROM lake_objects WHERE reason = :r AND s3_key IN "
                               "(SELECT jsonb_array_elements_text(CAST(:keys AS jsonb)))",
                               r=OPERATOR, keys=[c["s3_key"] for c in chunk])[0]["n"]
    log("rollback_plan", snapshot_rows=len(before), still_operator=restorable)
    if not args.apply:
        return {"would_restore": restorable, "snapshot_rows": len(before)}
    done = 0
    for i in range(0, len(before), PAGE):
        chunk = before[i:i + PAGE]
        done += db.execute(
            "UPDATE lake_objects lo SET company_id = CAST(s.company_id AS uuid), "
            "user_id = CAST(s.user_id AS uuid), reason = s.reason, updated_at = now() "
            "FROM jsonb_to_recordset(CAST(:rows AS jsonb)) "
            "AS s(s3_key text, company_id text, user_id text, reason text) "
            "WHERE lo.s3_key = s.s3_key AND lo.reason = :r", rows=chunk, r=OPERATOR)
    if done != restorable:
        raise SystemExit(f"restored {done}, expected {restorable}; rolled back")
    db.commit()
    log("rollback_committed", restored=done)
    return {"restored": done}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default=common.TEST_DATABASE)
    ap.add_argument("--allow-prod", action="store_true")
    ap.add_argument("--profile", default=common.DEFAULT_PROFILE)
    ap.add_argument("--region", default=common.DEFAULT_REGION)
    ap.add_argument("--from-folder")
    ap.add_argument("--to-folder")
    ap.add_argument("--expect-company")
    ap.add_argument("--manifest-out", help="dry run: where to write the manifest (= undo snapshot)")
    ap.add_argument("--manifest", help="--apply: the dry run's manifest")
    ap.add_argument("--rollback", help="restore the `before` rows of this manifest")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--log", help="JSONL action log (appended)")
    args = ap.parse_args(argv)
    if common.refuse_prod_without_flag(args.database, args.allow_prod):
        return 2
    log = common.Log(args.log)
    if args.rollback:
        mode = "rollback-apply" if args.apply else "rollback-dry"
    else:
        if not (args.from_folder and args.to_folder and args.expect_company):
            ap.error("--from-folder, --to-folder and --expect-company are required")
        common.validate_folders([args.from_folder, args.to_folder])
        if args.apply and not args.manifest:
            ap.error("--apply needs --manifest (the dry run's output)")
        if not args.apply and not args.manifest_out:
            ap.error("a dry run needs --manifest-out")
        mode = "apply" if args.apply else "dry"
    log("start", mode=mode, database=args.database)
    db = common.Db(args.database, read_only=not args.apply, profile=args.profile,
                   region=args.region, log=log)
    try:
        if args.rollback:
            out = run_rollback(db, args, log)
        elif args.apply:
            out = run_apply(db, args, log)
        else:
            m = run_dry(db, args, log)
            common.write_json(args.manifest_out, m)
            out = {"summary": m["summary"], "target": m["target"],
                   "changes": len(m["before"]), "manifest": args.manifest_out,
                   "rows_with_folder_segment_but_other_folder_column":
                       m["rows_with_folder_segment_but_other_folder_column"]}
    finally:
        db.rollback()  # no-op after a commit
    import json
    print(json.dumps(out, indent=2, default=str))
    if not args.apply:
        print("DRY RUN: nothing was written (read-only transaction, rolled back).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
