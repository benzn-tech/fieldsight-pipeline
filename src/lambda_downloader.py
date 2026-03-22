"""
Lambda 2: Downloader v2.1 - Download a single file and upload to S3

Changes from v2:
- ADD: Size gate - files > 75 MB skip download entirely, saved to pending_downloads/ for Fargate
- ADD: HEAD request to check Content-Length before downloading
- ADD: save_to_pending() function for Fargate handoff

This Lambda function:
1. Receives file info from Orchestrator (async invocation)
2. Checks file size via HEAD request
3. If > 75 MB: saves to pending_downloads/ for Fargate pickup (returns 202)
4. If <= 75 MB: downloads file and uploads to S3 (returns 200)

Trigger: Async invocation from Lambda 1 (Orchestrator)

Expected event payload:
{
    "file_info": {
        "type": "upload|audio|video",
        "s3_key": "users/John_Smith/audio/device_2024-01-15_10-30-00.wav",
        "display_name": "John Smith",
        "device_account": "Benl1",
        ... (type-specific fields)
    },
    "s3_bucket": "bucket-name"
}
"""

import os
import json
import logging
import boto3
import urllib3
from urllib.parse import urlparse, parse_qs, unquote

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize S3 client
s3_client = boto3.client('s3')

# Download timeout settings (seconds)
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 840  # 14 minutes for large files (Lambda max is 15min)

# Files larger than this skip Lambda download entirely → Fargate
SIZE_THRESHOLD_MB = 75


def create_http_client():
    """Create HTTP client with appropriate timeouts"""
    return urllib3.PoolManager(
        cert_reqs='CERT_NONE',
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)
    )


def extract_file_url(down_path):
    """
    Extract real file URL from REAL PTT download path
    
    The down_path is a redirect URL like:
    http://api.realptt.com/ptt/uploadFile?method=download&FileUrl=https://xxx.mp4&UserId=xxx
    
    We need to extract the FileUrl parameter value
    """
    try:
        parsed = urlparse(down_path)
        params = parse_qs(parsed.query)
        if 'FileUrl' in params:
            return unquote(params['FileUrl'][0])
        return down_path
    except Exception as e:
        logger.warning(f"Failed to parse URL: {e}")
        return down_path


def download_file(http, url, max_retries=3):
    """
    Download file from URL with retry logic
    
    Returns: (success: bool, data: bytes, error: str)
    """
    for attempt in range(max_retries):
        try:
            logger.info(f"Download attempt {attempt + 1}/{max_retries}: {url[:100]}...")
            
            response = http.request(
                'GET', 
                url,
                preload_content=False
            )
            
            # Accept both 200 and 206 (partial content) as success
            if response.status not in (200, 206):
                logger.warning(f"HTTP {response.status} for {url}")
                if attempt < max_retries - 1:
                    continue
                return False, None, f"HTTP {response.status}"
            
            # Read data in chunks
            chunks = []
            for chunk in response.stream(65536):
                chunks.append(chunk)
            
            data = b''.join(chunks)
            response.release_conn()
            
            if len(data) == 0:
                logger.warning("Downloaded file is empty")
                if attempt < max_retries - 1:
                    continue
                return False, None, "Empty file"
            
            logger.info(f"Downloaded {len(data)} bytes")
            return True, data, None
            
        except urllib3.exceptions.TimeoutError:
            logger.warning(f"Timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                continue
            return False, None, "Timeout"
            
        except Exception as e:
            logger.error(f"Download error: {str(e)}")
            if attempt < max_retries - 1:
                continue
            return False, None, str(e)
    
    return False, None, "Max retries exceeded"


def upload_to_s3(bucket, key, data, content_type=None):
    """
    Upload data to S3
    
    Returns: (success: bool, error: str)
    """
    try:
        params = {
            'Bucket': bucket,
            'Key': key,
            'Body': data
        }
        
        if content_type:
            params['ContentType'] = content_type
        
        s3_client.put_object(**params)
        logger.info(f"Uploaded to s3://{bucket}/{key}")
        return True, None
        
    except Exception as e:
        logger.error(f"S3 upload error: {str(e)}")
        return False, str(e)


def get_content_type(key):
    """Determine content type based on file extension"""
    ext = os.path.splitext(key)[1].lower()
    content_types = {
        '.wav': 'audio/wav',
        '.mp3': 'audio/mpeg',
        '.mp4': 'video/mp4',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.pdf': 'application/pdf',
    }
    return content_types.get(ext, 'application/octet-stream')


def save_to_pending(bucket, s3_key, download_url, file_info, reason=""):
    """Save download job to pending_downloads/ for Fargate pickup"""
    pending_key = "pending_downloads/" + s3_key.replace("/", "_") + ".json"
    job = {
        "s3_key": s3_key,
        "download_url": download_url,
        "file_info": file_info,
        "reason": reason,
    }
    s3_client.put_object(
        Bucket=bucket,
        Key=pending_key,
        Body=json.dumps(job, indent=2),
        ContentType='application/json'
    )
    logger.info(f"Saved to pending: {pending_key}")
    return pending_key


def check_file_size(http, url):
    """
    HEAD request to get Content-Length before downloading.
    Returns size in MB, or 0 if unknown.
    """
    try:
        resp = http.request('HEAD', url, preload_content=False)
        content_length = resp.headers.get('Content-Length', '0')
        resp.release_conn()
        size_mb = int(content_length) / (1024 * 1024)
        return size_mb
    except Exception as e:
        logger.warning(f"HEAD request failed: {e}, will attempt download")
        return 0


def lambda_handler(event, context):
    """Main Lambda handler"""
    logger.info("=" * 50)
    logger.info("File Downloader v2.1 - Starting")
    
    # Parse event
    file_info = event.get('file_info', {})
    s3_bucket = event.get('s3_bucket', '')
    
    if not file_info or not s3_bucket:
        logger.error("Missing file_info or s3_bucket")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Missing required parameters'})
        }
    
    # Get file URL based on type
    file_type = file_info.get('type', '')
    s3_key = file_info.get('s3_key', '')
    display_name = file_info.get('display_name', 'Unknown')
    device_account = file_info.get('device_account', 'unknown')
    
    logger.info(f"File type: {file_type}")
    logger.info(f"User: {display_name} (device: {device_account})")
    logger.info(f"S3 key: {s3_key}")
    
    # Determine download URL based on file type
    if file_type == 'upload':
        raw_url = file_info.get('down_path', '')
        download_url = extract_file_url(raw_url)
    elif file_type == 'audio':
        download_url = file_info.get('download_url', '')
    elif file_type == 'video':
        download_url = file_info.get('url', '')
    else:
        logger.error(f"Unknown file type: {file_type}")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': f'Unknown file type: {file_type}'})
        }
    
    if not download_url:
        logger.error("No download URL found")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'No download URL'})
        }
    
    # Create HTTP client
    http = create_http_client()
    
    logger.info(f"Download URL: {download_url[:120]}")
    
    # === SIZE GATE: skip large files, send straight to Fargate ===
    file_size_mb = check_file_size(http, download_url)
    if file_size_mb > 0:
        logger.info(f"Content-Length: {file_size_mb:.1f} MB")
    
    if file_size_mb > SIZE_THRESHOLD_MB:
        logger.info(f"File too large for Lambda: {file_size_mb:.1f} MB > {SIZE_THRESHOLD_MB} MB")
        logger.info(f"Skipping download, saving directly to pending_downloads/")
        pending_key = save_to_pending(
            s3_bucket, s3_key, download_url, file_info,
            reason=f"exceeds {SIZE_THRESHOLD_MB}MB threshold ({file_size_mb:.1f}MB)"
        )
        return {
            'statusCode': 202,
            'body': json.dumps({
                'status': 'deferred_to_fargate',
                's3_key': s3_key,
                'size_mb': round(file_size_mb, 1),
                'pending_key': pending_key,
                'user': display_name
            })
        }
    
    # Download file
    success, data, error = download_file(http, download_url)
    
    if not success:
        logger.error(f"Download failed: {error}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': error,
                's3_key': s3_key,
                'user': display_name
            })
        }
    
    # Upload to S3
    content_type = get_content_type(s3_key)
    success, error = upload_to_s3(s3_bucket, s3_key, data, content_type)
    
    if not success:
        logger.error(f"Upload failed: {error}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': error,
                's3_key': s3_key,
                'user': display_name
            })
        }
    
    logger.info("=" * 50)
    logger.info("Download complete!")
    logger.info(f"  User: {display_name}")
    logger.info(f"  File: {s3_key}")
    logger.info(f"  Size: {len(data)} bytes")
    logger.info("=" * 50)
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            's3_key': s3_key,
            'size': len(data),
            'user': display_name,
            'device': device_account
        })
    }