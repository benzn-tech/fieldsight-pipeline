"""
transcript_utils.py — Shared transcript normalization

Unified time extraction logic for all report generators.
Both lambda_report_generator and lambda_meeting_minutes import this module.

Input:  Raw AWS Transcribe JSON + filename
Output: Normalized transcript dict with absolute timestamps per speaker turn

Time resolution logic:
  1. Extract base_time from filename (recording session start)
  2. If filename has VAD offset (_off{X}_to{Y}_), add offset to base_time
     → This gives the absolute start time of this audio segment
  3. Read per-word timestamps from Transcribe JSON (relative to segment start)
  4. Absolute time per word = base_time (with offset) + word.start_time

  Example (VAD segment):
    filename: Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json
    base_time = 12:18:34
    vad_offset = 1465.8s → segment_base = 12:18:34 + 1465.8s = 12:42:59
    word[0].start_time = 0.079s → absolute = 12:42:59 + 0.079s = 12:43:00
    word[last].end_time = 263.9s → absolute = 12:42:59 + 263.9s = 12:47:23

  Example (full audio, no offset):
    filename: Benl1_2026-03-20_12-18-34.json
    base_time = 12:18:34, no offset → segment_base = 12:18:34
    word[0].start_time = 0.079s → absolute = 12:18:34
    word[last].end_time = 7371.4s → absolute = 14:21:25
"""

import os
import re
from datetime import datetime, timedelta


# ============================================================
# Filename Parsing
# ============================================================

def extract_base_time_from_filename(filename):
    """
    Extract the recording session start time from filename.

    Supports:
      Benl1_2026-03-20_12-18-34.json           → 2026-03-20 12:18:34
      Benl1_2026-03-20_12-18-34_off1465.8_...   → 2026-03-20 12:18:34  (offset handled separately)
      Benl1_2025-12-17-20-30-46.json            → 2025-12-17 20:30:46

    Returns: datetime or None
    """
    # Standard: YYYY-MM-DD_HH-MM-SS
    match = re.search(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', filename)
    if match:
        try:
            return datetime.strptime(match.group(1), '%Y-%m-%d_%H-%M-%S')
        except ValueError:
            pass

    # Hyphens-only: YYYY-MM-DD-HH-MM-SS
    match = re.search(r'(\d{4}-\d{2}-\d{2})-(\d{2}-\d{2}-\d{2})', filename)
    if match:
        try:
            return datetime.strptime(
                f"{match.group(1)}_{match.group(2)}", '%Y-%m-%d_%H-%M-%S'
            )
        except ValueError:
            pass

    return None


def extract_vad_offsets_from_filename(filename):
    """
    Extract VAD segment offsets from filename.

    Input:  Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json
    Output: (1465.8, 1729.8)  — (offset_start_sec, offset_end_sec)

    Returns: (float, float) or (0.0, 0.0) if no offset present
    """
    match = re.search(r'_off([\d.]+)_to([\d.]+)', filename)
    if match:
        try:
            return float(match.group(1)), float(match.group(2))
        except (ValueError, OverflowError):
            pass
    return 0.0, 0.0


def extract_vad_metadata_from_filename(filename):
    """
    Extract full VAD metadata from segment filename.

    Returns: dict with has_vad, offset_start, offset_end, segment_duration,
             source_format, has_video
    """
    off_start, off_end = extract_vad_offsets_from_filename(filename)

    info = {
        'has_vad': off_start > 0 or off_end > 0,
        'offset_start': off_start,
        'offset_end': off_end,
        'segment_duration': off_end - off_start if off_end > off_start else 0.0,
        'source_format': 'unknown',
        'has_video': False,
    }

    src_match = re.search(r'_src(\w+)\.', filename)
    if src_match:
        fmt = src_match.group(1).lower()
        info['source_format'] = fmt
        info['has_video'] = fmt in ('mp4', 'webm', 'mov', 'avi', 'mkv')

    return info


def extract_device_from_filename(filename):
    """Extract device account from filename (first segment before _)"""
    parts = filename.split('_')
    return parts[0] if len(parts) >= 2 else 'Unknown'


def compute_segment_base_time(filename):
    """
    Compute the absolute start time of this audio segment.

    = base_time (from filename) + VAD offset (if present)

    This is the "zero point" for all word timestamps inside the Transcribe JSON.
    word absolute time = segment_base_time + word.start_time

    Returns: datetime or None
    """
    base_time = extract_base_time_from_filename(filename)
    if not base_time:
        return None

    off_start, _ = extract_vad_offsets_from_filename(filename)
    if off_start > 0:
        base_time += timedelta(seconds=off_start)

    return base_time


# ============================================================
# Transcribe JSON Parsing
# ============================================================

def parse_transcribe_json(transcript_data):
    """
    Parse raw AWS Transcribe JSON output.

    Returns: dict with
        full_text:        str — concatenated transcript
        words:            list — per-word timing [{word, start_time, end_time, confidence, speaker}]
        word_count:       int
        duration_seconds: float — audio length from last word end_time
        speakers:         set — unique speaker labels found
    """
    if not transcript_data:
        return None

    results = transcript_data.get('results', {})
    transcripts = results.get('transcripts', [])
    full_text = transcripts[0].get('transcript', '') if transcripts else ''

    items = results.get('items', [])
    words = []
    max_end_time = 0.0
    speakers = set()

    for item in items:
        if item.get('type') == 'pronunciation':
            alts = item.get('alternatives', [])
            if alts:
                end_t = float(item.get('end_time', 0))
                max_end_time = max(max_end_time, end_t)
                spk = item.get('speaker_label', None)
                if spk:
                    speakers.add(spk)
                words.append({
                    'word': alts[0].get('content', ''),
                    'start_time': float(item.get('start_time', 0)),
                    'end_time': end_t,
                    'confidence': float(alts[0].get('confidence', 0)),
                    'speaker': spk,
                })

    return {
        'full_text': full_text,
        'words': words,
        'word_count': len(words),
        'duration_seconds': max_end_time,
        'speakers': speakers,
    }


# ============================================================
# Core: Normalize Transcript
# ============================================================

def normalize_transcript(transcript_data, filename, user_mapping=None):
    """
    THE unified normalization function.

    Takes raw Transcribe JSON + filename → produces a fully resolved transcript
    with absolute timestamps on every speaker turn.

    Args:
        transcript_data: dict — raw AWS Transcribe JSON
        filename: str — S3 object basename (used for time extraction)
        user_mapping: dict — optional {device_id: display_name}

    Returns: dict with
        filename:          str
        device:            str — device account from filename
        speaker_name:      str — display name from user_mapping (or device)
        full_text:         str
        word_count:        int
        duration_seconds:  float
        segment_base_time: datetime — absolute zero-point for this segment
        segment_end_time:  datetime — segment_base_time + last word end
        vad:               dict — VAD metadata

        speaker_turns: list of dicts, each with:
            speaker:    str — spk_0, spk_1, etc.
            text:       str — concatenated words for this turn
            start_sec:  float — offset from segment start (relative)
            end_sec:    float — offset from segment start (relative)
            abs_start:  datetime — absolute start time
            abs_end:    datetime — absolute end time
            abs_start_str: str — "HH:MM:SS"
            abs_end_str:   str — "HH:MM:SS"

        If no speaker diarization: speaker_turns has one entry with speaker='unknown'
    """
    parsed = parse_transcribe_json(transcript_data)
    if not parsed or not parsed['full_text']:
        return None

    device = extract_device_from_filename(filename)
    speaker_name = device
    if user_mapping:
        speaker_name = user_mapping.get(device, device)

    segment_base = compute_segment_base_time(filename)
    vad_info = extract_vad_metadata_from_filename(filename)

    # Build speaker turns with both relative and absolute times
    speaker_turns = []
    words = parsed['words']
    has_diarization = any(w.get('speaker') for w in words)

    if has_diarization:
        current_speaker = None
        current_words = []
        seg_start_sec = 0.0
        seg_end_sec = 0.0

        for w in words:
            spk = w.get('speaker', current_speaker)
            if spk != current_speaker and current_words:
                turn = _build_turn(
                    current_speaker, current_words,
                    seg_start_sec, seg_end_sec, segment_base
                )
                speaker_turns.append(turn)
                current_words = []
                seg_start_sec = w['start_time']

            if not current_words:
                seg_start_sec = w['start_time']

            current_speaker = spk
            current_words.append(w['word'])
            seg_end_sec = w['end_time']

        if current_words:
            turn = _build_turn(
                current_speaker, current_words,
                seg_start_sec, seg_end_sec, segment_base
            )
            speaker_turns.append(turn)
    else:
        # No diarization — single turn for entire transcript
        if words:
            turn = _build_turn(
                'unknown', [w['word'] for w in words],
                words[0]['start_time'], words[-1]['end_time'], segment_base
            )
            speaker_turns.append(turn)

    # Compute segment end time
    segment_end = None
    if segment_base and parsed['duration_seconds'] > 0:
        segment_end = segment_base + timedelta(seconds=parsed['duration_seconds'])

    return {
        'filename': filename,
        'device': device,
        'speaker_name': speaker_name,
        'full_text': parsed['full_text'],
        'word_count': parsed['word_count'],
        'duration_seconds': parsed['duration_seconds'],
        'speakers': sorted(parsed['speakers']),
        'segment_base_time': segment_base,
        'segment_end_time': segment_end,
        'base_time_str': segment_base.strftime('%H:%M:%S') if segment_base else '',
        'end_time_str': segment_end.strftime('%H:%M:%S') if segment_end else '',
        'vad': vad_info,
        'speaker_turns': speaker_turns,
    }


def _build_turn(speaker, word_list, start_sec, end_sec, segment_base):
    """Build a single speaker turn dict with absolute timestamps."""
    abs_start = None
    abs_end = None
    abs_start_str = ''
    abs_end_str = ''

    if segment_base:
        abs_start = segment_base + timedelta(seconds=start_sec)
        abs_end = segment_base + timedelta(seconds=end_sec)
        abs_start_str = abs_start.strftime('%H:%M:%S')
        abs_end_str = abs_end.strftime('%H:%M:%S')

    return {
        'speaker': speaker,
        'text': ' '.join(word_list),
        'start_sec': round(start_sec, 2),
        'end_sec': round(end_sec, 2),
        'abs_start': abs_start,
        'abs_end': abs_end,
        'abs_start_str': abs_start_str,
        'abs_end_str': abs_end_str,
    }


# ============================================================
# Prompt Formatting Helpers
# ============================================================

def format_turns_for_prompt(normalized, label_override=None, use_absolute_time=True):
    """
    Format speaker turns into prompt-ready text lines.

    Args:
        normalized: dict from normalize_transcript()
        label_override: str — override speaker_name (e.g. device ID for attendee mapping)
        use_absolute_time: bool — True for meeting minutes (per-turn timestamps),
                           False for site reports (one timestamp per segment)

    Returns: list of formatted strings
    """
    label = label_override or normalized.get('speaker_name', 'Unknown')
    turns = normalized.get('speaker_turns', [])

    if not turns:
        # Fallback: flat text with segment time
        time_str = normalized.get('base_time_str', '??:??')
        return [f"[{time_str}] {label}: {normalized.get('full_text', '')}"]

    lines = []
    if use_absolute_time:
        # Per-turn absolute timestamps (meeting minutes mode)
        for turn in turns:
            t_start = turn.get('abs_start_str', '??:??')
            t_end = turn.get('abs_end_str', '')
            spk = turn.get('speaker', '?')
            text = turn.get('text', '')

            if t_end and t_start != t_end:
                time_label = f"{t_start} – {t_end}"
            else:
                time_label = t_start

            lines.append(f"[{time_label}] {label} ({spk}): {text}")
    else:
        # One timestamp per segment (site report mode)
        # Use segment_base_time as the label
        time_str = normalized.get('base_time_str', '??:??')
        # Still include speaker turns for context, but with segment-level time
        for turn in turns:
            spk = turn.get('speaker', '?')
            text = turn.get('text', '')
            lines.append(f"[{time_str}] {label} ({spk}): {text}")

    return lines


def get_time_bounds(normalized_list):
    """
    Get earliest and latest absolute times across a list of normalized transcripts.

    Returns: (earliest: datetime|None, latest: datetime|None, duration_minutes: int)
    """
    earliest = None
    latest = None

    for n in normalized_list:
        for turn in n.get('speaker_turns', []):
            s = turn.get('abs_start')
            e = turn.get('abs_end')
            if s and (earliest is None or s < earliest):
                earliest = s
            if e and (latest is None or e > latest):
                latest = e

        # Also check segment-level bounds
        sb = n.get('segment_base_time')
        se = n.get('segment_end_time')
        if sb and (earliest is None or sb < earliest):
            earliest = sb
        if se and (latest is None or se > latest):
            latest = se

    duration_min = 0
    if earliest and latest:
        duration_min = int((latest - earliest).total_seconds() / 60)

    return earliest, latest, duration_min


# ============================================================
# Meeting Manifest — mutual exclusion between generators
# ============================================================
# When meeting minutes processes transcripts, it writes a manifest
# listing which S3 keys it consumed. The report generator reads
# this manifest and skips those keys.
#
# Path: reports/{date}/{user}/.meeting_manifest.json
# Content: {"transcript_keys": ["transcripts/.../file1.json", ...],
#           "meeting_title": "...", "generated_at": "..."}
# ============================================================

import json as _json  # local alias to avoid collision with caller's imports


def write_meeting_manifest(s3_client, bucket, report_prefix, target_date,
                           user_path, transcript_keys, meeting_title=''):
    """
    Write manifest marking transcript keys as consumed by meeting minutes.

    Args:
        s3_client: boto3 S3 client
        bucket: S3 bucket name
        report_prefix: e.g. 'reports/'
        target_date: 'YYYY-MM-DD'
        user_path: user folder name (e.g. 'Jarley_Trainor')
        transcript_keys: list of S3 keys that were processed
        meeting_title: optional title for reference
    """
    manifest_key = f"{report_prefix}{target_date}/{user_path}/.meeting_manifest.json"
    manifest = {
        'transcript_keys': transcript_keys,
        'meeting_title': meeting_title,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
    }
    s3_client.put_object(
        Bucket=bucket, Key=manifest_key,
        Body=_json.dumps(manifest, ensure_ascii=False, indent=2),
        ContentType='application/json'
    )
    return manifest_key


def read_meeting_manifest(s3_client, bucket, report_prefix, target_date, user_path):
    """
    Read manifest to get transcript keys already consumed by meeting minutes.

    Returns: set of S3 keys, or empty set if no manifest exists.
    """
    manifest_key = f"{report_prefix}{target_date}/{user_path}/.meeting_manifest.json"
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=manifest_key)
        manifest = _json.loads(resp['Body'].read().decode('utf-8'))
        return set(manifest.get('transcript_keys', []))
    except Exception:
        return set()

