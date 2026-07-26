#!/usr/bin/env bash
# ============================================================
# wire-s3-events.sh <bucket> <stage> <region> [--apply]
# ------------------------------------------------------------
# SAM cannot attach S3 ObjectCreated events to an EXTERNAL bucket
# (BUG-33), so we wire them here, idempotently, AFTER `sam deploy`.
#
#   VAD         ← users/**.wav , users/**.mp4   (raw uploads)
#   Transcribe  ← audio_segments/**.wav         (VAD output)
#   EmbedReport ← reports/**daily_report.json   (report generator output, non-VPC DashScope embed)
#   Ingest      ← embeddings/**vectors.json     (embed-report output, in-VPC Aurora insert)
#   EmbedReport ← reindex_requests/**.json      (content-edit reindex REQUEST, org-api output, non-VPC DashScope embed)
#   Ingest      ← reindex_requests/**.vectors.json (embed-report reindex VECTORS output, in-VPC Aurora apply)
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
case "$STAGE" in
  test) PREFIX="fieldsight-test" ;;
  prod) PREFIX="fieldsight-prod" ;;
  *) echo "unknown stage '$STAGE' (test|prod)"; exit 1 ;;
esac
VAD_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-vad"
TRANSCRIBE_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-transcribe"
INGEST_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-ingest"
EMBED_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-embed-report"
EXTRACT_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-extract-session"
ITEM_WRITER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-item-writer"
MATCHER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-programme-matcher"
KEYFRAME_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-keyframe"

echo "Bucket=${BUCKET} Stage=${STAGE} VAD=${PREFIX}-vad Transcribe=${PREFIX}-transcribe EmbedReport=${PREFIX}-embed-report Ingest=${PREFIX}-ingest ExtractSession=${PREFIX}-extract-session ItemWriter=${PREFIX}-item-writer ProgrammeMatcher=${PREFIX}-programme-matcher Keyframe=${PREFIX}-keyframe"

# ---- desired LambdaFunctionConfigurations (our managed entries, Id prefix fs-) ----
# Only wire functions that actually exist: VadFunction is conditional in the
# template (HasVadLayer), so a stack deployed without VadLayerArn has no VAD —
# including a nonexistent ARN makes PutBucketNotificationConfiguration fail
# with "Unable to validate the following destination configurations".
fn_exists() { aws lambda get-function --function-name "$1" --region "$REGION" >/dev/null 2>&1; }

DESIRED='[]'
WIRE_FNS=()
if fn_exists "${PREFIX}-vad"; then
  WIRE_FNS+=("${PREFIX}-vad")
  DESIRED=$(jq -c --arg arn "$VAD_ARN" '. + [
    {"Id":"fs-vad-wav","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"users/"},{"Name":"suffix","Value":".wav"}]}}},
    {"Id":"fs-vad-mp4","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"users/"},{"Name":"suffix","Value":".mp4"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-vad not deployed (no VadLayerArn) — skipping VAD triggers"
fi
if fn_exists "${PREFIX}-transcribe"; then
  WIRE_FNS+=("${PREFIX}-transcribe")
  DESIRED=$(jq -c --arg arn "$TRANSCRIBE_ARN" '. + [
    {"Id":"fs-transcribe-wav","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"audio_segments/"},{"Name":"suffix","Value":".wav"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-transcribe not deployed — skipping transcribe trigger"
fi
# NOTE(Phase 4d): embed-report triggers on reports/*daily_report.json (the
# report generator output) — non-VPC, calls DashScope over the public
# internet, chunks the report + transcripts, and writes the
# {sha256(chunk_text): vector} sidecar to embeddings/. Zero prefix overlap
# with ingest below (reports/ vs embeddings/), so no double-trigger loop.
# NOTE(Task 20, content-correction reindex chain): embed-report ALSO triggers
# on reindex_requests/*.json — the per-topic reindex REQUEST artifact org-api
# (in-VPC) writes after a content edit commits. embed-report's VECTORS output
# goes to a SEPARATE prefix (reindex_vectors/, see fs-ingest-reindex below),
# NOT back under reindex_requests/, so it never re-triggers embed-report (no
# BUG-13 loop) and the two rules don't share a prefix — S3 rejects two rules
# with overlapping suffixes on a shared prefix ("Configuration is ambiguously
# defined").
if fn_exists "${PREFIX}-embed-report"; then
  WIRE_FNS+=("${PREFIX}-embed-report")
  DESIRED=$(jq -c --arg arn "$EMBED_ARN" '. + [
    {"Id":"fs-embed-report","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"reports/"},{"Name":"suffix","Value":"daily_report.json"}]}}},
    {"Id":"fs-embed-reindex","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"reindex_requests/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-embed-report not deployed — skipping embed-report trigger"
fi
# NOTE(Phase 4d): ingest migrated off reports/*daily_report.json onto
# embeddings/*vectors.json (the embed-report output above) — ingest no
# longer calls Bedrock, it looks up pre-computed vectors from this sidecar
# by sha256(chunk_text) and inserts into Aurora. This entry wires
# DataBucketName (the test bucket, whose embeddings/ prefix is empty) —
# harmless. The REAL lake trigger lives on the prod bucket
# (fieldsight-data-509194952652) and is managed MANUALLY there (that
# bucket has hand-managed notifications; see IngestBucketName param).
# NOTE(Task 20, content-correction reindex chain): ingest ALSO triggers on
# reindex_vectors/*.json — the VECTORS result fs-embed-reindex above writes to
# that separate prefix. lambda_ingest.lambda_handler routes it to
# apply_reindex_vectors (delete_chunks_for_topic + insert_chunk), which writes
# to Aurora only, not back to S3 — no BUG-13 loop.
if fn_exists "${PREFIX}-ingest"; then
  WIRE_FNS+=("${PREFIX}-ingest")
  DESIRED=$(jq -c --arg arn "$INGEST_ARN" '. + [
    {"Id":"fs-ingest-report","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"embeddings/"},{"Name":"suffix","Value":"vectors.json"}]}}},
    {"Id":"fs-ingest-reindex","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"reindex_vectors/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-ingest not deployed — skipping ingest trigger"
fi
# NOTE(Phase 4b): extract-session triggers on transcripts/*.json, writing to
# extractions/ -- distinct prefix from every other trigger in this script.
if fn_exists "${PREFIX}-extract-session"; then
  WIRE_FNS+=("${PREFIX}-extract-session")
  DESIRED=$(jq -c --arg arn "$EXTRACT_ARN" '. + [
    {"Id":"fs-extract-transcripts","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"transcripts/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-extract-session not deployed — skipping extract-session trigger"
fi
# NOTE(Phase 4b): item-writer triggers on extractions/*.json, the output of
# extract-session above -- a two-stage chain, zero prefix overlap.
if fn_exists "${PREFIX}-item-writer"; then
  WIRE_FNS+=("${PREFIX}-item-writer")
  DESIRED=$(jq -c --arg arn "$ITEM_WRITER_ARN" '. + [
    {"Id":"fs-write-extractions","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"extractions/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-item-writer not deployed — skipping item-writer trigger"
fi
# NOTE(Programme<->Item feedback, Task 4): the non-VPC programme-matcher
# (Task 3) triggers on match_requests/*.json, written by ItemWriterFunction
# and IngestFunction (match_request.emit) after they commit a batch of
# topics. Prefix "match_requests/" is disjoint from every other prefix
# wired above (users/, audio_segments/, transcripts/, reports/, embeddings/,
# extractions/) — no double-trigger.
if fn_exists "${PREFIX}-programme-matcher"; then
  WIRE_FNS+=("${PREFIX}-programme-matcher")
  DESIRED=$(jq -c --arg arn "$MATCHER_ARN" '. + [
    {"Id":"fs-programme-match","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"match_requests/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-programme-matcher not deployed — skipping programme-matcher trigger"
fi
# NOTE(video-keyframe plan): the in-VPC keyframe extractor triggers on
# keyframe_requests/*.json, written post-commit by item-writer. Prefix is
# disjoint from every other wired prefix (users/, audio_segments/,
# transcripts/, reports/, embeddings/, extractions/, match_requests/,
# reindex_requests/, reindex_vectors/) — no double-trigger. Its own OUTPUT
# (.jpg under users/*/pictures/) matches no rule here (the users/ rules are
# suffix .wav/.mp4) — no BUG-13 loop. Same fn_exists guard as the others: a
# stack deployed without the VAD layer has no keyframe fn.
if fn_exists "${PREFIX}-keyframe"; then
  WIRE_FNS+=("${PREFIX}-keyframe")
  DESIRED=$(jq -c --arg arn "$KEYFRAME_ARN" '. + [
    {"Id":"fs-keyframe-requests","LambdaFunctionArn":$arn,"Events":["s3:ObjectCreated:*"],
     "Filter":{"Key":{"FilterRules":[{"Name":"prefix","Value":"keyframe_requests/"},{"Name":"suffix","Value":".json"}]}}}
  ]' <<<"$DESIRED")
else
  echo "NOTE: ${PREFIX}-keyframe not deployed — skipping keyframe trigger"
fi

CURRENT=$(aws s3api get-bucket-notification-configuration --bucket "$BUCKET" --output json 2>/dev/null || echo '{}')
# A bucket with NO notification config returns EMPTY stdout (success) — not JSON.
[ -n "$CURRENT" ] || CURRENT='{}'

# Keep every non-"fs-" lambda config + all SNS/SQS/EventBridge entries; replace our fs-* set.
# RETIRE_IDS: comma-separated non-"fs-" notification Ids to DROP in this same
# atomic PUT (the legacy hand-named lake entries, e.g. vad-on-users). Default
# empty = preserve all non-fs entries, exactly as before.
RETIRE_JSON=$(jq -cn --arg s "${RETIRE_IDS:-}" '$s | split(",") | map(select(length > 0))')
MERGED=$(jq -n --argjson cur "$CURRENT" --argjson des "$DESIRED" --argjson retire "$RETIRE_JSON" '
  ($cur.LambdaFunctionConfigurations // []) as $lam
  | { LambdaFunctionConfigurations: (($lam | map(select((.Id | startswith("fs-") | not) and (.Id as $i | $retire | index($i) | not)))) + $des) }
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

# Grant S3 permission to invoke each EXISTING lambda (idempotent — ignore AlreadyExists).
for fn in "${WIRE_FNS[@]}"; do
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
