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
    if avatar is not None and not str(avatar).startswith(ORG_ASSETS_PREFIX):
        return error(f"avatar_s3_key must start with {ORG_ASSETS_PREFIX}", 400)
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
    name = (body.get("name") or "").strip()
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
