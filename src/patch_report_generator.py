#!/usr/bin/env python3
"""
patch_report_generator.py

Applies 4 surgical changes to lambda_report_generator.py v3.4:
  1. Add transcript_utils import
  2. Remove internal parse/extract functions (replaced by transcript_utils)
  3. Rewrite process_user_data() to use normalize_transcript()
  4. Add manifest check in generate_daily_report() to skip meeting transcripts

Usage:
  python3 patch_report_generator.py lambda_report_generator.py
  → Outputs: lambda_report_generator_v35.py
"""

import re
import sys

def patch(content):
    """Apply all patches to v3.4 content. Returns patched string."""
    
    # ============================================================
    # PATCH 1: Update version in docstring
    # ============================================================
    content = content.replace(
        'Lambda 5: Daily/Weekly/Monthly Report Generator v3.4',
        'Lambda 5: Daily/Weekly/Monthly Report Generator v3.5'
    )
    content = content.replace(
        "Changes from v3.3:",
        "Changes from v3.4:\n"
        "- REFACTOR: Transcript parsing delegated to shared transcript_utils.py\n"
        "- ADD: Meeting manifest reading — skips transcripts already consumed by meeting minutes\n"
        "- ADD: read_meeting_manifest() excludes meeting transcripts from daily site reports\n"
        "\nChanges from v3.3:",
    )
    
    # ============================================================
    # PATCH 2: Add transcript_utils import after existing imports
    # ============================================================
    content = content.replace(
        "from io import BytesIO",
        "from io import BytesIO\n"
        "from transcript_utils import (\n"
        "    normalize_transcript, format_turns_for_prompt, get_time_bounds,\n"
        "    extract_device_from_filename as tu_extract_device,\n"
        "    extract_vad_metadata_from_filename as tu_extract_vad_info,\n"
        "    read_meeting_manifest,\n"
        ")"
    )
    
    # ============================================================
    # PATCH 3: Replace internal transcript parsing functions
    # with thin wrappers around transcript_utils
    # ============================================================
    
    # Find and replace extract_timestamp_from_filename
    old_extract_ts = re.search(
        r'def extract_timestamp_from_filename\(filename\):.*?return base_time',
        content, re.DOTALL
    )
    if old_extract_ts:
        content = content[:old_extract_ts.start()] + \
            'def extract_timestamp_from_filename(filename):\n' \
            '    """Extract timestamp with VAD offset — delegates to transcript_utils."""\n' \
            '    from transcript_utils import compute_segment_base_time\n' \
            '    return compute_segment_base_time(filename)' + \
            content[old_extract_ts.end():]
    
    # Find and replace extract_device_from_filename
    old_extract_dev = re.search(
        r"def extract_device_from_filename\(filename\):.*?return parts\[0\] if len\(parts\) >= 2 else 'Unknown'",
        content, re.DOTALL
    )
    if old_extract_dev:
        content = content[:old_extract_dev.start()] + \
            'def extract_device_from_filename(filename):\n' \
            '    """Extract device account — delegates to transcript_utils."""\n' \
            '    return tu_extract_device(filename)' + \
            content[old_extract_dev.end():]
    
    # Find and replace extract_vad_info_from_filename
    old_vad = re.search(
        r"def extract_vad_info_from_filename\(filename\):.*?return info",
        content, re.DOTALL
    )
    if old_vad:
        content = content[:old_vad.start()] + \
            'def extract_vad_info_from_filename(filename):\n' \
            '    """Extract VAD metadata — delegates to transcript_utils."""\n' \
            '    return tu_extract_vad_info(filename)' + \
            content[old_vad.end():]
    
    # Find and replace parse_transcript
    old_parse = re.search(
        r"def parse_transcript\(transcript_data\):.*?'duration_seconds': max_end_time.*?\}",
        content, re.DOTALL
    )
    if old_parse:
        content = content[:old_parse.start()] + \
            'def parse_transcript(transcript_data):\n' \
            '    """Parse AWS Transcribe JSON — delegates to transcript_utils."""\n' \
            '    from transcript_utils import parse_transcribe_json\n' \
            '    parsed = parse_transcribe_json(transcript_data)\n' \
            '    if not parsed:\n' \
            '        return None\n' \
            '    return {\n' \
            "        'full_text': parsed['full_text'],\n" \
            "        'words': parsed['words'],\n" \
            "        'word_count': parsed['word_count'],\n" \
            "        'duration_seconds': parsed['duration_seconds'],\n" \
            '    }' + \
            content[old_parse.end():]
    
    # ============================================================
    # PATCH 4: Add manifest reading in generate_daily_report
    # Right after "all_users_data = {}" and before the for loop
    # ============================================================
    
    # Find the spot: after "combined_photos = []" and before "for user_name in sorted(users):"
    content = content.replace(
        "    all_users_data = {}\n"
        "    combined_transcripts = []\n"
        "    combined_photos = []\n"
        "\n"
        "    for user_name in sorted(users):",
        
        "    all_users_data = {}\n"
        "    combined_transcripts = []\n"
        "    combined_photos = []\n"
        "\n"
        "    # --- Read meeting manifests: skip transcripts consumed by meeting minutes ---\n"
        "    meeting_consumed_keys = set()\n"
        "    for user_name in sorted(users):\n"
        "        manifest_keys = read_meeting_manifest(\n"
        "            s3_client, S3_BUCKET, REPORT_PREFIX, target_date, user_name)\n"
        "        if manifest_keys:\n"
        "            meeting_consumed_keys |= manifest_keys\n"
        "            logger.info(f'  Meeting manifest for {user_name}: {len(manifest_keys)} transcripts excluded')\n"
        "\n"
        "    for user_name in sorted(users):"
    )
    
    # ============================================================
    # PATCH 5: In process_user_data, skip meeting-consumed transcripts
    # Add filter after "if target_date not in key: continue"
    # ============================================================
    
    # Add meeting_consumed_keys parameter to process_user_data
    content = content.replace(
        "def process_user_data(bucket, user_name, target_date):",
        "def process_user_data(bucket, user_name, target_date, exclude_keys=None):"
    )
    
    # Add exclusion check after date filter in the transcript loop
    content = content.replace(
        "        # Double-check date match (in case of prefix overlap)\n"
        "        if target_date not in key:\n"
        "            continue",
        "        # Double-check date match (in case of prefix overlap)\n"
        "        if target_date not in key:\n"
        "            continue\n"
        "\n"
        "        # Skip transcripts consumed by meeting minutes\n"
        "        if exclude_keys and key in exclude_keys:\n"
        "            logger.info(f'    Skipping (meeting): {os.path.basename(key)}')\n"
        "            continue"
    )
    
    # Pass exclude_keys in the caller
    content = content.replace(
        "        user_data = process_user_data(S3_BUCKET, user_name, target_date)",
        "        user_data = process_user_data(S3_BUCKET, user_name, target_date,\n"
        "                                       exclude_keys=meeting_consumed_keys)"
    )
    
    # ============================================================
    # PATCH 6: Update version references
    # ============================================================
    content = content.replace("'version': 'v3.4'", "'version': 'v3.5'")
    content = content.replace(
        'logger.info("Report Generator v3.4 - Starting")',
        'logger.info("Report Generator v3.5 - Starting")'
    )
    
    return content


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 patch_report_generator.py lambda_report_generator.py")
        print("Output: lambda_report_generator_v35.py")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = input_file.replace('.py', '_v35.py')
    if output_file == input_file:
        output_file = 'lambda_report_generator_v35.py'
    
    with open(input_file, 'r') as f:
        content = f.read()
    
    # Verify it's v3.4
    if 'v3.4' not in content:
        print(f"WARNING: Input file doesn't appear to be v3.4")
        print(f"  Found versions: {set(re.findall(r'v3\.\d', content))}")
    
    patched = patch(content)
    
    with open(output_file, 'w') as f:
        f.write(patched)
    
    # Verify syntax
    import ast
    try:
        ast.parse(patched)
        print(f"✓ Patched file is valid Python")
    except SyntaxError as e:
        print(f"✗ Syntax error in patched file: {e}")
        sys.exit(1)
    
    # Verify changes
    checks = [
        ('from transcript_utils import', 'transcript_utils import'),
        ('read_meeting_manifest', 'manifest reading'),
        ('meeting_consumed_keys', 'meeting exclusion'),
        ('exclude_keys', 'exclude_keys parameter'),
        ("'version': 'v3.5'", 'version v3.5'),
    ]
    for pattern, label in checks:
        if pattern in patched:
            print(f"  ✓ {label}")
        else:
            print(f"  ✗ MISSING: {label}")
    
    print(f"\nOutput: {output_file}")
    print(f"Lines: {len(patched.splitlines())}")


if __name__ == '__main__':
    main()
