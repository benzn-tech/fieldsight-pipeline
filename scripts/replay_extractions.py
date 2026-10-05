"""Replay one person's extractions for one day into the item-writer.

When item-writer raised "folder ... has no directory row" the extraction JSON still
reached S3 (extractions/{folder}/{date}/*.json) but nothing reached Aurora. Once the
directory row exists (or the folder key is fixed), this re-delivers each extraction to
the item-writer with the S3 event the bucket would have sent.

    AWS_PROFILE=fieldsight-deployer python scripts/replay_extractions.py \
        --env prod --folder Deandre__Alberts --date 2026-10-05          # dry run
    ... same line plus --apply                                           # invoke

DRY RUN BY DEFAULT: lists the keys and invokes nothing. --apply invokes the item-writer
once per key (synchronously) and prints each response payload, so a skipped/failed
replay is visible rather than assumed.
"""
import argparse
import datetime
import json
import re
import sys

ENVS = {
    "prod": {"bucket": "fieldsight-data-509194952652",
             "function": "fieldsight-prod-item-writer"},
    "test": {"bucket": "fieldsight-data-test-509194952652",
             "function": "fieldsight-test-item-writer"},
}
REGION = "ap-southeast-2"
_FOLDER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _date(value):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date")


def _folder(value):
    if not _FOLDER_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a folder key ([A-Za-z0-9._-] only)")
    return value


def build_event(bucket, key):
    """The S3 ObjectCreated event the bucket would have sent for `key`."""
    return {"Records": [{"s3": {"bucket": {"name": bucket}, "object": {"key": key}}}]}


def list_keys(s3, bucket, folder, date):
    prefix = f"extractions/{folder}/{date}/"
    keys, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".json")]
        if not page.get("IsTruncated"):
            return sorted(keys)
        token = page["NextContinuationToken"]


def replay(s3, lam, env, folder, date, apply, out=print):
    cfg = ENVS[env]
    keys = list_keys(s3, cfg["bucket"], folder, date)
    out(f"{env}: {len(keys)} extraction(s) under extractions/{folder}/{date}/")
    for key in keys:
        out(f"  {key}")
    if not apply:
        out("dry run -- nothing invoked; add --apply to re-invoke "
            f"{cfg['function']} for each key")
        return keys, []
    results = []
    for key in keys:
        resp = lam.invoke(FunctionName=cfg["function"],
                          InvocationType="RequestResponse",
                          Payload=json.dumps(build_event(cfg["bucket"], key)).encode())
        payload = resp["Payload"].read().decode()
        if resp.get("FunctionError"):
            payload = f"FUNCTION ERROR ({resp['FunctionError']}): {payload}"
        out(f"{key} -> {payload}")
        results.append((key, payload))
    return keys, results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", choices=sorted(ENVS), required=True)
    ap.add_argument("--folder", type=_folder, required=True)
    ap.add_argument("--date", type=_date, required=True)
    ap.add_argument("--apply", action="store_true",
                    help="actually invoke the item-writer (default: list only)")
    args = ap.parse_args(argv)
    import boto3  # AWS_PROFILE comes from the environment
    session = boto3.Session(region_name=REGION)
    replay(session.client("s3"), session.client("lambda"),
           args.env, args.folder, args.date, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
