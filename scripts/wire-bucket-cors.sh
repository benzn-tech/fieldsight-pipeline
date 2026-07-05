#!/usr/bin/env bash
# ============================================================
# wire-bucket-cors.sh <bucket> <region> [extra-origin ...]
# ------------------------------------------------------------
# Phase 3: the UI uploads org-assets (avatars / site icons) with presigned
# PUT URLs straight from the browser — a cross-origin request, so the data
# bucket must answer CORS preflights. SAM can't manage CORS on an EXTERNAL
# bucket (same constraint as BUG-33), so it's wired here after deploy.
#
# Idempotent: PutBucketCors replaces the whole CORS document, and this
# script owns it (nothing else configures CORS on the data buckets).
# GET is included so presigned image GETs also work cross-origin.
# ============================================================
set -euo pipefail

BUCKET="${1:?usage: wire-bucket-cors.sh <bucket> <region> [extra-origin ...]}"
REGION="${2:?missing region}"
shift 2

ORIGINS='"https://dev.d2fssznicvuckr.amplifyapp.com", "http://localhost:8765", "http://localhost:3000"'
for extra in "$@"; do
  ORIGINS="${ORIGINS}, \"${extra}\""
done

aws s3api put-bucket-cors --bucket "$BUCKET" --region "$REGION" \
  --cors-configuration "{
    \"CORSRules\": [{
      \"AllowedOrigins\": [${ORIGINS}],
      \"AllowedMethods\": [\"GET\", \"PUT\"],
      \"AllowedHeaders\": [\"*\"],
      \"ExposeHeaders\": [\"ETag\"],
      \"MaxAgeSeconds\": 3000
    }]
  }"

echo "CORS wired on ${BUCKET}: GET/PUT from [${ORIGINS}]"
