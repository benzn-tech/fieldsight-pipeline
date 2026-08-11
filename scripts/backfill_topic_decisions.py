"""One-off: recover decisions from retained extraction artifacts.

The extraction has always produced them and nothing stored them, so every topic
written before migration 0038 has none. The artifacts are still in S3, which is
why this is a gap rather than a loss.

Reaches the database through the **RDS Data API**, following
scripts/verify_programme_schema.py: the cluster is VPC-private, so a psycopg
DSN is not reachable from an operator's machine — the one place this script
will ever be run from.

MAPPING. The key is (source_s3_key, topic title). Position cannot be used:
every topic of one extraction inserts in a single transaction, so `created_at`
is identical across them and the `id` tiebreaker is a random uuid — ordering by
either does not reproduce artifact order. Where a title repeats inside one
extraction, BOTH are skipped and logged: a wrong attachment is worse than a
missing one.

YIELD. The artifact count is an upper bound, not a target. Topics superseded by
the nightly report path, or removed by a group merge, have no row to attach to.
Those are reported as unmatched and are NOT faults — read the counts, do not
just check that it ran.

IDEMPOTENCY. Item-writer's dedup is delete-by-source_s3_key on TOPICS; a direct
insert bypasses it and topic_decisions has no unique constraint, so a second
run would duplicate everything. This inserts only for topics that currently
have ZERO decisions.

Dry run by default; --apply writes.

  python scripts/backfill_topic_decisions.py --bucket fieldsight-data-test-509194952652
  python scripts/backfill_topic_decisions.py --bucket ... --db fieldsight --apply
"""
import argparse
import collections
import json
import subprocess

CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
          "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
REGION = "ap-southeast-2"


def aws(*args):
    r = subprocess.run(["aws"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:2000])
    return json.loads(r.stdout) if r.stdout.strip() else {}


def sql(db, statement, params=None):
    """One statement, parameterised. Never interpolate a title into SQL — they
    come from an LLM and contain quotes."""
    args = ["rds-data", "execute-statement", "--resource-arn", CLUSTER,
            "--secret-arn", SECRET, "--database", db, "--region", REGION,
            "--format-records-as", "JSON", "--sql", statement]
    if params:
        args += ["--parameters", json.dumps(params)]
    out = aws(*args)
    return json.loads(out.get("formattedRecords") or "[]")


def s(name, value):
    return {"name": name, "value": {"stringValue": value}}


def artifact_keys(bucket, prefix):
    token = None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", bucket,
                "--prefix", prefix, "--region", REGION, "--output", "json"]
        if token:
            args += ["--starting-token", token]
        page = aws(*args)
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".json"):
                yield obj["Key"]
        token = page.get("NextToken")
        if not token:
            return


def read_artifact(bucket, key):
    r = subprocess.run(["aws", "s3", "cp", f"s3://{bucket}/{key}", "-",
                        "--region", REGION], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:400])
    return json.loads(r.stdout.decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--db", default="fieldsight_test")
    ap.add_argument("--prefix", default="extractions/")
    ap.add_argument("--apply", action="store_true",
                    help="write; without it nothing is inserted")
    args = ap.parse_args()

    # Preflight: without this the failure lands mid-scan, as a stack trace
    # from a SELECT, after minutes of S3 listing -- and the operator has to
    # work out that the migration simply has not deployed yet.
    exists = sql(args.db,
                 "SELECT to_regclass('public.topic_decisions') IS NOT NULL AS ok")
    if not (exists and exists[0]["ok"]):
        raise SystemExit(
            f"topic_decisions does not exist in {args.db!r} -- migration 0038 has "
            f"not been applied there yet. Deploy it, then run this.")

    stats = collections.Counter()
    for key in artifact_keys(args.bucket, args.prefix):
        stats["artifacts"] += 1
        try:
            art = read_artifact(args.bucket, key)
        except Exception as exc:
            stats["unreadable"] += 1
            print(f"unreadable {key}: {exc}")
            continue

        topics = art.get("topics") or []
        titles = collections.Counter((t.get("topic_title") or "") for t in topics)

        for t in topics:
            ds = [d for d in (t.get("decisions") or [])
                  if isinstance(d, dict) and (d.get("decision") or "").strip()]
            if not ds:
                continue
            stats["topics_with_decisions"] += 1
            title = t.get("topic_title") or ""
            if not title or titles[title] > 1:
                stats["ambiguous_title"] += 1
                print(f"ambiguous title in {key}: {title!r}")
                continue

            rows = sql(args.db,
                       "SELECT id::text FROM topics "
                       "WHERE source_s3_key = :k AND title = :t",
                       [s("k", key), s("t", title)])
            if len(rows) != 1:
                stats["no_target_row" if not rows else "multiple_rows"] += 1
                continue
            topic_id = rows[0]["id"]

            existing = sql(args.db,
                           "SELECT count(*) AS n FROM topic_decisions "
                           "WHERE topic_id = :id::uuid", [s("id", topic_id)])
            if existing and existing[0]["n"]:
                stats["already_has_decisions"] += 1
                continue

            stats["would_insert"] += len(ds)
            if args.apply:
                for d in ds:
                    sql(args.db,
                        "INSERT INTO topic_decisions "
                        "(topic_id, decision, rationale, decided_by) "
                        "VALUES (:id::uuid, :d, :r, :b)",
                        [s("id", topic_id),
                         s("d", d["decision"].strip()),
                         s("r", d.get("rationale") or ""),
                         s("b", d.get("decided_by") or "")])
                stats["inserted"] += len(ds)

    print()
    for k, v in sorted(stats.items()):
        print(f"{k}: {v}")
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN — nothing written'}. "
          f"topics_with_decisions minus would_insert is the expected shortfall, "
          f"not a fault: superseded and merged-away topics have no row.")


if __name__ == "__main__":
    main()
