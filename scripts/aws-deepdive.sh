#!/usr/bin/env bash
#
# aws-deepdive.sh  —  READ-ONLY deep dive on the company fieldsightai account.
# ---------------------------------------------------------------------------
# Fills the gaps from aws-inventory.sh and DIAGNOSES the ingest-video DLQ alarm.
# Values are hardcoded from the prior inventory (account 164088480050).
#
# 100% read-only / non-destructive:
#   - only list-*/describe-*/get-* + CloudWatch Logs read
#   - the DLQ "peek" uses receive-message with --visibility-timeout 0, which
#     does NOT delete or hide messages (they stay in the queue untouched)
#
# HOW TO RUN (AWS CloudShell, company SSO):
#   sed -i 's/\r$//' aws-deepdive.sh
#   bash aws-deepdive.sh | tee fieldsight-deepdive.txt
#   then paste / upload fieldsight-deepdive.txt back to me.
# ---------------------------------------------------------------------------

REGION="ap-southeast-2"
APP_ID="d19u3207g5s0sc"
BUCKETS="fsai-videos fsai-media fsai-transcripts"
STACKS="fieldsightai-lambdas FieldSightAIStack"
LAMBDAS="fieldsightai-ingest-video fieldsightai-trigger-transcription fieldsightai-process-transcript fieldsightai-export-to-safebase fieldsightai-export-to-aconex"
INGEST_DLQ="https://sqs.ap-southeast-2.amazonaws.com/164088480050/fieldsightai-ingest-video-dlq"

hr()  { printf '\n========================================================================\n'; }
sec() { hr; printf '### %s\n\n' "$1"; }
run() { echo "+ $*"; "$@" 2>&1 || echo "   (call failed / no access / not present)"; echo; }

sec "A. FULL STACK RESOURCES (what CDK actually created — RDS? AppSync? S3 notifs?)"
for s in $STACKS; do
  echo "--- resources in stack: $s"
  aws cloudformation list-stack-resources --region "$REGION" --stack-name "$s" \
    --query "StackResourceSummaries[].[ResourceType,LogicalResourceId,PhysicalResourceId]" \
    --output table 2>&1 || echo "   (cannot read)"
  echo
done

sec "B. RDS / AURORA (the DB behind DB_CONNECTION_STRING)"
run aws rds describe-db-instances --region "$REGION" \
  --query "DBInstances[].[DBInstanceIdentifier,Engine,EngineVersion,DBInstanceClass,Endpoint.Address,Endpoint.Port,MultiAZ,DBInstanceStatus]" --output table
run aws rds describe-db-clusters --region "$REGION" \
  --query "DBClusters[].[DBClusterIdentifier,Engine,EngineVersion,Endpoint,Status]" --output table

sec "C. APPSYNC GRAPHQL (does the frontend use AppSync?)"
run aws appsync list-graphql-apis --region "$REGION" \
  --query "graphqlApis[].[name,apiId,authenticationType,uris]" --output json

sec "D. SECRETS MANAGER + SSM PARAMS (names only — NO values)"
run aws secretsmanager list-secrets --region "$REGION" \
  --query "SecretList[].[Name,Description,LastChangedDate]" --output table
run aws ssm describe-parameters --region "$REGION" \
  --query "Parameters[].[Name,Type,LastModifiedDate]" --output table

sec "E. AMPLIFY APP DETAIL (what the frontend connects to + build spec)"
echo "--- app-level config + env vars:"
aws amplify get-app --app-id "$APP_ID" --region "$REGION" \
  --query "app.{Name:name, Repo:repository, Platform:platform, EnvVars:environmentVariables, CustomRules:customRules}" \
  --output json 2>&1 || echo "   (cannot read)"
echo
echo "--- build spec:"
aws amplify get-app --app-id "$APP_ID" --region "$REGION" --query "app.buildSpec" --output text 2>&1 || true
echo
for br in dev main; do
  echo "--- branch '$br' env vars + framework:"
  aws amplify get-branch --app-id "$APP_ID" --branch-name "$br" --region "$REGION" \
    --query "branch.{Stage:stage, Framework:framework, EnvVars:environmentVariables, AutoBuild:enableAutoBuild}" \
    --output json 2>&1 || echo "   (cannot read)"
  echo
done

sec "F. S3 EVENT NOTIFICATIONS on fsai buckets (the real ingest/transcribe wiring)"
for b in $BUCKETS; do
  echo "--- notifications on: $b"
  aws s3api get-bucket-notification-configuration --bucket "$b" --output json 2>&1 \
    || echo "   (none / no access)"
  echo
done

sec "G. EVENTBRIDGE RULE *PATTERNS* (how the null-schedule rules actually fire)"
for r in $(aws events list-rules --region "$REGION" --query "Rules[].Name" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- rule: $r"
  aws events describe-rule --region "$REGION" --name "$r" \
    --query "{Schedule:ScheduleExpression, State:State, EventPattern:EventPattern, Desc:Description}" --output json 2>&1 || true
  echo
done

sec "H. LAMBDA EVENT-SOURCE MAPPINGS + VPC CONFIG (per function)"
for fn in $LAMBDAS; do
  echo "--- $fn : event source mappings"
  aws lambda list-event-source-mappings --function-name "$fn" --region "$REGION" \
    --query "EventSourceMappings[].[EventSourceArn,State,BatchSize]" --output table 2>&1 || true
  echo "--- $fn : vpc / dlq config"
  aws lambda get-function-configuration --function-name "$fn" --region "$REGION" \
    --query "{Vpc:VpcConfig.SubnetIds, SG:VpcConfig.SecurityGroupIds, DLQ:DeadLetterConfig.TargetArn}" --output json 2>&1 || true
  echo
done

sec "I. 🔴 DIAGNOSE: ingest-video DLQ (alarm is firing) + recent ERROR logs"
echo "--- DLQ depth:"
aws sqs get-queue-attributes --queue-url "$INGEST_DLQ" --region "$REGION" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
  --query "Attributes" --output json 2>&1 || true
echo
echo "--- peek up to 5 DLQ messages (visibility-timeout 0 = non-destructive, msgs stay):"
aws sqs receive-message --queue-url "$INGEST_DLQ" --region "$REGION" \
  --max-number-of-messages 5 --visibility-timeout 0 --wait-time-seconds 1 \
  --message-attribute-names All --attribute-names All \
  --query "Messages[].{Attrs:MessageAttributes, BodyHead:Body}" --output json 2>&1 \
  || echo "   (no messages visible / cannot read)"
echo
echo "--- recent ERROR/Exception lines from ingest-video logs (last 24h, up to 40):"
START_MS=$(( ( $(date +%s) - 86400 ) * 1000 ))
aws logs filter-log-events --region "$REGION" \
  --log-group-name "/aws/lambda/fieldsightai-ingest-video" \
  --start-time "$START_MS" \
  --filter-pattern '?ERROR ?Error ?Exception ?Traceback ?Timed ?timeout' \
  --max-items 40 \
  --query "events[].message" --output text 2>&1 \
  || echo "   (no matching log events / log group not found)"

sec "J. CONFIRM the two duplicate ingest-video poll schedules"
for r in $(aws events list-rules --region "$REGION" --query "Rules[?contains(Name,'realptt') || contains(Name,'IngestVideo')].Name" --output text 2>/dev/null | tr '\t' '\n'); do
  echo "--- $r"
  aws events describe-rule --region "$REGION" --name "$r" \
    --query "{Schedule:ScheduleExpression, State:State, Desc:Description}" --output json 2>&1 || true
  aws events list-targets-by-rule --region "$REGION" --rule "$r" \
    --query "Targets[].Arn" --output text 2>&1 || true
  echo
done

hr
echo "DEEP DIVE COMPLETE. Paste fieldsight-deepdive.txt back to Claude."
