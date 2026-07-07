"""
Lambda: fieldsight-extract-session v1.0 — session-level realtime extraction
(Phase 4b, Task 2).

Non-VPC (talks to Claude directly over HTTPS via claude_utils, mirrors
lambda_report_generator's urllib3 pattern; no Aurora access here).

Triggered by an S3 event on `transcripts/{user}/{date}/{filename}.json`
(BUG-13: this Lambda only ever WRITES `extractions/`, never `transcripts/`,
so it can never re-trigger itself). On each transcript segment landing:
  1. Identify the recording SESSION this segment belongs to (BUG-11 filename
     metadata: `{device}_{YYYY-MM-DD_HH-MM-SS}` prefix, `_off{X}_to{Y}_`
     stripped -- a whole-file segment with no `_off` suffix IS its own
     session).
  2. Gather every transcript segment currently in S3 under the same
     `transcripts/{user}/{date}/` prefix that shares this session_base (a
     session usually lands as several VAD-split segments over time; each one
     re-triggers this Lambda and re-gathers the full set seen so far).
  3. Normalize every segment (transcript_utils.normalize_transcript),
     flatten all speaker turns across segments, sort by absolute time.
  4. One Claude call (claude_utils) extracts topics/action_items/safety_flags
     -- and, as of this pilot, a `declared_site` field for explicit "I've
     arrived at X site" statements (never inferred from mere mentions).
  5. Write `extractions/{user}/{date}/{session_base}.json` (idempotent
     overwrite -- same session_base always maps to the same key regardless
     of how many segments have landed so far).

A Claude-call or JSON-parse failure raises RuntimeError so the S3 event
retries the invocation (no partial/empty extraction is ever written).

Environment Variables:
    S3_BUCKET   - S3 bucket name (the data lake -- IngestBucketName)
    CONFIG_KEY  - S3 key for user/site mapping (default: config/user_mapping.json)
    ANTHROPIC_API_KEY / CLAUDE_MODEL - read by claude_utils
"""
import difflib
import json
import logging
import os
from datetime import datetime
from urllib.parse import unquote_plus

import boto3

import claude_utils
from transcript_utils import (
    extract_base_time_from_filename,
    extract_device_from_filename,
    normalize_transcript,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get('S3_BUCKET', '')
CONFIG_KEY = os.environ.get('CONFIG_KEY', 'config/user_mapping.json')

TRANSCRIPTS_PREFIX = 'transcripts/'
EXTRACTIONS_PREFIX = 'extractions/'
TRANSCRIPT_TEXT_LIMIT = 60000  # BUG-15: must match expected input size
SITE_MATCH_CUTOFF = 0.6

_s3_client = None
_sites_cache = None


def s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client('s3')
    return _s3_client


def load_sites():
    """Load + cache the `sites` dict of config/user_mapping.json (declared_site
    fuzzy-match target list) for the module's lifetime (warm container reuse) --
    mirrors lambda_ingest.load_mapping's caching style."""
    global _sites_cache
    if _sites_cache is None:
        try:
            obj = s3().get_object(Bucket=S3_BUCKET, Key=CONFIG_KEY)
            data = json.loads(obj['Body'].read().decode('utf-8'))
            _sites_cache = data.get('sites', {})
        except Exception as e:
            logger.warning(f"Failed to load site config for declared_site match: {e}")
            _sites_cache = {}
    return _sites_cache


# ============================================================
# Session identification (BUG-11 filename metadata)
# ============================================================

def session_base_from_key(key):
    """`transcripts/{user}/{date}/{filename}.json` -> (user_folder, date,
    session_base) where session_base = filename minus '.json', VAD offset
    suffix stripped (`.split('_off')[0]`). Returns None for anything that
    isn't a parseable transcript key (wrong prefix/shape, non-.json, or a
    filename transcript_utils itself can't extract a device/base-time from --
    validated here rather than downstream so a bad key is skipped once, with
    a single log line, instead of failing deeper in the pipeline)."""
    parts = key.split('/')
    if len(parts) != 4 or parts[0] != 'transcripts':
        return None
    user_folder, date, filename = parts[1], parts[2], parts[3]
    if not filename.endswith('.json'):
        return None

    device = extract_device_from_filename(filename)
    base_time = extract_base_time_from_filename(filename)
    if not device or device == 'Unknown' or not base_time:
        logger.warning(f"Skipping unparseable transcript key: {key}")
        return None

    session_base = filename[:-len('.json')].split('_off')[0]
    return user_folder, date, session_base


def gather_session_segments(bucket, user_folder, date, session_base):
    """List `transcripts/{user_folder}/{date}/` and return the S3 keys
    (sorted) whose OWN session_base matches this one -- i.e. every VAD
    segment (and/or whole-file recording) belonging to the same session,
    never a neighboring session recorded the same day."""
    prefix = f"{TRANSCRIPTS_PREFIX}{user_folder}/{date}/"
    matched = []
    paginator = s3().get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            parsed = session_base_from_key(key)
            if parsed is not None and parsed[2] == session_base:
                matched.append(key)
    return sorted(matched)


# ============================================================
# Prompt construction
# ============================================================

EXTRACTION_SCHEMA = """{
  "topics": [
    {
      "topic_title": "Short descriptive title",
      "category": "safety | progress | quality",
      "summary": "2-4 sentence summary of what was discussed and decided",
      "time_range": "HH:MM – HH:MM",
      "participants": ["Name1", "Name2"],
      "action_items": [
        {
          "action": "What needs to be done",
          "responsible": "Person name",
          "deadline": "When, or null if not mentioned",
          "priority": "high | medium | low"
        }
      ],
      "safety_flags": [
        {
          "observation": "What was observed",
          "risk_level": "high | medium | low",
          "recommended_action": "What should be done"
        }
      ]
    }
  ],
  "declared_site": {"stated": "site name as said", "confidence": 0.0} or null
}"""


def build_extraction_prompt(user_folder, date, session_base, turns, n_segments):
    lines = [f"[{t['abs_start_str']}] {t['speaker']}: {t['text']}" for t in turns]
    transcript_text = "\n".join(lines)[:TRANSCRIPT_TEXT_LIMIT]

    return f"""You are a construction site documentation assistant for a New Zealand construction company.

Analyze the following radio transcript from ONE continuous field-recording SESSION
(worker: {user_folder}, date: {date}, session: {session_base}, {n_segments} recorded segment(s)
merged in chronological order) and produce STRUCTURED operational items.

## Session Transcript (chronological, absolute times)
{transcript_text}

## Instructions
1. Group the transcript into logical ops TOPICS (e.g. "Morning Safety Briefing", "Block C Pour").
2. For each topic, classify as safety/progress/quality, list participants by name, and extract
   action_items and safety_flags.
3. declared_site: set this ONLY if a speaker EXPLICITLY declares arrival at a named site
   (e.g. "I've arrived at X site", "我到了 XX 工地", "now at X"). Simply MENTIONING a
   site name (discussing it, planning to go there, referencing a past visit) is NOT a
   declaration. If no explicit arrival declaration is present anywhere in this transcript,
   declared_site MUST be null.

## Output Format
Return ONLY valid JSON matching this EXACT schema (no markdown fences, no explanation):

{EXTRACTION_SCHEMA}

Rules:
- category MUST be one of: safety, progress, quality
- priority and risk_level MUST be one of: high, medium, low
- time_range format: "HH:MM – HH:MM" (en dash), derived from the [HH:MM:SS] timestamps above
- participants, action_items, safety_flags may be empty arrays
- declared_site.confidence is YOUR OWN confidence (0.0-1.0) that this is truly an explicit
  arrival declaration, not a mention
- Do NOT include any text outside the JSON object"""


# ============================================================
# declared_site post-processing — fuzzy match against config/user_mapping.json
# ============================================================

def _fuzzy_match_site(stated):
    sites = load_sites()
    names = [info.get('name', '') for info in sites.values() if info.get('name')]
    if not stated or not names:
        return None
    matches = difflib.get_close_matches(stated, names, n=1, cutoff=SITE_MATCH_CUTOFF)
    return matches[0] if matches else None


def process_declared_site(declared):
    """Claude's raw {"stated", "confidence"} (or None) -> the extraction
    contract's {"stated", "matched_site", "confidence"} (or None). v1 only
    stores this for record -- it does not change any site attribution
    (that consumption waits on the identity-system's recording_sessions,
    Phase 4b Global Constraints)."""
    if not declared or not declared.get('stated'):
        return None
    stated = declared['stated']
    return {
        'stated': stated,
        'matched_site': _fuzzy_match_site(stated),
        'confidence': declared.get('confidence', 0.0),
    }


# ============================================================
# Core: extract one session
# ============================================================

def extract_session(bucket, user_folder, date, session_base):
    keys = gather_session_segments(bucket, user_folder, date, session_base)

    normalized_list = []
    source_filenames = []
    for key in keys:
        try:
            obj = s3().get_object(Bucket=bucket, Key=key)
            data = json.loads(obj['Body'].read().decode('utf-8'))
        except Exception as e:
            logger.warning(f"Skipping corrupt transcript segment {key}: {e}")
            continue

        filename = key.rsplit('/', 1)[-1]
        normalized = normalize_transcript(data, filename)
        if normalized is None:
            logger.warning(f"Skipping unnormalizable transcript segment {key}")
            continue

        normalized_list.append(normalized)
        source_filenames.append(filename)

    turns = []
    for normalized in normalized_list:
        for turn in normalized.get('speaker_turns', []):
            if turn.get('abs_start') is None:
                continue
            turns.append(turn)
    turns.sort(key=lambda t: t['abs_start'])

    n_segments = len(normalized_list)
    prompt = build_extraction_prompt(user_folder, date, session_base, turns, n_segments)
    max_tokens = min(4096 + n_segments * 350, 8000)  # BUG-16

    raw_response, error = claude_utils.call_claude(prompt, max_tokens=max_tokens)
    if raw_response is None:
        raise RuntimeError(f"Claude call failed for session {session_base}: {error}")

    parsed = claude_utils.extract_json(raw_response)
    if parsed is None:
        raise RuntimeError(f"Failed to parse Claude JSON for session {session_base}")

    extraction = {
        'schema_version': 1,
        'user_folder': user_folder,
        'date': date,
        'session_base': session_base,
        'source_transcripts': sorted(source_filenames),
        'extracted_at': datetime.utcnow().isoformat() + 'Z',
        'declared_site': process_declared_site(parsed.get('declared_site')),
        'topics': parsed.get('topics', []),
    }

    out_key = f"{EXTRACTIONS_PREFIX}{user_folder}/{date}/{session_base}.json"
    s3().put_object(
        Bucket=bucket, Key=out_key,
        Body=json.dumps(extraction, ensure_ascii=False, indent=2),
        ContentType='application/json',
    )
    return extraction


# ============================================================
# Lambda entry point — S3 event
# ============================================================

def lambda_handler(event, context):
    results = []
    for record in event.get('Records', []):
        key = unquote_plus(record['s3']['object']['key'])
        parsed = session_base_from_key(key)
        if parsed is None:
            logger.warning(f"Skipping S3 event record with unparseable key: {key}")
            continue
        user_folder, date, session_base = parsed
        results.append(extract_session(S3_BUCKET, user_folder, date, session_base))
    return {'results': results}
