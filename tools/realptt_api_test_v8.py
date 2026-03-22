#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REAL PTT Audio Download URL Verification v8

Tests constructing and downloading audio files from record.realptt.com.

Audio download URL pattern (from HTML source):
  https://record.realptt.com/voice/?SpkId={spk_id}&time={server_date}&filename={server_datetime}.wav&CodecType=0

Time conversion:
  Server time = Client time - timeDifference (46800000ms = 13 hours for NZ)
  Server date format: YYYY_M_D (no zero padding)
  Server time format: H_M_S (no zero padding)
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import hashlib
import hmac
import re
import time
import json
import os
from datetime import datetime, timedelta

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
ACCOUNT = "benl"
PASSWORD = "Realptt1"
START_DATE = "2026-02-03"
END_DATE = "2026-02-05"
TIMEZONE_OFFSET = -780
WEB_BASE = "https://realptt.com"
TIME_DIFFERENCE_MS = 46800000  # 13 hours in ms (NZ -> server/UTC)
# ============================================================


def create_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    })
    session.verify = False
    return session


def login_web(session):
    resp = session.get(f"{WEB_BASE}/ptt/random", timeout=30)
    data = resp.json()
    random_str = data['data']['random']
    pwd_sha1 = hashlib.sha1(PASSWORD.encode('utf-8')).hexdigest()
    pwd_enc = hmac.new(random_str.encode('utf-8'), pwd_sha1.encode('utf-8'), hashlib.sha1).hexdigest()
    resp = session.get(f"{WEB_BASE}/ptt/organization", params={
        'method': 'login', 'account': ACCOUNT,
        'pwd': pwd_enc, 'timeZoneOffset': TIMEZONE_OFFSET,
    }, timeout=30)
    data = resp.json()
    assert data['code'] == 0
    print(f"[OK] Logged in")


def _parse_args(arg_string):
    args = []
    current = ''
    in_quote = False
    for char in arg_string:
        if char == "'" and not in_quote:
            in_quote = True
        elif char == "'" and in_quote:
            in_quote = False
        elif char == ',' and not in_quote:
            args.append(current.strip().strip("'"))
            current = ''
            continue
        current += char
    args.append(current.strip().strip("'"))
    return args


def format_date_no_pad(date_str):
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return f"{dt.year}-{dt.month}-{dt.day}"


def client_to_server_time(client_time_str):
    """
    Convert client time (NZ) to server time (UTC).
    
    JS logic:
      var dbtime = new Date(getDbTime(time));
      function getDbTime(time) { return time.getTime() - timeDifference; }
      timeDifference = 46800000 ms = 13 hours
    
    So: server_time = client_time - 13 hours
    """
    # Parse client time
    client_dt = datetime.strptime(client_time_str, '%Y-%m-%d %H:%M:%S')
    
    # Subtract timeDifference
    server_dt = client_dt - timedelta(milliseconds=TIME_DIFFERENCE_MS)
    
    return server_dt


def build_audio_urls(spk_id, client_time_str):
    """
    Build play and download URLs for an audio recording.
    
    Returns dict with various URL attempts to test.
    """
    server_dt = client_to_server_time(client_time_str)
    
    # Server date: YYYY_M_D (no zero padding)
    server_date = f"{server_dt.year}_{server_dt.month}_{server_dt.day}"
    
    # Server time: H_M_S (no zero padding)  
    server_time = f"{server_dt.hour}_{server_dt.minute}_{server_dt.second}"
    
    # Server datetime for filename
    server_datetime = f"{server_date} {server_time}"
    
    urls = {
        # Play URL (filename=1)
        'play': f"https://record.realptt.com/voice/?SpkId={spk_id}&time={server_date}&filename=1&CodecType=0",
        
        # Download URL with .wav extension
        'download_wav': f"https://record.realptt.com/voice/?SpkId={spk_id}&time={server_date}&filename={server_datetime}.wav&CodecType=0",
        
        # Download without extension
        'download_no_ext': f"https://record.realptt.com/voice/?SpkId={spk_id}&time={server_date}&filename={server_datetime}&CodecType=0",
        
        # Also try with zero-padded date
        'play_padded': f"https://record.realptt.com/voice/?SpkId={spk_id}&time={server_dt.strftime('%Y_%m_%d')}&filename=1&CodecType=0",
        
        # Try CodecType=1 (8K)
        'play_8k': f"https://record.realptt.com/voice/?SpkId={spk_id}&time={server_date}&filename=1&CodecType=1",
        
        # Also try the original API format from docs
        'api_format': f"https://record.realptt.com/voice?SpkId={spk_id}&time={server_date}&filename=1&CodecType=0",
    }
    
    return urls, server_dt


def query_audio_for_date(session, group_id, date_str):
    """Query audio for a specific group and date"""
    date_fmt = format_date_no_pad(date_str)
    
    params = {
        'event': 'org_audiolist',
        'GroupId': group_id,
        'time': f'{date_fmt}_00:00:00',
        'endTime': f'{date_fmt}_23:59:59',
        'pageSize': 20,
        'sort': 0,
        'autoPlay': 'false',
    }
    
    resp = session.get(f"{WEB_BASE}/ptt/webserver", params=params, timeout=60)
    
    if resp.status_code != 200 or len(resp.content) == 0:
        return [], resp.text
    
    # Parse Ud() data calls
    pattern = r"Ud\((\d+[^)]+)\)"
    matches = re.findall(pattern, resp.text)
    
    audios = []
    for match in matches:
        args = _parse_args(match)
        if len(args) >= 7:
            audios.append({
                'spk_id': args[0],
                'time': args[1],
                'user_name': args[2],
                'group_name': args[3],
                'duration': args[4],
                'status': args[5],
                'url_params': args[6],
                'record_decode': args[7] if len(args) > 7 else '0',
            })
    
    return audios, resp.text


def test_audio_download(session, audio_record):
    """Test downloading an audio recording with various URL formats"""
    spk_id = audio_record['spk_id']
    client_time = audio_record['time']
    user = audio_record['user_name']
    
    print(f"\n--- Testing download for: SpkId={spk_id}, Time={client_time}, User={user} ---")
    
    urls, server_dt = build_audio_urls(spk_id, client_time)
    
    print(f"Client time:  {client_time}")
    print(f"Server time:  {server_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"timeDiff:     {TIME_DIFFERENCE_MS}ms = {TIME_DIFFERENCE_MS/3600000}h")
    
    for label, url in urls.items():
        print(f"\n  [{label}]")
        print(f"  URL: {url}")
        
        try:
            resp = session.get(url, timeout=30, stream=True)
            content_type = resp.headers.get('content-type', 'N/A')
            content_len = resp.headers.get('content-length', 'N/A')
            
            print(f"  HTTP {resp.status_code} | Type: {content_type} | Length: {content_len}")
            
            if resp.status_code == 200 and content_len and int(content_len) > 100:
                # Download a bit to verify it's actual audio
                data = resp.content[:1000]
                print(f"  First bytes (hex): {data[:20].hex()}")
                
                # Check for WAV header (RIFF)
                if data[:4] == b'RIFF':
                    print(f"  >>> VALID WAV FILE! <<<")
                elif data[:3] == b'ID3' or data[:2] == b'\xff\xfb':
                    print(f"  >>> VALID MP3 FILE! <<<")
                elif data[:4] == b'#!AM':
                    print(f"  >>> AMR FILE! <<<")
                else:
                    print(f"  File type unknown, first 4 bytes: {data[:4]}")
                
                # Save test file
                test_file = f"test_audio_{label}_{spk_id}.wav"
                with open(test_file, 'wb') as f:
                    f.write(data)
                    for chunk in resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                file_size = os.path.getsize(test_file)
                print(f"  Saved: {test_file} ({file_size} bytes)")
                
            elif resp.status_code == 200 and (content_len == '0' or content_len == 'N/A'):
                # Read actual content
                data = resp.content
                print(f"  Actual content length: {len(data)} bytes")
                if len(data) > 0:
                    print(f"  Content: {data[:200]}")
            else:
                body = resp.content[:200]
                if body:
                    print(f"  Body: {body}")
                    
        except Exception as e:
            print(f"  Error: {e}")


def analyze_audio_html_urls(html):
    """Extract actual URL construction logic from the audio HTML"""
    print("\n--- Analyzing audio HTML for URL patterns ---")
    
    # Find the Play function
    play_func = re.search(r'function\s+Play\s*\([^)]*\)\s*\{(.*?)\n\}', html, re.DOTALL)
    if play_func:
        print(f"\nPlay function:")
        print(f"  {play_func.group(0)[:500]}")
    
    # Find the Data/Download function  
    data_func = re.search(r'function\s+Data\s*\([^)]*\)\s*\{(.*?)\n\}', html, re.DOTALL)
    if data_func:
        print(f"\nData function:")
        print(f"  {data_func.group(0)[:500]}")
    
    # Find the Decode function
    decode_func = re.search(r'function\s+Decode\s*\([^)]*\)\s*\{(.*?)\n\}', html, re.DOTALL)
    if decode_func:
        print(f"\nDecode function:")
        print(f"  {decode_func.group(0)[:500]}")
    
    # Find getDbTime function
    getdb_func = re.search(r'function\s+getDbTime\s*\([^)]*\)\s*\{(.*?)\n?\}', html, re.DOTALL)
    if getdb_func:
        print(f"\ngetDbTime function:")
        print(f"  {getdb_func.group(0)[:300]}")
    
    # Find timeDifference variable
    td_match = re.search(r'timeDifference\s*=\s*(\d+)', html)
    if td_match:
        print(f"\ntimeDifference = {td_match.group(1)}")
    
    # Find all record.realptt.com URL patterns
    url_patterns = re.findall(r'(record\.realptt\.com[^\'"<>\s]+)', html)
    print(f"\nrecord.realptt.com URL patterns ({len(url_patterns)}):")
    for u in set(url_patterns):
        print(f"  {u}")
    
    # Find the Ud function definition for audio (different from video)
    ud_func = re.search(r'function\s+Ud\s*\([^)]*\)\s*\{(.*?)(?=\nfunction\s|\n\})', html, re.DOTALL)
    if ud_func:
        print(f"\nAudio Ud function:")
        print(f"  {ud_func.group(0)[:800]}")


def main():
    session = create_session()
    login_web(session)
    
    # Get groups
    resp = session.get(f"{WEB_BASE}/ptt/group", params={
        'method': 'get', 'limit': 20, 'page': 0,
    }, timeout=30)
    groups = resp.json().get('data', {}).get('groups', [])
    print(f"Groups: {[(g['group_id'], g['group_name']) for g in groups]}")
    
    # Query audio for Feb 4 (NorthIsland had 16 recordings)
    print("\n" + "=" * 60)
    print("STEP 1: Query audio for NorthIsland / 2026-02-04")
    print("=" * 60)
    
    north_id = None
    for g in groups:
        if 'north' in g['group_name'].lower():
            north_id = g['group_id']
            break
    
    if not north_id:
        print("ERROR: NorthIsland group not found")
        return
    
    audios, html = query_audio_for_date(session, north_id, '2026-02-04')
    print(f"Found {len(audios)} audio recordings")
    
    if not audios:
        print("No audio found! Check date/group.")
        return
    
    # Analyze HTML for URL construction logic
    print("\n" + "=" * 60)
    print("STEP 2: Analyze HTML URL construction")
    print("=" * 60)
    analyze_audio_html_urls(html)
    
    # Test downloading first audio record
    print("\n" + "=" * 60)
    print("STEP 3: Test audio download")
    print("=" * 60)
    test_audio_download(session, audios[0])
    
    # Also test second audio (different user)
    if len(audios) > 2:
        # Find one from a different user
        first_user = audios[0]['user_name']
        for a in audios:
            if a['user_name'] != first_user:
                test_audio_download(session, a)
                break
    
    # Logout
    try:
        session.get(f"{WEB_BASE}/ptt/organization", params={'method': 'logout'}, timeout=10)
    except:
        pass
    print("\n\nLogged out.")


if __name__ == '__main__':
    main()
