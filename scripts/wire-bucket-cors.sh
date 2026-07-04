#!/usr/bin/env bash
# wire-bucket-cors.sh BUCKET [REGION]
# Browser-direct presigned PUT/GET (org-assets uploads) is cross-origin
# fetch — S3 must answer CORS. put-bucket-cors REPLACES the whole config;
# this bucket has no other CORS consumers, so a full replace is safe.
set -euo pipefail
BUCKET="${1:?usage: wire-bucket-cors.sh BUCKET [REGION]}"
REGION="${2:-ap-southeast-2}"

aws s3api put-bucket-cors --bucket "$BUCKET" --region "$REGION" \
  --cors-configuration '{
    "CORSRules": [
      {
        "AllowedOrigins": ["https://*.amplifyapp.com", "http://localhost:8765"],
        "AllowedMethods": ["PUT", "GET"],
        "AllowedHeaders": ["*"],
        "MaxAgeSeconds": 3000
      }
    ]
  }'
echo "CORS applied to s3://$BUCKET"
aws s3api get-bucket-cors --bucket "$BUCKET" --region "$REGION"
