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
  PATCH /api/org/members/{sub}/folder     → set recording-folder identity (admin)
  POST  /api/org/members/enroll-backfill  → bulk-enroll folder_name for unenrolled company logins (admin)
  POST  /api/org/upload-url               → presigned PUT for avatar / site icon
  GET   /api/org/asset-url?key=…          → presigned GET for an org asset
  PATCH /api/org/sites/{id}               → update site fields / swap icon (admin/gm)
  POST  /api/org/sites/{id}/(un)archive   → soft-delete / restore site (admin/gm)
  GET   /api/org/sites/{id}/contributors  → folders with topics attributed to
                                            (site,date); members∪this = the
                                            aggregated-timeline fan-out set
  GET   /api/org/sites/{id}/members       → site's members from memberships (ACL,
                                             fixes legacy USERS ON SITE empty for Aurora-only sites)
  POST  /api/org/members/{sub}/(un)archive→ soft-delete / restore member (admin/gm, never self)
  POST  /api/org/observations             → create safety/quality observation (any member)
  GET   /api/org/observations             → list observations (company-scoped filters)
  PATCH /api/org/observations/{id}        → update status (author or admin/gm)
  PATCH /api/org/action-items/{id}        → update priority/status/deadline/responsible
                                             (site-authority ACL + member-validated reassignment)
  POST  /api/org/observations/{id}/archive→ soft-delete observation (admin/gm)
  GET   /api/org/live-items?date=…        → live topics dashboard feed (ACL)
  GET   /api/org/dates?months=&site=      → Timeline dots: report-date index scoped to
                                             caller's accessible sites (ACL, kills dots leak)
  GET   /api/org/timeline?date=…&user=…   → daily_report.json compat shim: S3 verbatim
                                             or Aurora extraction override (ACL, authority-flip)
  GET   /api/org/transcripts?date=&user=&start=&end= → transcript speaker segments for a
                                             (user,date) window, Aurora-identity ACL (mirrors
                                             /timeline's graded-off shape; fixes the legacy
                                             /transcripts gateway's DynamoDB-identity 403 for
                                             Aurora-only accounts)
  GET   /api/org/programme?site=<site_id> → read site's Programme JSON (S3-backed, ACL)
  PUT   /api/org/programme?site=<site_id> → write site's Programme JSON (admin/gm/pm)
  GET   /api/org/rollup/portfolio         → per-site open-count rollup + last_activity_at + red/yellow/green (ACL)
  GET   /api/org/programme/suggestions            → list matcher suggestions for a site (admin/gm/pm)
  POST  /api/org/programme/suggestions/{id}/confirm → apply + write back to programme.json (admin/gm/pm)
  POST  /api/org/programme/suggestions/{id}/reject  → dismiss a suggestion (admin/gm/pm)

Credentials: PG* env vars injected at deploy time (BUG-36 — no runtime
Secrets Manager call from a NAT-less VPC). Cognito calls need the
cognito-idp VPC interface endpoint (db stack).
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError
from psycopg.errors import UniqueViolation

import reindex
from db.connection import get_connection
from psycopg.rows import dict_row as RealDictRow
from repositories import (action_items, aliases, companies, content, content_edits, memberships,
                          observations, programme, programme_suggestions, recordings, redactions,
                          rollup, scope, sites, topics, users, voice_messages)
from repositories.acl import is_cross_company, resolve_scope
from text_normalize import diff_candidates

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
# The read-only lake bucket (reports/, extractions/) — a DIFFERENT bucket
# than S3_BUCKET (org-assets) on the TEST stack, same split as
# MatcherFunction's DataBucketName vs IngestBucketName. Read via the same
# module-level s3() client (boto3 clients aren't bucket-bound; Bucket is a
# per-call param) — no second client needed.
LAKE_BUCKET = os.environ.get("LAKE_BUCKET", "")
# Fix wave 1 (review finding 1): the lake-owner/internal company name —
# reuses the SAME marker lambda_ingest.py/lambda_item_writer.py already
# introduced for MultiTenantResolution's company pin (resolve_company /
# COMPANY_NAME), rather than inventing a second one. Resolved to a company
# row via companies.get_company_by_name at call time (no template.yaml
# wiring needed here either — those two functions don't wire it as an env
# var, they rely on this same in-code default).
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")
ORG_ASSETS_PREFIX = os.environ.get("ORG_ASSETS_PREFIX", "org-assets/")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
PRESIGNED_URL_EXPIRY = 900
# Phase 3 graded roles (visibility spec §3.1). Default OFF: _allowed_site_ids
# and every read path below behave EXACTLY as today until an environment is
# cut over (repo var PROD_GRADED_ROLES/TEST_GRADED_ROLES -> env, deploy-*.yml,
# same pattern as AUTHORITY_FLIP). No user silently gains visibility.
GRADED_ROLES = os.environ.get("GRADED_ROLES", "").lower() == "true"

ALLOWED_GLOBAL_ROLES = {"admin", "gm", "regional_manager", "pm", "site_manager", "worker", "platform_admin"}
ALLOWED_MEMBERSHIP_ROLES = {"pm", "site_manager", "worker"}
ALLOWED_OBSERVATION_KINDS = {"safety", "quality"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
ALLOWED_OBSERVATION_STATUS = {"open", "closed"}
ALLOWED_ACTION_STATUS = {"open", "in_progress", "blocked", "done"}
ALLOWED_ACTION_PRIORITY = {"low", "medium", "high"}
RECORDING_KINDS = {"video", "audio", "photo"}
_KIND_FOLDER = {"video": "video", "audio": "audio", "photo": "pictures"}

# Site voice (off-the-record): a DEDICATED voice/ prefix that matches NO S3
# event trigger (BUG-13), and the voice_messages table — never recordings /
# create_recording_upload_url (data-isolation invariant).
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "voice/")
ALLOWED_VOICE_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav",
                       "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac"}

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
    if route == "/members/enroll-backfill" and method == "POST":
        return backfill_member_folders(conn, caller)
    m = re.match(r"^/members/([^/]+)/role$", route)
    if m and method == "PATCH":
        return patch_member_role(conn, caller, m.group(1), parse_body(event))
    m_mf = re.match(r"^/members/([^/]+)/folder$", route)
    if m_mf and method == "PATCH":
        return patch_member_folder(conn, caller, m_mf.group(1), parse_body(event))
    m_sp = re.match(r"^/sites/([^/]+)$", route)
    if m_sp and method == "PATCH":
        return patch_org_site(conn, caller, m_sp.group(1), parse_body(event))
    m_sa = re.match(r"^/sites/([^/]+)/(archive|unarchive)$", route)
    if m_sa and method == "POST":
        return archive_site_endpoint(conn, caller, m_sa.group(1), m_sa.group(2))
    m_sm = re.match(r"^/sites/([^/]+)/members$", route)
    if m_sm and method == "GET":
        return list_site_members(conn, caller, m_sm.group(1))
    m_sc = re.match(r"^/sites/([^/]+)/contributors$", route)
    if m_sc and method == "GET":
        return list_site_contributors(conn, caller, m_sc.group(1), event)
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
    m_ai = re.match(r"^/action-items/([^/]+)$", route)
    if m_ai and method == "PATCH":
        return patch_action_item(conn, caller, m_ai.group(1), parse_body(event))

    m_ce = re.match(r"^/content/([^/]+)/([^/]+)$", route)
    if m_ce and method == "PATCH":
        return patch_content(conn, caller, m_ce.group(1), m_ce.group(2), parse_body(event))
    m_ch = re.match(r"^/content/([^/]+)/([^/]+)/history$", route)
    if m_ch and method == "GET":
        return get_content_history(conn, caller, m_ch.group(1), m_ch.group(2))

    if route == "/aliases" and method == "POST":
        return create_alias_endpoint(conn, caller, parse_body(event), event)

    if route == "/live-items":
        if method == "GET":
            return list_live_items(conn, caller, event)

    if route == "/dates" and method == "GET":
        return get_org_dates(conn, caller, event)

    if route == "/timeline" and method == "GET":
        return get_timeline_compat(conn, caller, event)

    if route == "/transcripts" and method == "GET":
        return get_org_transcripts(conn, caller, event)

    if route == "/rollup/portfolio" and method == "GET":
        return list_portfolio_rollup(conn, caller, event)

    if route == "/programme":
        if method == "GET":
            return get_programme(conn, caller, event)
        if method == "PUT":
            return put_programme(conn, caller, event, parse_body(event))

    if route == "/programme/suggestions" and method == "GET":
        return list_suggestions(conn, caller, event)
    m_sc = re.match(r"^/programme/suggestions/([^/]+)/confirm$", route)
    if m_sc and method == "POST":
        return confirm_suggestion(conn, caller, m_sc.group(1), parse_body(event))
    m_sr = re.match(r"^/programme/suggestions/([^/]+)/reject$", route)
    if m_sr and method == "POST":
        return reject_suggestion(conn, caller, m_sr.group(1))

    if route == "/recordings/upload-url" and method == "POST":
        return create_recording_upload_url(conn, caller, parse_body(event))
    m_rc = re.match(r"^/recordings/([^/]+)/complete$", route)
    if m_rc and method == "POST":
        return complete_recording(conn, caller, m_rc.group(1), parse_body(event))

    if route == "/voice/upload-url" and method == "POST":
        return create_voice_upload_url(conn, caller, parse_body(event))
    if route == "/voice/asset-url" and method == "GET":
        return get_voice_asset_url(event, caller)
    m_sv = re.match(r"^/sites/([^/]+)/voice$", route)
    if m_sv and method == "GET":
        return list_site_voice(conn, caller, m_sv.group(1), event)

    return error("not found", 404)


# ----------------------------------------------------------
# /recordings — mobile app (GrandTime) media upload + metadata
# ----------------------------------------------------------
def _safe_seg(s):
    # Conservative S3 path-segment cleanse: non [alnum . _ -] -> underscore
    # (re is imported at module top).
    return re.sub(r"[^A-Za-z0-9._-]", "_", (s or "").strip()) or "unknown"


def _recording_s3_key(display_name, kind, started_at, file_name):
    # Mirror the existing users/{name}/{video|audio|pictures}/{date}/{file}
    # convention so the downstream transcribe/report pipeline consumes app
    # uploads unchanged (site attribution lives in the recordings row, not the key).
    date_str = str(started_at)[:10]  # ISO 'YYYY-MM-DD...' -> 'YYYY-MM-DD'
    folder = _KIND_FOLDER[kind]
    return f"users/{_safe_seg(display_name)}/{folder}/{date_str}/{_safe_seg(file_name)}"


def create_recording_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    kind = body.get("kind")
    if kind not in RECORDING_KINDS:
        return error(f"kind must be one of {sorted(RECORDING_KINDS)}", 400)
    client_uuid = body.get("clientUuid")
    file_name = body.get("fileName")
    content_type = body.get("contentType")
    started_at = body.get("startedAt")
    if not (client_uuid and file_name and content_type and started_at):
        return error("clientUuid, fileName, contentType, startedAt are required", 400)

    site_id = body.get("siteId")
    if site_id:
        site = sites.get_site(conn, site_id)
        if site is None or site["company_id"] != caller["company_id"]:
            return error("site not accessible", 403)

    # Idempotent on the device-side capture id: a resend (retry) reuses the
    # existing row and just re-signs a fresh URL, never creating a duplicate.
    existing = recordings.get_by_client_uuid(conn, caller["id"], client_uuid)
    if existing is not None:
        rec_id, key = existing["id"], existing["s3_key"]
    else:
        display_name = caller.get("folder_name") or \
            f"{caller.get('first_name', '')}_{caller.get('last_name', '')}"
        key = _recording_s3_key(display_name, kind, started_at, file_name)
        try:
            with conn.transaction():          # savepoint: on failure, roll back to here so conn stays usable
                row = recordings.insert_pending(
                    conn, company_id=caller["company_id"], user_id=caller["id"], site_id=site_id,
                    kind=kind, s3_key=key, client_uuid=client_uuid, started_at=started_at,
                    ended_at=body.get("endedAt"), duration_s=body.get("durationS"),
                    resolution=body.get("resolution"), codec=body.get("codec"),
                    size_bytes=body.get("sizeBytes"),
                )
            rec_id = row["id"]
        except UniqueViolation:
            # Either a concurrent request with the same clientUuid (idempotent → return the existing row),
            # or a genuine s3_key collision (different recording, same key) → 409.
            dup = recordings.get_by_client_uuid(conn, caller["id"], client_uuid)
            if dup is not None:
                rec_id, key = dup["id"], dup["s3_key"]
            else:
                return error("a recording with this s3 key already exists", 409)

    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY,
    )
    return ok({"recordingId": rec_id, "uploadUrl": url, "s3Key": key})


def complete_recording(conn, caller, rec_id, body):
    b = body or {}
    gps_track = b.get("gpsTrack")
    if gps_track is not None and not isinstance(gps_track, list):
        # Lenient by design: complete must NOT 400 over a malformed optional
        # telemetry field — a failing complete would strand the recording in
        # un-uploaded state on mobile retry. Drop the bad track and log.
        logger.warning("complete_recording %s: dropping non-list gpsTrack (%s)",
                       rec_id, type(gps_track).__name__)
        gps_track = None
    row = recordings.mark_uploaded(conn, rec_id, caller["company_id"],
                                    b.get("sizeBytes"), gps_track)
    if row is None:
        return error("recording not found", 404)
    return ok({"ok": True})


# ----------------------------------------------------------
# /voice — Site voice (off-the-record; dedicated voice/ prefix + voice_messages)
# ----------------------------------------------------------
def _voice_s3_key(company_id, site_id, sender_id, file_ext):
    # Dedicated voice/ prefix — matches NO S3 event trigger (BUG-13 / data
    # isolation). Scoped by company/site so a listing (and the asset-url ACL)
    # stays tenant-bounded. sender_id keeps sibling clips distinct in audit.
    return (f"{VOICE_PREFIX}{_safe_seg(str(company_id))}/{_safe_seg(str(site_id))}/"
            f"{_safe_seg(str(sender_id))}_{uuid.uuid4().hex}.{file_ext}")


def create_voice_upload_url(conn, caller, body):
    if body is None:
        return error("malformed JSON body", 400)
    content_type = body.get("contentType")
    ext = ALLOWED_VOICE_TYPES.get(content_type) if isinstance(content_type, str) else None
    if ext is None:
        return error(f"contentType must be one of {sorted(ALLOWED_VOICE_TYPES)}", 400)
    site_id = body.get("siteId")
    if not site_id:
        return error("siteId is required", 400)
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("site not accessible", 403)
    key = _voice_s3_key(caller["company_id"], site_id, caller["id"], ext)
    # NOTE: no voice_messages insert here — sendVoice (Task 6) is the sole writer
    # of the row (created when the clip is actually sent). An abandoned recording
    # thus leaves at most an orphan S3 object, reaped by the 30-day voice/ lifecycle.
    url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGNED_URL_EXPIRY)
    return ok({"uploadUrl": url, "s3Key": key})


def get_voice_asset_url(event, caller):
    """Presigned GET for a voice clip. Tenant-isolated: the key must live under
    the caller's own company prefix (voice/{company}/...), so a caller can only
    fetch their company's clips."""
    key = (event.get("queryStringParameters") or {}).get("key", "")
    prefix = f"{VOICE_PREFIX}{_safe_seg(str(caller['company_id']))}/"
    if not key.startswith(prefix):
        return error("key must be one of your company's voice clips", 400)
    url = s3().generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=PRESIGNED_URL_EXPIRY)
    return ok({"url": url, "expiresIn": PRESIGNED_URL_EXPIRY})


def list_site_voice(conn, caller, site_id, event):
    """Reconnect backfill: recent voice messages on a site the caller can see.
    ACL mirrors list_site_members / list_live_items (_allowed_site_ids)."""
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    since = (event.get("queryStringParameters") or {}).get("since") or "1970-01-01T00:00:00Z"
    rows = voice_messages.list_since(conn, caller["company_id"], site_id, since)
    # Serialize to camelCase (matches upload-url/asset-url + the app's parser);
    # never leak snake_case DB column names across the API boundary.
    items = [{"s3Key": r["s3_key"], "senderUserId": str(r["sender_user_id"]),
              "durationS": float(r["duration_s"]) if r["duration_s"] is not None else None,
              "createdAt": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else str(r["created_at"])}
             for r in rows]
    return ok({"items": items, "site": str(site_id)})


# ----------------------------------------------------------
# /me
# ----------------------------------------------------------
def get_me(conn, caller):
    if GRADED_ROLES:
        # MINOR-2: source /me's site_ids from the graded reach (visible_scope
        # via _allowed_site_ids) so the site-selector matches /live-items -- a
        # regional_manager/platform_admin no longer under-returns here vs the
        # dashboard. Non-graded path below is byte-identical to before.
        site_ids = sorted(_allowed_site_ids(conn, caller))
    else:
        site_ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
    # Strip the request-scoped visible_scope memo (MINOR-1) before echoing the
    # caller profile -- it's an internal cache, not part of the /me contract.
    profile = {k: v for k, v in caller.items() if k != "_visible_scope"}
    # Company name for the profile UI (the caller carries company_id; surface
    # the human name too — tenant-split shows each user their company/Corp).
    company = companies.get_company_by_id(conn, caller["company_id"]) if caller.get("company_id") else None
    return ok({**profile, "site_ids": site_ids,
               "scope": resolve_scope(caller["global_role"]),
               "company_name": (company or {}).get("name")})


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
    if GRADED_ROLES:
        # MINOR-2: graded reach (visible_scope) so the selector matches
        # /live-items exactly -- a regional_manager/platform_admin no longer
        # sees fewer sites here than the dashboard. visible_scope and
        # list_sites_by_ids both exclude archived sites, so include_archived
        # has no effect under graded roles (same as every graded read path).
        rows = sites.list_sites_by_ids(conn, _allowed_site_ids(conn, caller))
    elif resolve_scope(caller["global_role"]) == "ALL":
        rows = sites.list_company_sites(conn, caller["company_id"],
                                        include_archived=include_archived)
    else:
        # membership scope never includes archived rows (param ignored)
        ids = memberships.accessible_site_ids(
            conn, caller["id"], caller["global_role"])
        rows = sites.list_sites_by_ids(conn, ids)
    # Card KPIs / labels: member count per site + owning company name. Both are
    # additive and scoped to the sites already returned, so they never widen
    # visibility -- a platform_admin sees every company here, a company role
    # only its own. (#2 company tag on sites, #8 Users count.)
    counts = memberships.count_by_site(conn, [r["id"] for r in rows])
    co_name = {str(c["id"]): c["name"] for c in companies.list_companies(conn)}
    for r in rows:
        r["user_count"] = counts.get(str(r["id"]), 0)
        r["company_name"] = co_name.get(str(r.get("company_id")))
    return ok({"sites": rows})


def _coerce_coord(value, lo, hi, label):
    """Validate an optional coordinate from a request body. Returns
    (coord_or_None, error_response_or_None). org-api is in-VPC — this only
    validates; it never geocodes (BUG-36)."""
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, error(f"{label} must be a number", 400)
    if not (lo <= value <= hi):
        return None, error(f"{label} must be between {lo} and {hi}", 400)
    return float(value), None


def create_org_site(conn, caller, body):
    if caller["global_role"] not in ("admin", "gm", "platform_admin"):
        return error("admin, gm, or platform_admin role required", 403)
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
    # D6: target_company_id lets ONLY a platform_admin write a site into
    # another company; absent (or equal to caller's own) -> today's behavior,
    # unchanged, for every other role.
    target_company_id = caller["company_id"]
    req_company_id = body.get("target_company_id")
    if req_company_id and str(req_company_id) != str(caller["company_id"]):
        if not is_cross_company(caller["global_role"]):
            return error("only platform_admin may create sites in another company", 403)
        if companies.get_company_by_id(conn, req_company_id) is None:
            return error("target company not found", 404)
        target_company_id = req_company_id
    lat, lat_err = _coerce_coord(body.get("latitude"), -90.0, 90.0, "latitude")
    if lat_err:
        return lat_err
    lng, lng_err = _coerce_coord(body.get("longitude"), -180.0, 180.0, "longitude")
    if lng_err:
        return lng_err
    row = sites.create_site(
        conn, target_company_id, name,
        location=body.get("location"), client=body.get("client"),
        industry=body.get("industry"), icon_s3_key=None,
        address=body.get("address"), latitude=lat, longitude=lng,
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
    lat, lat_err = _coerce_coord(body.get("latitude"), -90.0, 90.0, "latitude")
    if lat_err:
        return lat_err
    lng, lng_err = _coerce_coord(body.get("longitude"), -180.0, 180.0, "longitude")
    if lng_err:
        return lng_err
    row = sites.update_site(
        conn, site_id, caller["company_id"],
        name=name, location=body.get("location"),
        client=body.get("client"), industry=body.get("industry"),
        address=body.get("address"), latitude=lat, longitude=lng,
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


def list_site_members(conn, caller, site_id):
    """Members of one site, from memberships (NOT user_mapping) -- the Aurora
    replacement for legacy /site-users. ACL mirrors /live-items: the site id
    must be in the caller's accessible set (graded reach -> platform_admin
    spans companies), which also blocks archived sites."""
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    # Query members with the SITE's company, not the caller's. members_for_site
    # is company-pinned on both joins; pinning to caller["company_id"] returns
    # [] for a cross-company site the platform_admin operator can legitimately
    # reach (its own operator company is empty). The reach gate above already
    # authorized the site, and its company is the correct tenant. For every
    # non-cross caller the site's company == caller's, so behavior is unchanged.
    site = sites.get_site(conn, site_id)
    if site is None:
        return error("site not found", 404)
    rows = memberships.members_for_site(conn, site["company_id"], site_id)
    return ok({"members": rows, "site": str(site_id)})


def list_site_contributors(conn, caller, site_id, event):
    """Recording folders whose topics are attributed to this (site, date) but
    who may NOT be site members -- the read-side complement to G5b write-side
    attribution (recordings.site_id). The site-aggregated timeline fans out over
    members UNION these folders, so a non-member recorder's site-tagged topics
    stop vanishing from the site view (the aggregation-attribution quirk). Same
    ACL as list_site_members (_allowed_site_ids -> graded / cross-company)."""
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    p = event.get("queryStringParameters") or {}
    date = (p.get("date") or "").strip()
    if not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    folders = topics.list_contributor_folders_for_site_date(conn, site_id, date)
    return ok({"folders": folders, "site": str(site_id), "date": date})


# ----------------------------------------------------------
# /members
# ----------------------------------------------------------
def list_members(conn, caller, event):
    # platform_admin (is_cross_company) sits in its own operator company, so
    # the legacy resolve_scope==ALL company-pin would return an empty directory
    # -- span every tenant instead and tag each row with its company name.
    cross = is_cross_company(caller["global_role"])
    if not cross and resolve_scope(caller["global_role"]) != "ALL":
        return error("admin or gm role required", 403)
    include_archived = ((event.get("queryStringParameters") or {})
                        .get("include_archived") == "1")
    if cross:
        rows = users.list_all_users(conn, include_archived=include_archived)
        mem_rows = memberships.list_all_memberships(conn)
        co_name = {str(c["id"]): c["name"] for c in companies.list_companies(conn)}
    else:
        rows = users.list_company_users(conn, caller["company_id"],
                                        include_archived=include_archived)
        mem_rows = memberships.list_company_memberships(conn, caller["company_id"])
        co_name = None
    per_user = {}
    for mem in mem_rows:
        per_user.setdefault(mem["user_id"], []).append(
            {"site_id": mem["site_id"], "role": mem["role"]})
    for row in rows:
        row["memberships"] = per_user.get(row["id"], [])
        if co_name is not None:
            row["company_name"] = co_name.get(str(row.get("company_id")))
    return ok({"members": rows})


def patch_member_role(conn, caller, target_sub, body):
    # widened to admit platform_admin (D6) alongside admin -- required so a
    # platform_admin can reach the grant guard below and patch a role at all;
    # a plain "admin" caller's path is unchanged.
    if caller["global_role"] not in ("admin", "platform_admin"):
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    role = body.get("global_role")
    if not isinstance(role, str) or role not in ALLOWED_GLOBAL_ROLES:
        return error(f"global_role must be one of {sorted(ALLOWED_GLOBAL_ROLES)}", 400)
    # D6: only a platform_admin may grant platform_admin.
    if role == "platform_admin" and not is_cross_company(caller["global_role"]):
        return error("only platform_admin may grant platform_admin", 403)
    row = users.set_global_role(conn, target_sub, caller["company_id"], role)
    if row is None:
        return error("member not found in your company", 404)
    return ok(row)


def patch_member_folder(conn, caller, target_sub, body):
    """Admin-only enrollment step: links a member's login (cognito_sub) to
    the recording-folder identity (folder_name) the orchestrator/app write
    S3 keys under (users/{folder_name}/...) — without this, POST /members
    creates the login+membership but the member's own Today/Timeline (which
    is self-keyed on folder_name) never shows their clips. Normalization
    mirrors lambda_orchestrator.py's safe_name EXACTLY, so an admin typing
    the display name they see in the app produces the identical S3 folder
    segment. folder_name is globally unique (0012) — collision_guard below
    avoids crashing that unique index with a raw IntegrityError."""
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    raw = body.get("folder_name")
    if not isinstance(raw, str) or not raw.strip():
        return error("folder_name is required", 400)
    folder = re.sub(r'[<>:"/\\|?*\s]', '_', raw.strip())
    target = users.get_user_by_sub(conn, target_sub)
    if target is None or target["company_id"] != caller["company_id"]:
        return error("member not found in your company", 404)
    clash = users.get_by_folder_name_global(conn, folder)
    if clash and clash["cognito_sub"] != target_sub:
        return error(f"folder_name '{folder}' is already used by another user", 409)
    users.set_folder_name(conn, target_sub, folder)
    return ok(users.get_user_by_sub(conn, target_sub))


def backfill_member_folders(conn, caller):
    """Admin-only bulk enrollment: links folder_name for every existing
    company login that never got one (e.g. seeded before create_member's
    D4 auto-enroll shipped) — so an admin doesn't have to walk each old
    user through PATCH /members/{sub}/folder by hand. Same normalization
    + collision guard as patch_member_folder/create_member's auto-enroll;
    a folder_name collision skips that one user (reason returned) rather
    than 500ing the whole batch on the global unique index (0012)."""
    if caller["global_role"] != "admin":
        return error("admin role required", 403)
    rows = users.list_company_logins_unenrolled(conn, caller["company_id"])
    enrolled, skipped = [], []
    for row in rows:
        sub = row["cognito_sub"]
        name = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)
        fn = re.sub(r'[<>:"/\\|?*\s]', '_', name.strip())
        if not fn:
            skipped.append({"sub": sub, "reason": "no name"})
            continue
        clash = users.get_by_folder_name_global(conn, fn)
        if clash and clash["cognito_sub"] != sub:
            skipped.append({"sub": sub, "reason": "folder taken by another user"})
            continue
        users.set_folder_name(conn, sub, fn)
        enrolled.append({"sub": sub, "folder_name": fn})
    return ok({"enrolled": enrolled, "skipped": skipped})


def create_member(conn, caller, body):
    """Admin-only. Creates the Cognito login (email invite w/ temp password),
    the Aurora profile, and site memberships. Idempotent: an existing Cognito
    user is looked up instead of failing, and the DB writes are upserts —
    safe to retry after a partial failure (Cognito ok, DB rolled back).
    An existing user keeps their current global_role unless global_role is
    explicitly sent in the body (no silent reset to "worker" on re-invite).
    If the resolved Cognito user already belongs to another company, the
    request is rejected with 409 rather than re-parenting them.
    D6: an optional target_company_id lets ONLY a platform_admin create a
    member in another company (default = caller's own company, unchanged for
    everyone else); granting global_role="platform_admin" itself requires the
    caller to already be a platform_admin."""
    if caller["global_role"] not in ("admin", "platform_admin"):
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
    # D6: only a platform_admin may grant platform_admin.
    if global_role == "platform_admin" and not is_cross_company(caller["global_role"]):
        return error("only platform_admin may grant platform_admin", 403)
    # D6: target_company_id lets ONLY a platform_admin create a member in
    # another company; absent (or equal to caller's own) -> today's behavior,
    # unchanged, for every other role.
    target_company_id = caller["company_id"]
    req_company_id = body.get("target_company_id")
    if req_company_id and str(req_company_id) != str(caller["company_id"]):
        if not is_cross_company(caller["global_role"]):
            return error("only platform_admin may create members in another company", 403)
        if companies.get_company_by_id(conn, req_company_id) is None:
            return error("target company not found", 404)
        target_company_id = req_company_id
    wanted = body.get("memberships") or []
    for mem in wanted:
        if not isinstance(mem, dict) or not mem.get("site_id"):
            return error("each membership needs a site_id", 400)
        if not isinstance(mem.get("role"), str) or mem.get("role") not in ALLOWED_MEMBERSHIP_ROLES:
            return error(
                f"membership role must be one of {sorted(ALLOWED_MEMBERSHIP_ROLES)}", 400)
        site = sites.get_site(conn, mem["site_id"])
        if site is None or site["company_id"] != target_company_id:
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
    if existing and existing["company_id"] and existing["company_id"] != target_company_id:
        return error("user already belongs to another company", 409)
    if existing and existing["company_id"] == target_company_id and existing.get("archived_at"):
        return error("user is archived — unarchive them instead", 409)

    user = users.upsert_user(
        conn, sub, email,
        company_id=target_company_id,
        first_name=body.get("first_name"),
        last_name=body.get("last_name"),
        global_role=global_role,
    )
    # Auto-enroll the recording-folder identity (D4): link the login to its report
    # folder from creation. Underscored safe_name (matches lambda_orchestrator.safe_name
    # and patch_member_folder). Skip on collision — the global unique index (0012)
    # would otherwise 500; folder can still be set later via PATCH /members/{sub}/folder.
    if not user.get("folder_name"):
        fn = re.sub(r'[<>:"/\\|?*\s]', '_', display_name.strip())
        if fn:
            clash = users.get_by_folder_name_global(conn, fn)
            if clash is None or clash["cognito_sub"] == sub:
                user = users.set_folder_name(conn, sub, fn) or user
            else:
                logger.info("create_member: folder_name %r taken, left unset for %s", fn, sub)
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
    allowed_slugs = None
    if GRADED_ROLES:
        sc = scope.visible_scope(conn, caller)
        if sc["user_scope"] != "ALL":                         # admin/gm stay company-wide
            allowed_slugs = {s["slug"] for s in sites.list_sites_by_ids(conn, sc["site_ids"])
                             if s.get("slug")}
    rows = observations.list_observations(
        conn, caller["company_id"], kind=kind,
        date_from=params.get("from"), date_to=params.get("to"),
        site_slug=params.get("site_slug"), allowed_site_slugs=allowed_slugs,
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


def _display_name(caller):
    return " ".join(p for p in (caller.get("first_name"), caller.get("last_name")) if p).strip()


def patch_action_item(conn, caller, action_item_id, body):
    """Edit priority/status/deadline/responsible on one action item (spec §3).
    ACL mirrors patch_observation_status widened to site authority: the task's
    site must be in the caller's reach, and the caller must be admin/gm, a
    pm/site_manager of THAT site, or the current assignee. Reassignment target
    must be a member of the task's site. Addressed by durable action_items.id."""
    if body is None:
        return error("malformed JSON body", 400)
    row = action_items.get_action_item(conn, action_item_id)
    # platform_admin (is_cross_company) edits across every tenant; company roles
    # stay pinned to their own company (mirrors the Team/sites fix in #96).
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return error("action item not found", 404)            # incl. cross-company
    site_id = str(row["site_id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this task's site", 403)  # reach gate
    site_role = memberships.caller_site_roles(conn, caller["id"]).get(site_id)
    is_admin = resolve_scope(caller["global_role"]) == "ALL" or cross
    is_site_authority = site_role in ("pm", "site_manager")
    is_assignee = bool(row["responsible"]) and row["responsible"] == _display_name(caller)
    if not (is_admin or is_site_authority or is_assignee):
        return error("admin/gm, this site's pm/site_manager, or the assignee only", 403)

    fields = {}
    if "priority" in body:
        if body["priority"] not in ALLOWED_ACTION_PRIORITY:
            return error(f"priority must be one of {sorted(ALLOWED_ACTION_PRIORITY)}", 400)
        fields["priority"] = body["priority"]
    if "status" in body:
        if body["status"] not in ALLOWED_ACTION_STATUS:
            return error(f"status must be one of {sorted(ALLOWED_ACTION_STATUS)}", 400)
        fields["status"] = body["status"]
    if "deadline" in body:
        dl = body["deadline"]
        if dl is not None and not (isinstance(dl, str) and REPORT_DATE_RE.match(dl)):
            return error("deadline must be YYYY-MM-DD or null", 400)
        fields["deadline"] = dl                               # write both so the
        fields["deadline_text"] = dl                          # date + free-text mirror agree (§3.5)
    if "responsible" in body:
        target = body["responsible"]
        if not isinstance(target, str) or not target.strip():
            return error("responsible must be a non-empty display name", 400)
        target = target.strip()
        member_names = {" ".join(p for p in (m.get("first_name"), m.get("last_name")) if p).strip()
                        for m in memberships.members_for_site(conn, row["company_id"], site_id)}
        if target not in member_names:
            return error("assignee must be a member of this site", 400)
        fields["responsible"] = target
    if not fields:
        return error("no editable fields provided", 400)

    updated = action_items.update_action_item_fields(conn, action_item_id, fields, caller["cognito_sub"])
    if updated is None:
        return error("action item not found", 404)
    return ok(updated)


def patch_content(conn, caller, table, row_id, body):
    """Edit one free-text content field (spec §3/§5.2, D1). ACL is the D7
    per-item tier -- mirrors patch_action_item exactly: platform_admin
    (cross-company) edits any tenant; company roles stay pinned; the row's
    site must be in the caller's reach; and the caller must be admin/gm, THIS
    site's pm/site_manager, or the item's author (the owning topic's user).
    Writes the corrected text + a content_edits audit row atomically, then
    enqueues a best-effort per-topic re-index (never rolls back the edit)."""
    if body is None:
        return error("malformed JSON body", 400)
    if table not in content.EDITABLE:
        return error(f"table must be one of {sorted(content.EDITABLE)}", 400)
    # Exactly one whitelisted field per request (D1 whole-field edit).
    fields = {k: v for k, v in body.items() if content.is_editable(table, k)}
    if len(fields) != 1:
        return error("exactly one editable field required", 400)
    field, value = next(iter(fields.items()))
    if not isinstance(value, str):
        return error("value must be a string", 400)

    row = content.get_content_row(conn, table, row_id)
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return error("content row not found", 404)      # incl. cross-company
    site_id = str(row["site_id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return error("access denied to this content's site", 403)
    site_role = memberships.caller_site_roles(conn, caller["id"]).get(site_id)
    is_admin = resolve_scope(caller["global_role"]) == "ALL" or cross
    is_site_authority = site_role in ("pm", "site_manager")
    is_author = row.get("author_user_id") is not None and \
        str(row["author_user_id"]) == str(caller["id"])
    if not (is_admin or is_site_authority or is_author):
        return error("admin/gm, this site's pm/site_manager, or the author only", 403)

    before = row.get(field)
    updated = content.update_content_field(conn, table, row_id, field, value)
    if updated is None:
        return error("content row not found", 404)
    content_edits.append_content_edit(
        conn, row["company_id"], table, row_id, field, before, value,
        caller["id"], caller["global_role"])

    # Best-effort per-topic re-index (spec §6: async, never blocks/rolls back
    # the edit). Topic id + folder/date come from the row's owning topic.
    try:
        _enqueue_content_reindex(conn, table, row_id)
    except Exception:
        logger.exception("content edit %s/%s: reindex enqueue failed (edit kept)",
                          table, row_id)

    candidates = diff_candidates(before or "", value)
    return ok({"row": updated, "candidates": candidates})


def _enqueue_content_reindex(conn, table, row_id):
    """Resolve the edited row's owning topic + its (folder, date), then write
    the reindex request artifact. topic_id = the row itself for `topics`, else
    the child's topic_id."""
    if table == "topics":
        tid = row_id
    else:
        r = conn.cursor(row_factory=RealDictRow).execute(
            f"SELECT topic_id FROM {table} WHERE id=%s", (row_id,)).fetchone()
        if not r:
            return
        tid = r["topic_id"]
    meta = conn.cursor(row_factory=RealDictRow).execute(
        "SELECT t.report_date, u.folder_name FROM topics t "
        "LEFT JOIN users u ON u.id = t.user_id WHERE t.id=%s", (tid,)).fetchone()
    if not meta or not meta.get("folder_name"):
        return                                          # unattributed -> skip re-index
    # LAKE_BUCKET (IngestBucketName) is the lake the embed/ingest lambdas read;
    # org-api's S3_BUCKET is DataBucketName, a DIFFERENT bucket. The reindex
    # chain lives on the lake, so enqueue writes there (see Task 20 IAM grant).
    reindex.enqueue_topic_reindex(s3(), LAKE_BUCKET, conn, tid,
                                  meta["folder_name"], str(meta["report_date"]))


_ALIAS_KINDS = ("person", "product", "company", "other")


def create_alias_endpoint(conn, caller, body, event):
    """Confirm a diff candidate into a scoped name_aliases row (spec §5.4, D5,
    D2 glossary confirm). D7 alias tier: site_manager+ only. Optional ?site=
    scopes it to one site; absent => company-wide (site_id NULL)."""
    if body is None:
        return error("malformed JSON body", 400)
    if caller["global_role"] not in ("site_manager", "pm", "gm", "admin", "platform_admin"):
        return error("site_manager or above required to add a glossary alias", 403)
    wrong = (body.get("wrong_term") or "").strip()
    right = (body.get("right_term") or "").strip()
    if not wrong or not right:
        return error("wrong_term and right_term are required", 400)
    kind = body.get("kind") or "other"
    if kind not in _ALIAS_KINDS:
        return error(f"kind must be one of {sorted(_ALIAS_KINDS)}", 400)
    site_id = None
    site_param = (event.get("queryStringParameters") or {}).get("site")
    if site_param:
        site_id, err = _resolve_site_param(conn, caller, site_param)
        if err is not None:
            return err
    row = aliases.create_alias(conn, caller["company_id"], site_id, wrong, right,
                               kind, caller["id"], source="correction")
    return ok(row)


def get_content_history(conn, caller, table, row_id):
    """content_edits trail for one row (spec §5.5 History view). Company-guarded
    via get_content_row (which also resolves cross-company for platform_admin)."""
    if table not in content.EDITABLE:
        return error(f"table must be one of {sorted(content.EDITABLE)}", 400)
    row = content.get_content_row(conn, table, row_id)
    cross = is_cross_company(caller["global_role"])
    if row is None or (not cross and str(row["company_id"]) != str(caller["company_id"])):
        return error("content row not found", 404)
    edits = content_edits.list_content_edits(conn, row["company_id"], table, row_id)
    return ok({"edits": edits})


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
    site_ids = list(_allowed_site_ids(conn, caller))          # graded-aware reach
    rows = topics.list_topics_for_date(conn, site_ids, date,
                                       author_ids=_author_filter(conn, caller))
    return ok({"topics": rows})


# ----------------------------------------------------------
# /dates — Timeline "dots" (Phase 2 read consolidation). Replaces legacy
# get_dates (S3 user_mapping-based scan) which had no ?site access check and,
# on an empty accessible-user set, fell through to marking every report-date
# across ALL users/companies (visibility spec §1.1 dots leak). ACL mirrors
# list_live_items EXACTLY via _allowed_site_ids/_resolve_site_param (defined
# below in the /programme section).
# ----------------------------------------------------------
def _dates_window_start(months) -> "datetime.date":
    """First day of the dots window, in NZ (BUG-37/BUG-19: never derive a
    'today' date from a bare UTC now). months defaults to 2 and is clamped
    to 1..24 so a hostile ?months can't force a full-table scan."""
    try:
        m = int(months)
    except (TypeError, ValueError):
        m = 2
    m = max(1, min(m, 24))
    now_nz = datetime.now(timezone.utc) + timedelta(hours=13)
    return (now_nz - timedelta(days=m * 30)).date()


def get_org_dates(conn, caller, event):
    """Membership-scoped report-date index for the Timeline dots — the Aurora
    replacement for legacy /api/dates (get_dates), whose missing ?site check
    leaked cross-user/cross-company report-dates (visibility spec §1.1). ACL
    mirrors /live-items and /programme EXACTLY: admin/gm see every company
    site, everyone else only their memberships; an explicit ?site outside
    that set is 403'd here (via _resolve_site_param) before any read."""
    p = event.get("queryStringParameters") or {}
    since = _dates_window_start(p.get("months"))
    site_param = p.get("site")
    if site_param:
        site_id, err = _resolve_site_param(conn, caller, site_param)
        if err is not None:
            return err                                  # 403 (out of scope) / 404 (unknown slug)
        site_ids = [site_id]
    else:
        site_ids = list(_allowed_site_ids(conn, caller))
    rows = topics.list_report_dates(conn, site_ids, since,
                                    author_ids=_author_filter(conn, caller))
    return ok({"dates": {str(d): {"hasReport": True} for d in rows}})


# ----------------------------------------------------------
# /rollup/portfolio (Phase 4c leg-1 — deterministic SQL aggregation; no LLM,
# no narrative, no materialization. ACL mirrors list_live_items EXACTLY via
# _allowed_site_ids, defined below in the /programme section — admin/gm see
# every non-archived site in their company, everyone else only their
# non-archived memberships. Status is a pure rule over the merged counts:
# any open high-risk safety observation -> red; else any open safety
# observation or open action item -> yellow; else green. Each row also
# carries last_activity_at — the ALL-TIME max topics.report_date as an ISO
# string or None (rollup.portfolio_counts) — consumed by the Sites cards'
# "Last activity" KPI; _status deliberately ignores it.)
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
# `site` accepts EITHER the org site's UUID (original contract — NOT a
# display name, which can drift from the DB name on rename and used to 403
# every request; see Fable review High #2) OR its slug, resolved via
# _resolve_site_param → sites.get_company_site_by_slug (a first-class,
# company-scoped column — not name-derived, so it doesn't reintroduce that
# drift risk). The ACL below mirrors list_live_items EXACTLY: admin/gm
# (resolve_scope == "ALL") may touch any non-archived site in their own
# company; everyone else is scoped to their own non-archived memberships.
# Because the id must appear in one of those two company/membership-scoped
# sets, this also blocks cross-company access and archived sites for free
# — no separate lookup needed, and the S3 key (programmes/{site_id}/…)
# always uses the resolved UUID, never the slug, so there's no injection
# surface and no orphaned object on a site rename.
# ----------------------------------------------------------
def _author_filter(conn, caller):
    """Per-author id allow-set (visibility spec §3.1 user_scope) when graded
    roles are on, else None = today's site-only scoping. None => unrestricted."""
    if not GRADED_ROLES:
        return None
    return scope.visible_scope(conn, caller)["author_ids"]


def _allowed_site_ids(conn, caller):
    if GRADED_ROLES:
        return scope.visible_scope(conn, caller)["site_ids"]   # graded reach (incl. platform_admin)
    # str() both sides: psycopg returns site ids as uuid.UUID objects, but the
    # ?site= query param arrives as a string — a UUID-vs-str `in` check is
    # always False (every request 403'd, incl. admins). Unit mocks used string
    # ids so this only surfaced against real Aurora (smoke-caught).
    if resolve_scope(caller["global_role"]) == "ALL":
        return {str(s["id"]) for s in sites.list_company_sites(conn, caller["company_id"])}
    return {str(x) for x in memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])}


_SITE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _resolve_site_param(conn, caller, site_param):
    """Resolve a `?site=` query param to (site_id_str, None) or (None,
    error_response). Accepts EITHER the site's UUID (original contract,
    unchanged) OR its slug (new — lets the report side's ?site=<slug> reach
    org endpoints without a lookup of its own). Either way the resolved id
    is ACL-checked against _allowed_site_ids, so a slug can't be used to
    bypass company/membership scoping."""
    if not site_param:
        return None, error("site required", 400)
    if _SITE_UUID_RE.match(site_param):
        site_id = str(site_param)
    else:
        row = sites.get_company_site_by_slug(conn, caller["company_id"], site_param)
        if row is None:
            return None, error("site not found", 404)
        site_id = str(row["id"])
    if site_id not in _allowed_site_ids(conn, caller):
        return None, error("access denied to this site", 403)
    return site_id, None


def get_programme(conn, caller, event):
    site_param = (event.get("queryStringParameters") or {}).get("site")
    site_id, err = _resolve_site_param(conn, caller, site_param)
    if err is not None:
        return err
    doc = programme.read_programme(s3(), S3_BUCKET, site_id)
    return ok({"programme": doc})


def put_programme(conn, caller, event, body):
    site_param = (event.get("queryStringParameters") or {}).get("site")
    if not site_param:
        return error("site required", 400)
    if body is None:
        return error("malformed JSON body", 400)
    if caller["global_role"] not in ("admin", "gm", "pm"):
        return error("programme write requires manager role", 403)
    # Write requires BOTH the manager-role gate above AND site access below
    # — a pm can only write programmes for sites in their own memberships.
    site_id, err = _resolve_site_param(conn, caller, site_param)
    if err is not None:
        return err
    # NZ "today"/"now" — the codebase-wide UTC+13 display convention (BUG-19
    # / see create_org_observation's report_date default).
    updated_at = (datetime.utcnow() + timedelta(hours=13)).isoformat()
    saved = programme.write_programme(s3(), S3_BUCKET, site_id, body, updated_at)
    return ok({"programme": saved})


# ----------------------------------------------------------
# /programme/suggestions (Task 5 — manager review queue for the programme
# matcher's `pending` rows; see docs/superpowers/specs/
# 2026-07-12-programme-item-feedback-design.md §6. Same manager-role gate as
# put_programme, plus the same site ACL as get_programme/put_programme
# (_resolve_site_param for list; _allowed_site_ids directly for confirm/
# reject, since those are addressed by suggestion id, not `?site=`).
#
# Confirm staleness (Fable review CRITICAL #1): re-reads programme.json and
# compares the LIVE task's status/progress_pct against THIS suggestion's own
# snapshot (task_status_before/task_progress_before, taken by the matcher at
# match time) — 409 only when the ONE task this suggestion is about has
# moved on. The original design (design doc §3 D3, §8 item 5) compared the
# whole doc's `updated_at` against match_evidence.programme_updated_at
# instead; that broke because confirm's OWN write re-stamps updated_at, so
# confirming any one suggestion permanently 409'd every other still-pending
# suggestion for the same site (upsert never refreshes match_evidence for a
# pending row). match_evidence.programme_updated_at is still recorded by the
# matcher for audit — it is simply no longer gated on here.
#
# Confirm CAS (Fable review IMPORTANT #2): programme_suggestions.decide()
# (state='pending' -> 'confirmed', guarded by `WHERE state='pending'`) is
# called BEFORE write_programme, and its return value is the authoritative
# gate — None means another request already decided this suggestion (a
# race lost), and the S3 write is skipped entirely. This runs inside
# `with get_connection() as conn:` (see lambda_handler above) — a real
# transaction that commits on clean return / rolls back on any exception —
# so a write_programme failure AFTER a successful decide() rolls the decide
# back too (no orphaned "confirmed" row with no matching S3 write). Were
# this ever called under autocommit instead, that edge would NOT roll back:
# the DB would show "confirmed" while programme.json was never updated.
# ----------------------------------------------------------
_ALLOWED_CONFIRM_STATUSES = ("in_progress", "completed", "blocked", "delayed")


def list_suggestions(conn, caller, event):
    if caller["global_role"] not in ("admin", "gm", "pm"):
        return error("forbidden", 403)
    params = event.get("queryStringParameters") or {}
    site_id, err = _resolve_site_param(conn, caller, params.get("site"))
    if err is not None:
        return err
    state = params.get("state") or "pending"
    rows = programme_suggestions.list_for_site(
        conn, site_id, state=None if state == "all" else state)
    return ok({"suggestions": rows})


def confirm_suggestion(conn, caller, suggestion_id, body):
    if caller["global_role"] not in ("admin", "gm", "pm"):
        return error("forbidden", 403)
    if body is None:
        return error("malformed JSON body", 400)
    row = programme_suggestions.get(conn, suggestion_id)
    if row is None:
        return error("not found", 404)
    if row["state"] != "pending":
        return error("already decided", 409)
    if str(row["site_id"]) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    if row["topic_id"] is None:
        # Fable review IMPORTANT #5: the source topic was deleted/superseded
        # (ON DELETE SET NULL — topics.py delete_topics_for_source[_prefix])
        # before anyone reviewed this suggestion. Caught here, at confirm
        # time, rather than proactively when the topic is superseded (see
        # programme_suggestions.mark_stale docstring for why).
        programme_suggestions.mark_stale(conn, suggestion_id)
        return error("source topic was superseded; re-review", 409)

    # Fable review MINOR #9: validate reviewer overrides BEFORE they can
    # reach programme.json — a bad "status" or non-int/out-of-range
    # "progress_pct" used to either write garbage or raise a TypeError deep
    # in the never-lower-progress comparison below (str < int -> 500).
    if "status" in body:
        status_override = body.get("status")
        if status_override not in _ALLOWED_CONFIRM_STATUSES:
            return error(f"status must be one of {sorted(_ALLOWED_CONFIRM_STATUSES)}", 400)
    if "progress_pct" in body:
        progress_override = body.get("progress_pct")
        if (isinstance(progress_override, bool) or not isinstance(progress_override, int)
                or not (0 <= progress_override <= 100)):
            return error("progress_pct must be an integer 0-100", 400)

    doc = programme.read_programme(s3(), S3_BUCKET, row["site_id"])
    if doc is None:
        return error("programme not found", 409)
    task = next((t for t in doc.get("leaves", []) if t.get("task_id") == row["task_id"]), None)
    if task is None:
        programme_suggestions.mark_stale(conn, suggestion_id)
        return error("task no longer in programme", 409)

    # Fable review CRITICAL #1: per-task staleness — the live task must
    # still be in the state the matcher saw when it made THIS suggestion.
    # Scoped to the one task this suggestion is about (see module comment
    # above list_suggestions for why the old whole-doc check was wrong).
    if (task.get("status") != row["task_status_before"]
            or task.get("progress_pct") != row["task_progress_before"]):
        return error("task changed since this suggestion was made; re-review", 409)

    new_status = body.get("status") if "status" in body else row["suggested_status"]
    new_progress = body.get("progress_pct") if "progress_pct" in body else row["suggested_progress"]
    # Never silently lower progress from the auto-suggested value — only an
    # explicit reviewer-typed number ("progress_pct" present in body) may.
    if (new_progress is not None and task.get("progress_pct") is not None
            and new_progress < task["progress_pct"] and "progress_pct" not in body):
        new_progress = task["progress_pct"]

    if new_status is not None:
        task["status"] = new_status
    if new_progress is not None:
        task["progress_pct"] = new_progress

    # Fable review IMPORTANT #2: decide() is the compare-and-swap gate —
    # called BEFORE the S3 write, and its return value decides whether we
    # write at all (see module comment above list_suggestions).
    decided = programme_suggestions.decide(
        conn, suggestion_id, "confirmed", decided_by=caller["id"],
        applied_status=new_status, applied_progress=new_progress)
    if decided is None:
        return error("already decided", 409)

    new_ts = datetime.now(timezone.utc).isoformat()
    programme.write_programme(s3(), S3_BUCKET, row["site_id"], doc, new_ts)
    return ok({"confirmed": True, "task_id": row["task_id"],
              "applied_status": new_status, "applied_progress": new_progress})


def reject_suggestion(conn, caller, suggestion_id):
    if caller["global_role"] not in ("admin", "gm", "pm"):
        return error("forbidden", 403)
    row = programme_suggestions.get(conn, suggestion_id)
    if row is None:
        return error("not found", 404)
    if row["state"] != "pending":
        return error("already decided", 409)
    if str(row["site_id"]) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    programme_suggestions.decide(conn, suggestion_id, "rejected", decided_by=caller["id"])
    return ok({"rejected": True})


# ----------------------------------------------------------
# /timeline (authority-flip Task 4 — org-api compatibility shim). D1
# contract: byte-identical S3 daily_report.json for days without extraction
# topics; the same shape RENDERED from Aurora extraction topics for days
# that have them. Consumed by the same fieldsight-ui timeline.js that reads
# prod's /api/timeline today — this is the drop-in replacement read path.
#
# RETARGET override 5 (multi-tenant guard): the lake bucket (reports/,
# extractions/) and topics.source_s3_key are keyed by folder_name alone —
# no company scoping baked into the S3 key or the LIKE-prefix match. A
# caller resolving another company's folder_name (deliberately or by
# guessing) must never reach an S3 GetObject/Aurora read for it. Two
# distinct cases:
#   - non-ALL scope (worker/pm/site_manager): `user` is forced to the
#     caller's OWN folder_name below; an explicit ?user= that DIFFERS from
#     it is rejected with 403 before any read (Fix wave 1 review finding 2 —
#     previously it was silently overridden to self, returning the CALLER's
#     own report under the URL the caller asked a different user for).
#   - ALL scope (admin/gm) with an explicit ?user=: verified against
#     users.get_by_folder_name(company_id, user) BEFORE any Aurora/S3 read
#     (_render_timeline_for_user). admin_disambiguation's own candidate
#     list is pre-filtered the same way (S3-listed folders) or scoped by
#     construction (the Aurora union query filters u.company_id).
# summary_report.json (admin_disambiguation's first branch) is a day-level
# aggregate built LAKE-WIDE across every company's folders by the report
# generator — it has no per-folder identity to ACL-check, so instead the
# whole branch is gated on the CALLER's company (Fix wave 1 review finding
# 1): only the lake-owner/internal company (COMPANY_NAME, see above) may
# see it verbatim; every other company's admin/gm skips straight to the
# company-scoped disambiguation union below. Fail-closed: if the owner
# company can't be resolved, the branch is skipped for everyone.
# ----------------------------------------------------------
_LAKE_NOT_FOUND_CODES = ("NoSuchKey", "404")


def _get_lake_json(key):
    """Fetch+parse a lake-bucket JSON doc. None on S3 NoSuchKey/404 (no
    report for this user/date — a legitimate miss, not a failure); any
    other ClientError (e.g. AccessDenied) is re-raised — same posture as
    repositories/programme.py's read_programme (see its docstring): once
    s3:ListBucket is granted on reports/* the IAM policy should never
    produce AccessDenied for a merely-missing key."""
    try:
        obj = s3().get_object(Bucket=LAKE_BUCKET, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in _LAKE_NOT_FOUND_CODES:
            return None
        raise
    return json.loads(obj["Body"].read().decode("utf-8"))


def _list_report_folders(date):
    """S3 folder discovery for admin_disambiguation — mirrors
    lambda_fieldsight_api.py's find_any_report (lines 264-291): list every
    reports/{date}/*/daily_report.json object and take the folder segment,
    skipping any *_debug* companion file. Returns [] (not an error) on any
    listing failure — admin_disambiguation still has the Aurora union to
    fall back on, so a transient S3 issue here shouldn't 500 the request.
    Paginated (Fix wave 1 review finding 3): in the multi-tenant lake a
    single date prefix holds every company's folders, so a plain
    list_objects_v2 call silently truncates at 1000 keys and drops users
    from available_users — mirrors the paginator pattern used elsewhere in
    this repo (lambda_ingest.py, lambda_item_writer.py, lambda_fieldsight_
    api.py's get_dates)."""
    prefix = f"reports/{date}/"
    folders = []
    try:
        paginator = s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=LAKE_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/daily_report.json") and "_debug" not in key:
                    parts = key[len(prefix):].split("/")
                    if len(parts) >= 2:
                        folders.append(parts[0])
    except ClientError:
        pass
    return folders


_SEV_TO_RISK = {"major": "high", "minor": "medium", "none": "low"}


def render_report_shape(rows, doc, date, folder, conn=None):
    """Pure function: render Aurora extraction topics INTO the
    daily_report.json shape, optionally merging the doc's own prose fields
    (executive_summary etc.) when a same-day S3 doc also exists (e.g. an
    earlier extraction pass already wrote one before the nightly report
    ran). `rows` must already be D3-ordered (list_topics_for_source_prefix's
    ORDER BY time_range NULLS LAST, created_at, id) — topic_id here is
    purely positional (index into that order), not any DB id.

    `conn` (optional, trailing kwarg — Task 1b) enables the redaction-status
    lookup below; callers that don't pass it (or pass None) simply get
    `redacted: False` for every topic, unchanged from before this field
    existed."""
    doc = doc or {}
    topics_out = []
    _redacted = redactions.list_active_for_topics(conn, [r["id"] for r in rows]) if conn is not None else {}
    for i, t in enumerate(rows):
        flags = [{"observation": f["observation"],
                  "risk_level": _SEV_TO_RISK.get(f["severity"], "medium"),
                  "recommended_action": f["recommended_action"],
                  "id": str(f["id"]), "source_table": "findings"}
                 for f in t["findings"] if f["domain"] == "safety"]
        if not flags:                               # pre-#46 legacy extractions
            flags = [{"observation": s["observation"], "risk_level": s["risk_level"],
                      "recommended_action": None,
                      "id": str(s["id"]), "source_table": "safety_observations"}
                     for s in t["safety_observations"]]
        topics_out.append({
            "topic_id": i,
            "topic_row_id": str(t["id"]),           # durable topics.id (D fix — editable anchor)
            "time_range": t["time_range"],
            "topic_title": t["title"],
            "category": t["category"],
            "participants": t["participants"] or [],
            "summary": t["summary"],
            "key_decisions": [],                    # D3: v1, decisions table deferred
            "action_items": [{"id": str(a["id"]), "action": a["text"], "responsible": a["responsible"],
                              "deadline": a["deadline_text"] or (str(a["deadline"]) if a["deadline"] else None),
                              "priority": a["priority"], "status": a["status"]} for a in t["action_items"]],
            "safety_flags": flags,
            "related_photos": [ph["s3_key"].rsplit("/", 1)[-1] for ph in t["photos"]],
            "findings": t["findings"],              # additive passthrough (D3)
            "work_class": t.get("work_class"),
            "work_confidence": t.get("work_confidence"),
            "is_mixed": t.get("is_mixed"),
            "redacted": t["id"] in _redacted,
            "redaction_id": (_redacted.get(t["id"]) or {}).get("id"),
        })
    return {
        "report_date": date,
        "site": rows[0]["site_name"],
        "user_name": rows[0]["user_name"] or folder.replace("_", " "),
        "executive_summary": doc.get("executive_summary"),
        "safety_observations": doc.get("safety_observations", []),
        "quality_and_compliance": doc.get("quality_and_compliance", []),
        "critical_dates_and_deadlines": doc.get("critical_dates_and_deadlines", []),
        "_report_metadata": {"source": "live_extraction", "version": "flip-v1"},
        "topics": topics_out,
    }


def _render_timeline_for_user(conn, caller, date, user, cross_user_clip=False):
    """The single-(user, date) D1 read: Aurora override when extraction
    topics exist AND at least one survives the site ACL filter, else S3
    verbatim, else the 404 body. Callers (get_timeline_compat's explicit-
    user path, admin_disambiguation's one-candidate recursion) are each
    responsible for verifying `user`'s folder belongs to the caller's
    company BEFORE calling this — see the multi-tenant note on the /timeline
    section header above.

    cross_user_clip (review CRITICAL-1): set by get_timeline_compat's graded
    non-ALL path when a pm/regional_manager/site_manager views SOMEONE ELSE's
    timeline (user != own folder). The target may be a multi-site member whose
    day spans sites OUTSIDE the caller's scope, so the served doc must be built
    ONLY from the caller's site-clipped Aurora rows: the target's whole-day
    free-text prose (executive_summary/safety_observations/quality_and_
    compliance/critical_dates_and_deadlines in the S3 daily_report.json) is NOT
    site-scoped and would leak that out-of-scope content, so it is never merged
    (doc forced to None) and the verbatim S3 fallback is never served -- when no
    in-scope Aurora topics exist there is nothing safe to show (404). Own-
    timeline and ALL-scope (admin/gm/platform_admin) callers pass
    cross_user_clip=False and are UNCHANGED."""
    def _aurora_shape(prefix):
        """Return the id-carrying rendered shape for `prefix` if it has
        Aurora topics inside the caller's site ACL, else None."""
        if not topics.has_topics_for_source_prefix(conn, prefix):
            return None
        allowed = _allowed_site_ids(conn, caller)
        rows = [r for r in topics.list_topics_for_source_prefix(conn, prefix)
                if str(r["site_id"]) in allowed]
        if not rows:
            return None
        # CRITICAL-1: cross-user graded view never merges the target's whole-day
        # prose (not site-clipped). Topic rows are already site-clipped above.
        doc = None if cross_user_clip else \
            _get_lake_json(f"reports/{date}/{user}/daily_report.json")
        return render_report_shape(rows, doc, date, user, conn=conn)

    # D fix (spec §5.1): prefer the Aurora-rendered shape whenever Aurora topics
    # exist for this (user, date) -- extraction-sourced OR report-sourced -- so
    # report-sourced content is editable exactly like extraction-sourced. Only
    # a day with NO Aurora topics at all keeps the byte-verbatim S3 contract.
    for prefix in (f"extractions/{user}/{date}/", f"reports/{date}/{user}/"):
        shape = _aurora_shape(prefix)
        if shape is not None:
            return ok(shape)
    if cross_user_clip:
        # CRITICAL-1: no in-scope Aurora topics for this (target, date). The
        # verbatim S3 daily_report.json is NOT site-clipped, so serving it would
        # leak the target's out-of-scope content. Nothing safe to show -> 404.
        return ok({"message": f"No in-scope report for {user} on {date}", "date": date}, 404)
    doc = _get_lake_json(f"reports/{date}/{user}/daily_report.json")
    if doc is not None:
        return ok(doc)                              # VERBATIM (byte-identical history)
    return ok({"message": f"No report for {user} on {date}", "date": date}, 404)


def admin_disambiguation(conn, caller, date):
    """D1(iv): admin/gm asked for a date with no ?user=. Try the day's
    aggregate summary_report.json verbatim first — but ONLY for the
    lake-owner/internal company (Fix wave 1 review finding 1): this doc is
    built LAKE-WIDE across every company's folders by the report generator,
    so serving it to a customer-company admin was a cross-tenant leak.
    Fail-closed: if the owner company can't be resolved, the branch is
    skipped for everyone, not just non-owners. Otherwise union S3-listed
    report folders (company-filtered via users.get_by_folder_name —
    RETARGET override 5) with Aurora's extraction-sourced folder names
    (already company-scoped by the repository query itself). One candidate
    recurses into the single-user path; several return the disambiguation
    envelope the UI's meeting-picker expects; none is a 404."""
    owner = companies.get_company_by_name(conn, COMPANY_NAME)
    if owner is not None and str(caller["company_id"]) == str(owner["id"]):
        doc = _get_lake_json(f"reports/{date}/summary_report.json")
        if doc is not None:
            return ok(doc)
    candidates = set()
    for folder in _list_report_folders(date):
        if users.get_by_folder_name(conn, caller["company_id"], folder) is not None:
            candidates.add(folder)
    candidates.update(topics.list_extraction_folder_names_for_date(conn, caller["company_id"], date))
    if not candidates:
        return ok({"message": f"No reports for {date}", "date": date}, 404)
    if len(candidates) == 1:
        return _render_timeline_for_user(conn, caller, date, next(iter(candidates)))
    return ok({"date": date, "available_users": sorted(candidates)})


def _can_view_folder(conn, caller, target_folder):
    """GRADED /timeline authority (spec §3.2): may caller read target_folder's
    (folder, date) timeline? Own folder always; SITE (pm/regional) any user on
    an in-scope site; SELF+WORKERS (site_manager) own + workers on in-scope
    sites; SELF (worker) own only. Company-pinned: the target is resolved
    within caller.company_id first (unless caller is cross-company)."""
    sc = scope.visible_scope(conn, caller)
    if target_folder and target_folder == sc["self_folder"]:
        return True
    if sc["cross_company"]:
        target = users.get_by_folder_name_global(conn, target_folder)
    else:
        target = users.get_by_folder_name(conn, caller["company_id"], target_folder)
    if target is None:
        return False                                          # not in caller's company / unknown
    if sc["user_scope"] == "ALL":
        return True
    if sc["user_scope"] == "SITE":
        target_sites = memberships.caller_site_roles(conn, target["id"])
        return any(sid in sc["site_ids"] for sid in target_sites)   # target is on an in-scope site
    return str(target["id"]) in (sc["author_ids"] or set())   # SELF / SELF+WORKERS


def get_timeline_compat(conn, caller, event):
    p = event.get("queryStringParameters") or {}
    date, user = p.get("date"), (p.get("user") or "").strip()
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    if GRADED_ROLES:
        sc = scope.visible_scope(conn, caller)
        if sc["user_scope"] == "ALL":                         # admin/gm/platform_admin
            if not user:
                return admin_disambiguation(conn, caller, date)
            if not sc["cross_company"] and \
                    users.get_by_folder_name(conn, caller["company_id"], user) is None:
                return error("user not found in your company", 404)
            return _render_timeline_for_user(conn, caller, date, user)
        # graded non-ALL: default self, but pm/regional/site_manager may view
        # in-scope users (spec §3.2 -- no longer hard-forced to self).
        if not user:
            user = sc["self_folder"]
            if not user:
                return error("no folder mapping for your account", 403)
        if not _can_view_folder(conn, caller, user):
            return error("not permitted to view this timeline", 403)
        # CRITICAL-1: viewing SOMEONE ELSE (user != own folder) must return a
        # site-clipped view built only from in-scope Aurora rows -- never the
        # target's un-clipped whole-day prose or verbatim daily_report.json,
        # which can span the target's other, out-of-scope sites. Own timeline
        # (user == self_folder) is unchanged (full verbatim/prose).
        cross_user = user != sc["self_folder"]
        return _render_timeline_for_user(conn, caller, date, user, cross_user_clip=cross_user)
    # ---- GRADED_ROLES off: today's behavior, verbatim ----
    is_all = resolve_scope(caller["global_role"]) == "ALL"
    if not is_all:
        own = caller.get("folder_name")
        if not own:
            return error("no folder mapping for your account", 403)
        if user and user != own:
            # Fix wave 1 review finding 2: an explicit ?user= for a
            # different folder used to be silently overridden to self
            # (D10), returning the CALLER's own report under the URL the
            # caller asked a different user for — a mislabeled-data bug,
            # not a leak, but still wrong. Refines D10's forced-self
            # posture: reject instead of silently substituting.
            return error("you may only view your own timeline", 403)
        user = own                                  # D10: forced self, no ACL lookup needed
    if not user:                                    # admin/gm, no user
        return admin_disambiguation(conn, caller, date)
    if is_all and users.get_by_folder_name(conn, caller["company_id"], user) is None:
        # RETARGET override 5: an explicit ?user= from an ALL-scope caller
        # must resolve to a folder in THIS company before any lake read.
        return error("user not found in your company", 404)
    return _render_timeline_for_user(conn, caller, date, user)


# ----------------------------------------------------------
# /transcripts — Aurora-identity read of transcript S3 objects
# ----------------------------------------------------------

def _org_parse_time_to_seconds(time_str):
    """Mirror lambda_fieldsight_api.parse_time_to_seconds verbatim. Kept as
    a local copy rather than a cross-lambda import (the pattern lambda_
    item_writer.py/lambda_embed_report.py use for `import lambda_ingest`):
    lambda_fieldsight_api.py constructs LIVE boto3 clients (s3/lambda/
    dynamodb) at MODULE level, so importing it here would run that
    construction on every lambda_org_api import -- including test
    collection, where no AWS region/credentials are guaranteed to be
    configured (confirmed locally: boto3.client('s3') raises without a
    region unless AWS_DEFAULT_REGION is already set by some other test
    module's import order -- fragile to depend on)."""
    parts = time_str.replace(" ", "").split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0


def _org_extract_time_seconds_from_filename(filename):
    """Mirror lambda_fieldsight_api.extract_time_seconds_from_filename
    verbatim -- see _org_parse_time_to_seconds for why this is a local
    copy, not an import. BUG-01/BUG-11: always anchor on the full
    YYYY-MM-DD_ prefix before capturing HH-MM-SS, never a bare
    (\\d{2})-(\\d{2})-(\\d{2}) (matches the date, not the time)."""
    off_match = re.search(r"_off([\d.]+)_to", filename)
    base_match = re.search(r"\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})", filename)
    if off_match and base_match:
        h, m, s = int(base_match.group(1)), int(base_match.group(2)), int(base_match.group(3))
        return h * 3600 + m * 60 + s + int(float(off_match.group(1)))
    if base_match:
        return int(base_match.group(1)) * 3600 + int(base_match.group(2)) * 60 + int(base_match.group(3))
    return None


def _read_org_transcripts(date, folder, start_time, end_time):
    """S3 read + normalize for one (folder, date) window -- mirrors
    lambda_fieldsight_api.get_transcripts's locate/parse/response-shape
    verbatim (same per-file `segments[]` and speaker-turn `speaker_
    segments[]` fields) so scripts/composites/transcript-list.js needs no
    reshape. Unlike the legacy endpoint's 4-prefix fallback hunt (flat-
    folder / spaced-display-name variants kept for pre-BUG-12 data), this
    route only serves the current transcripts/{folder}/{date}/ convention:
    BUG-12 already normalized every write path onto it, and Aurora only
    ever hands this route a `folder_name` (never a spaced display name),
    so those extra legacy variants don't apply here."""
    start_sec = _org_parse_time_to_seconds(start_time) - 60 if start_time else 0
    end_sec = _org_parse_time_to_seconds(end_time) + 60 if end_time else 86400
    if start_sec < 0:
        start_sec = 0

    prefix = f"transcripts/{folder}/{date}/"
    transcript_files = []
    try:
        paginator = s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".json"):
                    transcript_files.append(key)
    except ClientError:
        # Same posture as _list_report_folders: a listing failure (e.g. an
        # IAM edge case) degrades to "no transcripts" rather than a 500.
        pass

    if not transcript_files:
        return {"text": "", "segments": [], "speaker_segments": [], "message": "No transcripts found"}

    all_speaker_segs = []
    segments = []
    for key in sorted(transcript_files):
        filename = key.split("/")[-1]
        file_time_sec = _org_extract_time_seconds_from_filename(filename)
        if file_time_sec is None:
            continue
        file_end_sec = file_time_sec + 600
        if file_end_sec < start_sec or file_time_sec > end_sec:
            continue
        try:
            obj = s3().get_object(Bucket=S3_BUCKET, Key=key)
            data = json.loads(obj["Body"].read().decode("utf-8"))
            results = data.get("results", {})
            full_text = results.get("transcripts", [{}])[0].get("transcript", "")

            # Speaker-segmented audio_segments from Transcribe
            audio_segs = results.get("audio_segments", [])
            for aseg in audio_segs:
                seg_start = float(aseg.get("start_time", 0))
                seg_end = float(aseg.get("end_time", 0))
                abs_start = file_time_sec + seg_start
                abs_end = file_time_sec + seg_end

                # Filter to topic time range
                if abs_end < start_sec or abs_start > end_sec:
                    continue

                speaker = aseg.get("speaker_label", "spk_0")
                text = aseg.get("transcript", "")
                if not text.strip():
                    continue

                ah, am, asec_v = int(abs_start) // 3600, (int(abs_start) % 3600) // 60, int(abs_start) % 60
                all_speaker_segs.append({
                    "speaker": speaker,
                    "text": text,
                    "start": round(abs_start, 1),
                    "end": round(abs_end, 1),
                    "time_label": f"{ah:02d}:{am:02d}:{asec_v:02d}",
                    "duration": round(seg_end - seg_start, 1),
                })

            # Word-level filtered text
            items = results.get("items", [])
            in_range_words = []
            total_words = 0
            for item in items:
                if item.get("type") != "pronunciation":
                    continue
                total_words += 1
                word_start = float(item.get("start_time", 0))
                abs_ws = file_time_sec + word_start
                if start_sec <= abs_ws <= end_sec:
                    in_range_words.append(item.get("alternatives", [{}])[0].get("content", ""))

            h, m, s = file_time_sec // 3600, (file_time_sec % 3600) // 60, file_time_sec % 60
            segments.append({
                "time": f"{h:02d}:{m:02d}:{s:02d}",
                "time_seconds": file_time_sec,
                "text": full_text,
                "filtered_text": " ".join(in_range_words),
                "filename": filename,
                "word_count": total_words,
                "in_range_count": len(in_range_words),
                "speaker_segment_count": len(
                    [sg for sg in all_speaker_segs if sg.get("start", 0) >= file_time_sec]),
            })
        except Exception as e:
            logger.warning("transcripts: failed to load %s: %s", key, e)

    all_speaker_segs.sort(key=lambda sg: sg["start"])
    filtered_full = " ".join(sg["text"] for sg in all_speaker_segs)
    speakers = sorted({sg["speaker"] for sg in all_speaker_segs})

    return {
        "text": filtered_full,
        "filtered_text": filtered_full,
        "segments": segments,
        "speaker_segments": all_speaker_segs,
        "speakers": speakers,
        "count": len(segments),
        "speaker_count": len(speakers),
        "total_speaker_segments": len(all_speaker_segs),
    }


def get_org_transcripts(conn, caller, event):
    """GET /api/org/transcripts?date=&user=&start=&end= -- Aurora-identity
    transcript read (Timeline "Transcript" tab bug fix). scripts/api/
    transcripts.js unconditionally called the LEGACY /transcripts gateway
    (lambda_fieldsight_api.get_transcripts), whose get_caller_identity
    resolves role/display_name from the OLD DynamoDB fieldsight-users
    table / config/user_mapping.json -- a DIFFERENT identity store than
    the Aurora `users` table. An Aurora-only account (e.g. site_manager
    Ben_UCPK) resolves there to role='viewer', display_name='', so
    can_access_user_data 403s even though the transcript S3 object
    exists. This route resolves the caller from Aurora instead (dispatch's
    `caller`, same as every other /api/org/* route) and applies get_
    timeline_compat's GRADED_ROLES-off ACL verbatim: a non-ALL caller may
    only read their own folder (?user= for anyone else is 403, silently
    forcing to self the way D10 used to is exactly the mislabeled-data bug
    fix wave 1 closed for /timeline -- reject, don't substitute); admin/gm
    may pass ?user=, defaulting to their own folder when omitted, and an
    explicit ?user= must resolve to a folder in their company (RETARGET
    override 5, same as /timeline). Phase 3 GRADED_ROLES still defaults
    off and this route doesn't add a graded branch -- when that flag
    flips, /timeline's graded path is the template to extend this with.
    The S3 read/normalize is delegated to _read_org_transcripts (mirrors
    the legacy endpoint's shape so transcript-list.js needs no reshape)."""
    p = event.get("queryStringParameters") or {}
    date = p.get("date")
    user = (p.get("user") or "").strip()
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    is_all = resolve_scope(caller["global_role"]) == "ALL"
    if not is_all:
        own = caller.get("folder_name")
        if not own:
            return error("no folder mapping for your account", 403)
        if user and user != own:
            return error("you may only view your own transcripts", 403)
        user = own
    elif not user:
        user = caller.get("folder_name") or ""
    if not user:
        return error("user required", 400)
    if is_all and users.get_by_folder_name(conn, caller["company_id"], user) is None:
        return error("user not found in your company", 404)
    return ok(_read_org_transcripts(date, user, p.get("start") or "", p.get("end") or ""))
