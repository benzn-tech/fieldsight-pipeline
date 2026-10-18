#!/usr/bin/env bash
#
# aws-inventory.sh  —  READ-ONLY deployment health check / inventory.
# ---------------------------------------------------------------------------
# Dumps what is currently deployed in THIS AWS account so we can see the real
# state: which Lambdas/API/tables/buckets/schedules exist, how they're wired
# (S3 event notifications, Cognito, schedules enabled/disabled, Amplify, ECS).
#
# 100% read-only: only list-* / describe-* / get-* calls. Nothing is created,
# changed, or deleted. Secret VALUES are never printed (env-var KEYS only).
#
# HOW TO RUN (AWS CloudShell):
#   1. Open CloudShell in the account you want to inspect.
#   2. Actions -> Upload file -> pick this script (or paste it into a new file).
#   3. Run, capturing output so you can paste it back to me:
#        REGION=ap-southeast-2 bash aws-inventory.sh | tee fieldsight-inventory.txt
#   4. Upload / paste fieldsight-inventory.txt back here.
#
# Override region if needed:  REGION=us-east-1 bash aws-inventory.sh
# ---------------------------------------------------------------------------

REGION="${REGION:-ap-southeast-2}"
FILTER="${FILTER:-fieldsight}"   # name substring used to spotlight relevant resources

hr()  { printf '\n========================================================================\n'; }
sec() { hr; printf '### %s\n' "$1"; printf '   region=%s  filter=%s\n' "$REGION" "$FILTER"; }
run() { echo "+ $*"; "$@" 2>&1 || echo "   (call failed / no access / not present)"; }

sec "0. IDENTITY"
run aws sts get-caller-identity --output table

sec "1. CLOUDFORMATION STACKS (all)"
run aws cloudformation list-stacks --region "$REGION" \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE \
    UPDATE_ROLLBACK_COMPLETE CREATE_FAILED ROLLBACK_FAILED UPDATE_ROLLBACK_FAILED \
    ROLLBACK_IN_PROGRESS REVIEW_IN_PROGRESS \
  --query "StackSummaries[].[StackName,StackStatus,CreationTime]" --output table

sec "1b. STACK OUTPUTS (anything matching filter)"
for s in $(aws cloudformation list-stacks --region "$REGION" \
            --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].StackName" \
            --output text 2>/dev/null | tr '\t' '\n' | grep -i "$FILTER"); do
  echo "--- outputs: $s"
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$s" \
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table 2>&1 \
    || echo "   (no outputs)"
done

sec "2. LAMBDA FUNCTIONS (all: name, runtime, lastModified, handler, memory)"
run aws lambda list-functions --region "$REGION" \
  --query "Functions[].[FunctionName,Runtime,LastModified,Handler,MemorySize]" --output table

sec "2b. LAMBDA ENV-VAR KEYS + role (filter only; VALUES hidden)"
for fn in $(aws lambda list-functions --region "$REGION" \
             --query "Functions[].FunctionName" --output text 2>/dev/null \
             | tr '\t' '\n' | grep -i "$FILTER"); do
  echo "--- $fn"
  aws lambda get-function-configuration --region "$REGION" --function-name "$fn" \
    --query "{Role:Role, EnvKeys:keys(Environment.Variables), Timeout:Timeout, LastModified:LastModified}" \
    --output json 2>&1 || echo "   (cannot read)"
done

sec "3. API GATEWAY (REST APIs + stages)"
run aws apigateway get-rest-apis --region "$REGION" \
  --query "items[].[name,id,createdDate]" --output table
for api in $(aws apigateway get-rest-apis --region "$REGION" \
              --query "items[].id" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- API $api stages:"
  aws apigateway get-stages --region "$REGION" --rest-api-id "$api" \
    --query "item[].[stageName,deploymentId,lastUpdatedDate]" --output table 2>&1 || true
done
echo "--- HTTP APIs (apigatewayv2):"
run aws apigatewayv2 get-apis --region "$REGION" \
  --query "Items[].[Name,ApiId,ApiEndpoint,ProtocolType]" --output table

sec "4. DYNAMODB TABLES (key schema + approx item count)"
for t in $(aws dynamodb list-tables --region "$REGION" \
            --query "TableNames[]" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- $t"
  aws dynamodb describe-table --region "$REGION" --table-name "$t" \
    --query "{Items:Table.ItemCount, Keys:Table.KeySchema, GSIs:Table.GlobalSecondaryIndexes[].IndexName, Status:Table.TableStatus, SizeBytes:Table.TableSizeBytes}" \
    --output json 2>&1 || true
done

sec "5. S3 BUCKETS (all names)"
run aws s3api list-buckets --query "Buckets[].Name" --output table

sec "5b. S3 EVENT NOTIFICATIONS (filter buckets — shows VAD/Transcribe wiring)"
for b in $(aws s3api list-buckets --query "Buckets[].Name" --output text 2>/dev/null \
            | tr '\t' '\n' | grep -i "$FILTER"); do
  echo "--- notifications on: $b"
  aws s3api get-bucket-notification-configuration --bucket "$b" --output json 2>&1 \
    || echo "   (none / no access)"
done

sec "6. EVENTBRIDGE / SCHEDULES (rule, schedule expr, ENABLED/DISABLED, target)"
for r in $(aws events list-rules --region "$REGION" \
            --query "Rules[].Name" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- rule: $r"
  aws events describe-rule --region "$REGION" --name "$r" \
    --query "{Schedule:ScheduleExpression, State:State, Desc:Description}" --output json 2>&1 || true
  aws events list-targets-by-rule --region "$REGION" --rule "$r" \
    --query "Targets[].Arn" --output text 2>&1 || true
done
echo "--- EventBridge Scheduler (newer service):"
run aws scheduler list-schedules --region "$REGION" \
  --query "Schedules[].[Name,State,GroupName]" --output table

sec "7. COGNITO USER POOLS + APP CLIENTS"
for p in $(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
            --query "UserPools[].Id" --output text 2>/dev/null | tr '\t' '\n'); do
  name=$(aws cognito-idp describe-user-pool --user-pool-id "$p" --region "$REGION" \
          --query "UserPool.Name" --output text 2>/dev/null)
  echo "--- pool: $p  ($name)"
  aws cognito-idp list-user-pool-clients --user-pool-id "$p" --region "$REGION" \
    --query "UserPoolClients[].[ClientName,ClientId]" --output table 2>&1 || true
done

sec "8. ECS / FARGATE (clusters, services, task defs — transcription worker)"
run aws ecs list-clusters --region "$REGION" --query "clusterArns" --output table
for c in $(aws ecs list-clusters --region "$REGION" --query "clusterArns" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- services in $c:"
  aws ecs list-services --cluster "$c" --region "$REGION" --query "serviceArns" --output text 2>&1 || true
done
echo "--- task definition families:"
run aws ecs list-task-definition-families --region "$REGION" --status ACTIVE --query "families" --output table

sec "9. STEP FUNCTIONS"
run aws stepfunctions list-state-machines --region "$REGION" \
  --query "stateMachines[].[name,stateMachineArn]" --output table

sec "10. SNS TOPICS / SQS QUEUES"
run aws sns list-topics --region "$REGION" --query "Topics[].TopicArn" --output table
run aws sqs list-queues --region "$REGION" --query "QueueUrls" --output table

sec "11. CLOUDWATCH ALARMS"
run aws cloudwatch describe-alarms --region "$REGION" \
  --query "MetricAlarms[].[AlarmName,StateValue,MetricName]" --output table

sec "12. AMPLIFY APPS (frontend hosting — may be empty on this account)"
run aws amplify list-apps --region "$REGION" \
  --query "apps[].[name,appId,defaultDomain,repository]" --output table
for a in $(aws amplify list-apps --region "$REGION" --query "apps[].appId" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- branches of app $a:"
  aws amplify list-branches --app-id "$a" --region "$REGION" \
    --query "branches[].[branchName,stage,enableAutoBuild]" --output table 2>&1 || true
done

sec "13. CLOUDFRONT DISTRIBUTIONS (UI delivery)"
run aws cloudfront list-distributions \
  --query "DistributionList.Items[].[Id,DomainName,Origins.Items[0].DomainName,Status]" --output table

sec "14. IAM ROLES matching filter (deploy/OIDC + lambda roles)"
run aws iam list-roles \
  --query "Roles[?contains(RoleName,'$FILTER') || contains(RoleName,'github') || contains(RoleName,'lambda')].[RoleName,CreateDate]" \
  --output table

hr
echo "INVENTORY COMPLETE.  Paste fieldsight-inventory.txt back to Claude."
