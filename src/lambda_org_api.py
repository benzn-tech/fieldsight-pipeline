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
  PATCH /api/org/sites/{id}               → update site fields / swap icon (admin/gm)
  POST  /api/org/sites/{id}/(un)archive   → soft-delete / restore site (admin/gm)
  POST  /api/org/members/{sub}/(un)archive→ soft-delete / restore member (admin/gm, never self)
  POST  /api/org/observations             → create safety/quality observation (any member)
  GET   /api/org/observations             → list observations (company-scoped filters)
  PATCH /api/org/observations/{id}        → update status (author or admin/gm)
  POST  /api/org/observations/{id}/archive→ soft-delete observation (admin/gm)
  GET   /api/org/live-items?date=…        → live topics dashboard feed (ACL)
  GET   /api/org/programme?site=<site_id> → read site's Programme JSON (S3-backed, ACL)
  PUT   /api/org/programme?site=<site_id> → write site's Programme JSON (admin/gm/pm)
  GET   /api/org/rollup/portfolio         → per-site open-count rollup + red/yellow/green (ACL)

Credentials: PG* env vars injected at deploy time (BUG-36 — no runtime
Secrets Manager call from a NAT-less VPC). Cognito calls need the
cognito-idp VPC interface endpoint (db stack).
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta

import boto3
from botocore.exceptions import ClientError

from db.connection import get_connection
from repositories import memberships, observations, programme, rollup, sites, topics, users
from repositories.acl import resolve_scope

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
ORG_ASSETS_PREFIX = os.environ.get("ORG_ASSETS_PREFIX", "org-assets/")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
PRESIGNED_URL_EXPIRY = 900

ALLOWED_GLOBAL_ROLES = {"admin", "gm", "pm", "site_manager", "worker"}
ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}
ALLOWED_OBSERVATION_KINDS = {"safety", "quality"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_OBSERVATION_STATUS = {"open", "closed"}

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
            "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,OPTIONS",
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
    if caller.get("archived_at") is not None and not (route == "/me" and method == "GET"):
        return error("account archived", 403)

    if route == "/me":
        if method == "GET":
            return get_me(conn, caller)
        if method == "PATCH":
            return patch_me(conn, caller, parse_body(event))

    if route == "/sites":
        if method == "GET":
            return list_org_sites(conn, caller, event)
        if method == "POST":
            return create_org_site(conn, caller, parse_body(event))

    if route == "/members":
        if method == "GET":
            return list_members(conn, caller, event)
        if method == "POST":
            return create_member(conn, caller, parse_body(event))
    m = re.match(r"^/members/([^/]+)/role$", route)
    if m and method == "PATCH":
        return patch_member_role(conn, caller, m.group(1), parse_body(event))
    m_sp = re.match(r"^/sites/([^/]+)$", route)
    if m_sp and method == "PATCH":
        return patch_org_site(conn, caller, m_sp.group(1), parse_body(event))
    m_sa = re.match(r"^/sites/([^/]+)/(archive|unarchive)$", route)
    if m_sa and method == "POST":
        return archive_site_endpoint(conn, caller, m_sa.group(1), m_sa.group(2))
    m_ma = re.match(r"^/members/([^/]+)/(archive|unarchive)$", route)
    if m_ma and method == "POST":
        return archive_member_endpoint(conn, caller, m_ma.group(1), m_ma.group(2))

    if route == "/upload-url" and method == "POST":
        return create_upload_url(conn, caller, parse_body(event))
    if route == "/asset-url" and method == "GET":
        return get_asset_url(event)

    if route == "/observations":
        if method == "GET":
            return list_org_observations(conn, caller, event)
        if method == "POST":
            return create_org_observation(conn, caller, parse_body(event))
    m_op = re.match(r"^/observations/([^/]+)$", route)
    if m_op and method == "PATCH":
        return patch_observation_status(conn, caller, m_op.group(1), parse_body(event))
    m_oa = re.match(r"^/observations/([^/]+)/archive$", route)
    if m_oa and method == "POST":
        return archive_observation_endpoint(conn, caller, m_oa.group(1))

    if route == "/live-items":
        if method == "GET":
            return list_live_items(conn, caller, event)

    if route == "/rollup/portfolio" and method == "GET":
        return list_portfolio_rollup(conn, caller, event)

    if route == "/programme":
        if method == "GET":
            return get_programme(conn, caller, event)
        if method == "PUT":
            return put_programme(conn, caller, event, parse_body(event))

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
    old_avatar = caller.get("avatar_s3_key")
    avatar = body.get("avatar_s3_key")
    clear = "avatar_s3_key" in body and avatar is None
    final_avatar = None
    if avatar is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(avatar, str) or not avatar.startswith(pending_prefix):
            return error(f"avatar_s3_key must be your pending upload ({pending_prefix}…)", 400)
        fname = avatar.rsplit("/", 1)[-1]
        final_avatar = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}/{fname}"
        # Relocate BEFORE the DB write. A DB failure after this leaves at most
        # one unreferenced object in avatars/ (rare; retry re-uploads) — same
        # pragmatic tradeoff as create_member's Cognito orphan.
        if not _relocate_asset(avatar, final_avatar):
            return error("upload expired or missing — please re-upload the image", 400)
    row = users.update_profile(
        conn, caller["cognito_sub"],
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        avatar_s3_key=final_avatar,
    )
    if row is None:
        return error("user not found", 404)
    if clear:
        row = users.clear_avatar(conn, caller["cognito_sub"])
        if old_avatar:
            _delete_asset(old_avatar)
    elif final_avatar and old_avatar and old_avatar != final_avatar:
        _delete_asset(old_avatar)
    return ok(row)


# ----------------------------------------------------------
# /sites
# ----------------------------------------------------------
def list_org_sites(conn, caller, event):
    include_archived = ((event.get("queryStringParameters") or {})
                        .get("include_archived") == "1")
    if resolve_scope(caller["global_role"]) == "ALL":
        rows = sites.list_company_sites(conn, caller["company_id"],
                                        include_archived=include_archived)
    else:
        # membership scope never includes archived rows (param ignored)
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
    if icon is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(icon, str) or not icon.startswith(pending_prefix):
            return error(f"icon_s3_key must be your pending upload ({pending_prefix}…)", 400)
    row = sites.create_site(
        conn, caller["company_id"], name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=None,
    )
    if icon is not None:
        fname = icon.rsplit("/", 1)[-1]
        final_icon = f"{ORG_ASSETS_PREFIX}site-icons/{row['id']}/{fname}"
        if not _relocate_asset(icon, final_icon):
            return error("upload expired or missing — please re-upload the image", 400)
        row = sites.set_site_icon(conn, row["id"], final_icon)
    return ok(row, 201)


def patch_org_site(conn, caller, site_id, body):
    if caller["global_role"] not in ("admin", "gm"):
        return error("admin or gm role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    name = body.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            return error("name must be a non-empty string", 400)
        name = name.strip()
    icon = body.get("icon_s3_key")
    if icon is not None:
        pending_prefix = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/"
        if not isinstance(icon, str) or not icon.startswith(pending_prefix):
            return error(f"icon_s3_key must be your pending upload ({pending_prefix}…)", 400)
    row = sites.update_site(
        conn, site_id, caller["company_id"],
        name=name, location=body.get("location"),
        client=body.get("client"), industry=body.get("industry"),
    )
    if row is None:
        return error("site not found in your company", 404)
    if icon is not None:
        old_icon = row.get("icon_s3_key")
        fname = icon.rsplit("/", 1)[-1]
        final_icon = f"{ORG_ASSETS_PREFIX}site-icons/{site_id}/{fname}"
        if not _relocate_asset(icon, final_icon):
            return error("upload expired or missing — please re-upload the image", 400)
        row = sites.set_site_icon(conn, site_id, final_icon)
        if old_icon and old_icon != final_icon:
            _delete_asset(old_icon)
    return ok(row)


# ----------------------------------------------------------
# /members
# ----------------------------------------------------------
def list_members(conn, caller, event):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    include_archived = ((event.get("queryStringParameters") or {})
                        .get("include_archived") == "1")
    rows = users.list_company_users(conn, caller["company_id"],
                                    include_archived=include_archived)
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
        if site.get("archived_at"):
            return error("site is archived — unarchive it first", 409)

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
    if existing and existing["company_id"] == caller["company_id"] and existing.get("archived_at"):
        return error("user is archived — unarchive them instead", 409)

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
# archive / unarchive (admin/gm, company-guarded)
# ----------------------------------------------------------
def archive_site_endpoint(conn, caller, site_id, action):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    fn = sites.archive_site if action == "archive" else sites.unarchive_site
    row = fn(conn, site_id, caller["company_id"])
    if row is None:
        return error("site not found in your company", 404)
    return ok(row)


def archive_member_endpoint(conn, caller, target_sub, action):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    if action == "archive" and target_sub == caller["cognito_sub"]:
        return error("cannot archive yourself", 400)
    fn = users.archive_user if action == "archive" else users.unarchive_user
    row = fn(conn, target_sub, caller["company_id"])
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)


# ----------------------------------------------------------
# assets (presigned PUT/GET; signing is offline — no VPC egress needed)
# ----------------------------------------------------------
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _relocate_asset(pending_key, final_key):
    """Copy a committed upload from pending to its permanent key and delete
    the pending object. Returns False when the pending object no longer
    exists (lifecycle-expired or bogus key) — callers turn that into a 400.
    S3 calls go through the S3 gateway endpoint (in-VPC, no NAT)."""
    try:
        s3().copy_object(Bucket=S3_BUCKET,
                         CopySource={"Bucket": S3_BUCKET, "Key": pending_key},
                         Key=final_key)
    except ClientError as e:
        # A missing copy source normally surfaces as NoSuchKey/404, but this
        # role holds no s3:ListBucket (least-privilege), so S3 returns
        # AccessDenied/403 for a nonexistent source rather than leaking its
        # existence. Within org-assets/* (where we DO hold object perms), any
        # of these means the pending upload is gone → caller returns 400
        # ("upload expired"). Verified against live S3 in the 3b smoke test.
        if e.response.get("Error", {}).get("Code") in (
                "NoSuchKey", "404", "AccessDenied", "403"):
            return False
        raise
    s3().delete_object(Bucket=S3_BUCKET, Key=pending_key)
    return True


def _delete_asset(key):
    if key and key.startswith(ORG_ASSETS_PREFIX):
        s3().delete_object(Bucket=S3_BUCKET, Key=key)


def create_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    content_type = body.get("content_type")
    ext = ALLOWED_IMAGE_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"content_type must be one of {sorted(ALLOWED_IMAGE_TYPES)}", 400)
    kind = body.get("kind")
    if kind == "avatar":
        pass
    elif kind == "site_icon":
        # Icons are uploaded BEFORE the site row exists (the UI create-modal
        # picks the image during creation), so keys scope by uploader sub,
        # not site id; POST /sites then stores the returned key.
        if caller["global_role"] not in ("admin", "gm"):
            return error("admin or gm role required", 403)
    else:
        return error("kind must be avatar or site_icon", 400)
    # Upload lands in a pending prefix; patch_me / site create+patch relocate
    # it to the permanent key on commit. Abandoned uploads are swept by the
    # 1-day S3 lifecycle rule on org-assets/pending/.
    key = f"{ORG_ASSETS_PREFIX}pending/{caller['cognito_sub']}/{uuid.uuid4().hex}.{ext}"
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
    if key.startswith(f"{ORG_ASSETS_PREFIX}pending/"):
        return error("pending uploads are not readable — commit them first", 400)
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"url": url, "expires_in": PRESIGNED_URL_EXPIRY})


# ----------------------------------------------------------
# /observations (any logged-in member may create/list; archived callers
# are already blocked by the dispatch guard above)
# ----------------------------------------------------------
REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_org_observations(conn, caller, event):
    params = event.get("queryStringParameters") or {}
    kind = params.get("kind")
    if kind is not None and kind not in ALLOWED_OBSERVATION_KINDS:
        return error(f"kind must be one of {sorted(ALLOWED_OBSERVATION_KINDS)}", 400)
    rows = observations.list_observations(
        conn, caller["company_id"], kind=kind,
        date_from=params.get("from"), date_to=params.get("to"),
        site_slug=params.get("site_slug"),
        include_archived=params.get("include_archived") == "1",
    )
    return ok({"observations": rows})


def create_org_observation(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    kind = body.get("kind")
    if kind not in ALLOWED_OBSERVATION_KINDS:
        return error(f"kind must be one of {sorted(ALLOWED_OBSERVATION_KINDS)}", 400)
    site_slug = body.get("site_slug")
    if not isinstance(site_slug, str) or not site_slug.strip():
        return error("site_slug is required", 400)
    observation_text = body.get("observation")
    if not isinstance(observation_text, str) or not observation_text.strip():
        return error("observation is required", 400)
    risk_level = body.get("risk_level")
    if risk_level is not None and risk_level not in ALLOWED_RISK_LEVELS:
        return error(f"risk_level must be one of {sorted(ALLOWED_RISK_LEVELS)}", 400)
    report_date = body.get("report_date")
    if report_date is not None:
        if not isinstance(report_date, str) or not REPORT_DATE_RE.match(report_date):
            return error("report_date must be YYYY-MM-DD", 400)
    else:
        # NZ "today" — the codebase-wide UTC+13 display convention (see
        # BUG-19); this endpoint is the only writer of report_date so the
        # default lives here, not in the repository layer.
        report_date = (datetime.utcnow() + timedelta(hours=13)).date().isoformat()
    author_name = " ".join(
        p for p in (caller.get("first_name"), caller.get("last_name")) if p
    ) or caller.get("email")
    row = observations.create_observation(
        conn, caller["company_id"], kind, site_slug.strip(),
        caller["cognito_sub"], author_name, observation_text.strip(),
        risk_level=risk_level, recommended_action=body.get("recommended_action"),
        report_date=report_date,
    )
    return ok(row, 201)


def patch_observation_status(conn, caller, obs_id, body):
    if body is None:
        return error("malformed JSON body", 400)
    status = body.get("status")
    if status not in ALLOWED_OBSERVATION_STATUS:
        return error(f"status must be one of {sorted(ALLOWED_OBSERVATION_STATUS)}", 400)
    row = observations.get_observation(conn, caller["company_id"], obs_id)
    if row is None:
        return error("observation not found", 404)
    if row["author_sub"] != caller["cognito_sub"] and resolve_scope(caller["global_role"]) != "ALL":
        return error("author or admin/gm role required", 403)
    updated = observations.set_status(conn, caller["company_id"], obs_id, status)
    return ok(updated)


def archive_observation_endpoint(conn, caller, obs_id):
    if resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    row = observations.set_archived(conn, caller["company_id"], obs_id, True)
    if row is None:
        return error("observation not found or already archived", 404)
    return ok(row)


# ----------------------------------------------------------
# /live-items (Phase 4b — dashboard read, ACL mirrors list_org_sites)
# ----------------------------------------------------------
def list_live_items(conn, caller, event):
    date = (event.get("queryStringParameters") or {}).get("date")
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    if resolve_scope(caller["global_role"]) == "ALL":
        site_ids = [s["id"] for s in sites.list_company_sites(conn, caller["company_id"])]
    else:
        site_ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
    rows = topics.list_topics_for_date(conn, site_ids, date)
    return ok({"topics": rows})


# ----------------------------------------------------------
# /rollup/portfolio (Phase 4c leg-1 — deterministic SQL aggregation; no LLM,
# no narrative, no materialization. ACL mirrors list_live_items EXACTLY via
# _allowed_site_ids, defined below in the /programme section — admin/gm see
# every non-archived site in their company, everyone else only their
# non-archived memberships. Status is a pure rule over the merged counts:
# any open high-risk safety observation -> red; else any open safety
# observation or open action item -> yellow; else green.)
# ----------------------------------------------------------
def _status(counts):
    if counts.get("open_high_safety", 0) > 0:
        return "red"
    if counts.get("open_safety", 0) > 0 or counts.get("open_actions", 0) > 0:
        return "yellow"
    return "green"


def list_portfolio_rollup(conn, caller, event):
    site_ids = _allowed_site_ids(conn, caller)
    counts = rollup.portfolio_counts(conn, site_ids)
    sites_rollup = [
        {"site_id": sid, **counts[sid], "status": _status(counts[sid])}
        for sid in sorted(str(x) for x in site_ids)  # stable order (set → deterministic)
    ]
    return ok({"sites": sites_rollup})


# ----------------------------------------------------------
# /programme (S3-backed JSON blob per site; no SQL table)
#
# `site` is the org site's UUID (NOT a name/slug — a slug can drift from
# the DB name on rename, which used to 403 every request; see Fable
# review High #2). The ACL below mirrors list_live_items EXACTLY: admin/gm
# (resolve_scope == "ALL") may touch any non-archived site in their own
# company; everyone else is scoped to their own non-archived memberships.
# Because the id must appear in one of those two company/membership-scoped
# sets, this also blocks cross-company access and archived sites for free
# — no separate lookup needed, and the S3 key (programmes/{site_id}/…) has
# no name/slug in it, so there's no injection surface and no orphaned
# object on a site rename.
# ----------------------------------------------------------
def _allowed_site_ids(conn, caller):
    # str() both sides: psycopg returns site ids as uuid.UUID objects, but the
    # ?site= query param arrives as a string — a UUID-vs-str `in` check is
    # always False (every request 403'd, incl. admins). Unit mocks used string
    # ids so this only surfaced against real Aurora (smoke-caught).
    if resolve_scope(caller["global_role"]) == "ALL":
        return {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
    return {str(x) for x in memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])}


def get_programme(conn, caller, event):
    site_id = (event.get("queryStringParameters") or {}).get("site")
    if not site_id:
        return error("site required", 400)
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    doc = programme.read_programme(s3(), S3_BUCKET, site_id)
    return ok({"programme": doc})


def put_programme(conn, caller, event, body):
    site_id = (event.get("queryStringParameters") or {}).get("site")
    if not site_id:
        return error("site required", 400)
    if body is None:
        return error("malformed JSON body", 400)
    if caller["global_role"] not in ("admin", "gm", "pm"):
        return error("programme write requires manager role", 403)
    # Write requires BOTH the manager-role gate above AND site access below
    # — a pm can only write programmes for sites in their own memberships.
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    # NZ "today"/"now" — the codebase-wide UTC+13 display convention (BUG-19
    # / see create_org_observation's report_date default).
    updated_at = (datetime.utcnow() + timedelta(hours=13)).isoformat()
    saved = programme.write_programme(s3(), S3_BUCKET, site_id, body, updated_at)
    return ok({"programme": saved})
