#!/bin/bash
# =============================================================================
# setup-test-env.sh — Duplicate prod user data into test environment
#
# Prerequisites:
#   - AWS CLI configured with correct credentials
#   - Test SAM stack already deployed (Cognito pool + DynamoDB table created)
#   - Test S3 bucket already created
#
# Usage:
#   bash scripts/setup-test-env.sh \
#     --test-pool-id ap-southeast-2_XXXXXXX \
#     --test-table fieldsight-users-test \
#     --test-bucket fieldsight-data-test-509194952652 \
#     --temp-password "FieldSight2026!"
# =============================================================================
set -euo pipefail

REGION="ap-southeast-2"
PROD_BUCKET="fieldsight-data-509194952652"

# --- Parse arguments ---
TEST_POOL_ID=""
TEST_TABLE="fieldsight-users-test"
TEST_BUCKET="fieldsight-data-test-509194952652"
TEMP_PASSWORD="FieldSight2026!"

while [[ $# -gt 0 ]]; do
  case $1 in
    --test-pool-id)  TEST_POOL_ID="$2";  shift 2 ;;
    --test-table)    TEST_TABLE="$2";     shift 2 ;;
    --test-bucket)   TEST_BUCKET="$2";    shift 2 ;;
    --temp-password) TEMP_PASSWORD="$2";  shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [ -z "$TEST_POOL_ID" ]; then
  echo "ERROR: --test-pool-id is required"
  echo ""
  echo "To find your test pool ID, run:"
  echo "  aws cognito-idp list-user-pools --max-results 20 --region $REGION"
  echo "  # or check your SAM stack outputs:"
  echo "  aws cloudformation describe-stacks --stack-name fieldsight-test --query 'Stacks[0].Outputs' --region $REGION"
  exit 1
fi

echo "========================================="
echo "FieldSight Test Environment Setup"
echo "========================================="
echo "Region:        $REGION"
echo "Test Pool:     $TEST_POOL_ID"
echo "Test Table:    $TEST_TABLE"
echo "Test Bucket:   $TEST_BUCKET"
echo "Prod Bucket:   $PROD_BUCKET"
echo "========================================="
echo ""

# --- Prod user data (hardcoded from export) ---
# Format: email|name|role|device_id|sites(comma-sep)|managed_sites(comma-sep)
USERS=(
  "benlin.chch+jt@gmail.com|Jarley Trainor|site_manager|Benl1|sb1108-ellesmere|sb1108-ellesmere"
  "benlin.chch+db@gmail.com|David Barillaro|site_manager|Benl3|sb1108-ellesmere|sb1108-ellesmere"
  "benl.tech@outlook.com|Ben Lin|admin||sb1108-ellesmere,mpi,sb1131-northbrook-wanaka|sb1108-ellesmere,mpi,sb1131-northbrook-wanaka"
)

# =============================================================================
# STEP 1: Create test DynamoDB table (if not exists)
# =============================================================================
echo "[Step 1] Checking DynamoDB table: $TEST_TABLE"

if aws dynamodb describe-table --table-name "$TEST_TABLE" --region "$REGION" >/dev/null 2>&1; then
  echo "  Table already exists, skipping creation."
else
  echo "  Creating table..."
  aws dynamodb create-table \
    --table-name "$TEST_TABLE" \
    --attribute-definitions \
      AttributeName=PK,AttributeType=S \
      AttributeName=SK,AttributeType=S \
    --key-schema \
      AttributeName=PK,KeyType=HASH \
      AttributeName=SK,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --region "$REGION"

  echo "  Waiting for table to become active..."
  aws dynamodb wait table-exists --table-name "$TEST_TABLE" --region "$REGION"
  echo "  Table created."
fi
echo ""

# =============================================================================
# STEP 2 & 3: Create Cognito users → get new sub → write DynamoDB profiles
# =============================================================================
echo "[Step 2-3] Creating Cognito users and DynamoDB profiles"
echo ""

for user_data in "${USERS[@]}"; do
  IFS='|' read -r email name role device_id sites managed_sites <<< "$user_data"

  echo "  --- $name ($email) ---"

  # Check if user already exists in test pool
  existing=$(aws cognito-idp list-users \
    --user-pool-id "$TEST_POOL_ID" \
    --filter "email = \"$email\"" \
    --region "$REGION" \
    --query 'Users[0].Username' \
    --output text 2>/dev/null || echo "None")

  if [ "$existing" != "None" ] && [ "$existing" != "" ]; then
    NEW_SUB="$existing"
    echo "  Cognito: already exists (sub: $NEW_SUB)"
  else
    # Create user with temporary password
    aws cognito-idp admin-create-user \
      --user-pool-id "$TEST_POOL_ID" \
      --username "$email" \
      --user-attributes \
        Name=email,Value="$email" \
        Name=email_verified,Value=true \
        Name=name,Value="$name" \
      --temporary-password "$TEMP_PASSWORD" \
      --message-action SUPPRESS \
      --region "$REGION" >/dev/null

    # Set permanent password (skip force-change-password on first login)
    aws cognito-idp admin-set-user-password \
      --user-pool-id "$TEST_POOL_ID" \
      --username "$email" \
      --password "$TEMP_PASSWORD" \
      --permanent \
      --region "$REGION"

    # Get the new sub
    NEW_SUB=$(aws cognito-idp admin-get-user \
      --user-pool-id "$TEST_POOL_ID" \
      --username "$email" \
      --region "$REGION" \
      --query "UserAttributes[?Name=='sub'].Value" \
      --output text)

    echo "  Cognito: created (sub: $NEW_SUB)"
  fi

  # Build DynamoDB sites list JSON
  sites_json="["
  IFS=',' read -ra site_arr <<< "$sites"
  for i in "${!site_arr[@]}"; do
    [ $i -gt 0 ] && sites_json+=","
    sites_json+="{\"S\":\"${site_arr[$i]}\"}"
  done
  sites_json+="]"

  # Build managed_sites list JSON
  managed_json="["
  IFS=',' read -ra msite_arr <<< "$managed_sites"
  for i in "${!msite_arr[@]}"; do
    [ $i -gt 0 ] && managed_json+=","
    managed_json+="{\"S\":\"${msite_arr[$i]}\"}"
  done
  managed_json+="]"

  # Write DynamoDB profile with NEW sub
  aws dynamodb put-item \
    --table-name "$TEST_TABLE" \
    --item "{
      \"PK\": {\"S\": \"USER#${NEW_SUB}\"},
      \"SK\": {\"S\": \"PROFILE\"},
      \"email\": {\"S\": \"$email\"},
      \"display_name\": {\"S\": \"$name\"},
      \"role\": {\"S\": \"$role\"},
      \"device_id\": {\"S\": \"$device_id\"},
      \"sites\": {\"L\": $sites_json},
      \"managed_sites\": {\"L\": $managed_json}
    }" \
    --region "$REGION"

  echo "  DynamoDB: profile written (PK: USER#${NEW_SUB})"
  echo ""
done

# =============================================================================
# STEP 4: Sync S3 buckets (prod → test)
# =============================================================================
echo "[Step 4] Syncing S3: $PROD_BUCKET → $TEST_BUCKET"
echo ""
echo "  This may take a while depending on bucket size."
echo "  Starting sync..."
echo ""

aws s3 sync \
  "s3://$PROD_BUCKET" \
  "s3://$TEST_BUCKET" \
  --region "$REGION"

echo ""
echo "========================================="
echo "Setup complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Update your test SAM stack parameters:"
echo "     - UsersTableName=$TEST_TABLE"
echo "     - DataBucketName=$TEST_BUCKET"
echo "  2. First login: use email + password '$TEMP_PASSWORD'"
echo "  3. Update frontend config to point to test API + Cognito pool"
echo ""
echo "Test users created:"
for user_data in "${USERS[@]}"; do
  IFS='|' read -r email name role _ _ _ <<< "$user_data"
  echo "  $name ($role): $email"
done
