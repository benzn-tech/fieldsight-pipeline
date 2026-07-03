#!/usr/bin/env bash
# ============================================================
# wire-s3-events.sh <bucket> <stage> <region> [--apply]
# ------------------------------------------------------------
# SAM cannot attach S3 ObjectCreated events to an EXTERNAL bucket
# (BUG-33), so we wire them here, idempotently, AFTER `sam deploy`.
#
#   VAD        ← users/**.wav , users/**.mp4   (raw uploads)
#   Transcribe ← audio_segments/**.wav         (VAD output)
#
# SAFETY:
#   * MERGE, not clobber: we read the existing notification config and
#     preserve every entry EXCEPT our own (Id prefix "fs-"). Other
#     consumers (SNS/SQS/EventBridge/other Lambdas) are kept intact.
#   * Dry-run by default. Pass --apply to actually write.
#   * deploy.yml runs --apply for TEST (fresh bucket) but DRY-RUN for PROD
#     — re-point prod's existing manual config to "fs-" Ids once, by hand,
#     then prod can be switched to --apply. See DEPLOYMENT-RUNBOOK.md.
# ============================================================
set -euo pipefail

BUCKET="${1:?usage: wire-s3-events.sh <bucket> <stage> <region> [--apply]}"
STAGE="${2:?missing stage (test|prod)}"
REGION="${3:?missing region}"
APPLY="${4:-}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
PREFIX="fieldsight"; [ "$STAGE" = "test" ] && PREFIX="fieldsight-test"
VAD_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-vad"
TRANSCRIBE_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-transcribe"

echo "Bucket=${BUCKET} Stage=${STAGE} VAD=${PREFIX}-vad Transcribe=${PREFIX}-transcribe"

# ---- desired LambdaFunctionConfigurations (our managed entries, Id prefix fs-) ----
DESIRED=$(cat <<JSON
[
  {"Id":"fs-vad-wav","LambdaFunctionArn":"${VAD_ARN}","Events":["s3:ObjectCreated:*"],
   "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"users/"},{"Name":"suffix","Value":".wav"}]}}},
  {"Id":"fs-vad-mp4","LambdaFunctionArn":"${VAD_ARN}","Events":["s3:ObjectCreated:*"],
   "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"users/"},{"Name":"suffix","Value":".mp4"}]}}},
  {"Id":"fs-transcribe-wav","LambdaFunctionArn":"${TRANSCRIBE_ARN}","Events":["s3:ObjectCreated:*"],
   "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"audio_segments/"},{"Name":"suffix","Value":".wav"}]}}}
]
JSON
)

CURRENT=$(aws s3api get-bucket-notification-configuration --bucket "$BUCKET" --output json 2>/dev/null || echo '{}')
# A bucket with NO notification config returns EMPTY stdout (success) — not JSON.
[ -n "$CURRENT" ] || CURRENT='{}'

# Keep every non-"fs-" lambda config + all SNS/SQS/EventBridge entries; replace our fs-* set.
MERGED=$(jq -n --argjson cur "$CURRENT" --argjson des "$DESIRED" '
  ($cur.LambdaFunctionConfigurations // []) as $lam
  | { LambdaFunctionConfigurations: (($lam | map(select(.Id | startswith("fs-") | not))) + $des) }
  + ( if $cur.TopicConfigurations    then {TopicConfigurations:    $cur.TopicConfigurations}    else {} end )
  + ( if $cur.QueueConfigurations    then {QueueConfigurations:    $cur.QueueConfigurations}    else {} end )
  + ( if $cur.EventBridgeConfiguration then {EventBridgeConfiguration: $cur.EventBridgeConfiguration} else {} end )
')

echo "--- CURRENT (Lambda configs) ---"; echo "$CURRENT" | jq -c '.LambdaFunctionConfigurations // []'
echo "--- DESIRED (after merge)     ---"; echo "$MERGED"  | jq -c '.LambdaFunctionConfigurations // []'

if [ "$APPLY" != "--apply" ]; then
  echo "DRY-RUN (no changes written). Re-run with --apply to write."
  exit 0
fi

# Grant S3 permission to invoke each lambda (idempotent — ignore AlreadyExists).
for fn in "${PREFIX}-vad" "${PREFIX}-transcribe"; do
  aws lambda add-permission --function-name "$fn" \
    --statement-id "s3invoke-${BUCKET}" --action lambda:InvokeFunction \
    --principal s3.amazonaws.com --source-arn "arn:aws:s3:::${BUCKET}" \
    --source-account "$ACCOUNT_ID" --region "$REGION" 2>/dev/null \
    && echo "added s3 invoke permission to $fn" \
    || echo "permission already present on $fn (ok)"
done

aws s3api put-bucket-notification-configuration --bucket "$BUCKET" \
  --notification-configuration "$MERGED" --region "$REGION"
echo "✅ S3 notifications wired on ${BUCKET}"
