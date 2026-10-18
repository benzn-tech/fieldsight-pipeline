#!/usr/bin/env bash
#
# fix-sam-deploy-role.sh
# ----------------------
# One-time fix for the failed TEST deploy.
#
# Root cause (confirmed from CloudFormation stack events):
#   The GitHub OIDC deploy role `github-actions-fieldsight-deploy` lacks the S3
#   permissions SAM needs to create its managed artifact bucket, so the helper
#   stack `aws-sam-cli-managed-default` is stuck:
#     - s3:PutEncryptionConfiguration  -> bucket CREATE_FAILED
#     - s3:DeleteBucket                -> rollback DELETE_FAILED (stuck)
#
# This script (idempotent, safe to re-run):
#   1. grants the role a SCOPED policy (only aws-sam-cli-managed-default-* buckets)
#   2. shows the latest failure reason for the record
#   3. deletes the stuck helper stack so SAM can recreate it cleanly next run
#
# HOW TO RUN (AWS CloudShell, top-right region = Sydney / ap-southeast-2):
#   1. Open CloudShell.  Actions -> Upload file -> pick this script.
#   2. bash fix-sam-deploy-role.sh
#   Requires an account-admin identity (your normal console login is fine).
#
set -euo pipefail

REGION="ap-southeast-2"
ROLE="github-actions-fieldsight-deploy"
STUCK_STACK="aws-sam-cli-managed-default"

echo "==> Identity / account check (must be account 509194952652)"
aws sts get-caller-identity --output table

echo
echo "==> 1/3  Grant scoped SAM managed-bucket S3 permissions to: $ROLE"
aws iam put-role-policy \
  --role-name "$ROLE" \
  --policy-name sam-managed-bucket-s3 \
  --policy-document '{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SamManagedArtifactBucket",
      "Effect": "Allow",
      "Action": [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:PutBucketPolicy",
        "s3:GetBucketPolicy",
        "s3:PutEncryptionConfiguration",
        "s3:PutBucketVersioning",
        "s3:PutBucketTagging",
        "s3:PutBucketPublicAccessBlock",
        "s3:GetBucketLocation",
        "s3:ListBucket",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::aws-sam-cli-managed-default-*",
        "arn:aws:s3:::aws-sam-cli-managed-default-*/*"
      ]
    }
  ]
}'
echo "    inline policy 'sam-managed-bucket-s3' attached."

echo
echo "==> 2/3  Latest failure reason on $STUCK_STACK (for the record)"
aws cloudformation describe-stack-events \
  --stack-name "$STUCK_STACK" --region "$REGION" \
  --query "reverse(StackEvents[?contains(ResourceStatus, 'FAILED')].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason])" \
  --output table 2>/dev/null || echo "    (stack not found - nothing stuck)"

echo
echo "==> 3/3  Delete the stuck helper stack so SAM rebuilds it cleanly"
if aws cloudformation describe-stacks --stack-name "$STUCK_STACK" --region "$REGION" >/dev/null 2>&1; then
  aws cloudformation delete-stack --stack-name "$STUCK_STACK" --region "$REGION"
  echo "    delete requested; waiting..."
  if aws cloudformation wait stack-delete-complete --stack-name "$STUCK_STACK" --region "$REGION"; then
    echo "    stack deleted."
  else
    echo "    WARN: delete did not complete - open CloudFormation console and check $STUCK_STACK"
  fi
else
  echo "    stack already gone - nothing to delete."
fi

echo
echo "==> DONE."
echo "    Next: tell Claude 'fixed, re-run' (gh run rerun), or push any commit to develop."
