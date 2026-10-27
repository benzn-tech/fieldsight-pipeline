"""Shared plumbing for the owner-run lake operations of 2026-10-27.

Used by scripts/prod_lake_reassign_folder.py (A) and scripts/prod_lake_delete_folders.py (B).
Nothing here writes on import; a write happens only inside a script's --apply path.

* Db: one RDS Data API transaction (boto3 rds-data). A dry run opens it with
  SET TRANSACTION READ ONLY, checks SHOW transaction_read_only = on, and always rolls back.
  Parameters are always bound (:name), never formatted into the SQL; lists travel as one JSON
  string expanded in SQL with jsonb_array_elements_text.
* Folder matching: a key belongs to a folder only when a FOLDER segment of its path (any
  segment but the last, i.e. not the file name) equals the folder exactly.
  `users/Jarley_Trainor/audio/2026-07-15/Benl1_x.wav` is NOT a Benl1 key; neither is
  `users/Ben_UCPK/...` a `Ben_UCPK_` key. key_in_scope() and folder_segment_regex() are the
  same rule in Python and in Postgres, and the unit tests hold them together.
"""
import datetime
import json
import re
import sys

PROD_DATABASE = "fieldsight"
TEST_DATABASE = "fieldsight_test"
# prod and TEST share one Aurora cluster (different databases), see the backend skill.
CLUSTER_ARN = "arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9"
SECRET_ARN = ("arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
              "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
BUCKETS = {PROD_DATABASE: "fieldsight-data-509194952652",
           TEST_DATABASE: "fieldsight-data-test-509194952652"}
DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"

_FOLDER_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_PG_SPECIAL = re.compile(r"([.^$*+?()\[\]{}|\\])")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def validate_folders(folders):
    """Exact folder names, deduplicated, order kept. Raises ValueError on anything that is not
    a plain folder name (no wildcard, no slash, no whitespace, no empty string)."""
    out = []
    for f in folders:
        if not isinstance(f, str) or not _FOLDER_RE.match(f):
            raise ValueError(f"not a plain folder name: {f!r}")
        if f not in out:
            out.append(f)
    if not out:
        raise ValueError("no folders given")
    return out


def folder_segments(key):
    """The folder segments of an S3 key: every path segment except the last (the file name)."""
    return key.split("/")[:-1] if isinstance(key, str) else []


def key_in_scope(key, folders):
    wanted = set(folders)
    return any(seg in wanted for seg in folder_segments(key))


def name_only_match(key, folders):
    """The key mentions a folder name somewhere (typically a file name like Benl1_...wav) but
    no folder segment equals it: reported as excluded, never deleted."""
    return (not key_in_scope(key, folders)) and any(f in key for f in folders)


def folder_segment_regex(folders):
    """Postgres ARE (and Python re) pattern equivalent to key_in_scope: the folder followed
    by '/', at the start or after a '/'."""
    alts = "|".join(_PG_SPECIAL.sub(r"\\\1", f) for f in validate_folders(folders))
    return f"(^|/)({alts})/"


def _param(name, value):
    if value is None:
        return {"name": name, "value": {"isNull": True}}
    if isinstance(value, bool):
        return {"name": name, "value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"name": name, "value": {"longValue": value}}
    if isinstance(value, (list, dict)):
        value = json.dumps(value)
    return {"name": name, "value": {"stringValue": str(value)}}


class Db:
    """One Data API transaction. read_only=True is the dry-run mode."""

    def __init__(self, database, *, read_only, profile=DEFAULT_PROFILE, region=DEFAULT_REGION,
                 client=None, log=None):
        if client is None:
            import boto3
            client = boto3.Session(profile_name=profile, region_name=region).client("rds-data")
        self.c = client
        self.read_only = read_only
        self.base = {"resourceArn": CLUSTER_ARN, "secretArn": SECRET_ARN, "database": database}
        self.log = log or (lambda *a, **k: None)
        self.tx = self.c.begin_transaction(**self.base)["transactionId"]
        self.done = False
        if read_only:
            self.execute("SET TRANSACTION READ ONLY")
            ro = self.query("SHOW transaction_read_only")
            if not ro or ro[0].get("transaction_read_only") != "on":
                self.rollback()
                raise RuntimeError("could not make the transaction read only; refusing")

    def _run(self, sql, params, fmt):
        kw = dict(self.base, sql=sql, transactionId=self.tx,
                  parameters=[_param(k, v) for k, v in params.items()])
        if fmt:
            kw["formatRecordsAs"] = "JSON"
        return self.c.execute_statement(**kw)

    def query(self, sql, **params):
        r = self._run(sql, params, True)
        return json.loads(r.get("formattedRecords") or "[]")

    def execute(self, sql, **params):
        r = self._run(sql, params, False)
        return r.get("numberOfRecordsUpdated", 0)

    def commit(self):
        if self.read_only:
            raise RuntimeError("refusing to commit a read-only (dry-run) transaction")
        self.c.commit_transaction(resourceArn=self.base["resourceArn"],
                                  secretArn=self.base["secretArn"], transactionId=self.tx)
        self.done = True

    def rollback(self):
        if not self.done:
            self.c.rollback_transaction(resourceArn=self.base["resourceArn"],
                                        secretArn=self.base["secretArn"], transactionId=self.tx)
            self.done = True


class Log:
    """Every action as one JSON line (file) plus a short human line on stderr."""

    def __init__(self, path):
        self.path = path
        self.fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, event, **fields):
        rec = {"t": now_iso(), "event": event, **fields}
        if self.fh:
            self.fh.write(json.dumps(rec, default=str) + "\n")
            self.fh.flush()
        brief = {k: v for k, v in fields.items() if not isinstance(v, (list, dict))}
        print(f"[{rec['t']}] {event} {json.dumps(brief, default=str)}", file=sys.stderr)


def refuse_prod_without_flag(database, allow_prod):
    if database == PROD_DATABASE and not allow_prod:
        print(f"refusing --database {PROD_DATABASE!r} without --allow-prod (this is prod)",
              file=sys.stderr)
        return True
    return False


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, default=str, sort_keys=True)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
