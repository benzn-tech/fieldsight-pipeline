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

def parse_time_to_seconds(time_str):
    parts = time_str.replace(' ', '').split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0

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
    accessible = get_accessible_users(caller) if caller else None
    try:
        resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if key.endswith('/daily_report.json') and '_debug' not in key:
                parts = key.replace(prefix, '').split('/')
                if len(parts) >= 2:
                    user_name = parts[0]
                    # Filter by permission
                    if accessible:
                        if not any(u['folder_name'] == user_name or u['name'] == user_name for u in accessible):
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
    # Determine which user folders to check
    if role == 'worker':
        user_folders = [resolve_user_display_name(caller)]
    elif site:
        # Filter to specific site's users
        users = get_accessible_users(caller, site_filter=site)
        user_folders = [u['folder_name'] for u in users]
    elif role in ('admin', 'gm'):
        user_folders = []  # Check all (no filter)
    else:
        user_folders = [resolve_user_display_name(caller)]
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
                            if user_folders:
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
            if user_folders:
                for uf in user_folders:
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
            if not loaded:
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

def get_presigned_url(params):
    s3_key = unquote_plus(params.get('key', ''))
    if not s3_key:
        return error('Missing key')
    allowed = ['users/', 'audio_segments/', 'transcripts/', 'reports/']
    if not any(s3_key.startswith(p) for p in allowed):
        return error('Access denied', 403)
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
    start_sec = parse_time_to_seconds(start_time) - 60 if start_time else 0  # 60s buffer
    end_sec = parse_time_to_seconds(end_time) + 60 if end_time else 86400
    if start_sec < 0: start_sec = 0

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
        file_end_sec = file_time_sec + 600
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
    start_sec = parse_time_to_seconds(topic_start) if topic_start else 0
    end_sec = parse_time_to_seconds(topic_end) if topic_end else 86400

    prefix = f"audio_segments/{user_folder}/{date}/"
    segments = []
    try:
        resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        for obj in resp.get('Contents', []):
            key = obj['Key']
            if not key.endswith('.wav'):
                continue
            filename = key.split('/')[-1]
            base_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})_off', filename)
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
    start_sec = parse_time_to_seconds(topic_start) if topic_start else 0
    end_sec = parse_time_to_seconds(topic_end) if topic_end else 86400

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
                    filename = key.split('/')[-1]
                    time_match = re.search(r'\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})', filename)
                    if not time_match:
                        continue
                    h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
                    vid_start = h * 3600 + m * 60 + s
                    vid_end = vid_start + 600
                    if vid_end < start_sec or vid_start > end_sec:
                        continue
                    # Skip if we already have a preview version of this file
                    base_name = re.sub(r'\.\w+$', '', filename)
                    if not is_preview and any(v.get('base_name') == base_name for v in videos):
                        continue
                    offset = max(0, start_sec - vid_start)
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
    stats = {'video_count': 0, 'audio_count': 0, 'total_files': 0,
             'total_size_mb': 0, 'estimated_duration_min': 0}
    for media_type in ['video', 'audio']:
        for name_variant in [user_folder, user]:
            prefix = f"users/{name_variant}/{media_type}/{date}/"
            try:
                resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
                for obj in resp.get('Contents', []):
                    key = obj['Key'].lower()
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
    role = caller['role']
    # Get accessible user folders
    if role == 'worker':
        allowed_folders = [resolve_user_display_name(caller)]
    elif role in ('admin', 'gm'):
        allowed_folders = []  # no filter
    else:
        accessible = get_accessible_users(caller)
        allowed_folders = [u['folder_name'] for u in accessible]
    
    reports = []
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=REPORT_PREFIX):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if not key.endswith('_report.json') or '_debug' in key:
                    continue
                if allowed_folders:
                    if not any(f'/{uf}/' in key for uf in allowed_folders):
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
        elif path == '/api/media/presigned-url': return get_presigned_url(params)
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
        else: return error(f'Not found: {method} {path}', 404)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return error(f'Internal error: {e}', 500)