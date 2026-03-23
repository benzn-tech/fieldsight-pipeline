"""
Lambda 3: Transcribe Trigger v1.3 - Start speech-to-text when audio uploads to S3

Changes from v1.2:
- ADD: DynamoDB ledger write (fieldsight-transcripts) on job start — enables
       callback Lambda to track status: transcribing → pending → reported
- ADD: TRANSCRIPT_TABLE env var (default: fieldsight-transcripts)
- ADD: write_ledger_record() creates initial record with status 'transcribing'

Changes from v1.1:
- CHANGE: Output path now includes date subfolder: transcripts/{user}/{date}/{file}.json
- ADD: extract_date_from_key() extracts date from path or filename

Changes from v1.0:
- CHANGE: S3 trigger prefix from users/ to audio_segments/ (VAD output)
- CHANGE: extract_user_from_key() supports both users/ and audio_segments/ paths
- CHANGE: Default language list to NZ/AU/GB/IE/US English only (removed zh-CN)

This Lambda function:
1. Triggered by S3 ObjectCreated event on audio files
2. Starts AWS Transcribe job for the audio file
3. Automatically detects language from configured candidate list
4. Enables speaker diarization for speaker identification
5. Outputs transcript to user-specific folder in transcripts/

Trigger: S3 Event (ObjectCreated on audio_segments/*.wav)
         When VAD is enabled, VAD Lambda writes segments to audio_segments/
         and this Lambda is triggered by those new .wav files.
         
         Legacy: S3 Event (ObjectCreated on users/*/audio/*)
         When VAD is disabled, triggered directly by downloader output.

Environment Variables:
    LANGUAGE_OPTIONS    - Comma-separated language codes for auto-detection
                          (default: en-NZ,en-AU,en-GB,en-IE,en-US)
    OUTPUT_PREFIX       - Transcript output prefix (default: transcripts/)
    MAX_SPEAKERS        - Max speakers for diarization (default: 5, range 2-10)
    TRANSCRIPT_TABLE    - DynamoDB ledger table (default: fieldsight-transcripts)
                          Set to empty string to disable ledger writes.

Output structure:
    transcripts/{display_name}/{date}/{filename}.json
    
    e.g., transcripts/John_Smith/2024-01-15/Benl1_2024-01-15_10-30-00_off23.5_to67.2_srcmp4.json

Note on LanguageCode vs IdentifyLanguage:
    These two parameters are MUTUALLY EXCLUSIVE in the Transcribe API.
    - LanguageCode: Forces a single language (old approach)
    - IdentifyLanguage: Auto-detects from LanguageOptions (current approach)
    We use IdentifyLanguage for multi-language construction site environments.
"""

import os
import re
import json
import logging
import boto3
from urllib.parse import unquote_plus

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize Transcribe client
transcribe = boto3.client('transcribe')

# DynamoDB ledger (optional — tracks job status for callback Lambda)
TRANSCRIPT_TABLE = os.environ.get('TRANSCRIPT_TABLE', 'fieldsight-transcripts')
_dynamodb = None

def _get_dynamodb_table():
    """Lazy-init DynamoDB table resource."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb').Table(TRANSCRIPT_TABLE)
    return _dynamodb

# Configuration
# Language candidates for automatic detection
# AWS Transcribe will analyze the audio and pick the best match from this list
# Supported codes: https://docs.aws.amazon.com/transcribe/latest/dg/supported-languages.html
#
# Common English variants relevant to NZ construction sites:
#   en-NZ  - New Zealand English
#   en-AU  - Australian English (closest accent to NZ)
#   en-GB  - British English (covers Scottish & general UK accents)
#   en-IE  - Irish English
#   en-US  - American English
#   en-ZA  - South African English
#
# Non-English languages common on NZ construction sites:
#   zh-CN  - Mandarin Chinese
#   hi-IN  - Hindi
#   ko-KR  - Korean
#   tl-PH  - Filipino/Tagalog
#   sm     - Samoan (check Transcribe availability)
#
# Configure via environment variable, comma-separated
DEFAULT_LANGUAGE_OPTIONS = 'en-NZ,en-AU,en-GB,en-IE,en-US'
LANGUAGE_OPTIONS = [
    lang.strip()
    for lang in os.environ.get('LANGUAGE_OPTIONS', DEFAULT_LANGUAGE_OPTIONS).split(',')
    if lang.strip()
]

OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'transcripts/')

# Speaker diarization
MAX_SPEAKERS = int(os.environ.get('MAX_SPEAKERS', '5'))

# Custom Vocabulary (improves recognition of construction/NZ terms)
# Set via env var; leave empty to skip.
# Must be created first via: aws transcribe create-vocabulary ...
VOCABULARY_NAME = os.environ.get('VOCABULARY_NAME', '')

# Supported audio/video formats for Transcribe
SUPPORTED_FORMATS = {
    '.wav': 'wav',
    '.mp3': 'mp3',
    '.mp4': 'mp4',
    '.m4a': 'mp4',
    '.aac': 'mp4',   # AAC from body camera extraction
    '.flac': 'flac',
    '.ogg': 'ogg',
    '.webm': 'webm',
}


def get_media_format(key):
    """Get media format from file extension"""
    ext = os.path.splitext(key)[1].lower()
    return SUPPORTED_FORMATS.get(ext, '')


def sanitize_job_name(name):
    """
    Clean job name to meet Transcribe requirements
    
    Requirements:
    - Only alphanumeric, hyphens, underscores
    - Max 200 characters
    """
    sanitized = re.sub(r'[^a-zA-Z0-9\-_]', '_', name)
    sanitized = re.sub(r'_+', '_', sanitized)
    return sanitized[:200]


def extract_user_from_key(key):
    """
    Extract display name from S3 key.
    
    Handles both paths:
      users/{display_name}/audio/{date}/{filename}          ← original path (VAD off)
      audio_segments/{display_name}/{date}/{filename}       ← VAD output path
    """
    parts = key.split('/')
    if len(parts) >= 2 and parts[0] in ('users', 'audio_segments'):
        return parts[1]
    return 'Unknown'


def extract_date_from_key(key):
    """
    Extract date from S3 key for date-based transcript output folders.
    
    Handles both sources:
      audio_segments/{user}/{date}/{filename}   ← date from path (preferred)
      users/{user}/audio/{date}/{filename}      ← date from path
      .../{device}_{YYYY-MM-DD}_{HH-MM-SS}...  ← date from filename (fallback)
    
    Returns:
        str: Date string 'YYYY-MM-DD', or 'unknown' if not found.
    """
    parts = key.split('/')
    
    # Try path-based date: audio_segments/{user}/{date}/...
    if len(parts) >= 3 and parts[0] == 'audio_segments':
        candidate = parts[2]
        if re.match(r'^\d{4}-\d{2}-\d{2}$', candidate):
            return candidate
    
    # Try path-based date: users/{user}/audio/{date}/...
    if len(parts) >= 4 and parts[0] == 'users':
        candidate = parts[3]
        if re.match(r'^\d{4}-\d{2}-\d{2}$', candidate):
            return candidate
    
    # Fallback: extract from filename (e.g. Benl1_2026-02-09_09-56-40...)
    filename = os.path.basename(key)
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if date_match:
        return date_match.group(1)
    
    return 'unknown'


def build_transcribe_params(job_name, media_uri, media_format, bucket, output_key):
    """
    Build parameters for start_transcription_job API call.
    
    Combines:
    - Automatic language detection (IdentifyLanguage + LanguageOptions)
    - Speaker diarization (ShowSpeakerLabels + MaxSpeakerLabels)
    - Custom Vocabulary (if VOCABULARY_NAME is set)
    
    Note on Custom Vocabulary + IdentifyLanguage:
        When using IdentifyLanguage, VocabularyName cannot go in Settings.
        Instead, use LanguageIdSettings to map vocabulary per language code.
    
    Returns:
        dict: Complete parameter set for start_transcription_job()
    """
    params = {
        'TranscriptionJobName': job_name,
        'Media': {'MediaFileUri': media_uri},
        'MediaFormat': media_format,
        'OutputBucketName': bucket,
        'OutputKey': output_key,
    }
    
    # Speaker diarization
    settings = {
        'ShowSpeakerLabels': True,
        'MaxSpeakerLabels': min(max(MAX_SPEAKERS, 2), 10)
    }
    
    # Automatic language detection
    if len(LANGUAGE_OPTIONS) > 1:
        params['IdentifyLanguage'] = True
        params['LanguageOptions'] = LANGUAGE_OPTIONS
        logger.info(f"  Auto-detect from: {LANGUAGE_OPTIONS}")
        
        # Custom Vocabulary with IdentifyLanguage:
        # Map vocabulary to each English variant via LanguageIdSettings
        if VOCABULARY_NAME:
            lang_id_settings = {}
            for lang in LANGUAGE_OPTIONS:
                if lang.startswith('en-'):
                    lang_id_settings[lang] = {
                        'VocabularyName': VOCABULARY_NAME
                    }
            if lang_id_settings:
                params['LanguageIdSettings'] = lang_id_settings
                logger.info(f"  Custom vocabulary '{VOCABULARY_NAME}' mapped to: "
                            f"{list(lang_id_settings.keys())}")
    else:
        # Single language -- use LanguageCode directly
        params['LanguageCode'] = LANGUAGE_OPTIONS[0]
        logger.info(f"  Fixed language: {LANGUAGE_OPTIONS[0]}")
        
        # Custom Vocabulary with single LanguageCode: set in Settings
        if VOCABULARY_NAME:
            settings['VocabularyName'] = VOCABULARY_NAME
            logger.info(f"  Custom vocabulary: {VOCABULARY_NAME}")
    
    params['Settings'] = settings
    logger.info(f"  Speaker diarization: max {MAX_SPEAKERS} speakers")
    
    return params

def write_ledger_record(display_name, file_date, base_name, job_name,
                        media_uri, output_key):
    """
    Write initial ledger record to DynamoDB when a Transcribe job starts.
    
    The callback Lambda (Lambda 3b) will later update this record when
    the job completes or fails.
    
    PK: DATE#{YYYY-MM-DD}
    SK: TRANSCRIPT#{user}#{basename}
    
    Silently skips if TRANSCRIPT_TABLE is empty or write fails
    (ledger is optional — S3 scan fallback always works).
    """
    if not TRANSCRIPT_TABLE:
        return
    
    try:
        from datetime import datetime
        table = _get_dynamodb_table()
        now = datetime.utcnow().isoformat() + 'Z'
        
        table.put_item(Item={
            'PK': f'DATE#{file_date}',
            'SK': f'TRANSCRIPT#{display_name}#{base_name}',
            'status': 'transcribing',
            'user': display_name,
            'date': file_date,
            'job_name': job_name,
            'media_uri': media_uri,
            'output_key': output_key,
            'started_at': now,
            'attempts': 1,
        })
        logger.info(f"  Ledger: wrote {file_date}/{display_name}/{base_name} → transcribing")
        
    except Exception as e:
        # Non-fatal — S3 scan fallback still works without the ledger
        logger.warning(f"  Ledger write failed (non-fatal): {e}")


def lambda_handler(event, context):
    """Main Lambda handler"""
    logger.info(f"Received event: {json.dumps(event)}")
    logger.info(f"Language options: {LANGUAGE_OPTIONS}")
    logger.info(f"Max speakers: {MAX_SPEAKERS}")
    
    results = []
    
    for record in event.get('Records', []):
        try:
            # Get bucket and key from S3 event
            bucket = record['s3']['bucket']['name']
            key = unquote_plus(record['s3']['object']['key'])
            
            logger.info(f"Processing: s3://{bucket}/{key}")
            
            # Check if supported audio format
            media_format = get_media_format(key)
            if not media_format:
                logger.info(f"Skipping unsupported format: {key}")
                results.append({
                    'key': key,
                    'status': 'skipped',
                    'reason': 'unsupported format'
                })
                continue
            
            # Skip if already a transcript file
            if key.startswith(OUTPUT_PREFIX):
                logger.info(f"Skipping transcript file: {key}")
                continue
            
            # Extract user name and date from path
            display_name = extract_user_from_key(key)
            file_date = extract_date_from_key(key)
            
            # Generate job name from filename
            base_name = os.path.splitext(os.path.basename(key))[0]
            job_name = sanitize_job_name(f"fieldsight_{display_name}_{base_name}")
            
            # Check if job already exists
            try:
                existing = transcribe.get_transcription_job(
                    TranscriptionJobName=job_name
                )
                status = existing['TranscriptionJob']['TranscriptionJobStatus']
                logger.info(f"Job {job_name} already exists, status: {status}")
                results.append({
                    'key': key,
                    'status': 'exists',
                    'job_name': job_name,
                    'job_status': status
                })
                continue
            except transcribe.exceptions.BadRequestException:
                # Job doesn't exist, proceed to create
                pass
            
            # Build S3 URIs
            media_uri = f"s3://{bucket}/{key}"
            output_key = f"{OUTPUT_PREFIX}{display_name}/{file_date}/{base_name}.json"
            
            logger.info(f"Starting transcription job: {job_name}")
            logger.info(f"  Input: {media_uri}")
            logger.info(f"  Output: s3://{bucket}/{output_key}")
            
            # Build and execute transcription request
            params = build_transcribe_params(
                job_name, media_uri, media_format, bucket, output_key
            )
            response = transcribe.start_transcription_job(**params)
            
            job_status = response['TranscriptionJob']['TranscriptionJobStatus']
            logger.info(f"Job started: {job_name} ({job_status})")
            
            # Write initial ledger record for callback Lambda
            write_ledger_record(
                display_name, file_date, base_name, job_name,
                media_uri, output_key
            )
            
            results.append({
                'key': key,
                'status': 'started',
                'job_name': job_name,
                'output_key': output_key,
                'user': display_name,
                'language_detection': 'auto' if len(LANGUAGE_OPTIONS) > 1 else LANGUAGE_OPTIONS[0]
            })
            
        except Exception as e:
            logger.error(f"Error processing {key}: {str(e)}")
            results.append({
                'key': key,
                'status': 'error',
                'error': str(e)
            })
    
    # Summary statistics
    summary = {
        'total': len(results),
        'started': sum(1 for r in results if r.get('status') == 'started'),
        'skipped': sum(1 for r in results if r.get('status') == 'skipped'),
        'exists': sum(1 for r in results if r.get('status') == 'exists'),
        'errors': sum(1 for r in results if r.get('status') == 'error'),
    }
    
    logger.info(f"Processing complete: {json.dumps(summary)}")
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'summary': summary,
            'results': results
        })
    }