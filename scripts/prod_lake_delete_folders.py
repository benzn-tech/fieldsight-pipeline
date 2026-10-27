"""Delete everything that belongs to orphan folders: S3 objects plus the Aurora rows that point
at them.

OWNER DECISION 2026-10-27 (B): delete folders `MPI1`, `Benl1`, `Ben_UCPK_` (none has a users
row). `Ben_UCPK` (no trailing underscore, the real Southbase account) is a different folder and
is never matched.

SCOPE RULE: a key is in scope only when one of its FOLDER segments equals a folder exactly
(lake_ops_common.key_in_scope). A file NAME that merely contains `Benl1` under someone else's
folder is listed under `excluded_name_only` and never touched.

INVENTORY (the dry run, a READ ONLY transaction + S3/DynamoDB reads only):
  * S3: the bucket's top-level prefixes are listed, then every prefix in full; current objects
    in scope, plus every version and delete marker of an in-scope key (the bucket is
    versioned and has no noncurrent-version expiry, so old versions otherwise stay forever).
  * Aurora: lake_objects (folder or key), recordings.s3_key, topics.source_s3_key (+ the rows
    that cascade from them), report_chunks (by key or by an in-scope topic; its FK to topics
    is SET NULL, so they are deleted explicitly BEFORE the topics), report_chunks_archive.
    Then a sweep of EVERY text/json column of every table for the same folder rule: a match
    the script does not handle is `unhandled` and blocks --apply.
  * Ownership: a recordings row whose user exists, or a lake_objects row with a company or a
    user, means the "orphan" folder holds data attributed to a live account. Such a folder is
    `owned` and blocks --apply unless --allow-owned.
  * DynamoDB legacy tables: matches are listed, never deleted (report only).

--apply needs the dry run's manifest, re-runs the inventory and aborts on any difference, then
deletes S3 objects (default: current versions only -> delete markers, recoverable; with
--purge-versions: every version and delete marker, NOT recoverable), then in ONE transaction:
report_chunks, report_chunks_archive, topics (children cascade), recordings, and finally marks
lake_objects rows deleted_at = now() (the index's tombstone; every reader filters on it).
--skip-s3 resumes the database step after an S3 step that succeeded.
"""
import argparse
import json
import sys

try:
    from scripts import lake_ops_common as common
except ImportError:  # run as `python scripts/prod_lake_delete_folders.py`
    import lake_ops_common as common

PAGE = 500
MAX_MANIFEST_AGE_H = 24
DYNAMO = {common.PROD_DATABASE: ["fieldsight-transcripts", "fieldsight-items",
                                 "fieldsight-reports", "fieldsight-users"],
          common.TEST_DATABASE: ["fieldsight-test-transcripts", "fieldsight-test-items",
                                 "fieldsight-test-reports", "fieldsight-users-test"]}
# (table, column) pairs this script deletes by; any other sweep hit is `unhandled`.
HANDLED = {("lake_objects", "s3_key"), ("lake_objects", "folder"), ("recordings", "s3_key"),
           ("topics", "source_s3_key"), ("report_chunks", "source_s3_key"),
           ("report_chunks_archive", "source_s3_key")}
TOPIC_CHILDREN = ["action_items", "findings", "safety_observations", "topic_decisions",
                  "topic_questions", "topic_photos", "programme_progress_suggestions"]


# ---- pure logic (unit-tested) ------------------------------------------------------------

def folder_of(key, folders):
    """The first in-scope folder segment of a key, or None."""
    for seg in common.folder_segments(key):
        if seg in folders:
            return seg
    return None


def classify_s3(keys_sizes, folders):
    """[(key, size, etag)] -> (in_scope[{key,size,etag,folder,prefix}], excluded_name_only[key])."""
    inside, excluded = [], []
    for k, size, etag in keys_sizes:
        f = folder_of(k, folders)
        if f is not None:
            inside.append({"key": k, "size": size, "etag": etag, "folder": f,
                           "prefix": k.split("/", 1)[0] + "/"})
        elif common.name_only_match(k, folders):
            excluded.append(k)
    return inside, excluded


def owned_by_folder(recordings, lake_rows, folders):
    """Rows of an in-scope folder attributed to a live account: {folder: {...counts}}."""
    out = {}
    for r in recordings:
        if r.get("user_exists"):
            f = folder_of(r["s3_key"], folders)
            out.setdefault(f, {"recordings_with_live_user": 0, "lake_rows_with_owner": 0})
            out[f]["recordings_with_live_user"] += 1
    for r in lake_rows:
        if r.get("company_id") or r.get("user_id"):
            f = r.get("folder") if r.get("folder") in folders else folder_of(r["s3_key"], folders)
            out.setdefault(f, {"recordings_with_live_user": 0, "lake_rows_with_owner": 0})
            out[f]["lake_rows_with_owner"] += 1
    return out


def unhandled_hits(sweep, extra_ok=()):
    ok = HANDLED | set(extra_ok)
    return [h for h in sweep if (h["table"], h["column"]) not in ok]


def blockers(inv, allow_owned):
    out = []
    if inv["db"]["users_rows"]:
        out.append(f"folders have users rows: {inv['db']['users_rows']}")
    if inv["db"]["unhandled"]:
        out.append(f"unhandled references: {inv['db']['unhandled']}")
    if inv["owned"] and not allow_owned:
        out.append(f"folders hold data attributed to live accounts (needs --allow-owned): "
                   f"{inv['owned']}")
    return out


def comparable(inv, purge_versions):
    """The parts of an inventory that must be identical between the dry run and --apply."""
    db = inv["db"]
    c = {"s3": sorted((o["key"], o["etag"]) for o in inv["s3"]["objects"]),
         "lake": sorted(r["s3_key"] for r in db["lake_objects"]),
         "recordings": sorted(r["id"] for r in db["recordings"]),
         "topics": sorted(r["id"] for r in db["topics"]),
         "report_chunks": sorted(db["report_chunks"]),
         "report_chunks_archive": sorted(db["report_chunks_archive"]),
         "unhandled": db["unhandled"], "users_rows": db["users_rows"]}
    if purge_versions:
        c["versions"] = sorted((v["key"], v["version_id"]) for v in inv["s3"]["versions"])
    return c


def diff(a, b):
    return [k for k in sorted(set(a) | set(b)) if json.dumps(a.get(k), default=str) !=
            json.dumps(b.get(k), default=str)]


def s3_summary(objects, versions, excluded):
    per = {}
    for o in objects:
        p = per.setdefault(f"{o['prefix']}{o['folder']}", {"objects": 0, "bytes": 0})
        p["objects"] += 1
        p["bytes"] += int(o["size"] or 0)
    nonc = [v for v in versions if not v["is_latest"] and not v["delete_marker"]]
    return {"per_prefix_folder": dict(sorted(per.items())),
            "objects": len(objects), "bytes": sum(int(o["size"] or 0) for o in objects),
            "versions_total": len(versions),
            "noncurrent_versions": len(nonc),
            "noncurrent_bytes": sum(int(v["size"] or 0) for v in nonc),
            "delete_markers": sum(1 for v in versions if v["delete_marker"]),
            "keys_with_only_old_versions": len({v["key"] for v in versions} -
                                               {o["key"] for o in objects}),
            "excluded_name_only": len(excluded)}


# ---- inventory ---------------------------------------------------------------------------

def list_s3(s3, bucket, folders):
    first = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    tops = [p["Prefix"] for p in first.get("CommonPrefixes", [])]
    rows = [(o["Key"], o["Size"], o.get("ETag")) for o in first.get("Contents", [])]
    for t in tops:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=t):
            rows += [(o["Key"], o["Size"], o.get("ETag")) for o in page.get("Contents", [])]
    objects, excluded = classify_s3(rows, folders)
    versions = []
    for t in tops + [""] * (1 if first.get("Contents") else 0):
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=t):
            for v in page.get("Versions", []):
                if t == "" and "/" in v["Key"]:
                    continue
                if folder_of(v["Key"], folders):
                    versions.append({"key": v["Key"], "version_id": v["VersionId"],
                                     "is_latest": v["IsLatest"], "delete_marker": False,
                                     "size": v.get("Size")})
            for v in page.get("DeleteMarkers", []):
                if t == "" and "/" in v["Key"]:
                    continue
                if folder_of(v["Key"], folders):
                    versions.append({"key": v["Key"], "version_id": v["VersionId"],
                                     "is_latest": v["IsLatest"], "delete_marker": True,
                                     "size": 0})
    return {"top_level_prefixes": tops, "listed": len(rows), "objects": objects,
            "excluded_name_only": excluded, "versions": versions,
            "summary": s3_summary(objects, versions, excluded)}


def _paged(db, sql, **params):
    """Keyset pages: `sql` filters on `> :after`, orders by and returns its key as `k`
    (the Data API caps one response at 1 MB)."""
    rows, after = [], ""
    while True:
        page = db.query(sql, after=after, **params)
        rows += page
        if len(page) < PAGE:
            return rows
        after = page[-1]["k"]


def inventory_db(db, folders, lock=False):
    rx = common.folder_segment_regex(folders)
    fj = json.dumps(folders)
    fu = " FOR UPDATE" if lock else ""
    inv = {"regex": rx}
    inv["users_rows"] = db.query(
        "SELECT folder_name FROM users WHERE folder_name IN "
        "(SELECT jsonb_array_elements_text(CAST(:f AS jsonb)))", f=fj)
    inv["lake_objects"] = _paged(
        db, "SELECT s3_key AS k, s3_key, folder, kind, size_bytes, company_id::text AS company_id, "
            "user_id::text AS user_id, reason, updated_at::text AS updated_at FROM lake_objects "
            "WHERE deleted_at IS NULL AND (s3_key ~ :rx OR folder IN "
            "(SELECT jsonb_array_elements_text(CAST(:f AS jsonb)))) AND s3_key > :after "
            f"ORDER BY s3_key LIMIT {PAGE}{fu}", rx=rx, f=fj)
    inv["recordings"] = db.query(
        "SELECT r.id::text AS id, r.s3_key, r.user_id::text AS user_id, u.folder_name AS user_folder, "
        "r.company_id::text AS company_id, r.site_id::text AS site_id, (u.id IS NOT NULL) AS user_exists "
        "FROM recordings r LEFT JOIN users u ON u.id = r.user_id WHERE r.s3_key ~ :rx" +
        (" FOR UPDATE OF r" if lock else ""), rx=rx)
    inv["topics"] = db.query(
        "SELECT t.id::text AS id, t.source_s3_key, t.site_id::text AS site_id, s.name AS site, "
        "c.name AS company, t.report_date::text AS report_date FROM topics t "
        "LEFT JOIN sites s ON s.id = t.site_id LEFT JOIN companies c ON c.id = s.company_id "
        "WHERE t.source_s3_key ~ :rx" + (" FOR UPDATE OF t" if lock else ""), rx=rx)
    tids = json.dumps([t["id"] for t in inv["topics"]])
    inv["topic_children"] = {
        t: db.query(f"SELECT count(*) AS n FROM {t} WHERE topic_id IN "
                    "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))",
                    ids=tids)[0]["n"] for t in TOPIC_CHILDREN}
    inv["topic_children"]["topic_thread_suggestions"] = db.query(
        "SELECT count(*) AS n FROM topic_thread_suggestions WHERE topic_id IN "
        "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid)) OR parent_topic_id IN "
        "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))", ids=tids)[0]["n"]
    inv["report_chunks"] = [r["id"] for r in db.query(
        "SELECT id::text AS id FROM report_chunks WHERE source_s3_key ~ :rx OR topic_id IN "
        "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))" + fu,
        rx=rx, ids=tids)]
    inv["report_chunks_archive"] = [r["id"] for r in db.query(
        "SELECT id::text AS id FROM report_chunks_archive WHERE source_s3_key ~ :rx" + fu, rx=rx)]
    inv["decision_records_soft_refs"] = db.query(
        "SELECT count(*) AS n FROM decision_records WHERE input_key ~ :rx OR object_ref ~ :rx "
        "OR CAST(subject_stable_id AS text) IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
        rx=rx, ids=tids)[0]["n"]
    inv["sweep"] = sweep(db, rx, folders)
    # action_items.responsible = 'MPI1' is a person label, not a key: fine when every such row
    # is a child of an in-scope topic (it cascades with it).
    extra = []
    stray_resp = db.query(
        "SELECT count(*) AS n FROM action_items WHERE responsible IN "
        "(SELECT jsonb_array_elements_text(CAST(:f AS jsonb))) AND (topic_id IS NULL OR topic_id NOT IN "
        "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid)))", f=fj, ids=tids)[0]["n"]
    if stray_resp == 0:
        extra.append(("action_items", "responsible"))
    inv["unhandled"] = unhandled_hits(inv["sweep"], extra)
    return inv


def sweep(db, rx, folders):
    """Every text/json column of every base table, matched by the folder rule (a folder
    segment of a path, or the whole value equal to a folder)."""
    cols = db.query(
        "SELECT c.table_name AS t, c.column_name AS c FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
        "AND c.data_type IN ('text', 'character varying', 'jsonb', 'json', 'ARRAY') ORDER BY 1, 2")
    hits = []
    for col in cols:
        t, c = col["t"], col["c"]
        n = db.query(f'SELECT count(*) AS n FROM "{t}" WHERE CAST("{c}" AS text) ~ :rx OR '
                     f'CAST("{c}" AS text) IN (SELECT jsonb_array_elements_text(CAST(:f AS jsonb)))',
                     rx=rx, f=json.dumps(folders))[0]["n"]
        if n:
            hits.append({"table": t, "column": c, "rows": n})
    return hits


def inventory_dynamo(ddb, tables, folders):
    import re
    rx = re.compile(r"(^|[/#\"])(" + "|".join(re.escape(f) for f in folders) + r")([/#\"]|$)")
    out = {}
    for t in tables:
        try:
            hits = []
            for page in ddb.get_paginator("scan").paginate(TableName=t):
                for it in page["Items"]:
                    if rx.search(json.dumps(it)):
                        hits.append({"PK": it.get("PK", {}).get("S"), "SK": it.get("SK", {}).get("S")})
            out[t] = {"items": len(hits), "keys": hits}
        except Exception as exc:  # noqa: BLE001 - report only
            out[t] = {"error": str(exc)[:200]}
    return out


def inventory(db, s3, ddb, bucket, folders, dynamo_tables, lock=False):
    d = inventory_db(db, folders, lock=lock)
    s = list_s3(s3, bucket, folders)
    return {"db": d, "s3": s, "owned": owned_by_folder(d["recordings"], d["lake_objects"], folders),
            "dynamo_report_only": inventory_dynamo(ddb, dynamo_tables, folders) if ddb else {}}


def db_summary(d):
    return {"lake_objects_live_rows": len(d["lake_objects"]),
            "lake_objects_bytes": sum(int(r.get("size_bytes") or 0) for r in d["lake_objects"]),
            "recordings": len(d["recordings"]),
            "recordings_with_live_user": [
                {"id": r["id"], "s3_key": r["s3_key"], "user_folder": r["user_folder"]}
                for r in d["recordings"] if r["user_exists"]],
            "topics": len(d["topics"]),
            "topics_by_site": _count(d["topics"], lambda t: f"{t['company']} / {t['site']}"),
            "topic_children_cascade": d["topic_children"],
            "report_chunks": len(d["report_chunks"]),
            "report_chunks_archive": len(d["report_chunks_archive"]),
            "decision_records_soft_refs": d["decision_records_soft_refs"],
            "sweep": d["sweep"], "unhandled": d["unhandled"], "users_rows": d["users_rows"]}


def _count(rows, key):
    out = {}
    for r in rows:
        out[key(r)] = out.get(key(r), 0) + 1
    return out


# ---- apply -------------------------------------------------------------------------------

def delete_s3(s3, bucket, inv, purge_versions, log):
    if purge_versions:
        targets = [{"Key": v["key"], "VersionId": v["version_id"]} for v in inv["s3"]["versions"]]
    else:
        targets = [{"Key": o["key"]} for o in inv["s3"]["objects"]]
    deleted = 0
    for i in range(0, len(targets), 1000):
        chunk = targets[i:i + 1000]
        resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": False})
        for d in resp.get("Deleted", []):
            log("s3_deleted", key=d["Key"], version_id=d.get("VersionId"),
                delete_marker_created=d.get("DeleteMarker"),
                delete_marker_version=d.get("DeleteMarkerVersionId"))
        deleted += len(resp.get("Deleted", []))
        errs = resp.get("Errors", [])
        if errs:
            for e in errs:
                log("s3_error", key=e.get("Key"), code=e.get("Code"), message=e.get("Message"))
            raise SystemExit(f"{len(errs)} S3 delete error(s); database NOT touched. "
                             "Fix and re-run (a new dry run first).")
    return deleted


def delete_db(db, inv, log):
    d = inv["db"]
    steps = [
        ("report_chunks", "DELETE FROM report_chunks WHERE id IN "
         "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))", d["report_chunks"]),
        ("report_chunks_archive", "DELETE FROM report_chunks_archive WHERE id IN "
         "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))",
         d["report_chunks_archive"]),
        ("topics", "DELETE FROM topics WHERE id IN "
         "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))",
         [t["id"] for t in d["topics"]]),
        ("recordings", "DELETE FROM recordings WHERE id IN "
         "(SELECT CAST(jsonb_array_elements_text(CAST(:ids AS jsonb)) AS uuid))",
         [r["id"] for r in d["recordings"]]),
        ("lake_objects(deleted_at)", "UPDATE lake_objects SET deleted_at = now(), updated_at = now() "
         "WHERE deleted_at IS NULL AND s3_key IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb)))",
         [r["s3_key"] for r in d["lake_objects"]]),
    ]
    out = {}
    for name, sql, ids in steps:
        n = 0
        for i in range(0, len(ids), PAGE):
            n += db.execute(sql, ids=ids[i:i + PAGE])
        log("db_step", table=name, expected=len(ids), affected=n)
        if n != len(ids):
            raise SystemExit(f"{name}: affected {n}, expected {len(ids)}; rolled back")
        out[name] = n
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database", default=common.TEST_DATABASE)
    ap.add_argument("--allow-prod", action="store_true")
    ap.add_argument("--profile", default=common.DEFAULT_PROFILE)
    ap.add_argument("--region", default=common.DEFAULT_REGION)
    ap.add_argument("--folders", required=True, help="comma-separated exact folder names")
    ap.add_argument("--bucket", help="default: the data bucket of --database")
    ap.add_argument("--manifest-out", help="dry run: where to write the manifest")
    ap.add_argument("--manifest", help="--apply: the dry run's manifest")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--purge-versions", action="store_true",
                    help="delete every S3 version and delete marker (NOT recoverable)")
    ap.add_argument("--allow-owned", action="store_true",
                    help="delete even data attributed to a live account")
    ap.add_argument("--skip-s3", action="store_true",
                    help="--apply: database step only (after an S3 step that succeeded)")
    ap.add_argument("--no-dynamo", action="store_true")
    ap.add_argument("--log", help="JSONL action log (appended)")
    args = ap.parse_args(argv)
    if common.refuse_prod_without_flag(args.database, args.allow_prod):
        return 2
    folders = common.validate_folders([f for f in args.folders.split(",")])
    bucket = args.bucket or common.BUCKETS[args.database]
    if args.apply and not args.manifest:
        ap.error("--apply needs --manifest (the dry run's output)")
    if not args.apply and not args.manifest_out:
        ap.error("a dry run needs --manifest-out")
    log = common.Log(args.log)
    log("start", mode="apply" if args.apply else "dry", database=args.database, bucket=bucket,
        folders=",".join(folders), purge_versions=args.purge_versions, skip_s3=args.skip_s3)

    import boto3
    sess = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = sess.client("s3")
    ddb = None if args.no_dynamo else sess.client("dynamodb")
    tables = DYNAMO.get(args.database, [])

    if not args.apply:
        db = common.Db(args.database, read_only=True, profile=args.profile, region=args.region)
        try:
            inv = inventory(db, s3, ddb, bucket, folders, tables)
        finally:
            db.rollback()
        manifest = {"op": "delete_folders", "created_at": common.now_iso(),
                    "database": args.database, "bucket": bucket, "folders": folders, **inv}
        common.write_json(args.manifest_out, manifest)
        out = {"s3": inv["s3"]["summary"], "db": db_summary(inv["db"]), "owned": inv["owned"],
               "dynamo_report_only": {t: v.get("items", v) for t, v in
                                      inv["dynamo_report_only"].items()},
               "blockers_without_allow_owned": blockers(inv, False),
               "manifest": args.manifest_out}
        print(json.dumps(out, indent=2, default=str))
        print("DRY RUN: nothing was written (read-only transaction, rolled back; S3 listed only).",
              file=sys.stderr)
        return 0

    m = common.read_json(args.manifest)
    if (m.get("op"), m.get("database"), m.get("bucket"), m.get("folders")) != \
            ("delete_folders", args.database, bucket, folders):
        raise SystemExit("manifest does not match --database/--bucket/--folders")
    import datetime
    age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(m["created_at"])
    if age.total_seconds() > MAX_MANIFEST_AGE_H * 3600:
        raise SystemExit(f"manifest older than {MAX_MANIFEST_AGE_H}h; run a new dry run")
    b = blockers(m, args.allow_owned)
    if b:
        raise SystemExit("refusing: " + "; ".join(b))

    # 1) re-check S3 + DB against the manifest (read only), then delete S3
    if not args.skip_s3:
        ro = common.Db(args.database, read_only=True, profile=args.profile, region=args.region)
        try:
            now_inv = inventory(ro, s3, None, bucket, folders, [])
        finally:
            ro.rollback()
        changed = diff(comparable(m, args.purge_versions), comparable(now_inv, args.purge_versions))
        if changed:
            raise SystemExit(f"changed since the dry run: {changed}; nothing deleted")
        n = delete_s3(s3, bucket, m, args.purge_versions, log)
        log("s3_done", deleted=n, purge_versions=args.purge_versions)

    # 2) database, one transaction, rows re-read under lock and compared to the manifest
    db = common.Db(args.database, read_only=False, profile=args.profile, region=args.region)
    try:
        d = inventory_db(db, folders, lock=True)
        mine, theirs = comparable({"db": m["db"], "s3": {"objects": []}}, False), \
            comparable({"db": d, "s3": {"objects": []}}, False)
        changed = diff(mine, theirs)
        if changed:
            raise SystemExit(f"database changed since the dry run: {changed}; rolled back")
        out = delete_db(db, m, log)
        db.commit()
        log("db_committed", **out)
    finally:
        db.rollback()
    print(json.dumps({"db": out, "s3_purged_versions": args.purge_versions}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
