"""
Repository: per-site Programme JSON blob, stored directly in S3 (no SQL
table). Plain get/put -- reachable in-VPC via the S3 gateway endpoint (no
NAT), same as org-assets presign calls in lambda_org_api.py.

S3 layout: programmes/{site_id}/programme.json — keyed by the org site's
UUID (not name/slug), so a site rename never orphans its programme and
there's no name-collision/injection surface in the S3 key (Fable review).
"""
import json

from botocore.exceptions import ClientError

# S3 error codes that mean "no programme has ever been uploaded for this
# site" — a legitimate empty state, not a failure.
_NOT_FOUND_CODES = ("NoSuchKey", "404")


def _key(site_id: str) -> str:
    return f"programmes/{site_id}/programme.json"


def read_programme(s3, bucket, site_id) -> dict | None:
    """Fetch and parse the programme JSON for a site. Returns None when no
    programme has ever been uploaded (S3 NoSuchKey/404) -- the caller turns
    this into a friendly {"programme": null} 200 response, not a 404.
    Any other ClientError (notably AccessDenied, which the IAM policy
    should not produce once s3:ListBucket is granted on programmes/* --
    see template.yaml OrgApiFunction) is re-raised: it must surface as a
    real 500, not silently masquerade as "no programme"."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=_key(site_id))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
            return None
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


def write_programme(s3, bucket, site_id, doc, updated_at) -> dict:
    """Stamp doc with updated_at and persist it as the site's programme.json.
    Returns the same doc (mutated with updated_at) for the caller to echo
    back in the response."""
    doc["updated_at"] = updated_at
    s3.put_object(
        Bucket=bucket,
        Key=_key(site_id),
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )
    return doc
