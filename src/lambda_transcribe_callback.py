"""
Lambda 3b: Transcribe Callback — Handle Transcribe job completion/failure

Triggered by EventBridge rule on AWS Transcribe state changes:
  - COMPLETED → update ledger status to 'pending' (ready for report)
  - FAILED    → update ledger, schedule retry if attempts < MAX_ATTEMPTS

EventBridge event pattern:
{
    "source": ["aws.transcribe"],
    "detail-type": ["Transcribe Job State Change"],
    "detail": {
        "TranscriptionJobStatus": ["COMPLETED", "FAILED"]
    }
}

Environment Variables:
    TRANSCRIPT_TABLE    - DynamoDB table name (default: fieldsight-transcripts)
    MAX_ATTEMPTS        - Max retry attempts (default: 3)
    S3_BUCKET           - S3 bucket name (for retry job submissions)

DynamoDB ledger status flow:
    transcribing → pending (success) → reported (after report generation)
    transcribing → retry_scheduled (failure, attempts < max)
    retry_scheduled → transcribing (on retry)
    transcribing → abandoned (failure, attempts >= max)
"""

import os
import json
import logging
import boto3
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
transcribe_client = boto3.client('transcribe')

TRANSCRIPT_TABLE = os.environ.get('TRANSCRIPT_TABLE', 'fieldsight-transcripts')
MAX_ATTEMPTS = int(os.environ.get('MAX_ATTEMPTS', '3'))
S3_BUCKET = os.environ.get('S3_BUCKET', '')


def find_ledger_record(job_name):
    """
    Find the DynamoDB ledger record matching a Transcribe job name.

    Job name format: fieldsight_{user}_{basename}[_retryN]
    Ledger SK format: TRANSCRIPT#{user}#{basename}

    Returns (PK, SK, item) or (None, None, None) if not found.
    """
    table = dynamodb.Table(TRANSCRIPT_TABLE)

    # Try to get job details from Transcribe to find output_key
    try:
        job = transcribe_client.get_transcription_job(
            TranscriptionJobName=job_name
        )['TranscriptionJob']

        # Extract date and user from the output key
        # Output key format: transcripts/{user}/{date}/{basename}.json
        # or legacy: transcripts/{user}/{basename}.json
        output_uri = job.get('Transcript', {}).get('TranscriptFileUri', '')
        media_uri = job.get('Media', {}).get('MediaFileUri', '')

        # Extract date from media URI (more reliable)
        import re
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', media_uri)
        if not date_match:
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', output_uri)

        if not date_match:
            logger.warning(f"Cannot extract date from job {job_name}")
            return None, None, None

        date_str = date_match.group(1)
        pk = f'DATE#{date_str}'

        # Query all records for this date and find matching job_name
        response = table.query(
            KeyConditionExpression='PK = :pk',
            ExpressionAttributeValues={':pk': pk}
        )

        for item in response.get('Items', []):
            if item.get('job_name') == job_name:
                return item['PK'], item['SK'], item

        # Fallback: match by base job name (strip _retryN suffix)
        base_job = re.sub(r'_retry\d+$', '', job_name)
        for item in response.get('Items', []):
            stored_job = item.get('job_name', '')
            stored_base = re.sub(r'_retry\d+$', '', stored_job)
            if stored_base == base_job:
                return item['PK'], item['SK'], item

        logger.warning(f"No ledger record found for job {job_name} on {date_str}")
        return None, None, None

    except Exception as e:
        logger.error(f"Error finding ledger record for {job_name}: {e}")
        return None, None, None


def handle_completed(job_name, job_detail):
    """Handle successful Transcribe job completion."""
    pk, sk, item = find_ledger_record(job_name)
    if not pk:
        logger.warning(f"COMPLETED but no ledger record for {job_name} — transcript "
                       "will still be picked up by S3 scan fallback")
        return

    table = dynamodb.Table(TRANSCRIPT_TABLE)
    now = datetime.utcnow().isoformat() + 'Z'

    # Extract detected language if available
    language = job_detail.get('LanguageCode', '')

    table.update_item(
        Key={'PK': pk, 'SK': sk},
        UpdateExpression=(
            'SET #s = :status, completed_at = :now, '
            'detected_language = :lang, job_name = :jn'
        ),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':status': 'pending',
            ':now': now,
            ':lang': language,
            ':jn': job_name,
        }
    )

    user = item.get('user', '?')
    date = item.get('date', '?')
    attempts = item.get('attempts', 1)
    logger.info(f"✓ Transcription COMPLETED: {user} on {date} "
                f"(attempt {attempts}) → status: pending")


def handle_failed(job_name, job_detail):
    """
    Handle failed Transcribe job.

    - If attempts < MAX_ATTEMPTS → status: retry_scheduled
    - If attempts >= MAX_ATTEMPTS → status: abandoned
    """
    pk, sk, item = find_ledger_record(job_name)
    if not pk:
        logger.warning(f"FAILED but no ledger record for {job_name}")
        return

    table = dynamodb.Table(TRANSCRIPT_TABLE)
    now = datetime.utcnow().isoformat() + 'Z'
    attempts = item.get('attempts', 1)
    failure_reason = job_detail.get('FailureReason', 'Unknown')

    if attempts >= MAX_ATTEMPTS:
        # Give up — mark abandoned
        new_status = 'abandoned'
        logger.warning(f"✗ Transcription ABANDONED after {attempts} attempts: "
                       f"{item.get('user', '?')} on {item.get('date', '?')} "
                       f"— reason: {failure_reason}")
    else:
        # Schedule retry
        new_status = 'retry_scheduled'
        logger.info(f"↻ Transcription FAILED (attempt {attempts}/{MAX_ATTEMPTS}): "
                    f"{item.get('user', '?')} on {item.get('date', '?')} "
                    f"— reason: {failure_reason} — will retry")

    # Build failure history
    failure_log = item.get('failure_log', [])
    failure_log.append({
        'attempt': attempts,
        'failed_at': now,
        'reason': failure_reason,
        'job_name': job_name,
    })

    table.update_item(
        Key={'PK': pk, 'SK': sk},
        UpdateExpression=(
            'SET #s = :status, last_failed_at = :now, '
            'failure_reason = :reason, failure_log = :flog, '
            'job_name = :jn'
        ),
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={
            ':status': new_status,
            ':now': now,
            ':reason': failure_reason,
            ':flog': failure_log,
            ':jn': job_name,
        }
    )


def lambda_handler(event, context):
    """
    EventBridge handler for Transcribe job state changes.

    Event structure:
    {
        "source": "aws.transcribe",
        "detail-type": "Transcribe Job State Change",
        "detail": {
            "TranscriptionJobName": "fieldsight_MPI3_Benl5_2026-02-20_14-30-00",
            "TranscriptionJobStatus": "COMPLETED" | "FAILED",
            "FailureReason": "...",  (only on FAILED)
            "LanguageCode": "en-NZ"  (only on COMPLETED)
        }
    }
    """
    logger.info(f"Event: {json.dumps(event)}")

    detail = event.get('detail', {})
    job_name = detail.get('TranscriptionJobName', '')
    job_status = detail.get('TranscriptionJobStatus', '')

    if not job_name:
        logger.warning("No TranscriptionJobName in event")
        return {'statusCode': 400}

    # Only process our jobs (realptt_ prefix)
    if not job_name.startswith(('realptt_', 'fieldsight_')):
        logger.info(f"Ignoring non-FieldSight job: {job_name}")
        return {'statusCode': 200, 'body': 'ignored'}

    if job_status == 'COMPLETED':
        handle_completed(job_name, detail)
    elif job_status == 'FAILED':
        handle_failed(job_name, detail)
    else:
        logger.info(f"Ignoring status {job_status} for {job_name}")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'job_name': job_name,
            'status': job_status,
            'action': 'processed'
        })
    }
