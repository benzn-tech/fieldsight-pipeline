"""
Lambda 2.5: VAD Speech Segmentation — Extract speech segments from audio/video files

This Lambda function:
1. Triggered by S3 ObjectCreated event on users/*/audio/* and users/*/video/*
2. Downloads the media file from S3
3. Extracts audio from video files (ffmpeg) if needed
4. Runs Silero VAD to detect speech segments
5. Exports each speech segment as an individual WAV file
6. Saves segments to audio_segments/{user}/{date}/ with offset metadata in filename
7. Transcribe Lambda is then triggered by the new audio_segments/ files

Trigger: S3 Event (ObjectCreated on users/*/audio/* and users/*/video/*)
         Replaces direct Transcribe trigger on users/*

Filename convention for output segments:
    {device}_{date}_{time}_off{start}_to{end}_src{format}.wav

    Example: Benl1_2026-02-19_09-20-00_off180.0_to245.8_srcmp4.wav
    
    This encodes:
    - device:    Benl1 (source device)
    - date/time: 2026-02-19_09-20-00 (source file recording start time)
    - off/to:    offset within the source file in seconds (180.0s to 245.8s)
    - src:       source format (mp4, wav, m4a, etc.)
    
    From this filename, downstream code can:
    - Calculate absolute time: file_start_time + offset
    - Locate original video: same device + date + time without _off suffix
    - Know whether source was audio or video for UI playback

Output structure:
    audio_segments/{display_name}/{date}/{device}_{date}_{time}_off{start}_to{end}_src{fmt}.wav

Environment Variables:
    S3_BUCKET              - S3 bucket name
    OUTPUT_PREFIX          - Output prefix (default: audio_segments/)
    MIN_SPEECH_DURATION    - Minimum speech segment in seconds (default: 1.0)
    MIN_SILENCE_DURATION   - Minimum silence gap to split segments (default: 3.0)
    MERGE_GAP              - Merge segments closer than N seconds (default: 2.0)
    VAD_THRESHOLD          - VAD confidence threshold 0.0-1.0 (default: 0.5)
    SAMPLE_RATE            - Output audio sample rate (default: 16000)
    SKIP_EXISTING          - Skip if segments already exist (default: true)
    PASSTHROUGH_AUDIO      - If true, send short audio files directly to Transcribe
                             without VAD (default: false)

Requires Lambda Layer:
    sitesync-vad-layer  (contains: ffmpeg, onnxruntime, silero model, numpy, soundfile)
    See build_vad_layer.sh for build instructions.
"""

import os
import re
import json
import struct
import logging
import subprocess
import tempfile
from pathlib import Path

import boto3
from urllib.parse import unquote_plus

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
s3_client = boto3.client('s3')

# Configuration
S3_BUCKET = os.environ.get('S3_BUCKET', '')
OUTPUT_PREFIX = os.environ.get('OUTPUT_PREFIX', 'audio_segments/')
MIN_SPEECH_DURATION = float(os.environ.get('MIN_SPEECH_DURATION', '1.0'))
MIN_SILENCE_DURATION = float(os.environ.get('MIN_SILENCE_DURATION', '3.0'))
MERGE_GAP = float(os.environ.get('MERGE_GAP', '2.0'))
VAD_THRESHOLD = float(os.environ.get('VAD_THRESHOLD', '0.5'))
SAMPLE_RATE = int(os.environ.get('SAMPLE_RATE', '16000'))
SKIP_EXISTING = os.environ.get('SKIP_EXISTING', 'true').lower() == 'true'
PASSTHROUGH_AUDIO = os.environ.get('PASSTHROUGH_AUDIO', 'false').lower() == 'true'
WEB_VIDEO_PREFIX = os.environ.get('WEB_VIDEO_PREFIX', 'web_video/')
GENERATE_PREVIEW = os.environ.get('GENERATE_PREVIEW', 'true').lower() == 'true'

# Supported input formats
AUDIO_FORMATS = {'.wav', '.mp3', '.m4a', '.aac', '.flac', '.ogg'}
VIDEO_FORMATS = {'.mp4', '.webm', '.mov', '.avi', '.mkv'}
ALL_FORMATS = AUDIO_FORMATS | VIDEO_FORMATS

# ffmpeg path (Lambda Layer installs to /opt/bin)
FFMPEG_PATH = '/opt/bin/ffmpeg'
if not os.path.exists(FFMPEG_PATH):
    # Fallback for local testing
    FFMPEG_PATH = 'ffmpeg'

# Silero VAD model path (Lambda Layer installs to /opt/silero/)
SILERO_MODEL_PATH = '/opt/silero/silero_vad.onnx'
SILERO_MODEL_S3_KEY = os.environ.get('SILERO_MODEL_S3_KEY', 'models/silero_vad.onnx')
SILERO_MODEL_LOCAL = '/tmp/silero_vad.onnx'


# ============================================================
# Audio Processing
# ============================================================

def detect_codec(input_path):
    """
    Detect video/audio codec using ffmpeg -i (no ffprobe needed).
    
    Returns dict:
        {
            'video_codec': 'h264' | 'hevc' | None,
            'audio_codec': 'aac' | 'pcm_s16le' | None,
            'browser_playable': True | False,
            'resolution': '1920x1080' | None,
        }
    """
    result = {
        'video_codec': None,
        'audio_codec': None,
        'browser_playable': True,  # assume True unless H265 detected
        'resolution': None,
    }
    try:
        proc = subprocess.run(
            [FFMPEG_PATH, '-i', input_path, '-hide_banner'],
            capture_output=True, text=True, timeout=10
        )
        # ffmpeg -i always exits non-zero, info is in stderr
        info = proc.stderr

        # Video codec: "Video: h264" or "Video: hevc"
        vmatch = re.search(r'Video:\s+(\w+)', info)
        if vmatch:
            codec = vmatch.group(1).lower()
            result['video_codec'] = codec
            # H265/HEVC = not universally browser playable
            if codec in ('hevc', 'h265'):
                result['browser_playable'] = False

        # Audio codec: "Audio: aac" or "Audio: pcm_s16le"
        amatch = re.search(r'Audio:\s+(\w+)', info)
        if amatch:
            result['audio_codec'] = amatch.group(1).lower()

        # Resolution: "1920x1080" or "1280x720"
        rmatch = re.search(r'(\d{3,4})x(\d{3,4})', info)
        if rmatch:
            result['resolution'] = f"{rmatch.group(1)}x{rmatch.group(2)}"

    except Exception as e:
        logger.warning(f"  Codec detection failed: {e}")

    return result


def extract_audio_ffmpeg(input_path, output_path, sample_rate=16000):
    """
    Extract audio from any media file using ffmpeg.
    Output: mono WAV at specified sample rate.
    """
    cmd = [
        FFMPEG_PATH, '-y', '-i', input_path,
        '-vn',                          # drop video
        '-acodec', 'pcm_s16le',         # 16-bit PCM
        '-ar', str(sample_rate),        # resample
        '-ac', '1',                     # mono
        output_path
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[:500]}")
    return output_path


def generate_web_preview(input_path, output_path, timeout=180):
    """
    Generate browser-friendly H264 720p preview from any video.
    Uses faststart for instant seek. Gracefully fails (preview is optional).
    
    Input: any video (H264/H265/etc)
    Output: H264 720p MP4 with AAC audio, moov atom at front
    
    Typical: 200MB H265 → 30-50MB H264 720p
    """
    cmd = [
        FFMPEG_PATH, '-y', '-i', input_path,
        '-c:v', 'libx264',         # H264 codec (universal browser support)
        '-preset', 'veryfast',      # Speed over compression (Lambda time matters)
        '-crf', '28',               # Quality: 28 = medium (good enough for review)
        '-vf', 'scale=-2:720',     # 720p height, auto width (keeps aspect ratio)
        '-c:a', 'aac',             # AAC audio
        '-b:a', '64k',             # Low audio bitrate (speech only)
        '-ac', '1',                # Mono audio
        '-movflags', 'faststart',  # moov atom at front = instant seek
        '-max_muxing_queue_size', '1024',
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning(f"  Preview generation failed: {result.stderr[:300]}")
            return None
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info(f"  Preview generated: {size_mb:.1f} MB (H264 720p)")
        return output_path
    except subprocess.TimeoutExpired:
        logger.warning(f"  Preview generation timed out ({timeout}s)")
        return None
    except Exception as e:
        logger.warning(f"  Preview generation error: {e}")
        return None


def read_wav_pcm(wav_path):
    """
    Read a 16-bit mono PCM WAV file into a list of float samples [-1, 1].
    Pure Python — no numpy/soundfile dependency for the read path.
    """
    with open(wav_path, 'rb') as f:
        # Read RIFF header
        riff = f.read(4)
        if riff != b'RIFF':
            raise ValueError("Not a WAV file")
        f.read(4)  # file size
        wave = f.read(4)
        if wave != b'WAVE':
            raise ValueError("Not a WAV file")

        # Find data chunk
        sample_rate = 16000
        while True:
            chunk_id = f.read(4)
            if len(chunk_id) < 4:
                raise ValueError("No data chunk found")
            chunk_size = struct.unpack('<I', f.read(4))[0]

            if chunk_id == b'fmt ':
                fmt_data = f.read(chunk_size)
                audio_format = struct.unpack('<H', fmt_data[0:2])[0]
                channels = struct.unpack('<H', fmt_data[2:4])[0]
                sample_rate = struct.unpack('<I', fmt_data[4:8])[0]
                bits_per_sample = struct.unpack('<H', fmt_data[14:16])[0]
                if audio_format != 1:
                    raise ValueError(f"Not PCM format: {audio_format}")
                if channels != 1:
                    raise ValueError(f"Expected mono, got {channels} channels")
                if bits_per_sample != 16:
                    raise ValueError(f"Expected 16-bit, got {bits_per_sample}")
            elif chunk_id == b'data':
                num_samples = chunk_size // 2  # 16-bit = 2 bytes
                raw = f.read(chunk_size)
                samples = struct.unpack(f'<{num_samples}h', raw)
                # Normalize to float [-1, 1]
                return [s / 32768.0 for s in samples], sample_rate
            else:
                f.read(chunk_size)


def write_wav_segment(samples, sample_rate, output_path):
    """
    Write float samples to a 16-bit mono PCM WAV file.
    Pure Python — no external dependencies.
    """
    num_samples = len(samples)
    data_size = num_samples * 2  # 16-bit = 2 bytes per sample

    with open(output_path, 'wb') as f:
        # RIFF header
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')

        # fmt chunk
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))            # chunk size
        f.write(struct.pack('<H', 1))             # PCM format
        f.write(struct.pack('<H', 1))             # mono
        f.write(struct.pack('<I', sample_rate))   # sample rate
        f.write(struct.pack('<I', sample_rate * 2))  # byte rate
        f.write(struct.pack('<H', 2))             # block align
        f.write(struct.pack('<H', 16))            # bits per sample

        # data chunk
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        for s in samples:
            clamped = max(-1.0, min(1.0, s))
            f.write(struct.pack('<h', int(clamped * 32767)))


# ============================================================
# Silero VAD
# ============================================================

def load_silero_vad():
    """
    Load Silero VAD ONNX model.
    
    Priority:
      1. /tmp/silero_vad.onnx (already downloaded from S3)
      2. Download from S3 (models/silero_vad.onnx) → /tmp/
      3. Fallback to Lambda Layer (/opt/silero/silero_vad.onnx)
    
    S3 model takes priority because Lambda Layer may have a different
    model version with incompatible tensor formats (v3 vs v4/v5).
    """
    try:
        import onnxruntime as ort
    except ImportError:
        raise ImportError(
            "onnxruntime not found. Install the sitesync-vad-layer Lambda Layer."
        )

    model_path = None

    # 1. Already downloaded to /tmp
    if os.path.exists(SILERO_MODEL_LOCAL):
        model_path = SILERO_MODEL_LOCAL
        logger.info(f"  Using cached S3 model: {SILERO_MODEL_LOCAL}")
    
    # 2. Download from S3
    elif S3_BUCKET and SILERO_MODEL_S3_KEY:
        try:
            logger.info(f"  Downloading model from s3://{S3_BUCKET}/{SILERO_MODEL_S3_KEY}")
            s3_client.download_file(S3_BUCKET, SILERO_MODEL_S3_KEY, SILERO_MODEL_LOCAL)
            model_path = SILERO_MODEL_LOCAL
            logger.info(f"  Model downloaded to {SILERO_MODEL_LOCAL}")
        except Exception as e:
            logger.warning(f"  S3 model download failed: {e}, falling back to Layer")
    
    # 3. Fallback to Lambda Layer
    if not model_path:
        if os.path.exists(SILERO_MODEL_PATH):
            model_path = SILERO_MODEL_PATH
            logger.info(f"  Using Lambda Layer model: {SILERO_MODEL_PATH}")
        else:
            raise FileNotFoundError(
                f"Silero model not found. Checked: {SILERO_MODEL_LOCAL}, "
                f"s3://{S3_BUCKET}/{SILERO_MODEL_S3_KEY}, {SILERO_MODEL_PATH}"
            )

    session = ort.InferenceSession(
        model_path,
        providers=['CPUExecutionProvider']
    )
    
    # Log model info for debugging
    inputs = session.get_inputs()
    logger.info(f"  Model loaded: {len(inputs)} inputs: {[i.name for i in inputs]}")
    
    return session


def run_silero_vad(session, audio_samples, sample_rate=16000,
                   window_ms=32, threshold=0.5):
    """
    Run Silero VAD on audio samples.
    Compatible with Silero VAD v6 (combined state tensor).
    
    Returns list of (start_sec, end_sec) tuples for speech segments.
    """
    import numpy as np

    audio = np.array(audio_samples, dtype=np.float32)
    window_size = int(sample_rate * window_ms / 1000)  # 512 samples for 32ms @ 16kHz

    # Silero VAD v6: combined state [2, batch, 128]
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(sample_rate, dtype=np.int64)

    speech_probs = []
    num_windows = len(audio) // window_size

    for i in range(num_windows):
        chunk = audio[i * window_size:(i + 1) * window_size]
        chunk = chunk.reshape(1, -1)

        ort_inputs = {
            'input': chunk,
            'state': state,
            'sr': sr,
        }
        ort_outputs = session.run(None, ort_inputs)
        prob = ort_outputs[0].item()
        state = ort_outputs[1]
        speech_probs.append(prob)

    # Convert probabilities to speech segments
    segments = []
    in_speech = False
    start_idx = 0

    for i, prob in enumerate(speech_probs):
        if prob >= threshold and not in_speech:
            in_speech = True
            start_idx = i
        elif prob < threshold and in_speech:
            in_speech = False
            start_sec = start_idx * window_ms / 1000
            end_sec = i * window_ms / 1000
            segments.append((start_sec, end_sec))

    # Close final segment
    if in_speech:
        start_sec = start_idx * window_ms / 1000
        end_sec = num_windows * window_ms / 1000
        segments.append((start_sec, end_sec))

    return segments


def merge_close_segments(segments, merge_gap=2.0, min_duration=1.0):
    """
    Merge segments that are close together, drop very short ones.
    
    Args:
        segments: list of (start_sec, end_sec)
        merge_gap: merge segments closer than this (seconds)
        min_duration: drop segments shorter than this (seconds)
    
    Returns: list of (start_sec, end_sec)
    """
    if not segments:
        return []

    # Merge close segments
    merged = [segments[0]]
    for start, end in segments[1:]:
        prev_start, prev_end = merged[-1]
        if start - prev_end <= merge_gap:
            # Merge: extend previous segment
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))

    # Filter by minimum duration
    filtered = [
        (s, e) for s, e in merged
        if (e - s) >= min_duration
    ]

    return filtered


# ============================================================
# Filename Parsing & Generation
# ============================================================

def parse_source_filename(key):
    """
    Parse S3 key to extract device, date, time, and source format.
    
    Handles paths like:
        users/Jarley_Trainor/video/2026-02-19/Benl1_2026-02-19_09-20-00.mp4
        users/Jarley_Trainor/audio/2026-02-19/Benl1_2026-02-19_09-20-00.wav
    
    Returns dict:
        {
            'user_name': 'Jarley_Trainor',
            'device': 'Benl1',
            'date': '2026-02-19',
            'time': '09-20-00',
            'source_type': 'video' or 'audio',
            'source_ext': 'mp4' or 'wav',
            'basename_no_ext': 'Benl1_2026-02-19_09-20-00'
        }
    """
    parts = key.split('/')
    filename = os.path.basename(key)
    name_no_ext, ext = os.path.splitext(filename)
    ext = ext.lstrip('.').lower()

    # Extract user name from path: users/{user_name}/...
    user_name = parts[1] if len(parts) >= 2 and parts[0] == 'users' else 'Unknown'

    # Determine source type from path
    source_type = 'unknown'
    for part in parts:
        if part in ('video', 'audio', 'pictures', 'other'):
            source_type = part
            break

    # If not in path, infer from extension
    if source_type == 'unknown':
        if f'.{ext}' in VIDEO_FORMATS:
            source_type = 'video'
        elif f'.{ext}' in AUDIO_FORMATS:
            source_type = 'audio'

    # Extract device and timestamp from filename
    # Expected: Benl1_2026-02-19_09-20-00.mp4
    device = 'Unknown'
    date_str = ''
    time_str = ''

    match = re.match(r'^(\w+)_(\d{4}-\d{2}-\d{2})[-_](\d{2}-\d{2}-\d{2})', name_no_ext)
    if match:
        device = match.group(1)
        date_str = match.group(2)
        time_str = match.group(3)

    return {
        'user_name': user_name,
        'device': device,
        'date': date_str,
        'time': time_str,
        'source_type': source_type,
        'source_ext': ext,
        'basename_no_ext': name_no_ext,
    }


def build_segment_filename(source_info, start_sec, end_sec):
    """
    Build output filename with offset metadata.
    
    Format: {device}_{date}_{time}_off{start}_to{end}_src{ext}.wav
    Example: Benl1_2026-02-19_09-20-00_off180.0_to245.8_srcmp4.wav
    """
    return (
        f"{source_info['device']}_{source_info['date']}_{source_info['time']}"
        f"_off{start_sec:.1f}_to{end_sec:.1f}"
        f"_src{source_info['source_ext']}.wav"
    )


def build_segment_s3_key(source_info, segment_filename):
    """
    Build S3 key for output segment.
    
    Format: audio_segments/{user}/{date}/{segment_filename}
    """
    return f"{OUTPUT_PREFIX}{source_info['user_name']}/{source_info['date']}/{segment_filename}"


def check_segments_exist(bucket, source_info):
    """Check if segments already exist for this source file"""
    prefix = (
        f"{OUTPUT_PREFIX}{source_info['user_name']}/{source_info['date']}/"
        f"{source_info['device']}_{source_info['date']}_{source_info['time']}_off"
    )
    resp = s3_client.list_objects_v2(
        Bucket=bucket, Prefix=prefix, MaxKeys=1
    )
    return resp.get('KeyCount', 0) > 0


# ============================================================
# Main Handler
# ============================================================

def lambda_handler(event, context):
    """Main Lambda handler — S3 event triggered"""
    logger.info(f"VAD Lambda invoked: {json.dumps(event)}")

    results = []

    for record in event.get('Records', []):
        try:
            bucket = record['s3']['bucket']['name']
            key = unquote_plus(record['s3']['object']['key'])
            file_size = record['s3']['object'].get('size', 0)

            logger.info(f"Processing: s3://{bucket}/{key} ({file_size} bytes)")

            # Skip non-media files
            ext = os.path.splitext(key)[1].lower()
            if ext not in ALL_FORMATS:
                logger.info(f"  Skipping non-media file: {ext}")
                results.append({'key': key, 'status': 'skipped', 'reason': 'not media'})
                continue

            # Skip pictures and other non-audio/video
            if '/pictures/' in key or '/other/' in key:
                logger.info(f"  Skipping non-AV content")
                results.append({'key': key, 'status': 'skipped', 'reason': 'not AV'})
                continue

            # Parse source file info
            source_info = parse_source_filename(key)
            logger.info(f"  Source: device={source_info['device']}, "
                        f"type={source_info['source_type']}, ext={source_info['source_ext']}")

            # Skip if segments already exist
            if SKIP_EXISTING and check_segments_exist(bucket, source_info):
                logger.info(f"  Segments already exist, skipping")
                results.append({'key': key, 'status': 'skipped', 'reason': 'already processed'})
                continue

            # Work in /tmp
            tmp_dir = tempfile.mkdtemp(prefix='vad_')
            try:
                result = process_single_file(
                    bucket, key, source_info, tmp_dir
                )
                results.append(result)
            finally:
                # Clean up /tmp
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception as e:
            logger.error(f"Error processing record: {e}", exc_info=True)
            results.append({
                'key': key if 'key' in dir() else 'unknown',
                'status': 'error',
                'error': str(e)
            })

    # Summary
    summary = {
        'total': len(results),
        'processed': sum(1 for r in results if r.get('status') == 'processed'),
        'skipped': sum(1 for r in results if r.get('status') == 'skipped'),
        'errors': sum(1 for r in results if r.get('status') == 'error'),
        'total_segments': sum(r.get('segments_created', 0) for r in results),
    }
    logger.info(f"VAD complete: {json.dumps(summary)}")

    return {
        'statusCode': 200,
        'body': json.dumps({'summary': summary, 'results': results})
    }


def process_single_file(bucket, key, source_info, tmp_dir):
    """
    Process a single audio/video file through VAD pipeline.
    
    Returns result dict with status and segment info.
    """
    ext = os.path.splitext(key)[1].lower()
    input_path = os.path.join(tmp_dir, f"input{ext}")
    wav_path = os.path.join(tmp_dir, "audio_16k.wav")

    # Step 1: Download from S3
    logger.info(f"  Downloading {key}...")
    s3_client.download_file(bucket, key, input_path)
    input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    logger.info(f"  Downloaded: {input_size_mb:.1f} MB")

    # Step 2: Detect codec (before extracting audio, while we still have original)
    codec_info = {'video_codec': None, 'audio_codec': None,
                  'browser_playable': True, 'resolution': None}
    if source_info['source_type'] == 'video':
        logger.info(f"  Detecting codec...")
        codec_info = detect_codec(input_path)
        logger.info(f"  Codec: video={codec_info['video_codec']}, "
                    f"audio={codec_info['audio_codec']}, "
                    f"browser_ok={codec_info['browser_playable']}, "
                    f"res={codec_info['resolution']}")

    # Step 3: Extract audio (ffmpeg for all formats — ensures consistent 16kHz mono WAV)
    logger.info(f"  Extracting audio → 16kHz mono WAV...")
    extract_audio_ffmpeg(input_path, wav_path, SAMPLE_RATE)
    wav_size_mb = os.path.getsize(wav_path) / (1024 * 1024)
    logger.info(f"  Audio extracted: {wav_size_mb:.1f} MB")

    # Step 3b: Generate H264 web preview (before deleting input)
    # Skip if original is already H264 + browser playable
    preview_key = None
    if GENERATE_PREVIEW and source_info['source_type'] == 'video':
        if codec_info.get('browser_playable') and codec_info.get('video_codec') == 'h264':
            logger.info(f"  Original is H264 browser-playable, skipping preview generation")
            preview_key = key  # Point to original file
        else:
            # Only generate preview for non-H264 videos (e.g., H265)
            preview_s3_key = (
                f"{WEB_VIDEO_PREFIX}{source_info['user_name']}/{source_info['date']}/"
                f"{source_info['basename_no_ext']}.mp4"
            )
            preview_exists = False
            if SKIP_EXISTING:
                try:
                    s3_client.head_object(Bucket=bucket, Key=preview_s3_key)
                    preview_exists = True
                    logger.info(f"  Preview already exists: {preview_s3_key}")
                except:
                    pass
            
            if not preview_exists:
                preview_path = os.path.join(tmp_dir, "preview.mp4")
                logger.info(f"  Generating H264 720p preview (source is {codec_info.get('video_codec','unknown')})...")
                result = generate_web_preview(input_path, preview_path, timeout=150)
                if result and os.path.exists(preview_path):
                    s3_client.upload_file(
                        preview_path, bucket, preview_s3_key,
                        ExtraArgs={'ContentType': 'video/mp4'}
                    )
                    preview_key = preview_s3_key
                    logger.info(f"  Preview uploaded: {preview_s3_key}")
                    os.remove(preview_path)
                else:
                    logger.info(f"  Preview skipped (generation failed)")
            else:
                preview_key = preview_s3_key

    # Remove input to free /tmp space
    os.remove(input_path)

    # Step 4: Read WAV
    logger.info(f"  Reading WAV samples...")
    audio_samples, sr = read_wav_pcm(wav_path)
    duration_sec = len(audio_samples) / sr
    logger.info(f"  Audio: {duration_sec:.1f}s, {sr}Hz, {len(audio_samples)} samples")

    # Step 5: Run VAD
    logger.info(f"  Running Silero VAD (threshold={VAD_THRESHOLD})...")
    session = load_silero_vad()
    raw_segments = run_silero_vad(
        session, audio_samples, sr,
        window_ms=32, threshold=VAD_THRESHOLD
    )
    logger.info(f"  Raw VAD segments: {len(raw_segments)}")

    # Step 6: Merge close segments
    merged_segments = merge_close_segments(
        raw_segments, merge_gap=MERGE_GAP, min_duration=MIN_SPEECH_DURATION
    )
    logger.info(f"  Merged segments: {len(merged_segments)}")

    if not merged_segments:
        logger.info(f"  No speech at threshold={VAD_THRESHOLD}, retrying at 0.25...")
        
        # Retry with lower threshold
        raw_segments_retry = run_silero_vad(
            session, audio_samples, sr,
            window_ms=32, threshold=0.25
        )
        merged_segments = merge_close_segments(
            raw_segments_retry, merge_gap=MERGE_GAP, min_duration=MIN_SPEECH_DURATION
        )
        logger.info(f"  Retry at 0.25: {len(merged_segments)} segments")
    
    if not merged_segments:
        logger.info(f"  Still no speech at 0.25, fallback: sending entire audio to Transcribe")
        
        # Fallback: send entire audio as one segment so Transcribe can try
        # This handles cases where background noise confuses VAD but speech exists
        logger.info(f"  Fallback: sending entire audio as single segment for Transcribe")
        seg_start = 0
        seg_end = duration_sec
        seg_filename = build_segment_filename(source_info, seg_start, seg_end)
        seg_s3_key = build_segment_s3_key(source_info, seg_filename)
        
        # Upload the full WAV
        s3_client.upload_file(
            wav_path, bucket, seg_s3_key,
            ExtraArgs={'ContentType': 'audio/wav'}
        )
        logger.info(f"  Fallback segment uploaded: {seg_s3_key}")
        
        # Save metadata
        metadata = {
            'source_key': key,
            'source_type': source_info['source_type'],
            'device': source_info['device'],
            'user_name': source_info['user_name'],
            'date': source_info['date'],
            'file_start_time': source_info['time'],
            'total_duration_sec': round(duration_sec, 1),
            'speech_duration_sec': 0,
            'speech_ratio': 0,
            'vad_threshold': VAD_THRESHOLD,
            'vad_result': 'fallback_full_audio',
            'codec': codec_info,
            'web_preview_key': preview_key,
            'segments': [{'s3_key': seg_s3_key, 'offset_start': 0, 'offset_end': round(duration_sec, 1), 'duration': round(duration_sec, 1)}],
        }
        meta_key = f"{OUTPUT_PREFIX}{source_info['user_name']}/{source_info['date']}/{source_info['basename_no_ext']}_vad_metadata.json"
        s3_client.put_object(Bucket=bucket, Key=meta_key, Body=json.dumps(metadata, indent=2), ContentType='application/json')
        
        return {
            'key': key,
            'status': 'processed',
            'segments_created': 1,
            'speech_duration': round(duration_sec, 1),
            'total_duration': round(duration_sec, 1),
            'speech_ratio': 0,
            'vad_result': 'fallback_full_audio',
            'metadata_key': meta_key,
        }

    # Step 7: Export segments
    speech_total = 0
    segments_info = []

    for i, (seg_start, seg_end) in enumerate(merged_segments):
        start_sample = int(seg_start * sr)
        end_sample = min(int(seg_end * sr), len(audio_samples))
        segment_samples = audio_samples[start_sample:end_sample]

        if not segment_samples:
            continue

        seg_duration = len(segment_samples) / sr
        speech_total += seg_duration

        # Build filename with offset metadata
        seg_filename = build_segment_filename(source_info, seg_start, seg_end)
        seg_s3_key = build_segment_s3_key(source_info, seg_filename)
        seg_local_path = os.path.join(tmp_dir, seg_filename)

        # Write WAV segment
        write_wav_segment(segment_samples, sr, seg_local_path)

        # Upload to S3
        s3_client.upload_file(
            seg_local_path, bucket, seg_s3_key,
            ExtraArgs={'ContentType': 'audio/wav'}
        )

        seg_size_kb = os.path.getsize(seg_local_path) / 1024
        logger.info(
            f"  Segment {i+1}/{len(merged_segments)}: "
            f"{seg_start:.1f}s–{seg_end:.1f}s ({seg_duration:.1f}s, {seg_size_kb:.0f}KB) "
            f"→ {seg_s3_key}"
        )

        segments_info.append({
            's3_key': seg_s3_key,
            'offset_start': round(seg_start, 1),
            'offset_end': round(seg_end, 1),
            'duration': round(seg_duration, 1),
            'size_bytes': os.path.getsize(seg_local_path),
        })

        # Clean up segment file to save /tmp space
        os.remove(seg_local_path)

    speech_ratio = speech_total / duration_sec if duration_sec > 0 else 0

    logger.info(f"  ✅ Complete: {len(segments_info)} segments, "
                f"speech={speech_total:.1f}s/{duration_sec:.1f}s ({speech_ratio:.0%})")

    # Save metadata alongside segments
    metadata = {
        'source_key': key,
        'source_type': source_info['source_type'],
        'source_ext': source_info['source_ext'],
        'device': source_info['device'],
        'user_name': source_info['user_name'],
        'date': source_info['date'],
        'file_start_time': source_info['time'],
        'total_duration_sec': round(duration_sec, 1),
        'speech_duration_sec': round(speech_total, 1),
        'speech_ratio': round(speech_ratio, 3),
        'vad_threshold': VAD_THRESHOLD,
        'merge_gap': MERGE_GAP,
        'min_speech_duration': MIN_SPEECH_DURATION,
        'codec': codec_info,
        'web_preview_key': preview_key,
        'segments': segments_info,
    }

    meta_key = (
        f"{OUTPUT_PREFIX}{source_info['user_name']}/{source_info['date']}/"
        f"{source_info['basename_no_ext']}_vad_metadata.json"
    )
    s3_client.put_object(
        Bucket=bucket, Key=meta_key,
        Body=json.dumps(metadata, indent=2),
        ContentType='application/json'
    )
    logger.info(f"  Metadata saved: {meta_key}")

    return {
        'key': key,
        'status': 'processed',
        'segments_created': len(segments_info),
        'speech_duration': round(speech_total, 1),
        'total_duration': round(duration_sec, 1),
        'speech_ratio': round(speech_ratio, 3),
        'metadata_key': meta_key,
    }