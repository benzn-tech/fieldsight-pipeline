#!/usr/bin/env bash
# wire-bucket-lifecycle.sh BUCKET [REGION]
# Expire abandoned presigned uploads under org-assets/pending/ after 1 day
# (committed assets are relocated out of pending on save, so anything left is
# an abandoned upload). put-bucket-lifecycle-configuration REPLACES the whole
# config — abort if the bucket already has OTHER rules so we never clobber them.
set -euo pipefail
BUCKET="${1:?usage: wire-bucket-lifecycle.sh BUCKET [REGION]}"
REGION="${2:-ap-southeast-2}"

EXISTING="$(aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --region "$REGION" --query 'Rules[?ID!=`org-assets-pending-expiry`].ID' \
  --output text 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
  echo "ERROR: bucket $BUCKET has other lifecycle rules ($EXISTING); refusing to replace. Merge manually." >&2
  exit 1
fi

aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
  --lifecycle-configuration '{
    "Rules": [
      {
        "ID": "org-assets-pending-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "org-assets/pending/" },
        "Expiration": { "Days": 1 }
      }
    ]
  }'
echo "Lifecycle applied to s3://$BUCKET"
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION"
