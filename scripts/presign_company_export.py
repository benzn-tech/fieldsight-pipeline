"""Handover links for a company's export (spec 2026-11-01 company offboarding, section 7).

OPERATOR script: run from the operator's own machine with the operator's own AWS profile. It is
never imported or run by a Lambda; org-api keeps its 900 s links and the Lambda role never issues
long ones.

It lists every object under `exports/{company_id}/{export_id}/` (zip parts, manifest.json,
records/*, README.txt -- whatever is there), refuses any listed key that is not under that
prefix, presigns a GET URL for each one valid for --days (1..7; SigV4 cannot sign past 7 days),
and prints one line per object (key, size in bytes, URL) and the expiry time (UTC).

    python scripts/presign_company_export.py --company <uuid> --export-id <uuid> --days 7 \\
        --profile fieldsight-deployer [--database fieldsight --allow-prod] [--bucket NAME]

Prod: --database defaults to the TEST database and its bucket. The prod database, the prod
bucket, or ANY --bucket that is not the known TEST bucket counts as prod and is refused unless
--allow-prod is given (same rule as the other owner-run scripts, scripts/lake_ops_common.py).

Credentials: a presigned URL stops working when the credential that signed it expires. A
temporary credential (SSO, assumed role, `aws sts get-session-token`: anything with a session
token) therefore gives links that die with that session -- often within hours, whatever --days
says. The script prints a warning when the profile's credential is temporary; for a link that
really lasts N days, sign with a long-lived IAM user access key.
"""
import argparse
import datetime
import sys
import uuid

try:
    from scripts import lake_ops_common as common
except ImportError:  # run as `python scripts/presign_company_export.py`
    import lake_ops_common as common

MIN_DAYS = 1
MAX_DAYS = 7  # SigV4 presigned URL ceiling


class PresignError(Exception):
    pass


def _canon_uuid(value, what):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise PresignError(f"{what} must be a UUID, got {value!r}") from None


def export_prefix(company_id, export_id):
    # Same shape as src/company_export.export_prefix (which canonicalises both ids the same way).
    return f"exports/{company_id}/{export_id}/"


def is_prod_bucket(bucket):
    """Fail closed: only the known TEST bucket is not prod."""
    return bucket != common.BUCKETS[common.TEST_DATABASE]


def _check_days(days):
    if not isinstance(days, int) or isinstance(days, bool) or not MIN_DAYS <= days <= MAX_DAYS:
        raise PresignError(f"--days must be an integer in {MIN_DAYS}..{MAX_DAYS}, got {days!r}")


def _confined(key, prefix):
    return (isinstance(key, str) and key.startswith(prefix)
            and ".." not in key[len(prefix):].split("/"))


def presign_export(s3, bucket, company_id, export_id, days):
    """[{key, size, url}] for every object under the export's prefix. Raises PresignError on bad
    input, on any listed key outside the prefix (nothing is signed then), or on an empty prefix
    (almost always a wrong id or the wrong bucket)."""
    _check_days(days)
    prefix = export_prefix(_canon_uuid(company_id, "--company"),
                           _canon_uuid(export_id, "--export-id"))
    objects = []
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = obj.get("Key")
            if not _confined(key, prefix):
                raise PresignError(f"listed key {key!r} is outside {prefix}; refusing to sign")
            if key == prefix or key.endswith("/"):
                continue  # directory marker, nothing to download
            objects.append((key, obj.get("Size")))
        if not page.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = page["NextContinuationToken"]
    if not objects:
        raise PresignError(f"no objects under s3://{bucket}/{prefix}")
    expires_in = days * 86400
    return [{"key": key, "size": size,
             "url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                              ExpiresIn=expires_in)}
            for key, size in objects]


def _bucket_region(s3, bucket):
    loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    return loc or "us-east-1"  # S3 answers None for us-east-1


def _default_session_factory(profile):
    import boto3
    return boto3.Session(profile_name=profile)


def main(argv=None, session_factory=_default_session_factory, now=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", required=True, help="company UUID")
    ap.add_argument("--export-id", required=True, help="export UUID")
    ap.add_argument("--days", required=True, type=int, help=f"{MIN_DAYS}..{MAX_DAYS}")
    ap.add_argument("--profile", required=True, help="the operator's own AWS profile")
    ap.add_argument("--database", default=common.TEST_DATABASE,
                    choices=[common.TEST_DATABASE, common.PROD_DATABASE],
                    help="picks the default bucket")
    ap.add_argument("--bucket", help="default: the data bucket of --database")
    ap.add_argument("--allow-prod", action="store_true")
    args = ap.parse_args(argv)

    bucket = args.bucket or common.BUCKETS[args.database]
    prod = args.database == common.PROD_DATABASE or is_prod_bucket(bucket)
    if prod and not args.allow_prod:
        print(f"refusing bucket {bucket!r} without --allow-prod (prod, or not the known TEST "
              f"bucket)", file=sys.stderr)
        return 2
    try:
        _check_days(args.days)
        _canon_uuid(args.company, "--company")
        _canon_uuid(args.export_id, "--export-id")
    except PresignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    from botocore.config import Config
    session = session_factory(args.profile)
    creds = session.get_credentials()
    if creds is None:
        print(f"error: no AWS credentials for profile {args.profile!r}", file=sys.stderr)
        return 1
    if creds.get_frozen_credentials().token:
        print("WARNING: this profile's credential is temporary (it has a session token). The "
              "links stop working when that credential expires, which may be long before "
              f"{args.days} day(s). Sign with a long-lived access key for a real N-day link.",
              file=sys.stderr)
    region = _bucket_region(session.client("s3", region_name=common.DEFAULT_REGION), bucket)
    s3 = session.client("s3", region_name=region,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        links = presign_export(s3, bucket, args.company, args.export_id, args.days)
    except PresignError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    expires = now + datetime.timedelta(days=args.days)
    prefix = export_prefix(_canon_uuid(args.company, "--company"),
                           _canon_uuid(args.export_id, "--export-id"))
    print(f"# s3://{bucket}/{prefix}")
    print(f"# {len(links)} object(s); links expire {expires.isoformat(timespec='seconds')} (UTC)")
    for link in links:
        print(f"{link['key']}\t{link['size']}\t{link['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
