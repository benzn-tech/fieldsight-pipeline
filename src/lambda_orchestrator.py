"""
Lambda 1: Orchestrator v3 - Query file lists and trigger downloads

UPDATED: Uses web scraping endpoints (realptt.com) instead of JSON API
(api.realptt.com), which returned 0 results for video and audio.

Data sources:
1. Video:  realptt.com/ptt/webserver?event=org_videolist  (HTML → parse Ud() calls)
2. Audio:  realptt.com/ptt/webserver?event=org_audiolist  (HTML → per group/day)
3. Upload: realptt.com/ptt/uploadFile?method=get          (JSON, limit=20 max)

Key findings:
- api.realptt.com /ptt/video and /ptt/audio return 0 results
- Web interface uses realptt.com/ptt/webserver with HTML responses
- Video/audio data embedded as JavaScript Ud() function calls in HTML
- Audio endpoint requires: GroupId (capital G/I), time=YYYY-M-D_HH:MM:SS format
- Upload files API: limit must be 20 (100 causes empty body)
- Video times are server time (UTC+8), need +13h for NZ display/filenames
- Audio times are already client time (NZ), need -13h for download URL construction

Trigger: EventBridge scheduled event (daily at 8 PM NZDT)

Environment Variables:
    REALPTT_ACCOUNT     - REAL PTT company account
    REALPTT_PASSWORD    - REAL PTT password
    S3_BUCKET           - S3 bucket name
    DOWNLOADER_FUNCTION - Downloader Lambda function name
    START_DAYS_AGO      - Query data from N days ago (default: 1)
    DOWNLOAD_AUDIO      - Download audio files (true/false)
    DOWNLOAD_VIDEO      - Download video files (true/false)
    DOWNLOAD_FILES      - Download uploaded files (true/false)
    TIME_DIFFERENCE_MS  - Client-to-server time offset in ms (default: 46800000 = 13h for NZ)
"""

import os
import json
import re
import hashlib
import hmac
import logging
import boto3
import urllib3
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, unquote, urlencode, quote

# Disable SSL warnings (REAL PTT uses self-signed certificate)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
lambda_client = boto3.client('lambda')
s3_client = boto3.client('s3')

# User mapping cache
_user_mapping = None

# ============================================================
# Constants
# ============================================================
WEB_BASE = 'https://realptt.com'     # Must use realptt.com, NOT api.realptt.com
UPLOAD_FILE_LIMIT = 20               # API max per page (higher = empty body)
TIMEZONE_OFFSET = -780               # NZDT: -780 minutes from GMT


# ============================================================
# Configuration
# ============================================================

def get_config():
    """Get configuration from environment variables"""
    return {
        'account': os.environ.get('REALPTT_ACCOUNT', ''),
        'password': os.environ.get('REALPTT_PASSWORD', ''),
        's3_bucket': os.environ.get('S3_BUCKET', ''),
        'downloader_function': os.environ.get('DOWNLOADER_FUNCTION', 'fieldsight-downloader'),
        'start_days_ago': int(os.environ.get('START_DAYS_AGO', '1')),
        'download_audio': os.environ.get('DOWNLOAD_AUDIO', 'true').lower() == 'true',
        'download_video': os.environ.get('DOWNLOAD_VIDEO', 'true').lower() == 'true',
        'download_files': os.environ.get('DOWNLOAD_FILES', 'true').lower() == 'true',
        'time_difference_ms': int(os.environ.get('TIME_DIFFERENCE_MS', '46800000')),
    }


# ============================================================
# User Mapping
# ============================================================

def load_user_mapping(bucket):
    """
    Load user mapping from S3: config/user_mapping.json
    
    Supports BOTH formats:
      v2: {"mapping": {"Benl1": "Jarley Trainor"}}
      v3: {"mapping": {"Benl1": {"name": "Jarley Trainor", "role": "site_manager", ...}}}
    
    Always returns: {"Benl1": "Jarley Trainor", ...}  (device → display name string)
    """
    global _user_mapping
    if _user_mapping is not None:
        return _user_mapping
    try:
        response = s3_client.get_object(Bucket=bucket, Key='config/user_mapping.json')
        data = json.loads(response['Body'].read().decode('utf-8'))
        raw_mapping = data.get('mapping', {})
        
        # Normalize: extract name string from v3 objects
        normalized = {}
        for device, value in raw_mapping.items():
            if isinstance(value, str):
                normalized[device] = value
            elif isinstance(value, dict):
                normalized[device] = value.get('name', device)
            else:
                normalized[device] = str(value)
        
        _user_mapping = normalized
        logger.info(f"Loaded user mapping: {len(normalized)} entries")
        return _user_mapping
    except s3_client.exceptions.NoSuchKey:
        logger.warning("User mapping file not found, using device names as-is")
        _user_mapping = {}
        return _user_mapping
    except Exception as e:
        logger.warning(f"Failed to load user mapping: {e}")
        _user_mapping = {}
        return _user_mapping

def get_display_name(device_account, bucket):
    """Get display name for a device account"""
    if not device_account:
        return "Unknown"
    mapping = load_user_mapping(bucket)
    return mapping.get(device_account, device_account)


# ============================================================
# Auth & HTTP
# ============================================================

def sha1(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()

def hmac_sha1(key, message):
    return hmac.new(key.encode('utf-8'), message.encode('utf-8'), hashlib.sha1).hexdigest()


def create_http_client():
    """Create HTTP client with cookies support"""
    return urllib3.PoolManager(
        cert_reqs='CERT_NONE',
        timeout=urllib3.Timeout(connect=30, read=120)
    )


def login(http, account, password):
    """
    Login via realptt.com and return cookies dict for subsequent requests.

    Returns dict: {'JSESSIONID': '...', ...}
    """
    # Step 1: Get random number
    resp = http.request('GET', f'{WEB_BASE}/ptt/random')
    data = json.loads(resp.data.decode('utf-8'))
    if data.get('code') != 0:
        raise Exception(f"Failed to get random: {data.get('msg')}")

    random_str = data['data']['random']
    session_id = data['data']['sessionId']

    # Step 2: Encrypt password
    pwd_sha1 = sha1(password)
    pwd_encrypted = hmac_sha1(random_str, pwd_sha1)

    # Step 3: Login with cookie-based session
    login_url = (
        f"{WEB_BASE}/ptt/organization;jsessionid={session_id}"
        f"?method=login&account={account}&pwd={pwd_encrypted}"
        f"&timeZoneOffset={TIMEZONE_OFFSET}"
    )
    resp = http.request('GET', login_url)
    data = json.loads(resp.data.decode('utf-8'))
    if data.get('code') != 0:
        raise Exception(f"Login failed: {data.get('msg')}")

    logger.info(f"Login successful: {account}")
    return session_id


def web_get(http, session_id, path, params=None):
    """
    Make authenticated GET request via realptt.com with jsessionid.
    Returns response bytes.
    """
    url = f"{WEB_BASE}{path};jsessionid={session_id}"
    if params:
        url += '?' + urlencode(params, quote_via=quote)
    resp = http.request('GET', url)
    return resp


# ============================================================
# JS Argument Parser
# ============================================================

def parse_js_args(arg_string):
    """Parse comma-separated JS function arguments respecting single quotes"""
    args = []
    current = ''
    in_quote = False
    for ch in arg_string:
        if ch == "'" and not in_quote:
            in_quote = True
        elif ch == "'" and in_quote:
            in_quote = False
        elif ch == ',' and not in_quote:
            args.append(current.strip().strip("'"))
            current = ''
            continue
        current += ch
    args.append(current.strip().strip("'"))
    return args


# ============================================================
# 1. QUERY VIDEOS (Web HTML → parse Ud() calls)
# ============================================================

def query_video_files(http, session_id, start_date, end_date, time_diff_ms):
    """
    Query PTT video calls from web endpoint org_videolist.

    Video Ud() signature:
    Ud(id, 'time', src_user_id, dst_user_id, 'src_account', 'dst_account',
       'src_name', 'dst_name', 'timestamp_str', type, unknown,
       'mp4_url', unknown2, save_months, 'traffic', '')

    Video times are SERVER time (UTC+8). We convert to NZ time (+13h) for filenames.
    """
    logger.info("Querying videos via org_videolist...")

    resp = web_get(http, session_id, '/ptt/webserver', {
        'event': 'org_videolist',
        'SearchItem': 'SearchAll', 'SearchAll': '1',
        'beginDate': start_date, 'endDate': end_date,
        'videoSaveTimeFlag': '1',
    })

    html = resp.data.decode('utf-8', errors='replace')
    logger.info(f"  HTML size: {len(html)} bytes")

    videos = []
    for match in re.findall(r'Ud\(([^)]+)\)', html):
        args = parse_js_args(match)
        if len(args) >= 12 and args[11].startswith('http') and 'w3.org' not in args[11]:
            # Convert server time to NZ time
            server_time_str = args[1]
            try:
                server_dt = datetime.strptime(server_time_str, '%Y-%m-%d %H:%M:%S')
                nz_dt = server_dt + timedelta(milliseconds=time_diff_ms)
                nz_time_str = nz_dt.strftime('%Y-%m-%d %H:%M:%S')
            except:
                nz_time_str = server_time_str

            videos.append({
                'type': 'video',
                'time': nz_time_str,
                'server_time': server_time_str,
                'src_account': args[4],
                'src_name': args[6],
                'video_type': args[9],
                'url': args[11],
            })

    logger.info(f"  Found {len(videos)} videos")
    return videos


# ============================================================
# 2. QUERY AUDIO (Web HTML → per group/day, parse Ud() calls)
# ============================================================

def query_groups(http, session_id):
    """Fetch all groups (needed for audio queries)"""
    resp = web_get(http, session_id, '/ptt/group', {
        'method': 'get', 'limit': '20', 'page': '0',
    })
    data = json.loads(resp.data.decode('utf-8'))
    groups = data.get('data', {}).get('groups', []) if data.get('code') == 0 else []
    logger.info(f"  Groups: {[(g['group_id'], g['group_name']) for g in groups]}")
    return groups


def date_no_pad(date_str):
    """'2026-02-09' → '2026-2-9' (audio endpoint requires no zero-padding)"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return f"{dt.year}-{dt.month}-{dt.day}"


def query_audio_for_date(http, session_id, group_id, date_str):
    """
    Query audio for a single group on a single date.

    Audio Ud() signature:
    Ud(spkId, 'clientTime', 'userName', 'groupName', 'duration', status, 'urlParams', recordDecode)

    Audio times are already CLIENT time (NZ). The download URL needs server time.
    """
    dfmt = date_no_pad(date_str)
    resp = web_get(http, session_id, '/ptt/webserver', {
        'event': 'org_audiolist',
        'GroupId': str(group_id),
        'time': f'{dfmt}_00:00:00',
        'endTime': f'{dfmt}_23:59:59',
        'pageSize': '20', 'sort': '0', 'autoPlay': 'false',
    })

    html = resp.data.decode('utf-8', errors='replace')
    audios = []

    for match in re.findall(r'Ud\((\d+[^)]+)\)', html):
        args = parse_js_args(match)
        if len(args) >= 7:
            audios.append({
                'spk_id': args[0],
                'time': args[1],       # Client time (NZ)
                'user_name': args[2],
                'group_name': args[3],
                'duration': args[4],
            })

    return audios


def build_audio_download_url(spk_id, client_time_str, time_diff_ms):
    """
    Construct audio download URL from record.realptt.com.

    Server time = client time − timeDifference (46800000ms = 13h for NZ)
    URL: https://record.realptt.com/voice/?SpkId={id}&time={date}&filename={datetime}.wav&CodecType=0
    """
    try:
        client_dt = datetime.strptime(client_time_str, '%Y-%m-%d %H:%M:%S')
        server_dt = client_dt - timedelta(milliseconds=time_diff_ms)
    except:
        return ''

    server_date = f"{server_dt.year}_{server_dt.month}_{server_dt.day}"
    server_time = f"{server_dt.hour}_{server_dt.minute}_{server_dt.second}"

    return (
        f"https://record.realptt.com/voice/"
        f"?SpkId={spk_id}&time={server_date}"
        f"&filename={server_date} {server_time}.wav&CodecType=0"
    )


def query_audio_files(http, session_id, start_date, end_date, time_diff_ms):
    """
    Query all audio recordings across all groups and dates.
    Returns list of dicts with download_url constructed.
    """
    logger.info("Querying audio via org_audiolist (per group/day)...")
    groups = query_groups(http, session_id)
    if not groups:
        logger.warning("  No groups found")
        return []

    # Build date list
    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime('%Y-%m-%d'))
        cur += timedelta(days=1)

    total_queries = len(dates) * len(groups)
    logger.info(f"  {len(dates)} days x {len(groups)} groups = {total_queries} queries")

    all_audio = []
    n = 0
    for group in groups:
        gid = group['group_id']
        gname = group['group_name']
        for ds in dates:
            n += 1
            audios = query_audio_for_date(http, session_id, gid, ds)
            if audios:
                logger.info(f"  [{n}/{total_queries}] {gname}/{ds}: {len(audios)} recordings")
                for a in audios:
                    download_url = build_audio_download_url(
                        a['spk_id'], a['time'], time_diff_ms
                    )
                    all_audio.append({
                        'type': 'audio',
                        'spk_id': a['spk_id'],
                        'time': a['time'],       # NZ client time
                        'user_name': a['user_name'],
                        'group_name': a['group_name'],
                        'duration': a['duration'],
                        'download_url': download_url,
                    })

            # Brief pause to avoid overloading server
            import time as _time
            _time.sleep(0.2)

    logger.info(f"  Total audio: {len(all_audio)}")
    return all_audio


# ============================================================
# 3. QUERY UPLOAD FILES (JSON API, limit=20)
# ============================================================

def query_upload_files(http, session_id, start_date, end_date):
    """
    Query uploaded files (body cam video, pictures, etc.) via JSON API.
    limit must be 20 — higher values cause empty responses.
    Includes retry logic for intermittent empty responses.
    """
    logger.info("Querying upload files...")
    all_files = []
    page = 1
    consecutive_fails = 0

    while True:
        resp = web_get(http, session_id, '/ptt/uploadFile', {
            'method': 'get',
            'start_time': start_date,
            'end_time': end_date,
            'limit': str(UPLOAD_FILE_LIMIT),
            'page': str(page), 'sort': '0',
        })

        data = None
        try:
            body = resp.data.decode('utf-8')
            if body:
                data = json.loads(body)
        except:
            pass

        if data is None or data.get('code') != 0:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                break
            # Retry same page
            import time as _time
            _time.sleep(2)
            continue

        consecutive_fails = 0
        files = data.get('data', {}).get('uploadFiles', [])
        page_size = data.get('data', {}).get('pageSize', 1)

        if not files:
            break

        all_files.extend(files)
        logger.info(f"  Upload files: page {page}/{page_size}, total {len(all_files)}")

        if page >= page_size:
            break
        page += 1
        import time as _time
        _time.sleep(0.3)

    logger.info(f"  Upload files total: {len(all_files)}")
    return all_files


# ============================================================
# S3 Helpers
# ============================================================

def check_s3_exists(bucket, key):
    """Check if file already exists in S3"""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except:
        return False


def invoke_downloader(function_name, file_info, s3_bucket):
    """Asynchronously invoke the downloader Lambda"""
    payload = {'file_info': file_info, 's3_bucket': s3_bucket}
    lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='Event',  # Async
        Payload=json.dumps(payload)
    )


def safe_name(name):
    """Remove invalid characters from filename/folder name"""
    if not name:
        return "Unknown"
    return re.sub(r'[<>:"/\\|?*\s]', '_', str(name))


def format_time(time_str):
    """Format timestamp for filename: YYYY-MM-DD_HH-MM-SS"""
    if not time_str:
        return datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    return time_str.replace(' ', '_').replace(':', '-')


def extract_date_from_time(time_str):
    """
    Extract date (YYYY-MM-DD) from various time string formats.

    Handles:
        '2026-02-19 14:28:51'  → '2026-02-19'
        '2026-02-19_14-28-51'  → '2026-02-19'
        '2026-02-19'           → '2026-02-19'
    """
    if not time_str:
        return datetime.now().strftime('%Y-%m-%d')
    # Match YYYY-MM-DD at the start
    match = re.match(r'(\d{4}-\d{2}-\d{2})', time_str)
    if match:
        return match.group(1)
    return datetime.now().strftime('%Y-%m-%d')


def generate_s3_key(file_info, bucket):
    """
    Generate S3 key with user-based and date-based folder structure.

    Structure: users/{display_name}/{content_type}/{date}/{device}_{timestamp}.ext

    Examples:
        users/John_Smith/audio/2026-02-19/Benl1_2026-02-19_10-30-00.wav
        users/John_Smith/pictures/2026-02-19/Benl1_2026-02-19_10-28-00.jpg
        users/John_Smith/video/2026-02-19/Benl1_2026-02-19_10-23-04.mp4
    """
    file_type = file_info.get('type', '')

    if file_type == 'upload':
        device_account = file_info.get('sender_account', 'unknown')
        display_name = get_display_name(device_account, bucket)
        upload_time = file_info.get('upload_time', '')
        file_name = file_info.get('file_name', '')
        ftype = file_info.get('file_type', '').lower()

        # CRITICAL: Use the actual recording time from file_name, NOT upload_time.
        #
        # file_name contains the real timestamp: "2026-02-13-12-50-17"
        # upload_time is when it was uploaded to the server, which can be
        # days later (e.g. body cam footage uploaded in bulk).
        #
        # Example:
        #   upload_time = "2026-02-23 11:57:29"  ← wrong for folder/filename
        #   file_name   = "2026-02-13-12-50-17"  ← actual recording time
        #
        # This ensures files land in the correct date folder for report generation.
        actual_time = ''
        actual_date = ''
        if file_name:
            # file_name format: "YYYY-MM-DD-HH-MM-SS" (no extension)
            fn_match = re.match(r'(\d{4}-\d{2}-\d{2})-(\d{2}-\d{2}-\d{2})', file_name)
            if fn_match:
                actual_date = fn_match.group(1)
                actual_time = f"{fn_match.group(1)}_{fn_match.group(2)}"

        # Fall back to upload_time if file_name has no parseable timestamp
        if not actual_time:
            actual_time = format_time(upload_time)
        if not actual_date:
            actual_date = extract_date_from_time(upload_time)

        time_str = actual_time
        date_str = actual_date

        if 'picture' in ftype or 'image' in ftype:
            content_folder = 'pictures'
        elif 'video' in ftype:
            content_folder = 'video'
        elif 'audio' in ftype:
            content_folder = 'audio'
        else:
            content_folder = 'other'

        # Get extension from download URL
        down_path = file_info.get('down_path', '')
        ext = ''
        try:
            parsed = urlparse(down_path)
            params = parse_qs(parsed.query)
            if 'FileUrl' in params:
                real_url = unquote(params['FileUrl'][0])
                ext = os.path.splitext(urlparse(real_url).path)[1]
        except:
            pass
        if not ext:
            ext = {'pictures': '.jpg', 'video': '.mp4', 'audio': '.wav'}.get(content_folder, '')

        filename = safe_name(f"{device_account}_{time_str}{ext}")
        return f"users/{safe_name(display_name)}/{content_folder}/{date_str}/{filename}"

    elif file_type == 'audio':
        device_account = file_info.get('user_name', 'unknown')
        display_name = get_display_name(device_account, bucket)
        audio_time = file_info.get('time', '')
        time_str = format_time(audio_time)
        date_str = extract_date_from_time(audio_time)

        filename = safe_name(f"{device_account}_{time_str}.wav")
        return f"users/{safe_name(display_name)}/audio/{date_str}/{filename}"

    elif file_type == 'video':
        device_account = file_info.get('src_account', file_info.get('src_name', 'unknown'))
        display_name = get_display_name(device_account, bucket)
        video_time = file_info.get('time', '')
        time_str = format_time(video_time)
        date_str = extract_date_from_time(video_time)

        filename = safe_name(f"{device_account}_{time_str}.mp4")
        return f"users/{safe_name(display_name)}/video/{date_str}/{filename}"

    return f"other/unknown_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"


# ============================================================
# MAIN HANDLER
# ============================================================

def lambda_handler(event, context):
    """Main Lambda handler"""
    logger.info("=" * 60)
    logger.info("FieldSight Pipeline v3 - Starting")
    logger.info("=" * 60)

    config = get_config()


    # Allow event to override lookback window (for weekly catchup rule)
    if event.get('override_days_ago'):
        config['start_days_ago'] = int(event['override_days_ago'])
        logger.info(f"Override: scanning {config['start_days_ago']} days back")

    if not config['account'] or not config['password']:
        logger.error("Missing REALPTT_ACCOUNT or REALPTT_PASSWORD")
        return {'statusCode': 400, 'body': 'Missing credentials'}
    if not config['s3_bucket']:
        logger.error("Missing S3_BUCKET")
        return {'statusCode': 400, 'body': 'Missing S3_BUCKET'}

    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=config['start_days_ago'])
    start_date_str = start_date.strftime('%Y-%m-%d')
    end_date_str = end_date.strftime('%Y-%m-%d')

    logger.info(f"Query range: {start_date_str} to {end_date_str}")
    logger.info(f"S3 Bucket: {config['s3_bucket']}")
    logger.info(f"Time difference: {config['time_difference_ms']}ms")

    # Login via realptt.com
    http = create_http_client()
    session_id = login(http, config['account'], config['password'])

    # Pre-load user mapping
    load_user_mapping(config['s3_bucket'])

    # Statistics
    stats = {
        'total_found': 0,
        'already_exists': 0,
        'triggered': 0,
        'by_type': {'video': 0, 'audio': 0, 'upload': 0},
        'by_user': {},
    }

    def process_file(file_info):
        """Check S3 existence and trigger download if needed"""
        stats['total_found'] += 1
        ftype = file_info.get('type', 'upload')

        s3_key = generate_s3_key(file_info, config['s3_bucket'])
        device = (file_info.get('sender_account') or
                  file_info.get('user_name') or
                  file_info.get('src_account') or 'unknown')
        display_name = get_display_name(device, config['s3_bucket'])

        if check_s3_exists(config['s3_bucket'], s3_key):
            stats['already_exists'] += 1
            return

        file_info['s3_key'] = s3_key
        file_info['display_name'] = display_name
        file_info['device_account'] = device

        invoke_downloader(config['downloader_function'], file_info, config['s3_bucket'])
        stats['triggered'] += 1
        stats['by_type'][ftype] = stats['by_type'].get(ftype, 0) + 1
        stats['by_user'][display_name] = stats['by_user'].get(display_name, 0) + 1

    # --- 1. Videos ---
    if config['download_video']:
        videos = query_video_files(
            http, session_id, start_date_str, end_date_str,
            config['time_difference_ms']
        )
        for v in videos:
            if v.get('url'):
                process_file(v)

    # --- 2. Audio ---
    if config['download_audio']:
        audios = query_audio_files(
            http, session_id, start_date_str, end_date_str,
            config['time_difference_ms']
        )
        for a in audios:
            if a.get('download_url'):
                process_file(a)

    # --- 3. Upload Files ---
    if config['download_files']:
        uploads = query_upload_files(http, session_id, start_date_str, end_date_str)
        for f in uploads:
            file_info = {
                'type': 'upload',
                'down_path': f.get('down_path', ''),
                'file_name': f.get('file_name', ''),
                'file_type': f.get('file_type', ''),
                'sender_account': f.get('sender_account', ''),
                'upload_time': f.get('upload_time', ''),
            }
            if file_info['down_path']:
                process_file(file_info)

    # --- Logout ---
    try:
        web_get(http, session_id, '/ptt/organization', {'method': 'logout'})
    except:
        pass

    # --- Summary ---
    logger.info("=" * 60)
    logger.info("Sync Complete!")
    logger.info(f"  Total found:     {stats['total_found']}")
    logger.info(f"  Already exists:  {stats['already_exists']}")
    logger.info(f"  Downloads fired: {stats['triggered']}")
    logger.info(f"  By type: {stats['by_type']}")
    logger.info(f"  By user:")
    for user, count in stats['by_user'].items():
        logger.info(f"    {user}: {count} files")
    logger.info("=" * 60)

    return {
        'statusCode': 200,
        'body': json.dumps(stats, default=str)
    }