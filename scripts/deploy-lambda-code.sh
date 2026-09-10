#!/usr/bin/env bash
# ============================================================
# deploy-lambda-code.sh <prefix> <region>
# ------------------------------------------------------------
# Code-only deploy for the EXISTING (hand-assembled) PROD lambdas.
# Prod was never put in a CloudFormation stack, so SAM can't manage it
# without a risky re-architecture. This automates exactly the documented
# manual process (CLAUDE.md): zip handler + transcript_utils, then
# `aws lambda update-function-code`. It does NOT touch config, layers,
# IAM, env vars, or schedules — only the code.
#
#   bash scripts/deploy-lambda-code.sh fieldsight ap-southeast-2
#
# Dependencies stay in their Lambda Layers (python-docx, vad) — untouched.
# Each update publishes a new version, so you can roll back via the console
# or `aws lambda update-alias` / re-deploy the prior version.
# ============================================================
set -euo pipefail

PREFIX="${1:?usage: deploy-lambda-code.sh <prefix> <region>}"
REGION="${2:?missing region}"
# Every module in src/ goes in every zip, and this is deliberate.
#
# This used to be a hand-kept list of three files. On 2026-09-11 the handlers in
# MAP below imported 20 further local modules between them -- agent_turn_filter,
# output_language, nz_time, weather, site_coords, deletion_mirror, batch_stitch,
# batch_ledger, batch_seal, answer_language, corroboration, metric_render,
# metric_slots, query_slots, dashscope_utils and more -- none of them listed.
# Every one of those is an ImportError at cold start on the function this script
# had just reported as successfully updated, because `update-function-code`
# checks a zip, not an import graph.
#
# A list that must be edited every time an import is added will be wrong again,
# and it will be wrong the same silent way. The whole of src/ is 664 KB of text
# next to a 50 MB limit, so there is nothing to buy by choosing.
SHARED=()
while IFS= read -r _m; do SHARED+=("$_m"); done < <(ls src/*.py)

# function-name suffix → handler source file (the 9 real-logic lambdas).
# fieldsight-fargate-trigger is intentionally omitted: it is an inline-code
# launcher (Handler: index.handler), not a src/ handler that changes.
declare -A MAP=(
  [orchestrator]=lambda_orchestrator
  [downloader]=lambda_downloader
  [transcribe]=lambda_transcribe
  [vad]=lambda_vad
  [report-generator]=lambda_report_generator
  [transcribe-callback]=lambda_transcribe_callback
  [meeting-minutes]=lambda_meeting_minutes
  [ask-agent]=lambda_ask_agent
  [api]=lambda_fieldsight_api
)

[ "${#SHARED[@]}" -gt 1 ] || { echo "❌ no src/*.py found (run from repo root)"; exit 1; }
WORK="$(mktemp -d)"; FAIL=0

for suffix in "${!MAP[@]}"; do
  FN="${PREFIX}-${suffix}"
  HANDLER="src/${MAP[$suffix]}.py"
  if [ ! -f "$HANDLER" ]; then echo "⚠️  skip $FN — $HANDLER missing"; continue; fi
  ZIP="${WORK}/${FN}.zip"
  zip -j -q "$ZIP" "${SHARED[@]}"
  echo "→ update-function-code $FN  ($(basename "$HANDLER") + ${#SHARED[@]} src modules)"
  if aws lambda update-function-code --function-name "$FN" \
        --zip-file "fileb://${ZIP}" --publish --region "$REGION" \
        --query '{Fn:FunctionName,Ver:Version,Size:CodeSize,Mod:LastModified}' --output table; then
    aws lambda wait function-updated --function-name "$FN" --region "$REGION"
  else
    echo "❌ failed: $FN"; FAIL=1
  fi
done

rm -rf "$WORK"
if [ "$FAIL" != "0" ]; then echo "❌ one or more functions failed to update"; exit 1; fi
echo "✅ all prod lambda code updated."
