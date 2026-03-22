#!/bin/bash
# ============================================================
# Deploy Lambda 3b: Transcribe Callback + Update Lambda 3
# 
# Creates:
#   1. DynamoDB table: fieldsight-transcripts (ledger)
#   2. IAM role: sitesync-transcribe-callback-role
#   3. Lambda function: sitesync-transcribe-callback
#   4. EventBridge rule: sitesync-transcribe-state-change
#   5. Updates sitesync-transcribe Lambda (env var + IAM + code)
#
# Prerequisites:
#   - lambda_transcribe_callback.py in ~ or current dir
#   - lambda_transcribe.py (v1.3) in ~ or current dir
#
# Run: bash deploy_transcribe_callback.sh
# ============================================================

set -e

REGION="ap-southeast-2"
ACCOUNT_ID="509194952652"
S3_BUCKET="fieldsight-data-509194952652"

# Callback Lambda (new)
CB_FUNCTION_NAME="sitesync-transcribe-callback"
CB_ROLE_NAME="sitesync-transcribe-callback-role"
RULE_NAME="sitesync-transcribe-state-change"

# Existing Transcribe Lambda (update)
TR_FUNCTION_NAME="realptt-transcribe"

# Shared
TABLE_NAME="fieldsight-transcripts"

echo "============================================================"
echo "Deploying Lambda 3b + Updating Lambda 3"
echo "Region: ${REGION} | Account: ${ACCOUNT_ID}"
echo "============================================================"

# ============================================================
# Step 1: Create DynamoDB table
# ============================================================
echo ""
echo ">>> Step 1: DynamoDB table: ${TABLE_NAME}"

if aws dynamodb describe-table --table-name ${TABLE_NAME} --region ${REGION} 2>/dev/null; then
    echo "  Already exists, skipping"
else
    aws dynamodb create-table \
        --table-name ${TABLE_NAME} \
        --billing-mode PAY_PER_REQUEST \
        --attribute-definitions \
            AttributeName=PK,AttributeType=S \
            AttributeName=SK,AttributeType=S \
        --key-schema \
            AttributeName=PK,KeyType=HASH \
            AttributeName=SK,KeyType=RANGE \
        --region ${REGION}

    echo "  Waiting for ACTIVE..."
    aws dynamodb wait table-exists --table-name ${TABLE_NAME} --region ${REGION}
    echo "  ✅ Created"
fi

# ============================================================
# Step 2: Create IAM Role for Callback Lambda
# ============================================================
echo ""
echo ">>> Step 2: IAM role: ${CB_ROLE_NAME}"

TRUST_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }
  ]
}'

if aws iam get-role --role-name ${CB_ROLE_NAME} 2>/dev/null; then
    echo "  Already exists"
else
    aws iam create-role \
        --role-name ${CB_ROLE_NAME} \
        --assume-role-policy-document "${TRUST_POLICY}" \
        --description "SiteSync Transcribe Callback Lambda" \
        --query 'Role.Arn' --output text
    echo "  ✅ Created"
    sleep 10
fi

CB_ROLE_ARN=$(aws iam get-role --role-name ${CB_ROLE_NAME} --query 'Role.Arn' --output text)

aws iam attach-role-policy \
    --role-name ${CB_ROLE_NAME} \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
    2>/dev/null || true

CALLBACK_POLICY='{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDBLedger",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query"
      ],
      "Resource": "arn:aws:dynamodb:'${REGION}':'${ACCOUNT_ID}':table/'${TABLE_NAME}'"
    },
    {
      "Sid": "TranscribeRead",
      "Effect": "Allow",
      "Action": ["transcribe:GetTranscriptionJob"],
      "Resource": "*"
    }
  ]
}'

aws iam put-role-policy \
    --role-name ${CB_ROLE_NAME} \
    --policy-name "TranscribeCallbackAccess" \
    --policy-document "${CALLBACK_POLICY}"

echo "  ✅ Policies attached"

# ============================================================
# Step 3: Deploy Callback Lambda
# ============================================================
echo ""
echo ">>> Step 3: Lambda: ${CB_FUNCTION_NAME}"

cd /tmp && mkdir -p deploy_cb && cd deploy_cb

CB_SRC=""
for p in ~/lambda_transcribe_callback.py \
         /home/cloudshell-user/lambda_transcribe_callback.py \
         ./lambda_transcribe_callback.py; do
    if [ -f "$p" ]; then CB_SRC="$p"; break; fi
done

if [ -z "$CB_SRC" ]; then
    echo "  ⚠️  lambda_transcribe_callback.py not found!"
    echo "  Upload it to CloudShell home directory first."
    exit 1
fi

cp "$CB_SRC" ./lambda_transcribe_callback.py
zip -j callback_deploy.zip lambda_transcribe_callback.py

if aws lambda get-function --function-name ${CB_FUNCTION_NAME} --region ${REGION} 2>/dev/null; then
    echo "  Updating existing function..."
    aws lambda update-function-code \
        --function-name ${CB_FUNCTION_NAME} \
        --zip-file fileb://callback_deploy.zip \
        --region ${REGION} > /dev/null

    aws lambda wait function-updated-v2 --function-name ${CB_FUNCTION_NAME} --region ${REGION} 2>/dev/null || sleep 5

    aws lambda update-function-configuration \
        --function-name ${CB_FUNCTION_NAME} \
        --environment "Variables={TRANSCRIPT_TABLE=${TABLE_NAME},MAX_ATTEMPTS=3,S3_BUCKET=${S3_BUCKET}}" \
        --region ${REGION} > /dev/null
else
    aws lambda create-function \
        --function-name ${CB_FUNCTION_NAME} \
        --runtime python3.12 \
        --handler lambda_transcribe_callback.lambda_handler \
        --role ${CB_ROLE_ARN} \
        --zip-file fileb://callback_deploy.zip \
        --timeout 30 \
        --memory-size 128 \
        --environment "Variables={TRANSCRIPT_TABLE=${TABLE_NAME},MAX_ATTEMPTS=3,S3_BUCKET=${S3_BUCKET}}" \
        --region ${REGION} > /dev/null
fi

CB_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${CB_FUNCTION_NAME}"
echo "  ✅ Deployed: ${CB_FUNCTION_NAME}"

# ============================================================
# Step 4: EventBridge Rule
# ============================================================
echo ""
echo ">>> Step 4: EventBridge rule: ${RULE_NAME}"

EVENT_PATTERN='{
  "source": ["aws.transcribe"],
  "detail-type": ["Transcribe Job State Change"],
  "detail": {
    "TranscriptionJobStatus": ["COMPLETED", "FAILED"]
  }
}'

RULE_ARN=$(aws events put-rule \
    --name ${RULE_NAME} \
    --event-pattern "${EVENT_PATTERN}" \
    --state ENABLED \
    --description "Trigger callback on Transcribe job completion/failure" \
    --region ${REGION} \
    --query 'RuleArn' --output text)

aws lambda add-permission \
    --function-name ${CB_FUNCTION_NAME} \
    --statement-id "EventBridgeTranscribeCallback" \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn ${RULE_ARN} \
    --region ${REGION} \
    2>/dev/null || true

aws events put-targets \
    --rule ${RULE_NAME} \
    --targets "Id=TranscribeCallbackTarget,Arn=${CB_LAMBDA_ARN}" \
    --region ${REGION} > /dev/null

echo "  ✅ Rule → ${CB_FUNCTION_NAME}"

# ============================================================
# Step 5: Update existing Transcribe Lambda
# ============================================================
echo ""
echo ">>> Step 5: Update ${TR_FUNCTION_NAME} (IAM + env + code)"

TR_ROLE_ARN=$(aws lambda get-function-configuration \
    --function-name ${TR_FUNCTION_NAME} \
    --region ${REGION} \
    --query 'Role' --output text 2>/dev/null)

if [ -z "$TR_ROLE_ARN" ] || [ "$TR_ROLE_ARN" = "None" ]; then
    echo "  ⚠️  Cannot find ${TR_FUNCTION_NAME} — skipping"
else
    TR_ROLE_NAME=$(echo ${TR_ROLE_ARN} | awk -F'/' '{print $NF}')
    echo "  Role: ${TR_ROLE_NAME}"

    # 5a. Add DynamoDB PutItem permission
    TR_DYNAMO_POLICY='{
      "Version": "2012-10-17",
      "Statement": [
        {
          "Sid": "DynamoDBTranscriptLedgerWrite",
          "Effect": "Allow",
          "Action": ["dynamodb:PutItem"],
          "Resource": "arn:aws:dynamodb:'${REGION}':'${ACCOUNT_ID}':table/'${TABLE_NAME}'"
        }
      ]
    }'

    aws iam put-role-policy \
        --role-name ${TR_ROLE_NAME} \
        --policy-name "TranscriptLedgerWrite" \
        --policy-document "${TR_DYNAMO_POLICY}"

    echo "  ✅ IAM: DynamoDB PutItem on ${TABLE_NAME}"

    # 5b. Add TRANSCRIPT_TABLE env var (preserve existing)
    EXISTING_ENV=$(aws lambda get-function-configuration \
        --function-name ${TR_FUNCTION_NAME} \
        --region ${REGION} \
        --query 'Environment.Variables' --output json 2>/dev/null)

    if [ "$EXISTING_ENV" = "null" ] || [ -z "$EXISTING_ENV" ]; then
        EXISTING_ENV='{}'
    fi

    MERGED_ENV=$(echo "$EXISTING_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)
env['TRANSCRIPT_TABLE'] = '${TABLE_NAME}'
print(json.dumps({'Variables': env}))
")

    aws lambda update-function-configuration \
        --function-name ${TR_FUNCTION_NAME} \
        --environment "${MERGED_ENV}" \
        --region ${REGION} > /dev/null

    echo "  ✅ Env: TRANSCRIPT_TABLE=${TABLE_NAME}"

    # 5c. Update code if available
    TR_SRC=""
    for p in ~/lambda_transcribe.py \
             /home/cloudshell-user/lambda_transcribe.py \
             ./lambda_transcribe.py; do
        if [ -f "$p" ]; then TR_SRC="$p"; break; fi
    done

    if [ -n "$TR_SRC" ]; then
        aws lambda wait function-updated-v2 --function-name ${TR_FUNCTION_NAME} --region ${REGION} 2>/dev/null || sleep 5

        cd /tmp && mkdir -p deploy_tr && cd deploy_tr
        cp "$TR_SRC" ./lambda_transcribe.py
        zip -j transcribe_deploy.zip lambda_transcribe.py

        aws lambda update-function-code \
            --function-name ${TR_FUNCTION_NAME} \
            --zip-file fileb://transcribe_deploy.zip \
            --region ${REGION} > /dev/null

        echo "  ✅ Code: v1.3 (date folders + ledger writes)"
    else
        echo "  ⚠️  lambda_transcribe.py not found — code not updated"
    fi
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "============================================================"
echo "✅ All Done"
echo "============================================================"
echo ""
echo "  S3 audio upload"
echo "       │"
echo "       ▼"
echo "  ${TR_FUNCTION_NAME} (v1.3)"
echo "       ├──→ Transcribe: StartTranscriptionJob"
echo "       └──→ DynamoDB ${TABLE_NAME}: status=transcribing"
echo "                │"
echo "                │ (async)"
echo "                ▼"
echo "  EventBridge: ${RULE_NAME}"
echo "       │"
echo "       ▼"
echo "  ${CB_FUNCTION_NAME}"
echo "       └──→ DynamoDB ${TABLE_NAME}: status=pending/retry/abandoned"
echo ""
echo "Test:"
echo "  aws lambda invoke --function-name ${CB_FUNCTION_NAME} \\"
echo "    --payload '{\"source\":\"aws.transcribe\",\"detail-type\":\"Transcribe Job State Change\",\"detail\":{\"TranscriptionJobName\":\"realptt_test\",\"TranscriptionJobStatus\":\"COMPLETED\"}}' \\"
echo "    --cli-binary-format raw-in-base64-out /tmp/cb_test.json --region ${REGION}"
echo "============================================================"
