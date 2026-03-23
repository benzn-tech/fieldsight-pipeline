"""
Lambda 8: Ask Agent v1.0 — Report Q&A grounded in transcript + report data

Users can ask questions about any report or meeting minutes.
Answers are grounded in the actual transcript text + structured report JSON.

Architecture:
  1. Load report JSON from S3:  reports/{date}/{user}/daily_report.json
  2. Load raw transcript(s):    transcripts/{user}/{date}/*.json
  3. Normalize via transcript_utils.normalize_transcript()
  4. Build prompt: system context + report JSON + transcript text + user question
  5. Call Claude Haiku → return answer
  6. Stateless — no conversation memory (each question is independent)

Model: Claude Haiku 4.5 (retrieval + summarization, not complex reasoning)

Trigger:
  - API Gateway: POST /api/ask
    Body: {"date": "2026-03-20", "user": "Jarley_Trainor", "question": "..."}

  - Optional fields:
    "scope":    "report" (default) | "transcript" | "both"
    "topic_id": 2         — narrow to specific topic's time range

Environment Variables:
    S3_BUCKET           - S3 bucket name
    ANTHROPIC_API_KEY   - Anthropic API key (sk-ant-xxx)
    HAIKU_MODEL         - Claude model (default: claude-haiku-4-5-20251001)
    REPORT_PREFIX       - Report output prefix (default: reports/)
"""

import os
import json
import logging
import re
import boto3
import urllib3
from datetime import datetime, timedelta

# Import shared utilities — bundled in the same src/ directory
from transcript_utils import (
    normalize_transcript, format_turns_for_prompt, get_time_bounds,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3_client = boto3.client('s3')

# Configuration
S3_BUCKET = os.environ.get('S3_BUCKET', '')
REPORT_PREFIX = os.environ.get('REPORT_PREFIX', 'reports/')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
HAIKU_MODEL = os.environ.get('HAIKU_MODEL', 'claude-haiku-4-5-20251001')

# Limits
MAX_TRANSCRIPT_CHARS = 80000   # ~20K tokens for Haiku context
MAX_REPORT_CHARS = 20000       # Report JSON summary
MAX_ANSWER_TOKENS = 2048


# ============================================================
# S3 Helpers
# ============================================================

def download_json_from_s3(bucket, key):
    """Download and parse JSON file from S3."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        return json.loads(content)
    except Exception as e:
        logger.warning(f"Failed to load {key}: {e}")
        return None


def list_s3_objects(bucket, prefix):
    """List all objects under a prefix."""
    objects = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            objects.append({'key': obj['Key'], 'size': obj['Size']})
    return objects


# ============================================================
# User Mapping
# ============================================================

_user_mapping_cache = None

def load_user_mapping(bucket):
    """Load user mapping from S3 config/user_mapping.json."""
    global _user_mapping_cache
    if _user_mapping_cache is not None:
        return _user_mapping_cache
    try:
        data = download_json_from_s3(bucket, 'config/user_mapping.json')
        if data:
            raw = data.get('mapping', {})
            normalized = {}
            for device, value in raw.items():
                if isinstance(value, str):
                    normalized[device] = value
                elif isinstance(value, dict):
                    normalized[device] = value.get('name', device)
                else:
                    normalized[device] = str(value)
            _user_mapping_cache = normalized
            return normalized
    except Exception as e:
        logger.warning(f"User mapping load failed: {e}")
    _user_mapping_cache = {}
    return {}


# ============================================================
# Load Report
# ============================================================

def load_report(bucket, date, user):
    """
    Load daily report JSON. Tries per-user report first, then summary.
    Returns (report_dict, report_type) or (None, None).
    """
    user_folder = user.replace(' ', '_')

    # Try per-user daily report
    for name_variant in [user_folder, user]:
        key = f"{REPORT_PREFIX}{date}/{name_variant}/daily_report.json"
        data = download_json_from_s3(bucket, key)
        if data:
            return data, 'daily'

    # Try meeting minutes
    for name_variant in [user_folder, user]:
        key = f"{REPORT_PREFIX}{date}/{name_variant}/meeting_minutes.json"
        data = download_json_from_s3(bucket, key)
        if data:
            return data, 'meeting'

    # Try combined summary
    key = f"{REPORT_PREFIX}{date}/summary_report.json"
    data = download_json_from_s3(bucket, key)
    if data:
        return data, 'summary'

    return None, None


# ============================================================
# Load Transcripts
# ============================================================

def load_transcripts(bucket, date, user, topic_time_range=None):
    """
    Load and normalize all transcripts for a user on a date.
    Optionally filter to a specific topic time range (HH:MM – HH:MM).

    Returns list of normalized transcript dicts.
    """
    user_folder = user.replace(' ', '_')
    user_mapping = load_user_mapping(bucket)

    # Find transcript files
    transcript_files = []
    for name_variant in [user_folder, user]:
        prefix = f"transcripts/{name_variant}/{date}/"
        objects = list_s3_objects(bucket, prefix)
        json_files = [o for o in objects if o['key'].endswith('.json')]
        if json_files:
            transcript_files = json_files
            break

    if not transcript_files:
        return []

    # Parse time range filter if provided (e.g. "09:15 – 09:45")
    filter_start_sec = None
    filter_end_sec = None
    if topic_time_range:
        parts = re.split(r'\s*[–-]\s*', topic_time_range)
        if len(parts) == 2:
            filter_start_sec = _time_str_to_seconds(parts[0].strip())
            filter_end_sec = _time_str_to_seconds(parts[1].strip())

    # Load and normalize each transcript
    normalized_list = []
    for obj in transcript_files:
        data = download_json_from_s3(bucket, obj['key'])
        if not data:
            continue

        filename = os.path.basename(obj['key'])
        norm = normalize_transcript(data, filename, user_mapping=user_mapping)
        if not norm or not norm.get('full_text'):
            continue

        # Time range filter: skip segments outside topic window
        if filter_start_sec is not None and filter_end_sec is not None:
            seg_base = norm.get('segment_base_time')
            seg_end = norm.get('segment_end_time')
            if seg_base and seg_end:
                seg_start_sec = seg_base.hour * 3600 + seg_base.minute * 60 + seg_base.second
                seg_end_sec = seg_end.hour * 3600 + seg_end.minute * 60 + seg_end.second
                # Skip if completely outside the topic window (with 60s buffer)
                if seg_end_sec < (filter_start_sec - 60) or seg_start_sec > (filter_end_sec + 60):
                    continue

        normalized_list.append(norm)

    # Sort by segment start time
    normalized_list.sort(
        key=lambda n: n.get('segment_base_time') or datetime.min
    )

    return normalized_list


def _time_str_to_seconds(time_str):
    """Convert HH:MM or HH:MM:SS to seconds from midnight."""
    parts = time_str.split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return 0


# ============================================================
# Format Report for Prompt
# ============================================================

def format_report_for_prompt(report, report_type):
    """
    Convert report JSON into a concise text block for the prompt.
    Keeps structured data readable without dumping raw JSON.
    """
    lines = []

    # Executive summary
    exec_sum = report.get('executive_summary', '')
    if isinstance(exec_sum, list):
        lines.append("## Executive Summary")
        for bullet in exec_sum:
            lines.append(f"• {bullet}")
    elif exec_sum:
        lines.append(f"## Executive Summary\n{exec_sum}")

    # Recording session info
    session = report.get('recording_session', {})
    if session:
        lines.append(f"\nDate: {session.get('date', '?')} | "
                      f"Site: {session.get('site', '?')} | "
                      f"Worker: {session.get('worker', session.get('workers', '?'))}")

    # Safety observations
    safety = report.get('safety_observations', [])
    if safety:
        lines.append("\n## Safety Observations")
        for obs in safety:
            risk = obs.get('risk_level', '?').upper()
            lines.append(f"[{risk}] {obs.get('observation', '')} — "
                         f"{obs.get('location', '')} (raised by {obs.get('who_raised', '?')})")

    # Critical dates
    dates = report.get('critical_dates_and_deadlines', [])
    if dates:
        lines.append("\n## Critical Dates & Deadlines")
        for d in dates:
            lines.append(f"[{d.get('urgency', '?').upper()}] "
                         f"{d.get('date_mentioned', '?')} — {d.get('context', '')} "
                         f"({d.get('type', '')})")

    # Topics
    topics = report.get('topics', [])
    if topics:
        lines.append("\n## Topics")
        for t in topics:
            tid = t.get('topic_id', '?')
            time_range = t.get('time_range', '')
            title = t.get('topic_title', '')
            cat = t.get('category', '')
            lines.append(f"\n### Topic {tid}: {title} [{cat}] ({time_range})")
            lines.append(f"Participants: {', '.join(t.get('participants', []))}")
            lines.append(f"Summary: {t.get('summary', '')}")

            for d in t.get('key_decisions', []):
                if isinstance(d, dict):
                    lines.append(f"  Decision: {d.get('decision', d)}")
                else:
                    lines.append(f"  Decision: {d}")

            for ai in t.get('action_items', []):
                owner = ai.get('responsible', ai.get('owner', '?'))
                lines.append(f"  Action: {ai.get('action', '')} → {owner} "
                             f"by {ai.get('deadline', '?')} [{ai.get('priority', '?')}]")

            for sf in t.get('safety_flags', []):
                lines.append(f"  Safety: [{sf.get('risk_level', '?').upper()}] "
                             f"{sf.get('observation', '')}")

    # Meeting-specific fields
    follow_ups = report.get('follow_ups', [])
    if follow_ups:
        lines.append("\n## Follow-ups")
        for fu in follow_ups:
            lines.append(f"• {fu.get('item', '')} → {fu.get('owner', '?')} "
                         f"by {fu.get('deadline', '?')}")

    next_steps = report.get('next_steps', [])
    if next_steps:
        lines.append("\n## Next Steps")
        for ns in next_steps:
            lines.append(f"• {ns}")

    # Quality
    quality = report.get('quality_and_compliance', [])
    if quality:
        lines.append("\n## Quality & Compliance")
        for q in quality:
            lines.append(f"[{q.get('status', '?').upper()}] {q.get('item', '')} — "
                         f"{q.get('details', '')}")

    result = '\n'.join(lines)
    return result[:MAX_REPORT_CHARS]


# ============================================================
# Format Transcripts for Prompt
# ============================================================

def format_transcripts_for_prompt(normalized_list):
    """
    Format normalized transcripts into prompt-ready text with speaker turns.
    """
    all_lines = []
    for norm in normalized_list:
        lines = format_turns_for_prompt(norm, use_absolute_time=True)
        all_lines.extend(lines)

    result = '\n'.join(all_lines)
    return result[:MAX_TRANSCRIPT_CHARS]


# ============================================================
# Build Prompt
# ============================================================

SYSTEM_CONTEXT = """You are an AI assistant for FieldSight, a construction site monitoring platform used in New Zealand.
You answer questions about daily site reports and meeting minutes, grounded strictly in the provided report and transcript data.

Rules:
- Answer ONLY based on the report and transcript data provided below. Do NOT hallucinate or invent information.
- If the answer is not in the data, say so clearly.
- Use specific names, times, and details from the data when answering.
- Keep answers concise and direct — 2-5 sentences for simple questions, longer for complex ones.
- When quoting from transcripts, indicate the approximate time.
- For action items and decisions, always mention who is responsible and any deadlines.
- Answer in the same language the user asks in (English or 中文)."""


def build_prompt(question, report_text, transcript_text, scope, metadata):
    """Build the complete prompt for Claude Haiku."""
    parts = [SYSTEM_CONTEXT]

    # Metadata context
    meta_lines = []
    if metadata.get('date'):
        meta_lines.append(f"Date: {metadata['date']}")
    if metadata.get('user'):
        meta_lines.append(f"Worker: {metadata['user']}")
    if metadata.get('site'):
        meta_lines.append(f"Site: {metadata['site']}")
    if metadata.get('report_type'):
        meta_lines.append(f"Report type: {metadata['report_type']}")
    if meta_lines:
        parts.append("## Context\n" + '\n'.join(meta_lines))

    # Include report data
    if scope in ('report', 'both') and report_text:
        parts.append(f"## Structured Report\n{report_text}")

    # Include transcript data
    if scope in ('transcript', 'both') and transcript_text:
        parts.append(f"## Raw Transcript (speaker-separated, chronological)\n{transcript_text}")

    # The question
    parts.append(f"## User Question\n{question}")

    return '\n\n'.join(parts)


# ============================================================
# Claude API
# ============================================================

def call_claude(prompt, max_tokens=MAX_ANSWER_TOKENS):
    """Call Claude Haiku API and return (answer_text, error)."""
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY not configured"

    http = urllib3.PoolManager()
    body = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    })

    try:
        resp = http.request(
            'POST', 'https://api.anthropic.com/v1/messages',
            body=body,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            timeout=60.0,
        )
        data = json.loads(resp.data.decode('utf-8'))

        if resp.status == 200:
            text_blocks = [
                b['text'] for b in data.get('content', [])
                if b.get('type') == 'text'
            ]
            answer = '\n'.join(text_blocks)
            usage = data.get('usage', {})
            logger.info(f"  Haiku usage: input={usage.get('input_tokens', '?')}, "
                        f"output={usage.get('output_tokens', '?')}")
            return answer, None
        else:
            err = data.get('error', {}).get('message', f'HTTP {resp.status}')
            logger.error(f"Claude API error: {err}")
            return None, err

    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return None, str(e)


# ============================================================
# Response Helper
# ============================================================

def ok(body, status=200):
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type,Authorization',
            'Access-Control-Allow-Methods': 'POST,OPTIONS',
        },
        'body': json.dumps(body, default=str),
    }


def error(message, status=400):
    return ok({'error': message}, status)


# ============================================================
# MAIN HANDLER
# ============================================================

def lambda_handler(event, context):
    """
    POST /api/ask
    Body: {
        "date": "2026-03-20",
        "user": "Jarley_Trainor",
        "question": "What safety issues were raised today?",
        "scope": "both",           // optional: "report" | "transcript" | "both"
        "topic_id": 2              // optional: narrow to specific topic
    }
    """
    logger.info("=" * 50)
    logger.info("Ask Agent v1.0 - Starting")

    # Parse request
    if event.get('httpMethod') == 'OPTIONS':
        return ok({'message': 'CORS OK'})

    body = {}
    if event.get('body'):
        try:
            body = json.loads(event['body'])
        except Exception:
            return error('Invalid JSON body')

    # Also support direct Lambda invocation (event IS the body)
    if not body and 'question' in event:
        body = event

    date = body.get('date', '')
    user = body.get('user', '')
    question = body.get('question', '').strip()
    scope = body.get('scope', 'both')  # report | transcript | both
    topic_id = body.get('topic_id', None)

    if not question:
        return error('Missing question')
    if not date:
        return error('Missing date')
    if not user:
        return error('Missing user')
    if scope not in ('report', 'transcript', 'both'):
        scope = 'both'

    logger.info(f"  Date: {date}, User: {user}, Scope: {scope}")
    logger.info(f"  Question: {question[:200]}")

    # Reset mapping cache
    global _user_mapping_cache
    _user_mapping_cache = None

    # --- Load report ---
    report_text = ''
    report_type = None
    site_name = ''
    topic_time_range = None

    if scope in ('report', 'both'):
        report_data, report_type = load_report(S3_BUCKET, date, user)
        if report_data:
            report_text = format_report_for_prompt(report_data, report_type)
            site_name = (report_data.get('site', '') or
                         report_data.get('recording_session', {}).get('site', ''))

            # If topic_id specified, extract time range for transcript filtering
            if topic_id is not None:
                for t in report_data.get('topics', []):
                    if t.get('topic_id') == topic_id:
                        topic_time_range = t.get('time_range', '')
                        logger.info(f"  Narrowing to topic {topic_id}: {topic_time_range}")
                        break

            logger.info(f"  Report loaded: {report_type}, "
                        f"{len(report_text)} chars")
        else:
            logger.warning(f"  No report found for {user} on {date}")

    # --- Load transcripts ---
    transcript_text = ''
    if scope in ('transcript', 'both'):
        normalized_list = load_transcripts(
            S3_BUCKET, date, user,
            topic_time_range=topic_time_range
        )
        if normalized_list:
            transcript_text = format_transcripts_for_prompt(normalized_list)

            earliest, latest, duration_min = get_time_bounds(normalized_list)
            total_words = sum(n.get('word_count', 0) for n in normalized_list)
            logger.info(f"  Transcripts loaded: {len(normalized_list)} files, "
                        f"{total_words} words, {duration_min}min span, "
                        f"{len(transcript_text)} chars")
        else:
            logger.warning(f"  No transcripts found for {user} on {date}")

    # --- Check we have something to answer from ---
    if not report_text and not transcript_text:
        return ok({
            'answer': f"No report or transcript data found for {user} on {date}. "
                      f"The report may not have been generated yet, or there were "
                      f"no recordings on this date.",
            'grounded': False,
            'date': date,
            'user': user,
        })

    # --- Build prompt ---
    metadata = {
        'date': date,
        'user': user.replace('_', ' '),
        'site': site_name,
        'report_type': report_type,
    }
    prompt = build_prompt(question, report_text, transcript_text, scope, metadata)
    logger.info(f"  Prompt length: {len(prompt)} chars (~{len(prompt)//4} tokens)")

    # --- Call Claude Haiku ---
    answer, err = call_claude(prompt)

    if err:
        logger.error(f"  Claude error: {err}")
        return error(f"AI service error: {err}", 502)

    logger.info(f"  Answer length: {len(answer)} chars")
    logger.info("Ask Agent v1.0 - Complete")
    logger.info("=" * 50)

    return ok({
        'answer': answer,
        'grounded': True,
        'date': date,
        'user': user,
        'scope': scope,
        'topic_id': topic_id,
        'model': HAIKU_MODEL,
        'data_sources': {
            'report': bool(report_text),
            'report_type': report_type,
            'transcript_files': len(normalized_list) if scope in ('transcript', 'both') else 0,
        },
    })
