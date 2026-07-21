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
SHARED=("src/transcript_utils.py" "src/llm_utils.py")   # bundled in every zip (CLAUDE.md rule)

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

for f in "${SHARED[@]}"; do [ -f "$f" ] || { echo "❌ $f not found (run from repo root)"; exit 1; }; done
WORK="$(mktemp -d)"; FAIL=0

for suffix in "${!MAP[@]}"; do
  FN="${PREFIX}-${suffix}"
  HANDLER="src/${MAP[$suffix]}.py"
  if [ ! -f "$HANDLER" ]; then echo "⚠️  skip $FN — $HANDLER missing"; continue; fi
  ZIP="${WORK}/${FN}.zip"
  zip -j -q "$ZIP" "$HANDLER" "${SHARED[@]}"
  echo "→ update-function-code $FN  ($(basename "$HANDLER") + transcript_utils.py + llm_utils.py)"
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
