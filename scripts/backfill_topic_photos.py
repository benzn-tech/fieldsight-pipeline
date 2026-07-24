"""One-off operational backfill: bind historical photos to existing Aurora
topics, INSERT-ONLY.

Why this exists
---------------
`topic_photos` binding shipped with a bounded ±PHOTO_TOLERANCE_MIN rule
(`src/photo_binding.py::photos_for_topics`), but a photo is only ever bound
at item-write time. Days extracted BEFORE that rule shipped kept their photos
unbound when they missed a topic window by 1-2 minutes (e.g. Ben_UCPK /
2026-07-23: a 10:40 photo vs a "10:39 - 10:39" window). This script
retroactively binds those photos.

Safety (see docs/superpowers/plans/2026-07-23-prod-media-binding-fixes.md,
Phase 6 / Backfill):

  * INSERT-ONLY. The script only ever runs an INSERT into `topic_photos`,
    guarded by NOT EXISTS (topic_id, s3_key). It NEVER deletes, NEVER
    updates, NEVER re-inserts topics, NEVER re-runs extraction. Re-triggering
    extraction is the rejected-unsafe path: it cascade-deletes topic_photos
    and mints NEW topic ids, which silently un-redacts `redactions` and
    reverts `content_edits` (those audit tables have no FK). This script
    touches topics / redactions / content_edits / action_items NOT AT ALL.

  * Idempotent, PHOTO-LEVEL (NOT day-level). For each (folder, date) the set
    of photo s3_keys ALREADY bound that day is queried and EXCLUDED from the
    photos fed to the matcher. An already-bound photo is therefore never
    re-matched (it can never be double-bound to a different topic), while an
    unbound photo on a PARTIALLY-bound day still gets its chance -- the exact
    Ben_UCPK / 2026-07-23 case (12:15 + 12:16 already bound inside their
    windows, 10:40 left unbound 1 min outside "10:39 - 10:39") that the
    ±2 min rule exists to rescue. A whole-day skip would strand that 10:40
    photo forever, defeating the backfill's purpose. Each insert is ALSO
    NOT-EXISTS-guarded on (topic_id, s3_key) (the Data-API restatement of
    repositories.topics.add_topic_photo_if_absent) as belt-and-suspenders;
    re-running is a no-op.

  * Dry-run by DEFAULT. Without --apply the script only PRINTS what it WOULD
    insert and a summary; it performs no writes. --apply performs the inserts.

The matching rule is NOT reimplemented here: photo->topic assignment is done
verbatim by photo_binding.photos_for_topics, and the S3 pictures listing by
photo_binding.list_pictures. Topic ordering within a day mirrors the org-api
timeline shim's deterministic D3 order (time_range NULLS LAST, created_at, id)
so a photo binds to the same topic on every run.

Usage:
  # dry run (default) -- prints what WOULD be inserted, writes nothing
  uv run python scripts/backfill_topic_photos.py --database fieldsight

  # actually insert
  uv run python scripts/backfill_topic_photos.py --database fieldsight --apply

  # scope down
  uv run python scripts/backfill_topic_photos.py --folder Ben_UCPK --date 2026-07-23
  uv run python scripts/backfill_topic_photos.py --date-from 2026-07-01 --date-to 2026-07-23

Connectivity: Aurora Data API (boto3 rds-data) + S3, both via the
`fieldsight-deployer` profile in ap-southeast-2 -- no VPC path needed.
"""
import argparse
import datetime as _dt
import json
import os
import sys

# Reuse the shared matcher + pictures lister VERBATIM (src is on sys.path under
# pytest via pyproject `pythonpath=["src"]`; add it for standalone runs too).
_SRC = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from photo_binding import (  # noqa: E402  (path set up above)
    _distance,
    _hhmm_to_minutes,
    list_pictures,
    parse_time_range,
    photos_for_topics,
)

# ---------------------------------------------------------------------------
# Fixed prod wiring (overridable by flags where the plan calls for it).
# ---------------------------------------------------------------------------
DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"
DEFAULT_CLUSTER_ARN = (
    "arn:aws:rds:ap-southeast-2:509194952652:cluster:"
    "fieldsight-db-test-dbcluster-hywiixu8ihi9"
)
DEFAULT_SECRET_ARN = (
    "arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
    "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey"
)
DEFAULT_DATABASE = "fieldsight"
DEFAULT_BUCKET = "fieldsight-data-509194952652"

# INSERT-ONLY, NOT-EXISTS-guarded on (topic_id, s3_key). This is the Aurora
# Data-API restatement of repositories.topics.add_topic_photo_if_absent -- the
# ONLY statement this script ever writes. There is deliberately no DELETE /
# UPDATE / re-extract anywhere in this file.
INSERT_TOPIC_PHOTO_SQL = (
    "INSERT INTO topic_photos (topic_id, s3_key, caption_text) "
    "SELECT :tid::uuid, :key, :cap "
    "WHERE NOT EXISTS ("
    "  SELECT 1 FROM topic_photos WHERE topic_id = :tid::uuid AND s3_key = :key"
    ") RETURNING id"
)

# All topics (id + time_range). Folder is derived in Python (below); ordering
# is the shim's deterministic D3 order so grouping preserves per-day topic
# order for photos_for_topics' index semantics.
DISCOVER_TOPICS_SQL_HEAD = (
    "SELECT t.id::text AS id, "
    "       t.source_s3_key AS source_s3_key, "
    "       t.report_date::text AS report_date, "
    "       t.time_range AS time_range "
    "FROM topics t "
)
DISCOVER_TOPICS_SQL_TAIL = " ORDER BY t.report_date, t.time_range NULLS LAST, t.created_at, t.id"

# READ-ONLY. The photo s3_keys already bound to any topic of one (folder,
# date) -- fed to the matcher as an exclusion set so already-bound photos are
# never re-matched (photo-level idempotency). No write.
#
# The (folder, date) selection MIRRORS plan_candidates' grouping EXACTLY: the
# report_date column plus the SAME source_s3_key folder derivation as
# derive_folder / list_contributor_folders_for_site_date (extractions/ -> path
# part 2, reports/ -> path part 3, via split_part). That keeps the excluded-
# topic set identical to the discovery-grouped topic set. A per-topic-id scope
# would be equivalent, but the AWS RDS Data API does NOT support array-typed
# parameters (ValidationException: Array parameters are not supported), so
# day-scoped SCALAR params (:date, :folder) are used instead. The literal '%'
# needs no doubling: the Data API uses named params, not psycopg %-paramstyle.
BOUND_PHOTO_KEYS_SQL = (
    "SELECT DISTINCT tp.s3_key AS s3_key "
    "FROM topic_photos tp JOIN topics t ON t.id = tp.topic_id "
    "WHERE t.report_date = :date::date AND "
    "  CASE WHEN t.source_s3_key LIKE 'extractions/%' "
    "         THEN split_part(t.source_s3_key, '/', 2) "
    "       WHEN t.source_s3_key LIKE 'reports/%' "
    "         THEN split_part(t.source_s3_key, '/', 3) "
    "  END = :folder"
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested) -- no AWS, no I/O.
# ---------------------------------------------------------------------------
def derive_folder(source_s3_key):
    """The owner recording-folder encoded in a topic's source_s3_key, EXACTLY
    like repositories.topics.list_contributor_folders_for_site_date:
      extractions/{folder}/{date}/...  -> path part index 1
      reports/{date}/{folder}/...      -> path part index 2
    Anything else -> None (excluded from the backfill)."""
    if not source_s3_key:
        return None
    parts = source_s3_key.split("/")
    if source_s3_key.startswith("extractions/") and len(parts) > 1:
        return parts[1] or None
    if source_s3_key.startswith("reports/") and len(parts) > 2:
        return parts[2] or None
    return None


def plan_candidates(topic_rows, folder=None):
    """Group topic rows into per-(folder, date) candidates for backfill.

    EVERY (folder, date) with topics is a candidate -- there is deliberately NO
    day-level skip. Idempotency is enforced per-PHOTO downstream (already-bound
    s3_keys are excluded from the matcher), so a partially-bound day still has
    its unbound photos rescued. The input order is preserved, so each
    candidate's `topics` list stays in the deterministic order the discovery
    query imposed (time_range NULLS LAST, created_at, id) -- exactly the index
    order photos_for_topics relies on for stable tie-breaking.

    Returns (candidates, stats):
      candidates: [{folder, date, topics: [{id, time_range}, ...]}, ...]
      stats: {"days_total"}
    """
    groups = {}          # (folder, date) -> list[{id, time_range}]
    for row in topic_rows:
        f = derive_folder(row.get("source_s3_key"))
        if f is None:
            continue
        if folder is not None and f != folder:
            continue
        key = (f, row.get("report_date"))
        groups.setdefault(key, []).append(
            {"id": row.get("id"), "time_range": row.get("time_range")}
        )

    candidates = [{"folder": k[0], "date": k[1], "topics": t}
                  for k, t in groups.items()]
    return candidates, {"days_total": len(groups)}


def plan_bindings(topics_list, photo_objects):
    """Compute the photo->topic bindings for one day WITHOUT touching the DB.

    Delegates the whole matching decision to photo_binding.photos_for_topics
    (verbatim); this only reshapes the {topic_index: [photos]} result into flat
    (topic_id, s3_key, caption, distance) records for printing/inserting. The
    caption mirrors lambda_item_writer's rule (synthetic keyframes carry an
    "Auto keyframe" caption; real photos are caption-less)."""
    assignment = photos_for_topics(photo_objects, topics_list)
    bindings = []
    for idx, photos in assignment.items():
        if not photos:
            continue
        topic = topics_list[idx]
        window = parse_time_range(topic.get("time_range"))
        for p in photos:
            distance = None
            if window is not None and p.get("hhmm"):
                distance = _distance(_hhmm_to_minutes(p["hhmm"]), window)
            bindings.append({
                "topic_id": topic["id"],
                "topic_time_range": topic.get("time_range"),
                "s3_key": p["key"],
                "caption": "Auto keyframe" if "_kf_" in p["filename"] else None,
                "distance_min": distance,
            })
    return bindings


# ---------------------------------------------------------------------------
# Data API + S3 seams (injectable for tests).
# ---------------------------------------------------------------------------
def make_data_api_executor(profile, region, cluster_arn, secret_arn, database):
    """Return execute(sql, params=None, json_records=False) bound to the Aurora
    Data API. boto3 is imported lazily so importing this module (for unit
    tests) never requires AWS creds or the network."""
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("rds-data")

    def execute(sql, params=None, json_records=False):
        kwargs = {
            "resourceArn": cluster_arn,
            "secretArn": secret_arn,
            "database": database,
            "sql": sql,
        }
        if params:
            kwargs["parameters"] = params
        if json_records:
            kwargs["formatRecordsAs"] = "JSON"
        return client.execute_statement(**kwargs)

    return execute


def fetch_topic_rows(execute, date=None, date_from=None, date_to=None):
    """Run the discovery SELECT and return a list of plain dict rows."""
    where = []
    params = []
    if date:
        where.append("t.report_date = :d::date")
        params.append({"name": "d", "value": {"stringValue": date}})
    if date_from:
        where.append("t.report_date >= :df::date")
        params.append({"name": "df", "value": {"stringValue": date_from}})
    if date_to:
        where.append("t.report_date <= :dt::date")
        params.append({"name": "dt", "value": {"stringValue": date_to}})
    sql = DISCOVER_TOPICS_SQL_HEAD
    if where:
        sql += "WHERE " + " AND ".join(where)
    sql += DISCOVER_TOPICS_SQL_TAIL
    resp = execute(sql, params=params or None, json_records=True)
    formatted = resp.get("formattedRecords") or "[]"
    return json.loads(formatted)


def fetch_bound_photo_keys(execute, folder, date):
    """READ-ONLY. Return the set of photo s3_keys already bound to any topic of
    this (folder, date) -- the photo-level idempotency exclusion set.

    The (folder, date) is selected by BOUND_PHOTO_KEYS_SQL using the report_date
    column + the same source_s3_key folder derivation plan_candidates groups by,
    so the excluded-topic set == the candidate's grouped-topic set. SCALAR
    params only (the Data API rejects array params). Missing folder/date -> no
    round-trip, empty set."""
    if not folder or not date:
        return set()
    params = [
        {"name": "date", "value": {"stringValue": date}},
        {"name": "folder", "value": {"stringValue": folder}},
    ]
    resp = execute(BOUND_PHOTO_KEYS_SQL, params=params, json_records=True)
    rows = json.loads(resp.get("formattedRecords") or "[]")
    return {r["s3_key"] for r in rows if r.get("s3_key")}


def insert_topic_photo_if_absent(execute, topic_id, s3_key, caption_text):
    """INSERT-ONLY, idempotent. Executes INSERT_TOPIC_PHOTO_SQL via the Data
    API. Returns True iff a row was actually inserted (RETURNING id present).
    Never issues a DELETE/UPDATE -- this is the sole write path of the script."""
    params = [
        {"name": "tid", "value": {"stringValue": str(topic_id)}},
        {"name": "key", "value": {"stringValue": s3_key}},
        {"name": "cap",
         "value": {"isNull": True} if caption_text is None
         else {"stringValue": caption_text}},
    ]
    resp = execute(INSERT_TOPIC_PHOTO_SQL, params=params)
    records = resp.get("records") or resp.get("formattedRecords")
    return bool(records)


def process_candidate(candidate, s3_client, bucket, execute, apply, bound_keys=frozenset()):
    """List a day's pictures, EXCLUDE photos already bound (`bound_keys`),
    compute bindings on the remainder, and (only when apply=True) insert them.

    `bound_keys` is the photo-level idempotency set (see fetch_bound_photo_keys):
    an already-bound photo is dropped BEFORE the matcher runs, so it is never
    re-matched or double-bound. In dry-run mode `execute` is NEVER called -- no
    write of any kind reaches the database."""
    prefix = f"users/{candidate['folder']}/pictures/{candidate['date']}/"
    all_photos = list_pictures(s3_client, bucket, prefix)
    photo_objects = [p for p in all_photos if p["key"] not in bound_keys]
    already_bound = len(all_photos) - len(photo_objects)
    bindings = plan_bindings(candidate["topics"], photo_objects)

    inserted = 0
    if apply:
        for b in bindings:
            if insert_topic_photo_if_absent(execute, b["topic_id"], b["s3_key"], b["caption"]):
                inserted += 1

    return {
        "folder": candidate["folder"],
        "date": candidate["date"],
        "photos_listed": len(all_photos),
        "already_bound": already_bound,
        "topics": len(candidate["topics"]),
        "bindings": bindings,
        "inserted": inserted,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="INSERT-only backfill of historical topic_photos bindings "
                    "(dry-run by default).")
    p.add_argument("--apply", action="store_true",
                   help="Perform the inserts. Without this flag the script is a "
                        "read-only dry run and writes nothing.")
    p.add_argument("--folder", default=None,
                   help="Restrict to a single owner folder (e.g. Ben_UCPK).")
    p.add_argument("--date", default=None,
                   help="Restrict to a single report_date (YYYY-MM-DD).")
    p.add_argument("--date-from", default=None, dest="date_from",
                   help="Restrict to report_date >= this (YYYY-MM-DD).")
    p.add_argument("--date-to", default=None, dest="date_to",
                   help="Restrict to report_date <= this (YYYY-MM-DD).")
    p.add_argument("--db", "--database", default=DEFAULT_DATABASE, dest="database",
                   help="Aurora database name (default: fieldsight -- the prod DB; "
                        "pass fieldsight_test to target the isolated test DB).")
    p.add_argument("--bucket", default=DEFAULT_BUCKET, help="S3 data-lake bucket.")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="AWS profile.")
    p.add_argument("--region", default=DEFAULT_REGION, help="AWS region.")
    p.add_argument("--cluster-arn", default=DEFAULT_CLUSTER_ARN, dest="cluster_arn")
    p.add_argument("--secret-arn", default=DEFAULT_SECRET_ARN, dest="secret_arn")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    run_started = _dt.datetime.now(_dt.timezone.utc).isoformat()

    mode = "APPLY (INSERT-ONLY)" if args.apply else "DRY RUN (no writes)"
    print("=" * 72)
    print(f"  topic_photos backfill -- {mode}")
    print(f"  database={args.database}  bucket={args.bucket}  region={args.region}")
    print(f"  run_started_utc={run_started}"
          + ("   <- rollback: DELETE FROM topic_photos WHERE created_at >= this"
             if args.apply else ""))
    if args.folder or args.date or args.date_from or args.date_to:
        print(f"  scope: folder={args.folder} date={args.date} "
              f"from={args.date_from} to={args.date_to}")
    print("=" * 72)

    import boto3  # lazy: keeps the module import AWS-free for unit tests
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3_client = session.client("s3")
    execute = make_data_api_executor(
        args.profile, args.region, args.cluster_arn, args.secret_arn, args.database)

    topic_rows = fetch_topic_rows(
        execute, date=args.date, date_from=args.date_from, date_to=args.date_to)
    candidates, stats = plan_candidates(topic_rows, folder=args.folder)

    total_would = 0
    total_inserted = 0
    total_already_bound = 0
    days_with_photos = 0
    for cand in candidates:
        bound_keys = fetch_bound_photo_keys(execute, cand["folder"], cand["date"])
        result = process_candidate(cand, s3_client, args.bucket, execute, args.apply,
                                   bound_keys=bound_keys)
        total_already_bound += result["already_bound"]
        if result["photos_listed"] == 0:
            continue
        if not result["bindings"]:
            print(f"[{cand['folder']} {cand['date']}] "
                  f"{result['photos_listed']} photo(s) "
                  f"({result['already_bound']} already bound), {result['topics']} topic(s) "
                  f"-> 0 new bindings (no unbound photo within tolerance)")
            continue
        days_with_photos += 1
        print(f"[{cand['folder']} {cand['date']}] "
              f"{result['photos_listed']} photo(s) "
              f"({result['already_bound']} already bound), {result['topics']} topic(s) "
              f"-> {len(result['bindings'])} new binding(s):")
        for b in result["bindings"]:
            dist = "inside" if b["distance_min"] == 0 else (
                f"{b['distance_min']}min" if b["distance_min"] is not None else "n/a")
            verb = "INSERTED" if args.apply else "would-bind"
            print(f"    {verb}: photo {b['s3_key']}  ->  topic {b['topic_id']} "
                  f"(window {b['topic_time_range']!r}, dist {dist})")
        total_would += len(result["bindings"])
        total_inserted += result["inserted"]

    print("-" * 72)
    print(f"Summary: {stats['days_total']} day(s) scanned, "
          f"{days_with_photos} day(s) with new bindable photos, "
          f"{total_already_bound} photo(s) already bound (skipped).")
    if args.apply:
        print(f"         {total_inserted} row(s) inserted "
              f"(of {total_would} binding(s); NOT-EXISTS guard absorbed the rest).")
    else:
        print(f"         {total_would} binding(s) WOULD be inserted. "
              f"Re-run with --apply to write them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
