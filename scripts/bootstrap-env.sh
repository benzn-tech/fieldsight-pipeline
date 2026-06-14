#!/usr/bin/env bash
# ============================================================
# bootstrap-env.sh <test|prod> [--create]
# ------------------------------------------------------------
# Idempotent, READ-ONLY by default. Audits whether an environment's
# data plane exists and prints a "what to deploy" table. With --create
# it provisions the TEST data plane (bucket + 3 DynamoDB tables), mirroring
# the PROD table schemas so the item store shape matches.
#
# Run once after `aws login`:  bash scripts/bootstrap-env.sh test
# Then to create what's missing: bash scripts/bootstrap-env.sh test --create
# ============================================================
set -uo pipefail

STAGE="${1:?usage: bootstrap-env.sh <test|prod> [--create]}"
CREATE="${2:-}"
REGION="${AWS_REGION:-ap-southeast-2}"
[ "$STAGE" = "test" ] && SUF="-test" || SUF=""
BUCKET="fieldsight-data${SUF}-509194952652"
STACK="fieldsight-pipeline"; [ "$STAGE" = "test" ] && STACK="fieldsight-test"
declare -A TBL=( [items]="fieldsight${SUF}-items" [reports]="fieldsight${SUF}-reports" [audit]="fieldsight${SUF}-audit" )

ok()   { printf "  ✅ %-34s %s\n" "$1" "$2"; }
miss() { printf "  ❌ %-34s %s\n" "$1" "$2"; }

echo "================ FieldSight env audit: ${STAGE} (region ${REGION}) ================"
aws sts get-caller-identity >/dev/null 2>&1 || { echo "❌ AWS session invalid — run: aws login"; exit 1; }

echo "[identity]"; aws sts get-caller-identity --query '{acct:Account,arn:Arn}' --output text

echo "[S3 bucket]"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then ok "$BUCKET" "exists"; B_OK=1; else miss "$BUCKET" "MISSING"; B_OK=0; fi

echo "[DynamoDB tables]"
for k in items reports audit; do
  if aws dynamodb describe-table --table-name "${TBL[$k]}" --region "$REGION" >/dev/null 2>&1; then
    ok "${TBL[$k]}" "exists"; else miss "${TBL[$k]}" "MISSING"; fi
done

echo "[OIDC provider for GitHub Actions]"
if aws iam list-open-id-connect-providers --query "OpenIDConnectProviderList[?contains(Arn,'token.actions.githubusercontent.com')].Arn" --output text 2>/dev/null | grep -q token.actions; then
  ok "github OIDC provider" "present"; else miss "github OIDC provider" "create it (see runbook)"; fi

echo "[CloudFormation stack]"
ST=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "NONE")
[ "$ST" = "NONE" ] && miss "$STACK" "not deployed yet (CI will create on first push)" || ok "$STACK" "$ST"

echo
echo "================ what this env needs ================"
printf "  %-22s %s\n" "S3 bucket"        "$BUCKET"
printf "  %-22s %s\n" "DynamoDB tables"  "${TBL[items]}, ${TBL[reports]}, ${TBL[audit]}"
printf "  %-22s %s\n" "SAM stack"        "$STACK"
printf "  %-22s %s\n" "S3 events"        "wired by scripts/wire-s3-events.sh after deploy"
echo

if [ "$CREATE" != "--create" ]; then
  echo "(read-only audit. Re-run with --create to provision the TEST data plane.)"; exit 0
fi
if [ "$STAGE" != "test" ]; then
  echo "❌ refusing to --create on prod. Prod resources are managed deliberately."; exit 1
fi

echo "================ --create (TEST) ================"
if [ "${B_OK:-0}" != "1" ]; then
  echo "creating bucket $BUCKET ..."
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION"
  aws s3api put-public-access-block --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  ok "$BUCKET" "created"
fi
# Mirror each PROD table's schema into the TEST table (PAY_PER_REQUEST).
for k in items reports audit; do
  TEST_T="${TBL[$k]}"; PROD_T="fieldsight-${k}"
  if aws dynamodb describe-table --table-name "$TEST_T" --region "$REGION" >/dev/null 2>&1; then ok "$TEST_T" "already exists"; continue; fi
  SCHEMA=$(aws dynamodb describe-table --table-name "$PROD_T" --region "$REGION" \
            --query 'Table.{KeySchema:KeySchema,AttributeDefinitions:AttributeDefinitions}' --output json 2>/dev/null || echo "")
  if [ -z "$SCHEMA" ]; then
    echo "  ⚠️  prod table $PROD_T not found — creating $TEST_T with generic PK/SK"
    aws dynamodb create-table --table-name "$TEST_T" --region "$REGION" --billing-mode PAY_PER_REQUEST \
      --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
      --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE >/dev/null
  else
    KS=$(echo "$SCHEMA"  | jq -c '.KeySchema'); AD=$(echo "$SCHEMA" | jq -c '.AttributeDefinitions')
    aws dynamodb create-table --table-name "$TEST_T" --region "$REGION" --billing-mode PAY_PER_REQUEST \
      --key-schema "$KS" --attribute-definitions "$AD" >/dev/null
  fi
  ok "$TEST_T" "created (mirrors $PROD_T)"
done
echo "✅ TEST data plane ready."
