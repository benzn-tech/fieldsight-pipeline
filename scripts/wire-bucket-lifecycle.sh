#!/usr/bin/env bash
# wire-bucket-lifecycle.sh BUCKET [REGION]
# Expire abandoned presigned uploads under org-assets/pending/ after 1 day
# (committed assets are relocated out of pending on save, so anything left is
# an abandoned upload), and expire download_claims/ markers after 1 day
# (Phase 4b: a claim marker's job is done once the downloader releases it or
# a later sweep takes it over -- anything still there a day later is orphaned).
# put-bucket-lifecycle-configuration REPLACES the whole config — abort if
# the bucket already has OTHER rules so we never clobber them.
set -euo pipefail
BUCKET="${1:?usage: wire-bucket-lifecycle.sh BUCKET [REGION]}"
REGION="${2:-ap-southeast-2}"

EXISTING="$(aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --region "$REGION" --query 'Rules[?ID!=`org-assets-pending-expiry` && ID!=`download-claims-expiry` && ID!=`voice-clips-expiry` && ID!=`voiceprint-requests-expiry`].ID' \
  --output text 2>/dev/null || true)"
if [ -n "$EXISTING" ]; then
  # Refusing is right — this script REPLACES the whole configuration, and prod's rules
  # (DeleteOldTranscripts, CleanupPendingDownloads) are not in the list above, so running it
  # there would delete them.
  #
  # But it means this script cannot manage prod, and saying "the lifecycle rule is added"
  # after editing it is only true of TEST. On 2026-08-14 that was claimed for a rule whose
  # whole point was keeping correction artifacts from accumulating in the prod lake.
  echo "ERROR: bucket $BUCKET has lifecycle rules this script does not manage ($EXISTING)." >&2
  echo "       It replaces the WHOLE configuration, so it refuses rather than delete them." >&2
  echo "       For prod, add the rule by hand alongside the existing ones:" >&2
  echo '         aws s3api get-bucket-lifecycle-configuration --bucket BUCKET > current.json' >&2
  echo '         # append {"ID":"voiceprint-requests-expiry","Status":"Enabled",' >&2
  echo '         #         "Filter":{"Prefix":"voiceprint_requests/"},"Expiration":{"Days":7}}' >&2
  echo '         aws s3api put-bucket-lifecycle-configuration --bucket BUCKET ' >&2
  echo '           --lifecycle-configuration file://current.json' >&2
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
      },
      {
        "ID": "download-claims-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "download_claims/" },
        "Expiration": { "Days": 1 }
      }
      ,{
        "ID": "voiceprint-requests-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "voiceprint_requests/" },
        "Expiration": { "Days": 7 }
      }
      ,{
        "ID": "voice-clips-expiry",
        "Status": "Enabled",
        "Filter": { "Prefix": "voice/" },
        "Expiration": { "Days": 30 }
      }
    ]
  }'
echo "Lifecycle applied to s3://$BUCKET"
aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION"
