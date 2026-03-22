"""
Fargate Downloader v2 - Parallel large file downloads

Runs inside an ECS Fargate container with NO time limit.
Downloads multiple files CONCURRENTLY using thread pool.

Changes from v1:
  - ThreadPoolExecutor for parallel downloads (default: 3 concurrent)
  - Each thread gets its own S3 client (thread safety)
  - Thread-safe counters via threading.Lock
  - Progress logs prefixed with [N/total] for clarity

Triggered by: Orchestrator Lambda via ecs:RunTask
Stored at: s3://{bucket}/scripts/fargate_downloader.py

Environment Variables:
    S3_BUCKET            - S3 bucket name
    PARALLEL_DOWNLOADS   - Number of concurrent downloads (default: 3)
"""

import os
import json
import time
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

import boto3
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

S3_BUCKET = os.environ.get('S3_BUCKET', '')
PENDING_PREFIX = 'pending_downloads/'
MULTIPART_CHUNK_SIZE = 6 * 1024 * 1024  # 6MB per S3 part
PARALLEL_DOWNLOADS = int(os.environ.get('PARALLEL_DOWNLOADS', '3'))


# ============================================================
# Thread-safe counters
# ============================================================
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.success = 0
        self.failed = 0
        self.total_bytes = 0

    def add_success(self, nbytes=0):
        with self.lock:
            self.success += 1
            self.total_bytes += nbytes

    def add_failed(self):
        with self.lock:
            self.failed += 1


# ============================================================
# Helpers
# ============================================================
def fmt_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def fmt_speed(bps):
    if bps < 1024: return f"{bps:.0f} B/s"
    if bps < 1024**2: return f"{bps/1024:.1f} KB/s"
    return f"{bps/1024**2:.2f} MB/s"


def fmt_duration(secs):
    if secs < 60: return f"{secs:.0f}s"
    m, s = divmod(int(secs), 60)
    if m < 60: return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m{s}s"


def get_content_type(key):
    ext = os.path.splitext(key)[1].lower()
    return {
        '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4',
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    }.get(ext, 'application/octet-stream')


# ============================================================
# S3 helpers
# ============================================================
def list_pending_downloads(s3_client):
    """Read all pending download jobs from S3"""
    pending = []
    paginator = s3_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=PENDING_PREFIX):
        for obj in page.get('Contents', []):
            if not obj['Key'].endswith('.json'):
                continue
            try:
                resp = s3_client.get_object(Bucket=S3_BUCKET, Key=obj['Key'])
                data = json.loads(resp['Body'].read().decode('utf-8'))
                data['_pending_key'] = obj['Key']
                pending.append(data)
            except Exception as e:
                logger.warning(f"Failed to read {obj['Key']}: {e}")
    return pending


# ============================================================
# Download + S3 multipart upload (per-thread)
# ============================================================
def download_and_upload(s3_client, download_url, s3_key, content_type, tag=""):
    """
    Stream download from URL -> S3 multipart upload.
    Each thread calls this with its own s3_client.
    """
    http = urllib3.PoolManager(
        cert_reqs='CERT_NONE',
        timeout=urllib3.Timeout(connect=30, read=300)
    )

    logger.info(f"{tag}Connecting: {download_url[:120]}...")
    start_time = time.time()

    response = http.request('GET', download_url, preload_content=False)

    if response.status not in (200, 206):
        response.release_conn()
        raise Exception(f"HTTP {response.status}")

    content_length = None
    cl_header = response.headers.get('Content-Length')
    if cl_header:
        content_length = int(cl_header)
        logger.info(f"{tag}File size: {fmt_size(content_length)}")

    # Start multipart upload
    mpu = s3_client.create_multipart_upload(
        Bucket=S3_BUCKET, Key=s3_key, ContentType=content_type,
    )
    upload_id = mpu['UploadId']

    try:
        uploaded_parts = []
        part_num = 0
        total_bytes = 0
        current_buffer = b''
        last_log = 0

        for chunk in response.stream(65536):
            current_buffer += chunk
            total_bytes += len(chunk)

            # Upload part when buffer is full
            if len(current_buffer) >= MULTIPART_CHUNK_SIZE:
                part_num += 1
                part_resp = s3_client.upload_part(
                    Bucket=S3_BUCKET, Key=s3_key, UploadId=upload_id,
                    PartNumber=part_num, Body=current_buffer,
                )
                uploaded_parts.append({'PartNumber': part_num, 'ETag': part_resp['ETag']})
                current_buffer = b''

            # Progress every 10MB
            if total_bytes - last_log >= 10 * 1024 * 1024:
                elapsed = time.time() - start_time
                speed = total_bytes / elapsed if elapsed > 0 else 0
                if content_length:
                    pct = total_bytes / content_length * 100
                    remaining = (content_length - total_bytes) / speed if speed > 0 else 0
                    logger.info(f"{tag}{fmt_size(total_bytes)}/{fmt_size(content_length)} "
                                f"({pct:.0f}%) {fmt_speed(speed)} ETA:{fmt_duration(remaining)}")
                else:
                    logger.info(f"{tag}{fmt_size(total_bytes)} {fmt_speed(speed)}")
                last_log = total_bytes

        # Upload remaining
        if current_buffer:
            part_num += 1
            part_resp = s3_client.upload_part(
                Bucket=S3_BUCKET, Key=s3_key, UploadId=upload_id,
                PartNumber=part_num, Body=current_buffer,
            )
            uploaded_parts.append({'PartNumber': part_num, 'ETag': part_resp['ETag']})

        response.release_conn()

        if total_bytes == 0:
            raise Exception("Empty file")

        # Complete multipart
        s3_client.complete_multipart_upload(
            Bucket=S3_BUCKET, Key=s3_key, UploadId=upload_id,
            MultipartUpload={'Parts': uploaded_parts},
        )

        elapsed = time.time() - start_time
        speed = total_bytes / elapsed if elapsed > 0 else 0
        logger.info(f"{tag}Done: {fmt_size(total_bytes)} in {fmt_duration(elapsed)} ({fmt_speed(speed)})")
        return total_bytes

    except Exception as e:
        try:
            s3_client.abort_multipart_upload(
                Bucket=S3_BUCKET, Key=s3_key, UploadId=upload_id
            )
        except:
            pass
        raise


# ============================================================
# Process single job (runs in thread)
# ============================================================
def process_job(job, job_num, total_jobs, stats):
    """
    Process one pending download job.
    Creates its own S3 client (boto3 clients are NOT thread-safe for multipart).
    """
    # Each thread gets its own S3 client
    thread_s3 = boto3.client('s3')

    s3_key = job.get('s3_key', '')
    download_url = job.get('download_url', '')
    pending_key = job.get('_pending_key', '')
    user = job.get('file_info', {}).get('display_name', 'Unknown')
    tag = f"[{job_num}/{total_jobs}] "

    if not s3_key or not download_url:
        logger.warning(f"{tag}Missing info, skipping")
        return

    # Check if already exists
    try:
        thread_s3.head_object(Bucket=S3_BUCKET, Key=s3_key)
        logger.info(f"{tag}Already exists: {s3_key}")
        if pending_key:
            thread_s3.delete_object(Bucket=S3_BUCKET, Key=pending_key)
        stats.add_success()
        return
    except:
        pass

    logger.info(f"{tag}Downloading: {s3_key} (user: {user})")

    try:
        ct = get_content_type(s3_key)
        bytes_downloaded = download_and_upload(thread_s3, download_url, s3_key, ct, tag=f"{tag}  ")
        stats.add_success(bytes_downloaded)

        if pending_key:
            thread_s3.delete_object(Bucket=S3_BUCKET, Key=pending_key)

    except Exception as e:
        logger.error(f"{tag}Failed: {e}")
        stats.add_failed()


# ============================================================
# MAIN
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("Fargate Downloader v2 - Parallel Mode")
    logger.info(f"S3 Bucket: {S3_BUCKET}")
    logger.info(f"Parallel downloads: {PARALLEL_DOWNLOADS}")
    logger.info("=" * 60)

    if not S3_BUCKET:
        logger.error("S3_BUCKET not set!")
        sys.exit(1)

    s3_client = boto3.client('s3')

    # Load pending downloads
    pending = list_pending_downloads(s3_client)
    logger.info(f"Found {len(pending)} pending downloads")

    if not pending:
        logger.info("Nothing to do. Exiting.")
        return

    stats = Stats()
    overall_start = time.time()
    total_jobs = len(pending)

    logger.info(f"Starting {min(PARALLEL_DOWNLOADS, total_jobs)} parallel download threads...")
    logger.info("-" * 60)

    with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as executor:
        futures = {}
        for i, job in enumerate(pending):
            future = executor.submit(process_job, job, i + 1, total_jobs, stats)
            futures[future] = job.get('s3_key', f'job-{i}')

        # Wait for all to complete
        for future in as_completed(futures):
            s3_key = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error(f"Unexpected error for {s3_key}: {e}")
                stats.add_failed()

    overall_elapsed = time.time() - overall_start
    avg_speed = stats.total_bytes / overall_elapsed if overall_elapsed > 0 else 0

    logger.info("=" * 60)
    logger.info("Fargate Downloader v2 - Complete")
    logger.info(f"  Success: {stats.success}")
    logger.info(f"  Failed:  {stats.failed}")
    logger.info(f"  Total downloaded: {fmt_size(stats.total_bytes)}")
    logger.info(f"  Total time: {fmt_duration(overall_elapsed)}")
    logger.info(f"  Effective throughput: {fmt_speed(avg_speed)}")
    logger.info(f"  Speedup: {PARALLEL_DOWNLOADS}x parallel vs sequential")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()