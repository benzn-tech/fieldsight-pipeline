"""
Lambda: fieldsight-org-api v1.0 — Org write backend (Phase 3)

In-VPC (psycopg direct to Aurora). Routed at /api/org/{proxy+} on the TEST
FieldSightApi with a Cognito authorizer that also trusts the prod user pool,
so the UI's raw idToken works unchanged.

Routes (this file grows by task; see docs/superpowers/plans/2026-07-04-phase-3-org-api.md):
  GET   /api/org/me                       → caller profile + accessible site ids
  PATCH /api/org/me                       → update first/last name, avatar key
  GET   /api/org/sites                    → sites visible to caller (ACL)
  POST  /api/org/sites                    → create site (admin/gm)
  GET   /api/org/members                  → company members + memberships (admin/gm)
  POST  /api/org/members                  → cognito admin-create + upsert + memberships (admin)
  PATCH /api/org/members/{sub}/role       → explicit global role set (admin)
  POST  /api/org/upload-url               → presigned PUT for avatar / site icon
  GET   /api/org/asset-url?key=…          → presigned GET for an org asset

Credentials: PG* env vars injected at deploy time (BUG-36 — no runtime
Secrets Manager call from a NAT-less VPC). Cognito calls need the
cognito-idp VPC interface endpoint (db stack).
"""
import json
import logging
import os
import re
import uuid

import boto3

from db.connection import get_connection
from repositories import memberships, sites, users
from repositories.acl import resolve_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
ORG_ASSETS_PREFIX = os.environ.get("ORG_ASSETS_PREFIX", "org-assets/")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
PRESIGNED_URL_EXPIRY = 900

ALLOWED_GLOBAL_ROLES = {"admin", "gm", "pm", "site_manager", "worker"}
ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}

_s3_client = None
_cognito_client = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def cognito():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client("cognito-idp")
    return _cognito_client


def ok(body, status=200):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def error(message, status=400):
    return ok({"error": message}, status)


def parse_body(event):
    """Return the parsed JSON body dict, or None on malformed JSON."""
    raw = event.get("body") or "{}"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def lambda_handler(event, context):
    method = event.get("httpMethod", "")
    path = event.get("path", "")
    m = re.match(r"^/api/org(/.*)?$", path)
    route = (m.group(1) or "/") if m else path
    try:
        # psycopg3 `with conn:` commits on clean exit, rolls back on
        # exception, and closes the connection when the block ends.
        with get_connection() as conn:
            return dispatch(conn, event, method, route)
    except Exception:
        logger.exception("org api unhandled error")
        return error("internal error", 500)


def dispatch(conn, event, method, route):
    claims = (event.get("requestContext", {}) or {}).get("authorizer", {}).get("claims", {})
    sub = claims.get("sub", "")
    caller = users.get_user_by_sub(conn, sub) if sub else None
    if caller is None:
        return error("caller not provisioned in org database (run seed?)", 403)
    if not caller["company_id"]:
        return error("caller has no company", 403)

    if route == "/me":
        if method == "GET":
            return get_me(conn, caller)
        if method == "PATCH":
            return patch_me(conn, caller, parse_body(event))

    if route == "/sites":
        if method == "GET":
            return list_org_sites(conn, caller)
        if method == "POST":
            return create_org_site(conn, caller, parse_body(event))

    if route == "/members":
        if method == "GET":
            return list_members(conn, caller)
        if method == "POST":
            return create_member(conn, caller, parse_body(event))
    m = re.match(r"^/members/([^/]+)/role$", route)
    if m and method == "PATCH":
        return patch_member_role(conn, caller, m.group(1), parse_body(event))

    if route == "/upload-url" and method == "POST":
        return create_upload_url(conn, caller, parse_body(event))
    if route == "/asset-url" and method == "GET":
        return get_asset_url(event)

    return error("not found", 404)


# ----------------------------------------------------------
# /me
# ----------------------------------------------------------
def get_me(conn, caller):
    site_ids = memberships.accessible_site_ids(
        conn, caller["id"], caller["global_role"])
    return ok({**caller, "site_ids": site_ids,
               "scope": resolve_scope(caller["global_role"])})


def patch_me(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    avatar = body.get("avatar_s3_key")
    own_prefix = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/"
    if avatar is not None and (
            not isinstance(avatar, str) or not avatar.startswith(own_prefix)):
        return error(f"avatar_s3_key must start with {own_prefix}", 400)
    row = users.update_profile(
        conn, caller["cognito_sub"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        avatar_s3_key=avatar,
    )
    if row is None:
        return error("user not found", 404)
    return ok(row)


# ----------------------------------------------------------
# /sites
# ----------------------------------------------------------
def list_org_sites(conn, caller):
    if resolve_scope(caller["global_role"]) == "ALL":
        rows = sites.list_company_sites(conn, caller["company_id"])
    else:
        ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
        rows = sites.list_sites_by_ids(conn, ids)
    return ok({"sites": rows})


def create_org_site(conn, caller, body):
    if caller["global_role"] not in ("admin", "gm"):
        return error("admin or gm role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    name_raw = body.get("name")
    if name_raw is not None and not isinstance(name_raw, str):
        return error("name must be a string", 400)
    name = (name_raw or "").strip()
    if not name:
        return error("name is required", 400)
    icon = body.get("icon_s3_key")
    if icon is not None and not str(icon).startswith(ORG_ASSETS_PREFIX):
        return error(f"icon_s3_key must start with {ORG_ASSETS_PREFIX}", 400)
    row = sites.create_site(
        conn, caller["company_id"], name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=icon,
    )
    return ok(row, 201)


# ----------------------------------------------------------
# /members
# ----------------------------------------------------------
def list_members(conn, caller):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    rows = users.list_company_users(conn, caller["company_id"])
    per_user = {}
    for mem in memberships.list_company_memberships(conn, caller["company_id"]):
        per_user.setdefault(mem["user_id"], []).append(
            {"site_id": mem["site_id"], "role": mem["role"]})
    for row in rows:
        row["memberships"] = per_user.get(row["id"], [])
    return ok({"members": rows})


def patch_member_role(conn, caller, target_sub, body):
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    role = body.get("global_role")
    if not isinstance(role, str) or role not in ALLOWED_GLOBAL_ROLES:
        return error(f"global_role must be one of {sorted(ALLOWED_GLOBAL_ROLES)}", 400)
    row = users.set_global_role(conn, target_sub, caller["company_id"], role)
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)


def create_member(conn, caller, body):
    """Admin-only. Creates the Cognito login (email invite w/ temp password),
    the Aurora profile, and site memberships. Idempotent: an existing Cognito
    user is looked up instead of failing, and the DB writes are upserts —
    safe to retry after a partial failure (Cognito ok, DB rolled back).
    An existing user keeps their current global_role unless global_role is
    explicitly sent in the body (no silent reset to "worker" on re-invite).
    If the resolved Cognito user already belongs to another company, the
    request is rejected with 409 rather than re-parenting them."""
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return error("valid email is required", 400)
    global_role = body.get("global_role")
    if global_role is not None and (
            not isinstance(global_role, str) or global_role not in ALLOWED_GLOBAL_ROLES):
        return error(f"global_role must be one of {sorted(ALLOWED_GLOBAL_ROLES)}", 400)
    wanted = body.get("memberships") or []
    for mem in wanted:
        if not isinstance(mem, dict) or not mem.get("site_id"):
            return error("each membership needs a site_id", 400)
        if not isinstance(mem.get("role"), str) or mem.get("role") not in ALLOWED_MEMBERSHIP_ROLES:
            return error(
                f"membership role must be one of {sorted(ALLOWED_MEMBERSHIP_ROLES)}", 400)
        site = sites.get_site(conn, mem["site_id"])
        if site is None or site["company_id"] != caller["company_id"]:
            return error("site not found in your company", 403)

    client = cognito()
    display_name = " ".join(
        p for p in (body.get("first_name"), body.get("last_name")) if p) or email
    try:
        resp = client.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "name", "Value": display_name},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        attrs = resp["User"]["Attributes"]
    except client.exceptions.UsernameExistsException:
        resp = client.admin_get_user(UserPoolId=COGNITO_USER_POOL_ID, Username=email)
        attrs = resp["UserAttributes"]
    sub = next(a["Value"] for a in attrs if a["Name"] == "sub")

    existing = users.get_user_by_sub(conn, sub)
    if existing and existing["company_id"] and existing["company_id"] != caller["company_id"]:
        return error("user already belongs to another company", 409)

    user = users.upsert_user(
        conn, sub, email,
        company_id=caller["company_id"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        global_role=global_role,
    )
    created = [memberships.ensure_membership(conn, user["id"], mem["site_id"],
                                             mem["role"]) for mem in wanted]
    return ok({"user": user, "memberships": created}, 201)


# ----------------------------------------------------------
# assets (presigned PUT/GET; signing is offline — no VPC egress needed)
# ----------------------------------------------------------
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def create_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    content_type = body.get("content_type")
    ext = ALLOWED_IMAGE_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"content_type must be one of {sorted(ALLOWED_IMAGE_TYPES)}", 400)
    kind = body.get("kind")
    if kind == "avatar":
        key = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
    elif kind == "site_icon":
        # Icons are uploaded BEFORE the site row exists (the UI create-modal
        # picks the image during creation), so keys scope by uploader sub,
        # not site id; POST /sites then stores the returned key.
        if caller["global_role"] not in ("admin", "gm"):
            return error("admin or gm role required", 403)
        key = f"{ORG_ASSETS_PREFIX}site-icons/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
    else:
        return error("kind must be avatar or site_icon", 400)
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"url": url, "key": key, "expires_in": PRESIGNED_URL_EXPIRY})


def get_asset_url(event):
    key = (event.get("queryStringParameters") or {}).get("key", "")
    if not key.startswith(ORG_ASSETS_PREFIX):
        return error(f"key must start with {ORG_ASSETS_PREFIX}", 400)
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"url": url, "expires_in": PRESIGNED_URL_EXPIRY})
