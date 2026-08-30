"""
Lambda: sitesync-api v2.0 — Backend API for SiteSync Frontend

Changes from v1.0:
- ADD: Permission filtering — users only see their own data (admin sees all)
- ADD: GET /api/transcripts — raw Transcribe text for a topic time range
- ADD: GET /api/audio-segments — presigned URLs for VAD audio segments
- ADD: POST /api/actions/toggle — persist action item check/uncheck to DynamoDB
- ADD: GET /api/actions — load persisted action states
- CHANGE: /api/timeline auto-resolves user from JWT if not specified

Routes:
  GET  /api/health                                    → health check (no auth)
  GET  /api/timeline?date=YYYY-MM-DD&user=Name        → daily report JSON
  GET  /api/dates?months=2                             → dates with data
  GET  /api/media/presigned-url?key=xxx                → S3 presigned URL
  GET  /api/reports/history?limit=20                   → report generation history
  POST /api/reports/generate                           → trigger report generation
  GET  /api/users                                      → list all mapped users
  GET  /api/transcripts?date=YYYY-MM-DD&user=Name&start=HH:MM:SS&end=HH:MM:SS
  GET  /api/audio-segments?date=YYYY-MM-DD&user=Name&start=HH:MM:SS&end=HH:MM:SS
  POST /api/actions/toggle                             → { date, topic_id, action_index, checked }
  GET  /api/actions?date=YYYY-MM-DD                    → persisted action states

Environment Variables:
    S3_BUCKET           fieldsight-data-509194952652
    REPORT_PREFIX       reports/
    ITEMS_TABLE         fieldsight-items
    REPORTS_TABLE       fieldsight-reports
    AUDIT_TABLE         fieldsight-audit
    USERS_TABLE         fieldsight-users
    REPORT_FUNCTION     fieldsight-report-generator
"""

import os
import json
import logging
import re
import boto3
from datetime import datetime, timedelta
from urllib.parse import unquote_plus

import deletion_mirror

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')
lambda_client = boto3.client('lambda')
dynamodb = boto3.resource('dynamodb')

S3_BUCKET = os.environ.get('S3_BUCKET', 'fieldsight-data-509194952652')
REPORT_PREFIX = os.environ.get('REPORT_PREFIX', 'reports/')
ITEMS_TABLE = os.environ.get('ITEMS_TABLE', 'fieldsight-items')
REPORTS_TABLE = os.environ.get('REPORTS_TABLE', 'fieldsight-reports')
AUDIT_TABLE = os.environ.get('AUDIT_TABLE', 'fieldsight-audit')
USERS_TABLE = os.environ.get('USERS_TABLE', 'fieldsight-users')
REPORT_FUNCTION = os.environ.get('REPORT_FUNCTION', 'fieldsight-report-generator')
ASK_AGENT_FUNCTION = os.environ.get('ASK_AGENT_FUNCTION', 'fieldsight-ask-agent')
PRESIGNED_URL_EXPIRY = 900

_user_mapping_cache = None
_user_mapping_ts = 0


def ok(body, status=200):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }

def error(message, status=400):
    return ok({'error': message}, status)


def get_caller_identity(event):
    claims = event.get('requestContext', {}).get('authorizer', {}).get('claims', {})
    email = claims.get('email', '')
    name = claims.get('name', email)
    sub = claims.get('sub', '')
    user_info = {'sub': sub, 'email': email, 'name': name,
                 'role': 'viewer', 'display_name': '', 'device_id': '',
                 'sites': [], 'managed_sites': [], 'company_id': ''}
    if sub:
        try:
            table = dynamodb.Table(USERS_TABLE)
            resp = table.get_item(Key={'PK': f'USER#{sub}', 'SK': 'PROFILE'})
            if 'Item' in resp:
                item = resp['Item']
                user_info['role'] = item.get('role', 'viewer')
                user_info['display_name'] = item.get('display_name', name)
                user_info['device_id'] = item.get('device_id', '')
                user_info['sites'] = item.get('sites', [])
                user_info['managed_sites'] = item.get('managed_sites', [])
                user_info['company_id'] = item.get('company_id', '')
        except Exception as e:
            logger.warning(f"User lookup failed for {sub}: {e}")
    if not user_info['display_name']:
        mapping = load_user_mapping()
        for dev_id, info in mapping.get('mapping', {}).items():
            if info.get('name', '').lower() == name.lower():
                user_info['display_name'] = info['name']
                user_info['device_id'] = dev_id
                user_info['role'] = info.get('role', 'worker')
                user_info['sites'] = info.get('sites', [])
                break
    return user_info

def load_user_mapping():
    global _user_mapping_cache, _user_mapping_ts
    now = datetime.utcnow().timestamp()
    if _user_mapping_cache and (now - _user_mapping_ts) < 300:
        return _user_mapping_cache
    try:
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key='config/user_mapping.json')
        _user_mapping_cache = json.loads(obj['Body'].read().decode('utf-8'))
        _user_mapping_ts = now
    except Exception:
        _user_mapping_cache = {'mapping': {}, 'sites': {}}
    return _user_mapping_cache

def resolve_user_display_name(caller):
    if caller['display_name']:
        return caller['display_name'].replace(' ', '_')
    return ''

# Role hierarchy: admin/gm > pm > site_manager > worker
MANAGEMENT_ROLES = ('admin', 'gm', 'pm', 'site_manager')

def get_accessible_sites(caller):
    """Return list of site IDs this caller can access."""
    role = caller['role']
    if role in ('admin', 'gm'):
        mapping = load_user_mapping()
        return list(mapping.get('sites', {}).keys())
    if role == 'pm':
        return list(caller.get('managed_sites', []))
    if role == 'site_manager':
        return list(caller.get('managed_sites', []) or caller.get('sites', []))
    # worker: own sites only
    return list(caller.get('sites', []))

def get_accessible_users(caller, site_filter=None):
    """
    Return list of {name, device_id, role, sites} this caller can view.
    Optionally filtered to a specific site.
    """
    role = caller['role']
    mapping = load_user_mapping()
    all_users = []
    for dev_id, info in mapping.get('mapping', {}).items():
        all_users.append({
            'device_id': dev_id,
            'name': info.get('name', dev_id),
            'folder_name': info.get('name', dev_id).replace(' ', '_'),
            'role': info.get('role', 'worker'),
            'sites': info.get('sites', []),
            'primary_site': info.get('primary_site', ''),
        })

    accessible_sites = get_accessible_sites(caller)

    if role in ('admin', 'gm'):
        result = all_users
    elif role == 'pm':
        result = [u for u in all_users if any(s in accessible_sites for s in u['sites'])]
    elif role == 'site_manager':
        # Self + workers on same site (NOT other site_managers)
        own_name = caller.get('display_name', '')
        result = [u for u in all_users
                  if (u['name'] == own_name) or
                     (u['role'] == 'worker' and any(s in accessible_sites for s in u['sites']))]
    else:
        # worker: self only
        own_name = caller.get('display_name', '')
        result = [u for u in all_users if u['name'] == own_name]

    if site_filter:
        result = [u for u in result if site_filter in u.get('sites', [])]

    return result

def can_access_user_data(caller, target_user_name):
    """Check if caller can view target user's data."""
    if caller['role'] in ('admin', 'gm'):
        return True
    accessible = get_accessible_users(caller)
    target_clean = target_user_name.replace('_', ' ')
    return any(u['name'] == target_clean or u['folder_name'] == target_user_name for u in accessible)


def accessible_folder_scope(caller):
    """Folder-name scope for a caller, as a THREE-STATE value.

        None      -> unrestricted (admin/gm only). Apply no filter.
        set()     -> DENY ALL. The caller can see nothing.
        {"A","B"} -> exactly these folders.

    SECURITY (2026-07-23, live prod leak): the previous idiom used an
    empty LIST for both "unrestricted" and "nothing accessible", and every
    consumer tested it with `if allowed_folders:` -- falsy for both, so a
    caller with NO access took the "no filter" branch and received the
    whole bucket. Proven live: a UC PK site_manager (Aurora-provisioned,
    therefore absent from the DynamoDB users table -> role='viewer',
    display_name='' -> get_accessible_users -> []) pulled 88 report keys
    spanning every user folder in the lake.

    `None` and `set()` are different values and neither is a list, so a
    bare truthiness test on the result is an obviously wrong construct
    that review will catch. Callers MUST branch with `is None` and handle
    the empty set explicitly.

    Note get_accessible_users' own posture is unchanged -- this is a
    thin, honest wrapper around it, not a new policy."""
    if caller.get('role') in ('admin', 'gm'):
        return None
    return {u['folder_name'] for u in get_accessible_users(caller)}


def parse_time_to_seconds(time_str):
    parts = time_str.replace(' ', '').split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0

# Every media read MUST widen a topic window by the same amount, or the
# Transcript / Audio / Video tabs of ONE topic describe different moments
# (2026-08-09 prod, via the lambda_org_api copy of these readers: topic
# "12:07 - 12:07" read as the 12:07:15 chunk but played the 12:06:47 one).
#
# The buffer is not cosmetic. Aurora stores topic.time_range at MINUTE
# precision and timeline.js parseTimeRange expands "12:07 - 12:07" to
# start="12:07:00", end="12:07:00" -- so a topic contained in one minute
# arrives as a ZERO-WIDTH window, and an overlap test against it admits only
# whatever straddles that one instant. For a recorder emitting 30s chunks
# every 28s that is reliably the PRECEDING chunk, never the topic's own.
MEDIA_WINDOW_BUFFER_SEC = 60

# Assumed span for a media file whose name carries no length. Chunk-session
# files DO carry one (..._off{A}_to{B}_...) and are ~30s, so applying this
# 10-minute guess to them turns a window prefilter into a no-op.
LEGACY_MEDIA_SPAN_SEC = 600

def media_window(start_time, end_time):
    """(start_sec, end_sec) for a topic window, buffered. Absent bound -> whole day."""
    start_sec = (parse_time_to_seconds(start_time) - MEDIA_WINDOW_BUFFER_SEC
                 if start_time else 0)
    end_sec = (parse_time_to_seconds(end_time) + MEDIA_WINDOW_BUFFER_SEC
               if end_time else 86400)
    return max(0, start_sec), end_sec

def transcript_file_end_sec(filename, file_time_sec):
    """Absolute end of one transcript file, for the per-file window prefilter."""
    m = re.search(r'_off([\d.]+)_to([\d.]+)', filename)
    if m:
        return file_time_sec + (float(m.group(2)) - float(m.group(1)))
    return file_time_sec + LEGACY_MEDIA_SPAN_SEC

def extract_time_seconds_from_filename(filename):
    # Match time part after YYYY-MM-DD_ pattern: Benl1_2026-02-09_09-56-40_off...
    off_match = re.search(r'_off([\d.]+)_to', filename)
    base_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
    if off_match and base_match:
        h, m, s = int(base_match.group(1)), int(base_match.group(2)), int(base_match.group(3))
        return h * 3600 + m * 60 + s + int(float(off_match.group(1)))
    if base_match:
        return int(base_match.group(1)) * 3600 + int(base_match.group(2)) * 60 + int(base_match.group(3))
    return None


# ── GET /api/timeline ────────────────────────────────────────

# ── Deleted recordings ───────────────────────────────────────
#
# This gateway is the LEGACY one and it is still live -- 44 invocations in the 24h before
# this was written. Every content endpoint below lists S3 objects directly and none of them
# knew anything about the customer-facing delete, so a recording the customer removed was
# still readable here: its transcripts, its audio, its video, a presigned URL for any of
# them, and the pre-deletion daily_report served byte for byte.
#
# Nothing had CALLED those endpoints recently -- the live traffic is /api/actions, which
# returns only check-off state and no content -- so this was reachable rather than actively
# leaking. Reachable is enough: the promise made to the customer was that it is gone.
#
# The mirror, not the database: this lambda has no Aurora connection, exactly like the
# report generator and the ask agent, and `redactions/{folder}/{date}/deleted_sessions.json`
# is the copy of the answer written for readers in that position.
#
# `search`/`ask` need nothing here -- they PROXY to the ask agent and rag-search, which
# filter on their own side.

def _deleted_bases(user_folder, date):
    """Deleted session ids for one (folder, date), or an empty set."""
    if not user_folder or not date:
        return set()
    try:
        return deletion_mirror.deleted_sessions(s3_client, S3_BUCKET, user_folder, date)
    except Exception:
        # Fails OPEN and LOUD, like every guard on this path: an unreadable mirror must not
        # take the timeline down for everyone. It must not pass silently either -- "nothing
        # was deleted" and "could not check" are indistinguishable afterwards.
        logger.exception("deletion mirror unreadable for %s/%s -- serving unfiltered",
                         user_folder, date)
        return set()


def _drop_deleted_keys(keys, bases):
    """The subset of `keys` not belonging to a deleted session.

    Substring match, because the session id sits INSIDE the filename
    (`..._sid{32hex}_c0001.json`) rather than being a path component -- the same rule as
    deletion_mirror.drop_deleted, deliberately."""
    if not bases:
        return list(keys)
    return [k for k in keys if not any(b in k for b in bases)]


def _presign_key_is_deleted(s3_key):
    """Does this exact object belong to a recording the customer deleted?

    The (folder, date) has to come out of the key itself, because presign takes only a key.
    Four shapes carry both, and `reports/` puts them the other way round -- getting that
    order wrong reads the DATE as the folder and every lookup silently misses.

    Unparseable shapes return False: this endpoint already fails closed on ownership just
    above, so an unknown shape is refused there rather than here, and guessing would only
    add a second wrong answer.
    """
    parts = (s3_key or "").split("/")
    # reports/{date}/summary_report.json is only THREE segments, so the length check below
    # skipped it -- and admin/gm skip the ownership check entirely, so that one object
    # stayed downloadable byte for byte on a day whose sources were deleted. It is the same
    # lake-wide file get_timeline already refuses to serve; refusing it in one door and
    # signing it in the other is not a deletion.
    if len(parts) == 3 and parts[0] == "reports" and re.match(r"^\d{4}-\d{2}-\d{2}$",
                                                              parts[1] or ""):
        return _any_folder_deleted_on(parts[1])
    if len(parts) < 4:
        return False
    top = parts[0]
    if top in ("users", "audio_segments", "transcripts", "web_video"):
        folder, date = parts[1], parts[2]
        if top == "users":                       # users/{folder}/{kind}/{date}/...
            if len(parts) < 5:
                return False
            folder, date = parts[1], parts[3]
    elif top == "reports":                       # reports/{date}/{folder}/...
        date, folder = parts[1], parts[2]
    else:
        return False
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
        return False
    bases = _deleted_bases(folder, date)
    if top == "reports":
        # A day report is a SYNTHESIS of the day, so its key carries no session base and the
        # `base in key` test below can never fire for it -- the check above caught the
        # cross-folder `summary_report.json` and signed the per-folder `daily_report.json`
        # that actually holds the words. Measured on prod 2026-08-31:
        # reports/2026-08-14/Ben_UCPK2/daily_report.json still names the session deleted on
        # 2026-08-16 and still presigns, seventeen days later. The object's LastModified is
        # unchanged since it was written, so the nightly rebuild does not revisit past days
        # and the exposure is permanent rather than the one-night window it looks like.
        #
        # ANY deletion that day hides the whole document, which is what `lambda_ask_agent`
        # already does with the same objects ("has deleted recordings -- not serving a
        # stored report"). There is no per-session granularity inside a day report to filter
        # on, and the alternative to refusing it is serving it.
        return bool(bases)
    return bool(bases) and any(b in s3_key for b in bases)



def _any_folder_deleted_on(date):
    """Does ANY folder have a deletion on this date?

    `summary_report.json` is built across every folder at once, so one person's deleted
    session is inside a document everyone else's admin can read. There is no per-folder
    granularity to filter, so the aggregate is simply not served on such a day and the
    caller takes the per-folder path instead.

    Listing `redactions/` is the cheapest possible question -- the prefix exists only for
    days that HAVE a deletion, so on a normal day this is one empty list call.
    """
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix="redactions/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if f"/{date}/" not in key:
                    continue
                # The KEY existing is not the question -- undelete rewrites the document
                # with the remaining sessions and writes an EMPTY one when none are left,
                # rather than removing the object. Answering on key presence alone would
                # make a fully-reverted day look deleted forever, and the lake-wide
                # aggregate would never be served again. Ask what is inside.
                folder = key.split("/")[1] if len(key.split("/")) > 2 else None
                if folder and deletion_mirror.deleted_sessions(
                        s3_client, S3_BUCKET, folder, date):
                    return True
    except Exception:
        logger.exception("could not list redactions/ for %s -- serving unfiltered", date)
    return False



def get_timeline(params, caller):
    date = params.get('date', '')
    user = params.get('user', '')
    if not date:
        nzdt = datetime.utcnow() + timedelta(hours=13)
        date = (nzdt - timedelta(days=1)).strftime('%Y-%m-%d')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return error('Invalid date')

    role = caller['role']

    # Worker: forced to own data
    if role == 'worker':
        user = resolve_user_display_name(caller)
        if not user:
            return error('No device mapping for your account', 403)
    # Management roles: if user specified, check permission
    elif user:
        if not can_access_user_data(caller, user):
            return error('Access denied to this user', 403)
    # Management with no user: try summary, then first available
    elif not user:
        if role in ('admin', 'gm'):
            key = f"{REPORT_PREFIX}{date}/summary_report.json"
            try:
                # Same door, aggregate form. This doc is built across every folder, so one
                # deleted session's words sit inside it -- go the long way round instead,
                # where each folder is checked on its own.
                if _any_folder_deleted_on(date):
                    return find_any_report(date, caller)
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
                return ok(json.loads(obj['Body'].read().decode('utf-8')))
            except s3_client.exceptions.NoSuchKey:
                return find_any_report(date, caller)
        else:
            # PM/site_manager with no user → own data first
            user = resolve_user_display_name(caller)
            if not user:
                return find_any_report(date, caller)

    user_folder = user.replace(' ', '_')
    # A day whose sources were DELETED must not fall through to the pre-rendered doc.
    # `daily_report.json` was written BEFORE the delete and carries the removed session's
    # words byte for byte -- filtering the database and then serving the artifact rendered
    # from it is not a deletion. Same rule, and the same 404, as org-api's timeline.
    if _deleted_bases(user_folder, date) or _deleted_bases(user, date):
        logger.info("timeline: %s/%s has deleted recordings -- not serving the stored "
                    "report", user_folder, date)
        return ok({'message': f'No report for {user} on {date}', 'date': date}, 404)
    for name_variant in [user_folder, user]:
        key = f"{REPORT_PREFIX}{date}/{name_variant}/daily_report.json"
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            return ok(json.loads(obj['Body'].read().decode('utf-8')))
        except s3_client.exceptions.NoSuchKey:
            continue
    return ok({'message': f'No report for {user} on {date}', 'date': date}, 404)

def find_any_report(date, caller=None):
    prefix = f"{REPORT_PREFIX}{date}/"
    reports = []
    # SECURITY: same three-state scope as get_report_history. `None` here
    # means BOTH "no caller supplied" (dead today -- both call sites in
    # get_timeline pass one) and "admin/gm"; an empty set is deny-all and
    # short-circuits below. Previously `if accessible:` conflated deny-all
    # with unrestricted, and unlike get_report_history this function can
    # return a full report BODY when exactly one key survives the filter.
    folder_scope = accessible_folder_scope(caller) if caller else None
    if caller and folder_scope is None:
        # NO SILENT WIDENING. accessible_folder_scope returns None
        # ("unrestricted") for admin/gm, but the code this replaced ran
        # `accessible = get_accessible_users(caller)` for EVERY role --
        # and for an admin that returns the whole config/user_mapping.json
        # roster, a NON-empty list, so `if accessible:` was true and the
        # filter DID run: a folder absent from the mapping (e.g. an
        # Aurora-provisioned Ben_UCPK) was dropped and the caller got the
        # 404 envelope. Handing admin/gm a bare None here would widen that
        # to a 200 carrying the full report BODY -- unacceptable in a PR
        # whose entire purpose is to narrow. Admin/gm therefore keep the
        # mapping-derived allowlist, exactly as before.
        #
        # Deliberately LOCAL to find_any_report: get_report_history and
        # get_presigned_url already treated admin/gm as unrestricted
        # before this branch and must stay byte-identical.
        mapped = {u['folder_name'] for u in get_accessible_users(caller)}
        # Empty-mapping edge: load_user_mapping falls back to
        # {'mapping': {}} whenever the S3 read of config/user_mapping.json
        # fails, so an admin's derived set can be empty for reasons that
        # have nothing to do with authorisation. Failing that closed would
        # be a brand-new availability regression triggered by an unrelated
        # S3 hiccup, and it is not what the old code did either (an empty
        # list is falsy -> no filter ran). Fall back to None: identical to
        # the pre-branch behaviour, and not a fail-open, because admin/gm
        # are unrestricted in every sibling reader here anyway. Scoped
        # callers are untouched -- for them an empty set stays deny-all.
        folder_scope = mapped or None
    if folder_scope is not None and not folder_scope:
        return ok({'message': f'No reports for {date}', 'date': date}, 404)
    try:
        resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if key.endswith('/daily_report.json') and '_debug' not in key:
                parts = key.replace(prefix, '').split('/')
                if len(parts) >= 2:
                    user_name = parts[0]
                    # A day whose sources were deleted must not fall through to the
                    # pre-rendered doc. `daily_report.json` was written BEFORE the delete
                    # and holds the removed session's words byte for byte -- filtering
                    # elsewhere and serving the artifact rendered from it is not a
                    # deletion. Same rule, and the same 404, as the org-api path.
                    if _deleted_bases(user_name, date):
                        logger.info("timeline: %s/%s has deleted recordings -- not serving "
                                    "the stored report", user_name, date)
                        continue
                    # Filter by permission. Deliberate narrowing: the old
                    # predicate also matched the SPACED display name
                    # (u['name']) against what is always an S3 path
                    # segment, i.e. the folder form -- that could only
                    # ever match for a single-word name, where both forms
                    # are identical. No real caller loses access.
                    if folder_scope is not None and user_name not in folder_scope:
                        continue
                    reports.append({'user': user_name, 'key': key})
    except Exception:
        pass
    if not reports:
        return ok({'message': f'No reports for {date}', 'date': date}, 404)
    if len(reports) == 1:
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=reports[0]['key'])
            return ok(json.loads(obj['Body'].read().decode('utf-8')))
        except Exception:
            pass
    return ok({'date': date, 'available_users': [r['user'] for r in reports]})


# ── GET /api/dates ───────────────────────────────────────────

def get_dates(params, caller):
    months = int(params.get('months', '2'))
    site = params.get('site', '')
    nzdt = datetime.utcnow() + timedelta(hours=13)
    start_date = nzdt - timedelta(days=months * 30)
    
    role = caller['role']
    user_param = params.get('user', '')
    # SECURITY: three-state scope, same discipline as
    # get_report_history/find_any_report/accessible_folder_scope. None =
    # unrestricted (admin/gm with no ?site and no ?user); a list is an
    # allowlist. Previously the admin/gm "no filter" case used
    # `user_folders = []`, and `?site=` with no accessible users on that
    # site ALSO produced `[]` -- `if user_folders:` (below) is falsy for
    # both, so a scoped caller with nobody accessible on ?site= took the
    # "no filter" branch: every date was marked hasReport=True and
    # enriched from the UNSCOPED summary_report.json, leaking
    # company-wide topic/safety counts per day.
    if user_param and can_access_user_data(caller, user_param):
        # Explicit ?user= wins: the timeline date-picker asks for one user's
        # dates so its dots match the per-user report fetch. Without this the
        # admin path below marks a dot whenever ANY user has a report that
        # day — dotted dates with no content for the selected user.
        user_folders = [user_param]
    elif role == 'worker':
        user_folders = [resolve_user_display_name(caller)]
    elif site:
        # Filter to specific site's users
        users = get_accessible_users(caller, site_filter=site)
        user_folders = [u['folder_name'] for u in users]
    elif role in ('admin', 'gm'):
        user_folders = None  # unrestricted -- no filter
    else:
        user_folders = [resolve_user_display_name(caller)]

    # Deny-all: an empty list, OR a list of only-blank folder names (the
    # unmapped-caller shape -- resolve_user_display_name returns '' when
    # display_name is blank, and [''] is TRUTHY, which is why the old code
    # failed closed here only by accident rather than by design). Made
    # explicit so a future edit can't silently turn this back into a leak.
    if user_folders is not None and not any(user_folders):
        return ok({'dates': {}})

    dates = {}
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=REPORT_PREFIX, Delimiter='/'):
            for cp in page.get('CommonPrefixes', []):
                ds = cp['Prefix'].replace(REPORT_PREFIX, '').strip('/')
                if re.match(r'^\d{4}-\d{2}-\d{2}$', ds):
                    try:
                        d = datetime.strptime(ds, '%Y-%m-%d')
                        if d >= start_date:
                            if user_folders is not None:
                                # Check if any accessible user has a report
                                for uf in user_folders:
                                    try:
                                        s3_client.head_object(Bucket=S3_BUCKET, Key=f"{REPORT_PREFIX}{ds}/{uf}/daily_report.json")
                                        dates[ds] = {'hasReport': True, 'topics': 0, 'safety': 0}
                                        break
                                    except:
                                        pass
                            else:
                                dates[ds] = {'hasReport': True, 'topics': 0, 'safety': 0}
                    except ValueError:
                        pass
    except Exception as e:
        logger.error(f"Error scanning dates: {e}")
    # Enrich with topic counts (use first accessible user or summary)
    for ds in list(dates.keys()):
        try:
            loaded = False
            if user_folders is not None:
                for uf in user_folders:
                    # Same rule as the timeline: `daily_report.json` was written BEFORE the
                    # delete, so the topic and safety COUNTS derived from it still count the
                    # removed session. The date picker would show a dot with a nonzero
                    # count for a day the timeline now 404s -- an inconsistency, and a
                    # small aggregate leak of content the customer removed.
                    if _deleted_bases(uf, ds):
                        continue
                    try:
                        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=f"{REPORT_PREFIX}{ds}/{uf}/daily_report.json")
                        report = json.loads(obj['Body'].read().decode('utf-8'))
                        topics = report.get('topics', [])
                        if isinstance(topics, list):
                            dates[ds]['topics'] = max(dates[ds].get('topics', 0), len(topics))
                            dates[ds]['safety'] = max(dates[ds].get('safety', 0),
                                sum(1 for t in topics if t.get('category','').lower()=='safety' or t.get('safety_flags',[])))
                        loaded = True
                    except:
                        pass
            if not loaded and not _any_folder_deleted_on(ds):
                # The lake-wide aggregate, same object and same reason as get_timeline's:
                # no per-folder granularity exists inside it to filter.
                obj = s3_client.get_object(Bucket=S3_BUCKET, Key=f"{REPORT_PREFIX}{ds}/summary_report.json")
                report = json.loads(obj['Body'].read().decode('utf-8'))
                topics = report.get('topics', [])
                if isinstance(topics, list):
                    dates[ds]['topics'] = len(topics)
                    dates[ds]['safety'] = sum(1 for t in topics if t.get('category','').lower()=='safety' or t.get('safety_flags',[]))
        except Exception:
            pass
    return ok({'dates': dates})


# ── GET /api/media/presigned-url ─────────────────────────────

def get_presigned_url(params, caller=None):
    s3_key = unquote_plus(params.get('key', ''))
    if not s3_key:
        return error('Missing key')
    allowed = ['users/', 'audio_segments/', 'transcripts/', 'reports/', 'web_video/']
    if not any(s3_key.startswith(p) for p in allowed):
        return error('Access denied', 403)

    # Permission check: derive the owning user folder from the key and
    # verify the caller can reach it.
    #
    # SECURITY (2026-07-23): this block used to leave target_user = None
    # for any key shape it could not parse -- and `if target_user and not
    # can_access_user_data(...)` then short-circuited, issuing a presigned
    # URL with NO check at all. reports/{date}/summary_report.json (length
    # 3, so the old `len(key_parts) > 3` guard was False) and
    # reports/{date}/sites/... ('sites' explicitly excluded) both took that
    # path: 36 real objects on prod, each a full report body. An
    # undeterminable owner is now a DENY for every non-admin/gm caller.
    #
    # The `not caller` leg closes the second half of the same shape: the
    # guard used to read `if caller and ...`, so an ABSENT identity skipped
    # the whole block and was served a signed URL unchecked -- "no identity
    # == unrestricted", precisely what this branch exists to abolish.
    # Unreachable from lambda_handler today (it always passes a dict), but
    # the default `caller=None` in the signature keeps the door ajar.
    if not caller or caller.get('role') not in ('admin', 'gm'):
        # Extract user folder name from common path patterns:
        #   users/{name}/...  audio_segments/{name}/...  transcripts/{name}/...
        #   reports/{date}/{name}/...  web_video/{name}/...
        key_parts = s3_key.split('/')
        target_user = None
        if len(key_parts) >= 2 and key_parts[0] in ('users', 'audio_segments', 'transcripts', 'web_video'):
            target_user = key_parts[1] or None
        elif key_parts[0] == 'reports' and len(key_parts) > 3:
            # reports/{date}/{user}/... — 'sites' is a rollup namespace,
            # not a user folder; 'summary_report.json' can't appear at this
            # position today (it lives at length 3) but is kept in the
            # guard so a future reports/{date}/summary_report.json/... shape
            # can't sneak through as an owner name.
            candidate = key_parts[2]
            if candidate and candidate not in ('summary_report.json', 'sites'):
                target_user = candidate

        if target_user is None:
            # FAIL CLOSED. Company/site-level rollups have no owner folder
            # to check, so a scoped caller cannot be authorised for them by
            # this endpoint. Serving them to genuine site/company members is
            # desirable but needs a real membership check this legacy lambda
            # cannot perform (no Aurora connection; its identity store is the
            # same DynamoDB table that does not contain these users at all).
            # That belongs on org-api.
            logger.info("presign denied: no derivable owner for key=%s role=%s",
                        s3_key, caller.get('role') if caller else None)
            return error('Access denied to this media', 403)
        # `not caller` first: with no identity there is nothing to
        # authorise against, and can_access_user_data would dereference
        # caller['role'] and raise.
        if not caller or not can_access_user_data(caller, target_user):
            return error('Access denied to this user\'s media', 403)
    # A deleted recording's media must not be re-signed, for ANY caller -- including the
    # admin/gm branch above, which skips the ownership check entirely. 404, not 403: an
    # access-denied confirms the object exists, and the same choice was made on org-api.
    if _presign_key_is_deleted(s3_key):
        logger.info("presign refused: %s belongs to a deleted recording", s3_key)
        return error('Not found', 404)
    try:
        url = s3_client.generate_presigned_url('get_object', Params={'Bucket': S3_BUCKET, 'Key': s3_key}, ExpiresIn=PRESIGNED_URL_EXPIRY)
        return ok({'url': url, 'expires_in': PRESIGNED_URL_EXPIRY})
    except Exception as e:
        return error(f'Failed: {e}', 500)


# ── GET /api/transcripts ────────────────────────────────────

def get_transcripts(params, caller):
    date = params.get('date', '')
    user = params.get('user', '')
    start_time = params.get('start', '')
    end_time = params.get('end', '')
    if not date:
        return error('Missing date')
    if caller['role'] == 'worker':
        user = resolve_user_display_name(caller)
    elif user and not can_access_user_data(caller, user):
        return error('Access denied', 403)
    elif not user:
        user = resolve_user_display_name(caller)
    if not user:
        return error('Missing user')
    user_folder = user.replace(' ', '_')
    start_sec, end_sec = media_window(start_time, end_time)
    # Both folder spellings, because the prefixes below try both and filtering only one
    # would hide the recording for some users and not others -- indistinguishable from
    # working.
    deleted = _deleted_bases(user_folder, date) | _deleted_bases(user, date)

    transcript_files = []
    # Try date subfolder first, then flat folder filtered by date
    search_prefixes = [
        (f"transcripts/{user_folder}/{date}/", False),
        (f"transcripts/{user}/{date}/", False),
        (f"transcripts/{user_folder}/", True),   # flat folder, filter by date
        (f"transcripts/{user}/", True),
    ]
    for prefix, needs_date_filter in search_prefixes:
        try:
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if not key.endswith('.json'):
                        continue
                    if deleted and any(b in key for b in deleted):
                        continue          # a recording the customer deleted
                    # Skip if this is inside a subfolder and we're searching flat
                    parts = key.replace(prefix, '').split('/')
                    if needs_date_filter and len(parts) > 1:
                        continue  # this is in a date subfolder, skip
                    if needs_date_filter:
                        # Only include files matching target date in filename
                        if date not in key:
                            continue
                    transcript_files.append(key)
        except Exception:
            pass
        if transcript_files:
            break

    if not transcript_files:
        return ok({'text': '', 'segments': [], 'speaker_segments': [], 'message': 'No transcripts found'})

    all_speaker_segs = []
    segments = []
    for key in sorted(transcript_files):
        filename = key.split('/')[-1]
        file_time_sec = extract_time_seconds_from_filename(filename)
        if file_time_sec is None:
            continue
        file_end_sec = transcript_file_end_sec(filename, file_time_sec)
        if file_end_sec < start_sec or file_time_sec > end_sec:
            continue
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=key)
            data = json.loads(obj['Body'].read().decode('utf-8'))
            results = data.get('results', {})
            full_text = results.get('transcripts', [{}])[0].get('transcript', '')
            
            # Speaker-segmented audio_segments from Transcribe
            audio_segs = results.get('audio_segments', [])
            for aseg in audio_segs:
                seg_start = float(aseg.get('start_time', 0))
                seg_end = float(aseg.get('end_time', 0))
                abs_start = file_time_sec + seg_start
                abs_end = file_time_sec + seg_end
                
                # Filter to topic time range
                if abs_end < start_sec or abs_start > end_sec:
                    continue
                
                speaker = aseg.get('speaker_label', 'spk_0')
                text = aseg.get('transcript', '')
                if not text.strip():
                    continue
                
                ah, am, asec_v = int(abs_start)//3600, (int(abs_start)%3600)//60, int(abs_start)%60
                all_speaker_segs.append({
                    'speaker': speaker,
                    'text': text,
                    'start': round(abs_start, 1),
                    'end': round(abs_end, 1),
                    'time_label': f"{ah:02d}:{am:02d}:{asec_v:02d}",
                    'duration': round(seg_end - seg_start, 1),
                })
            
            # Word-level filtered text
            items = results.get('items', [])
            in_range_words = []
            total_words = 0
            for item in items:
                if item.get('type') != 'pronunciation':
                    continue
                total_words += 1
                word_start = float(item.get('start_time', 0))
                abs_ws = file_time_sec + word_start
                if start_sec <= abs_ws <= end_sec:
                    in_range_words.append(item.get('alternatives', [{}])[0].get('content', ''))
            
            h, m, s = file_time_sec // 3600, (file_time_sec % 3600) // 60, file_time_sec % 60
            segments.append({
                'time': f"{h:02d}:{m:02d}:{s:02d}",
                'time_seconds': file_time_sec,
                'text': full_text,
                'filtered_text': ' '.join(in_range_words),
                'filename': filename,
                'word_count': total_words,
                'in_range_count': len(in_range_words),
                'speaker_segment_count': len([s for s in all_speaker_segs if s.get('start', 0) >= file_time_sec]),
            })
        except Exception as e:
            logger.warning(f"Failed to load {key}: {e}")

    all_speaker_segs.sort(key=lambda s: s['start'])
    filtered_full = ' '.join(s['text'] for s in all_speaker_segs)
    
    # Count unique speakers
    speakers = list(set(s['speaker'] for s in all_speaker_segs))
    speakers.sort()
    
    return ok({
        'text': filtered_full,
        'filtered_text': filtered_full,
        'segments': segments,
        'speaker_segments': all_speaker_segs,
        'speakers': speakers,
        'count': len(segments),
        'speaker_count': len(speakers),
        'total_speaker_segments': len(all_speaker_segs),
    })


# ── GET /api/audio-segments ──────────────────────────────────

def get_audio_segments(params, caller):
    date = params.get('date', '')
    user = params.get('user', '')
    topic_start = params.get('start', '')
    topic_end = params.get('end', '')
    if not date:
        return error('Missing date')
    if caller['role'] == 'worker':
        user = resolve_user_display_name(caller)
    elif user and not can_access_user_data(caller, user):
        return error('Access denied', 403)
    elif not user:
        user = resolve_user_display_name(caller)
    if not user:
        return error('Missing user')
    user_folder = user.replace(' ', '_')
    start_sec, end_sec = media_window(topic_start, topic_end)

    prefix = f"audio_segments/{user_folder}/{date}/"
    deleted = _deleted_bases(user_folder, date)
    segments = []
    try:
        resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.wav'):
                continue
            if deleted and any(b in key for b in deleted):
                continue              # a recording the customer deleted
            filename = key.split('/')[-1]
            # Base time then offset, matched SEPARATELY -- a chunk-session segment
            # keeps sid/chunk tokens BETWEEN them (..._HH-MM-SS_sid{hex}_c{NNNN}_off...),
            # so anchoring the time on a trailing "_off" (the old whole-file shape)
            # skipped every chunk segment and left the Audio tab empty.
            base_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
            off_match = re.search(r'_off([\d.]+)_to([\d.]+)', filename)
            if not base_match or not off_match:
                continue
            h, m, s = int(base_match.group(1)), int(base_match.group(2)), int(base_match.group(3))
            base_sec = h * 3600 + m * 60 + s
            abs_start = base_sec + float(off_match.group(1))
            abs_end = base_sec + float(off_match.group(2))
            if abs_end < start_sec or abs_start > end_sec:
                continue
            url = s3_client.generate_presigned_url('get_object', Params={'Bucket': S3_BUCKET, 'Key': key}, ExpiresIn=PRESIGNED_URL_EXPIRY)
            ah, am, asec = int(abs_start)//3600, (int(abs_start)%3600)//60, int(abs_start)%60
            segments.append({
                'url': url, 'filename': filename,
                'absolute_start': abs_start, 'absolute_end': abs_end,
                'duration': round(abs_end - abs_start, 1),
                'time_label': f"{ah:02d}:{am:02d}:{asec:02d}",
            })
    except Exception as e:
        logger.error(f"Error listing audio segments: {e}")
    segments.sort(key=lambda s: s['absolute_start'])
    return ok({'segments': segments, 'count': len(segments)})


# ── POST /api/actions/toggle + GET /api/actions ──────────────

def toggle_action(body, caller):
    date = body.get('date', '')
    topic_id = body.get('topic_id', 0)
    action_index = body.get('action_index', 0)
    is_checked = body.get('checked', True)
    action_text = body.get('action_text', '')
    if not date:
        return error('Missing date')
    user_name = caller.get('display_name') or caller.get('name') or caller.get('email')
    now = datetime.utcnow().isoformat() + 'Z'
    table = dynamodb.Table(AUDIT_TABLE)
    
    # Current state key
    pk = f"ACTIONS#{date}"
    sk = f"TOPIC#{topic_id}#ACTION#{action_index}"
    
    # Audit log entry (append-only, never deleted)
    audit_pk = f"AUDIT#{date}"
    audit_sk = f"{now}#ACTION#{topic_id}#{action_index}"
    
    try:
        # Write/update current state
        if is_checked:
            table.put_item(Item={'PK': pk, 'SK': sk, 'action_text': action_text,
                                  'checked': True, 'checked_by': user_name, 'checked_at': now})
        else:
            table.put_item(Item={'PK': pk, 'SK': sk, 'action_text': action_text,
                                  'checked': False, 'unchecked_by': user_name, 'unchecked_at': now})
        
        # Append audit log (immutable history)
        table.put_item(Item={
            'PK': audit_pk, 'SK': audit_sk,
            'action': 'check' if is_checked else 'uncheck',
            'topic_id': topic_id, 'action_index': action_index,
            'action_text': action_text,
            'user': user_name, 'timestamp': now,
        })
        
        return ok({'message': 'Updated', 'checked': is_checked})
    except Exception as e:
        return error(f'Failed: {e}', 500)

def get_actions(params, caller):
    date = params.get('date', '')
    if not date:
        return error('Missing date')
    table = dynamodb.Table(AUDIT_TABLE)
    try:
        resp = table.query(KeyConditionExpression='PK = :pk', ExpressionAttributeValues={':pk': f"ACTIONS#{date}"})
        actions = {}
        for item in resp.get('Items', []):
            parts = item.get('SK', '').split('#')
            if len(parts) >= 4:
                actions[f"{parts[1]}_{parts[3]}"] = {
                    'checked': item.get('checked', False),
                    'checked_by': str(item.get('checked_by', '')),
                    'checked_at': str(item.get('checked_at', '')),
                }
        return ok({'actions': actions, 'date': date})
    except Exception as e:
        return error(f'Failed: {e}', 500)


# ── GET /api/video-segments ──────────────────────────────────

def get_video_segments(params, caller):
    """Find video files covering a time range, prefer H264 web preview."""
    date = params.get('date', '')
    user = params.get('user', '')
    topic_start = params.get('start', '')
    topic_end = params.get('end', '')
    if not date:
        return error('Missing date')
    if caller['role'] == 'worker':
        user = resolve_user_display_name(caller)
    elif user and not can_access_user_data(caller, user):
        return error('Access denied', 403)
    elif not user:
        user = resolve_user_display_name(caller)
    if not user:
        return error('Missing user')
    user_folder = user.replace(' ', '_')
    start_sec, end_sec = media_window(topic_start, topic_end)
    # offset_sec is a SEEK HINT, not a filter -- it must land on the topic
    # itself, so it is measured from the unbuffered start.
    seek_from_sec = parse_time_to_seconds(topic_start) if topic_start else 0

    deleted = _deleted_bases(user_folder, date) | _deleted_bases(user, date)
    videos = []
    for name_variant in [user_folder, user]:
        # First check web_video/ (H264 preview)
        for prefix_template in [f"web_video/{name_variant}/{date}/", f"users/{name_variant}/video/{date}/"]:
            is_preview = prefix_template.startswith('web_video/')
            try:
                resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix_template)
                for obj in resp.get('Contents', []):
                    key = obj['Key']
                    if not any(key.lower().endswith(e) for e in ['.mp4','.webm','.mov']):
                        continue
                    if deleted and any(b in key for b in deleted):
                        continue          # a recording the customer deleted
                    filename = key.split('/')[-1]
                    time_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
                    if not time_match:
                        continue
                    h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
                    vid_start = h * 3600 + m * 60 + s
                    vid_end = vid_start + LEGACY_MEDIA_SPAN_SEC
                    if vid_end < start_sec or vid_start > end_sec:
                        continue
                    # Skip if we already have a preview version of this file
                    base_name = re.sub(r'\.\w+$', '', filename)
                    if not is_preview and any(v.get('base_name') == base_name for v in videos):
                        continue
                    offset = max(0, seek_from_sec - vid_start)
                    url = s3_client.generate_presigned_url('get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': key}, ExpiresIn=PRESIGNED_URL_EXPIRY)
                    vh, vm, vs = vid_start//3600, (vid_start%3600)//60, vid_start%60
                    videos.append({
                        'url': url, 'key': key, 'filename': filename,
                        'base_name': base_name,
                        'video_start_sec': vid_start,
                        'time_label': f"{vh:02d}:{vm:02d}:{vs:02d}",
                        'offset_sec': round(offset, 1),
                        'size_mb': round(obj['Size']/(1024*1024), 1),
                        'is_preview': is_preview,
                        'codec': 'h264' if is_preview else 'unknown',
                    })
            except Exception:
                pass
        if videos:
            break
    videos.sort(key=lambda v: v['video_start_sec'])
    return ok({'videos': videos, 'count': len(videos)})


# ── GET /api/recording-stats ─────────────────────────────────

def get_recording_stats(params, caller):
    """Count original video+audio files and total duration."""
    date = params.get('date', '')
    user = params.get('user', '')
    if not date:
        return error('Missing date')
    if caller['role'] == 'worker':
        user = resolve_user_display_name(caller)
    elif user and not can_access_user_data(caller, user):
        return error('Access denied', 403)
    elif not user:
        user = resolve_user_display_name(caller)
    if not user:
        return error('Missing user')
    user_folder = user.replace(' ', '_')
    # Counts, sizes and durations are DERIVED FACTS about a recording: that it existed,
    # roughly how long it was, how big. A customer told their recording is gone should not
    # be able to read its shadow off a statistics endpoint.
    deleted = _deleted_bases(user_folder, date) | _deleted_bases(user, date)
    stats = {'video_count': 0, 'audio_count': 0, 'total_files': 0,
             'total_size_mb': 0, 'estimated_duration_min': 0}
    for media_type in ['video', 'audio']:
        for name_variant in [user_folder, user]:
            prefix = f"users/{name_variant}/{media_type}/{date}/"
            try:
                resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
                for obj in resp.get('Contents', []):
                    key = obj['Key'].lower()
                    if deleted and any(b.lower() in key for b in deleted):
                        continue          # a recording the customer deleted
                    if any(key.endswith(e) for e in ['.mp4','.webm','.mov','.wav','.mp3','.m4a']):
                        if media_type == 'video':
                            stats['video_count'] += 1
                        else:
                            stats['audio_count'] += 1
                        stats['total_size_mb'] += obj['Size']/(1024*1024)
                        stats['estimated_duration_min'] += 10
            except Exception:
                pass
    stats['total_files'] = stats['video_count'] + stats['audio_count']
    stats['total_size_mb'] = round(stats['total_size_mb'], 1)
    return ok(stats)


# ── GET /api/reports/history ─────────────────────────────────

def get_report_history(params, caller):
    limit = int(params.get('limit', '20'))
    # SECURITY: three-state scope -- None = unrestricted (admin/gm), an
    # empty set = deny-all (explicit early return below), otherwise an
    # allowlist. See accessible_folder_scope for the leak this replaces.
    # The old dedicated `worker` branch is gone: accessible_folder_scope
    # routes a worker through get_accessible_users' self-only arm, which
    # yields the SAME folder_name string resolve_user_display_name did
    # (display_name with spaces -> underscores). Behaviour equivalence,
    # not a widening -- pinned by test_history_worker_sees_only_own_folder.
    folder_scope = accessible_folder_scope(caller)
    if folder_scope is not None and not folder_scope:
        # DENY ALL -- return early rather than fall into a filter loop
        # whose predicate an empty container would silently satisfy.
        return ok({'reports': []})
    
    reports = []
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=REPORT_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not key.endswith('_report.json') or '_debug' in key:
                    continue
                if folder_scope is not None:
                    if not any(f'/{uf}/' in key for uf in folder_scope):
                        continue
                rtype = 'weekly' if 'weekly' in key else 'monthly' if 'monthly' in key else 'daily'
                dm = re.search(r'(\d{4}-\d{2}-\d{2})', key)
                reports.append({'key': key, 'type': rtype, 'date': dm.group(1) if dm else '',
                                'generated_at': obj['LastModified'].isoformat(), 'size': obj['Size']})
    except Exception as e:
        logger.error(f"Error: {e}")
    reports.sort(key=lambda r: r['date'], reverse=True)
    return ok({'reports': reports[:limit]})


# ── POST /api/reports/generate ───────────────────────────────

def trigger_report_generation(body, caller):
    rtype = body.get('report_type', 'daily')
    date = body.get('date', '')
    force = body.get('force', False)
    if not date:
        nzdt = datetime.utcnow() + timedelta(hours=13)
        date = (nzdt - timedelta(days=1)).strftime('%Y-%m-%d')
    payload = {'report_type': rtype, 'date': date}
    if caller['role'] == 'worker':
        user = resolve_user_display_name(caller)
        if user:
            payload['users_filter'] = [user.replace('_', ' ')]
    if force:
        payload['force'] = True
    try:
        lambda_client.invoke(FunctionName=REPORT_FUNCTION, InvocationType='Event', Payload=json.dumps(payload))
        return ok({'message': f'Report triggered for {date}', 'status': 'pending'}, 202)
    except Exception as e:
        return error(f'Failed: {e}', 500)


# ── POST /api/ask ───────────────────────────────────────────

def ask_question(body, caller):
    """Proxy question to Ask Agent Lambda. ACL is enforced downstream by
    rag-search via caller_sub (BUG-39 WS2) -- this proxy no longer gates."""
    question = body.get('question', '').strip()
    date = body.get('date', '')
    user = body.get('user', '')
    scope = body.get('scope', 'both')
    topic_id = body.get('topic_id', None)

    if not question:
        return error('Missing question')
    # `date` is forwarded and the RAG path does not read it -- it branches on
    # caller_sub before `date` is ever looked at, so a comment here once
    # claiming it was "soft context" described something that never happened.
    # It stays for the legacy S3 path, which does read it.
    #
    # `tz` is what the RAG path reads: an IANA zone id, not a date. The zone is
    # sent instead of a computed date because NZ and AU are both on daylight
    # saving for part of the year and do not switch on the same day, so a date
    # computed anywhere but in the caller's own zone is wrong for one of them.
    # Absent stays absent -- '' would be a blank every reader has to special-case.

    # REMOVED (BUG-39 WS2): legacy DynamoDB user/role gate. The RAG ACL is
    # enforced downstream by caller_sub -> rag-search (graded scope.visible_scope,
    # WS3). 'user' is optional soft context only.
    #   was: if not user: user = resolve_user_display_name(caller)
    #        if not user: return error('Missing user')
    #        if caller['role'] == 'worker': user = resolve_user_display_name(caller)
    #        elif user and not can_access_user_data(caller, user): return error('Access denied to this user', 403)

    payload = {
        'user': user,
        'question': question,
        'scope': scope,
        # Cognito sub bridge: rag-search resolves this via get_user_by_sub()
        # to scope retrieval to the caller's accessible sites (org ACL).
        'caller_sub': caller.get('sub', ''),
    }
    if date:
        payload['date'] = date
    if body.get('tz'):
        payload['tz'] = body['tz']
    if topic_id is not None:
        payload['topic_id'] = topic_id

    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        # An unhandled exception inside the Ask Agent lambda comes back as a
        # 200 InvocationType response with FunctionError set and a Payload
        # containing {errorMessage, errorType, stackTrace}. Never pass that
        # straight through to the client -- it leaks internal stack traces.
        if resp.get('FunctionError'):
            logger.error(f"Ask agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Ask agent error', 500)

        result = json.loads(resp['Payload'].read().decode('utf-8'))

        # The Ask Agent returns API Gateway format {statusCode, body}
        if 'body' in result:
            return result
        # Or direct invocation format
        return ok(result)
    except Exception as e:
        logger.error(f"Ask agent invocation failed: {e}")
        return error(f'Ask agent error: {e}', 500)


# ── POST /api/ask/voice (SP-Ask) ─────────────────────────────

# ~15s of 128kbps AAC ≈ 240KB ≈ 320K base64 chars; 1.5M chars (~1.1MB decoded)
# is generous headroom while still rejecting absurd payloads early.
MAX_VOICE_AUDIO_B64 = 1_500_000


def ask_voice(body, caller):
    """Hands-free voice ask (SP-Ask): forward the base64 clip to the Ask Agent,
    which chains DashScope STT -> RAG (caller_sub ACL, voice prompt, Haiku) ->
    DashScope TTS and returns {transcript, answerText, audioBase64, audioFormat}.

    Routed here (ApiFunction, non-VPC) and NOT on lambda_org_api: the org API
    is in-VPC with no NAT and no lambda VPC endpoint (BUG-36), so it can
    neither reach DashScope nor invoke AskAgentFunction. This function already
    holds LambdaInvokePolicy on AskAgentFunction and the /api/{proxy+} route.
    caller identity comes from the Cognito authorizer claims -- never from the
    client body (mirrors ask_question's caller_sub bridge)."""
    if not caller.get('sub'):
        return error('Unauthenticated', 401)
    audio_b64 = body.get('audio')
    if not audio_b64 or not isinstance(audio_b64, str):
        return error('Missing audio (base64 clip required)')
    if len(audio_b64) > MAX_VOICE_AUDIO_B64:
        return error('Audio too large', 413)

    payload = {
        'mode': 'voice',
        'audio': audio_b64,
        'format': body.get('format') or 'm4a',
        'caller_sub': caller['sub'],
    }
    if body.get('tz'):
        payload['tz'] = body['tz']   # see ask_question: an IANA zone, not a date
    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        # Same FunctionError posture as ask_question: never pass a crashed
        # agent's {errorMessage, stackTrace} payload through to the client.
        if resp.get('FunctionError'):
            logger.error(f"Voice ask agent FunctionError: {resp.get('FunctionError')}")
            return error('Ask agent error', 500)
        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Voice ask invocation failed: {e}")
        return error(f'Ask agent error: {e}', 500)


# ── POST /api/search ─────────────────────────────────────────

def search_topics(body, caller):
    """Retrieve-only topic search: forward to the Ask Agent with mode=search.
    Returns a ranked topic list (no LLM synthesis). ACL is enforced downstream
    in rag-search (org accessible sites via caller_sub), so no per-user gate is
    needed here. date_from/date_to are an optional inclusive range."""
    question = (body.get('question') or '').strip()
    if len(question) < 2:
        return ok({'results': [], 'count': 0})

    payload = {
        'mode': 'search',
        'question': question,
        'user': resolve_user_display_name(caller),  # soft context only
        'caller_sub': caller.get('sub', ''),
        'k': int(body.get('k', 30)) if str(body.get('k', 30)).isdigit() else 30,
    }
    date_from = body.get('date_from')
    date_to = body.get('date_to')
    if date_from and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date_from)):
        return error('Invalid date_from (expected YYYY-MM-DD)')
    if date_to and not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date_to)):
        return error('Invalid date_to (expected YYYY-MM-DD)')
    if date_from:
        payload['date_from'] = date_from
    if date_to:
        payload['date_to'] = date_to
    if body.get('site'):
        payload['site'] = body['site']  # project-scoped search (Ask omits site)

    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload),
        )
        if resp.get('FunctionError'):
            logger.error(f"Search agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Search error', 500)
        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Search invocation failed: {e}")
        return error(f'Search error: {e}', 500)


def get_users(params):
    try:
        mapping = load_user_mapping()
        return ok({'users': [{'device_id': k, 'name': v.get('name', k), 'role': v.get('role', 'worker'), 'sites': v.get('sites', [])}
                             for k, v in mapping.get('mapping', {}).items()]})
    except Exception as e:
        return error(f'Failed: {e}', 500)


def get_sites(params, caller):
    """Return sites this caller can access, with metadata."""
    mapping = load_user_mapping()
    all_sites = mapping.get('sites', {})
    accessible = get_accessible_sites(caller)
    
    sites = []
    for site_id in accessible:
        site_info = all_sites.get(site_id, {})
        # Count users on this site
        users_on_site = get_accessible_users(caller, site_filter=site_id)
        sites.append({
            'site_id': site_id,
            'name': site_info.get('name', site_id),
            'location': site_info.get('location', ''),
            'client': site_info.get('client', ''),
            'user_count': len(users_on_site),
        })
    
    return ok({
        'sites': sites,
        'role': caller['role'],
        'display_name': caller.get('display_name', caller.get('name', '')),
    })


def get_site_users(params, caller):
    """Return users on a specific site that this caller can access."""
    site = params.get('site', '')
    if not site:
        return error('Missing site parameter')
    
    # Verify caller has access to this site
    accessible_sites = get_accessible_sites(caller)
    if site not in accessible_sites:
        return error('Access denied to this site', 403)
    
    users = get_accessible_users(caller, site_filter=site)
    return ok({'users': users, 'site': site})

def health_check(params):
    return ok({'status': 'ok', 'service': 'sitesync-api', 'version': '2.0',
               'bucket': S3_BUCKET, 'timestamp': datetime.utcnow().isoformat() + 'Z'})


# ── Router ───────────────────────────────────────────────────

def lambda_handler(event, context):
    logger.info(f"Request: {event.get('httpMethod','GET')} {event.get('path','/')}")
    method = event.get('httpMethod', 'GET').upper()
    path = event.get('path', '/')
    params = event.get('queryStringParameters') or {}
    if method == 'OPTIONS':
        return ok({'message': 'CORS OK'})
    body = {}
    if method in ('POST','PATCH','PUT') and event.get('body'):
        try: body = json.loads(event['body'])
        except: body = {}
    if path == '/api/health':
        return health_check(params)
    caller = get_caller_identity(event)
    try:
        if path == '/api/timeline': return get_timeline(params, caller)
        elif path == '/api/dates': return get_dates(params, caller)
        elif path == '/api/media/presigned-url': return get_presigned_url(params, caller)
        elif path == '/api/reports/history': return get_report_history(params, caller)
        elif path == '/api/reports/generate' and method == 'POST': return trigger_report_generation(body, caller)
        elif path == '/api/users': return get_users(params)
        elif path == '/api/sites': return get_sites(params, caller)
        elif path == '/api/site-users': return get_site_users(params, caller)
        elif path == '/api/transcripts': return get_transcripts(params, caller)
        elif path == '/api/audio-segments': return get_audio_segments(params, caller)
        elif path == '/api/video-segments': return get_video_segments(params, caller)
        elif path == '/api/recording-stats': return get_recording_stats(params, caller)
        elif path == '/api/actions/toggle' and method == 'POST': return toggle_action(body, caller)
        elif path == '/api/actions': return get_actions(params, caller)
        elif path == '/api/ask' and method == 'POST': return ask_question(body, caller)
        elif path == '/api/ask/voice' and method == 'POST': return ask_voice(body, caller)
        elif path == '/api/search' and method == 'POST': return search_topics(body, caller)
        else: return error(f'Not found: {method} {path}', 404)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return error(f'Internal error: {e}', 500)