"""
Lambda: fieldsight-org-api v1.0 — Phase 3 org write backend.

Runs IN-VPC (psycopg direct to Aurora). All AWS API calls it makes at
runtime (cognito-idp, S3 for seed config) go through VPC endpoints added in
infra/db-template.yaml — a VPC Lambda with no NAT black-holes on any other
AWS call (BUG-36), so every boto3 client here uses a short connect timeout
to fail fast instead of hanging to the Lambda timeout.

Routes (API Gateway /api/org/{proxy+}, Cognito authorizer = ORG user pool):
  GET   /api/org/me                     → caller profile + memberships (auto-provisions row on first call)
  PATCH /api/org/me                     → { first_name?, last_name? } (role/company NOT patchable)
  GET   /api/org/sites                  → sites visible to caller (admin/gm: company; others: memberships)
  POST  /api/org/sites                  → { name, location?, client?, industry? } (admin/gm)
  GET   /api/org/members                → company roster + memberships (admin/gm)
  POST  /api/org/members                → { email, first_name?, last_name?, global_role?,
                                            memberships?: [{site_id, role}] } (admin/gm; anti-escalation)
  PATCH /api/org/members/{sub}/role     → { role } (admin/gm; anti-escalation; no self-change)
  POST  /api/org/upload-url             → { kind: 'avatar'|'site_icon', content_type, site_id? }
                                          presigned PUT into org-assets/; key persisted immediately
  GET   /api/org/asset-url?key=...      → presigned GET for an org-assets/ key
  POST  /api/org/seed                   → { company_name?, roles?: {email: role}, sites?: [...] }
                                          bootstrap allowed on pristine DB, admin-only afterwards

Environment Variables:
    PGHOST/PGDATABASE/PGUSER/PGPASSWORD  deploy-time injected (BUG-36: no runtime Secrets Manager)
    S3_BUCKET                            data bucket for org-assets/ + config/user_mapping.json
    USER_POOL_ID                         Cognito pool for member provisioning (the ORG pool)
"""
import json
import logging
import os
import re
import uuid

import boto3
from botocore.config import Config

from db.connection import get_connection
from repositories import companies, memberships, sites, users
from repositories.acl import (
    VALID_GLOBAL_ROLES,
    VALID_MEMBERSHIP_ROLES,
    can_assign_role,
    can_manage_org,
    can_modify_user,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
ORG_ASSETS_PREFIX = "org-assets/"
PRESIGNED_URL_EXPIRY = 900
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Fail fast on any AWS call: with a VPC endpoint present these succeed in ms;
# without one they'd otherwise hang the whole Lambda timeout (BUG-36).
_BOTO_CFG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1})

_clients: dict = {}


def _cognito():
    if "cognito" not in _clients:
        _clients["cognito"] = boto3.client("cognito-idp", config=_BOTO_CFG)
    return _clients["cognito"]


def _s3():
    if "s3" not in _clients:
        _clients["s3"] = boto3.client("s3", config=_BOTO_CFG)
    return _clients["s3"]


# ----------------------------------------------------------------------
# HTTP plumbing
# ----------------------------------------------------------------------

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


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _parse_body(event) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise ApiError("Invalid JSON body", 400)
    if not isinstance(body, dict):
        raise ApiError("Body must be a JSON object", 400)
    return body


def _claims(event) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("claims", {}) or {}


def _require_str(body, field, max_len=200, required=True) -> str | None:
    val = body.get(field)
    if val is None or val == "":
        if required:
            raise ApiError(f"'{field}' is required", 400)
        return None
    if not isinstance(val, str) or len(val) > max_len:
        raise ApiError(f"'{field}' must be a string of at most {max_len} chars", 400)
    return val.strip()


def _caller(conn, event) -> dict:
    """Resolve the caller's app profile; auto-provision on first sight
    (login-sync style partial upsert: never touches role/company)."""
    claims = _claims(event)
    sub, email = claims.get("sub"), claims.get("email")
    if not sub:
        raise ApiError("Unauthorized", 401)
    row = users.get_user_by_sub(conn, sub)
    if row is None:
        if not email:
            raise ApiError("Unauthorized", 401)
        row = users.upsert_user(conn, sub, email)
    return row


def _require_uuid(value, field) -> str:
    """Validate client-supplied ids up front: a malformed uuid string would
    otherwise surface as a psycopg DataError → opaque 500."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(f"'{field}' must be a valid uuid", 400)


def _require_company(caller) -> str:
    if not caller.get("company_id"):
        raise ApiError("Your profile is not attached to a company yet. Run org seed first.", 403)
    return caller["company_id"]


def _require_org_manager(caller):
    if not can_manage_org(caller.get("global_role", "")):
        raise ApiError("Requires admin or gm role", 403)


# ----------------------------------------------------------------------
# Presign helpers
# ----------------------------------------------------------------------

def _presign_get(key) -> str:
    return _s3().generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )


def _with_avatar_url(user_row) -> dict:
    out = dict(user_row)
    out["avatar_url"] = _presign_get(out["avatar_s3_key"]) if out.get("avatar_s3_key") else None
    return out


def _with_icon_url(site_row) -> dict:
    out = dict(site_row)
    out["icon_url"] = _presign_get(out["icon_s3_key"]) if out.get("icon_s3_key") else None
    return out


# ----------------------------------------------------------------------
# /api/org/me
# ----------------------------------------------------------------------

def get_me(conn, event, caller):
    my_sites = memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])
    return ok({"me": _with_avatar_url(caller), "site_ids": my_sites})


def patch_me(conn, event, caller):
    body = _parse_body(event)
    for locked in ("global_role", "company_id", "email", "cognito_sub"):
        if locked in body:
            raise ApiError(f"'{locked}' cannot be changed here", 400)
    first = _require_str(body, "first_name", 100, required=False)
    last = _require_str(body, "last_name", 100, required=False)
    if first is None and last is None:
        raise ApiError("Nothing to update", 400)
    row = users.update_profile(conn, caller["cognito_sub"], first_name=first, last_name=last)
    return ok({"me": _with_avatar_url(row)})


# ----------------------------------------------------------------------
# /api/org/sites
# ----------------------------------------------------------------------

def get_sites(conn, event, caller):
    if can_manage_org(caller.get("global_role", "")):
        rows = sites.list_company_sites(conn, _require_company(caller))
    else:
        ids = memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])
        rows = sites.list_sites_by_ids(conn, ids)
    return ok({"sites": [_with_icon_url(s) for s in rows]})


def post_sites(conn, event, caller):
    _require_org_manager(caller)
    company_id = _require_company(caller)
    body = _parse_body(event)
    name = _require_str(body, "name")
    site = sites.create_site(
        conn, company_id, name,
        location=_require_str(body, "location", required=False),
        client=_require_str(body, "client", required=False),
        industry=_require_str(body, "industry", required=False),
    )
    return ok({"site": _with_icon_url(site)}, 201)


# ----------------------------------------------------------------------
# /api/org/members
# ----------------------------------------------------------------------

def get_members(conn, event, caller):
    _require_org_manager(caller)
    company_id = _require_company(caller)
    rows = users.list_company_users(conn, company_id)
    mships = memberships.list_company_memberships(conn, company_id)
    by_user: dict = {}
    for m in mships:
        by_user.setdefault(str(m["user_id"]), []).append(
            {"site_id": m["site_id"], "role": m["role"]})
    members = []
    for u in rows:
        member = _with_avatar_url(u)
        member["memberships"] = by_user.get(str(u["id"]), [])
        members.append(member)
    return ok({"members": members})


def _cognito_provision(email, display_name) -> str:
    """Create the Cognito account (Cognito emails the temp password).
    Idempotent: an already-existing username resolves to its sub."""
    attrs = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    if display_name:
        attrs.append({"Name": "name", "Value": display_name})
    try:
        resp = _cognito().admin_create_user(
            UserPoolId=USER_POOL_ID, Username=email,
            UserAttributes=attrs, DesiredDeliveryMediums=["EMAIL"],
        )
        cognito_attrs = {a["Name"]: a["Value"] for a in resp["User"].get("Attributes", [])}
    except _cognito().exceptions.UsernameExistsException:
        resp = _cognito().admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        cognito_attrs = {a["Name"]: a["Value"] for a in resp.get("UserAttributes", [])}
    sub = cognito_attrs.get("sub")
    if not sub:
        raise ApiError("Cognito did not return a sub for the new user", 502)
    return sub


def post_members(conn, event, caller):
    _require_org_manager(caller)
    company_id = _require_company(caller)
    body = _parse_body(event)

    email = (_require_str(body, "email", 254) or "").lower()
    if not _EMAIL_RE.match(email):
        raise ApiError("Invalid email address", 400)
    first = _require_str(body, "first_name", 100, required=False)
    last = _require_str(body, "last_name", 100, required=False)
    role = body.get("global_role") or "worker"
    if not can_assign_role(caller["global_role"], role):
        raise ApiError(f"You cannot assign role '{role}'", 403)

    wanted = body.get("memberships") or []
    if not isinstance(wanted, list):
        raise ApiError("'memberships' must be a list", 400)
    company_site_ids = {str(s["id"]).lower() for s in sites.list_company_sites(conn, company_id)}
    for m in wanted:
        if not isinstance(m, dict) or not m.get("site_id"):
            raise ApiError("Each membership needs a 'site_id'", 400)
        if _require_uuid(m["site_id"], "site_id") not in company_site_ids:
            raise ApiError(f"Site {m['site_id']} is not in your company", 400)
        if (m.get("role") or "worker") not in VALID_MEMBERSHIP_ROLES:
            raise ApiError(f"Invalid membership role '{m.get('role')}'", 400)

    display_name = " ".join(x for x in (first, last) if x) or None
    sub = _cognito_provision(email, display_name)

    # Re-invite guard: an existing, already-provisioned profile must not be
    # silently rewritten (re-adding an admin with the default role would
    # demote them; adding another company's member would move them across
    # tenants). A row WITHOUT a company (someone who logged in before being
    # invited) is legitimately adopted below.
    existing = users.get_user_by_sub(conn, sub)
    if existing and existing.get("company_id"):
        if str(existing["company_id"]).lower() == str(company_id).lower():
            raise ApiError("Already a member of your company — use the role "
                           "endpoint to change their role", 409)
        raise ApiError("This email is already registered to another organisation", 409)

    member = users.upsert_user(conn, sub, email, company_id=company_id,
                               first_name=first, last_name=last, global_role=role)
    added = [memberships.ensure_membership(conn, member["id"], m["site_id"],
                                           m.get("role") or "worker")
             for m in wanted]
    out = _with_avatar_url(member)
    out["memberships"] = [{"site_id": a["site_id"], "role": a["role"]} for a in added]
    return ok({"member": out}, 201)


def patch_member_role(conn, event, caller, target_sub):
    _require_org_manager(caller)
    company_id = _require_company(caller)
    body = _parse_body(event)
    new_role = _require_str(body, "role", 50)

    if target_sub == caller["cognito_sub"]:
        raise ApiError("You cannot change your own role", 400)
    target = users.get_user_by_sub(conn, target_sub)
    if target is None or str(target.get("company_id")).lower() != str(company_id).lower():
        raise ApiError("Member not found in your company", 404)
    if not can_modify_user(caller["global_role"], target["global_role"]):
        raise ApiError("You cannot modify a member above your rank", 403)
    if not can_assign_role(caller["global_role"], new_role):
        raise ApiError(f"You cannot assign role '{new_role}'", 403)

    updated = users.set_global_role(conn, target_sub, new_role)
    return ok({"member": _with_avatar_url(updated)})


# ----------------------------------------------------------------------
# /api/org/upload-url + /api/org/asset-url
# ----------------------------------------------------------------------

def post_upload_url(conn, event, caller):
    body = _parse_body(event)
    kind = body.get("kind")
    content_type = body.get("content_type")
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ApiError(f"content_type must be one of {sorted(ALLOWED_IMAGE_TYPES)}", 400)

    # Deterministic keys: re-uploads overwrite in place, so the DB pointer
    # can be persisted at issuance (a failed browser PUT just means the
    # presigned GET 404s until the next successful upload).
    if kind == "avatar":
        key = f"{ORG_ASSETS_PREFIX}avatars/{caller['cognito_sub']}"
        users.update_profile(conn, caller["cognito_sub"], avatar_s3_key=key)
    elif kind == "site_icon":
        _require_org_manager(caller)
        company_id = _require_company(caller)
        site_id = body.get("site_id")
        if not site_id:
            raise ApiError("'site_id' is required for site_icon uploads", 400)
        site_id = _require_uuid(site_id, "site_id")
        site = sites.get_site(conn, site_id)
        if site is None or str(site["company_id"]).lower() != str(company_id).lower():
            raise ApiError("Site not found in your company", 404)
        key = f"{ORG_ASSETS_PREFIX}site-icons/{site_id}"
        sites.set_icon_key(conn, site_id, key)
    else:
        raise ApiError("kind must be 'avatar' or 'site_icon'", 400)

    upload_url = _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"upload_url": upload_url, "key": key,
               "expires_in": PRESIGNED_URL_EXPIRY, "content_type": content_type})


def get_asset_url(conn, event, caller):
    params = event.get("queryStringParameters") or {}
    key = params.get("key") or ""
    # org-assets only, no traversal. Profile/site images are intra-company
    # visible by design; keys embed unguessable uuids.
    if not key.startswith(ORG_ASSETS_PREFIX) or ".." in key:
        raise ApiError("key must be under org-assets/", 400)
    _require_company(caller)
    return ok({"url": _presign_get(key), "key": key, "expires_in": PRESIGNED_URL_EXPIRY})


# ----------------------------------------------------------------------
# /api/org/seed — company row + Cognito pool users + user_mapping sites
# ----------------------------------------------------------------------

def _load_user_mapping_from_s3() -> dict | None:
    try:
        obj = _s3().get_object(Bucket=S3_BUCKET, Key="config/user_mapping.json")
        return json.loads(obj["Body"].read())
    except Exception as e:  # missing key, no S3 endpoint, bad JSON — seed degrades gracefully
        logger.warning(f"user_mapping.json unavailable from s3://{S3_BUCKET}: {e}")
        return None


def post_seed(conn, event, caller):
    """Idempotent backfill. Bootstrap rule: on a pristine DB (no user attached
    to any company) ANY authenticated caller may seed and becomes admin;
    afterwards only an admin may re-run it."""
    body = _parse_body(event)
    if users.count_provisioned_users(conn) > 0 and caller.get("global_role") != "admin":
        raise ApiError("Seed already ran; only an admin can re-run it", 403)

    # Validate inputs BEFORE any write.
    # Explicit role map {email: role}; the seeding caller defaults to admin.
    role_map = {k.lower(): v for k, v in (body.get("roles") or {}).items()}
    role_map.setdefault(caller["email"].lower(), "admin")
    for r in role_map.values():
        if r not in VALID_GLOBAL_ROLES:
            raise ApiError(f"Invalid role '{r}' in roles map", 400)

    company_name = _require_str(body, "company_name", required=False) or "FieldSight"
    company = companies.get_company_by_name(conn, company_name) \
        or companies.create_company(conn, company_name)

    # 1. Pool users → app users
    seeded_users = []
    pager = _cognito().get_paginator("list_users")
    for page in pager.paginate(UserPoolId=USER_POOL_ID):
        for cu in page.get("Users", []):
            attrs = {a["Name"]: a["Value"] for a in cu.get("Attributes", [])}
            sub, email = attrs.get("sub"), (attrs.get("email") or "").lower()
            if not sub or not email:
                continue
            name_parts = (attrs.get("name") or "").split(" ", 1)
            seeded_users.append(users.upsert_user(
                conn, sub, email, company_id=company["id"],
                first_name=name_parts[0] or None,
                last_name=name_parts[1] if len(name_parts) > 1 else None,
                global_role=role_map.get(email),  # None → keep/default worker
            ))

    # 2. Sites: request body wins; else config/user_mapping.json from S3.
    # Memberships (step 3) always come from the mapping doc when available.
    mapping_doc = _load_user_mapping_from_s3()
    site_defs = body.get("sites")
    if not site_defs:
        site_defs = [
            {"name": s.get("name"), "location": s.get("location"), "client": s.get("client")}
            for s in (mapping_doc or {}).get("sites", {}).values()
        ]
    seeded_sites = []
    for sd in site_defs or []:
        if not isinstance(sd, dict) or not sd.get("name"):
            continue
        existing = sites.get_site_by_name(conn, company["id"], sd["name"])
        seeded_sites.append(existing or sites.create_site(
            conn, company["id"], sd["name"],
            location=sd.get("location"), client=sd.get("client"),
            industry=sd.get("industry")))

    # 3. Memberships: user_mapping person-name ↔ Cognito name attribute
    site_by_name = {s["name"]: s for s in seeded_sites}
    user_by_name = {}
    for u in seeded_users:
        full = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        if full:
            user_by_name[full.lower()] = u
    seeded_memberships = 0
    for entry in (mapping_doc or {}).get("mapping", {}).values():
        person = user_by_name.get((entry.get("name") or "").lower())
        if not person:
            continue
        m_role = entry.get("role") if entry.get("role") in VALID_MEMBERSHIP_ROLES else "worker"
        for site_key in entry.get("sites", []):
            site_def = (mapping_doc.get("sites") or {}).get(site_key) or {}
            site = site_by_name.get(site_def.get("name"))
            if site:
                memberships.ensure_membership(conn, person["id"], site["id"], m_role)
                seeded_memberships += 1

    return ok({
        "company": company,
        "users": len(seeded_users),
        "sites": len(seeded_sites),
        "memberships": seeded_memberships,
        "user_mapping_loaded": mapping_doc is not None,
    })


# ----------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------

_MEMBER_ROLE_RE = re.compile(r"^/api/org/members/([^/]+)/role$")


def _route(conn, event):
    method = event.get("httpMethod", "GET")
    path = (event.get("path") or "").rstrip("/")
    caller = _caller(conn, event)

    if path == "/api/org/me":
        if method == "GET":
            return get_me(conn, event, caller)
        if method == "PATCH":
            return patch_me(conn, event, caller)
    elif path == "/api/org/sites":
        if method == "GET":
            return get_sites(conn, event, caller)
        if method == "POST":
            return post_sites(conn, event, caller)
    elif path == "/api/org/members":
        if method == "GET":
            return get_members(conn, event, caller)
        if method == "POST":
            return post_members(conn, event, caller)
    elif path == "/api/org/upload-url" and method == "POST":
        return post_upload_url(conn, event, caller)
    elif path == "/api/org/asset-url" and method == "GET":
        return get_asset_url(conn, event, caller)
    elif path == "/api/org/seed" and method == "POST":
        return post_seed(conn, event, caller)
    else:
        m = _MEMBER_ROLE_RE.match(path)
        if m and method == "PATCH":
            return patch_member_role(conn, event, caller, m.group(1))

    raise ApiError(f"Not found: {method} {path}", 404)


def lambda_handler(event, context):
    logger.info(f"Request: {event.get('httpMethod', 'GET')} {event.get('path', '/')}")
    try:
        conn = get_connection()
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        return ok({"error": "Database unavailable"}, 503)
    try:
        result = _route(conn, event)
        conn.commit()
        return result
    except ApiError as e:
        conn.rollback()
        return ok({"error": str(e)}, e.status)
    except Exception as e:
        conn.rollback()
        logger.exception(f"Unhandled error: {e}")
        return ok({"error": "Internal server error"}, 500)
    finally:
        conn.close()
