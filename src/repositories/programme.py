"""
Repository: per-site Programme JSON blob, stored directly in S3 (no SQL
table). Plain get/put -- reachable in-VPC via the S3 gateway endpoint (no
NAT), same as org-assets presign calls in lambda_org_api.py.

S3 layout: programmes/{site_slug}/programme.json
"""
import json


def _key(site_slug: str) -> str:
    return f"programmes/{site_slug}/programme.json"


def read_programme(s3, bucket, site_slug) -> dict | None:
    """Fetch and parse the programme JSON for a site. Returns None when no
    programme has ever been uploaded (S3 NoSuchKey) -- the caller turns this
    into a friendly {"programme": null} 200 response, not a 404."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=_key(site_slug))
    except s3.exceptions.NoSuchKey:
        return None
    return json.loads(obj["Body"].read().decode("utf-8"))


def write_programme(s3, bucket, site_slug, doc, updated_at) -> dict:
    """Stamp doc with updated_at and persist it as the site's programme.json.
    Returns the same doc (mutated with updated_at) for the caller to echo
    back in the response."""
    doc["updated_at"] = updated_at
    s3.put_object(
        Bucket=bucket,
        Key=_key(site_slug),
        Body=json.dumps(doc).encode("utf-8"),
        ContentType="application/json",
    )
    return doc
