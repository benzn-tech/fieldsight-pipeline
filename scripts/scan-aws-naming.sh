#!/bin/bash
# ============================================================
# FieldSight AWS 资源命名扫描脚本
# 用途: 扫描 AWS 账户中所有包含 realptt / sitesync / SiteSync 的资源
# 执行: 在 CloudShell (ap-southeast-2) 中运行
# ============================================================

REGION="ap-southeast-2"
PATTERNS=("realptt" "sitesync" "SiteSync" "site-sync" "real-ptt" "RealPTT")
OUTPUT_FILE="/tmp/fieldsight-rename-scan-$(date +%Y%m%d-%H%M%S).txt"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

match_any() {
  local text="$1"
  local lower=$(echo "$text" | tr '[:upper:]' '[:lower:]')
  for p in "${PATTERNS[@]}"; do
    local lp=$(echo "$p" | tr '[:upper:]' '[:lower:]')
    if [[ "$lower" == *"$lp"* ]]; then
      return 0
    fi
  done
  return 1
}

section() {
  echo ""
  echo -e "${CYAN}========================================${NC}"
  echo -e "${CYAN}  $1${NC}"
  echo -e "${CYAN}========================================${NC}"
  echo ""
  echo "" >> "$OUTPUT_FILE"
  echo "========================================" >> "$OUTPUT_FILE"
  echo "  $1" >> "$OUTPUT_FILE"
  echo "========================================" >> "$OUTPUT_FILE"
}

found() {
  echo -e "  ${RED}[FOUND]${NC} $1"
  echo "  [FOUND] $1" >> "$OUTPUT_FILE"
}

clean() {
  echo -e "  ${GREEN}[CLEAN]${NC} $1"
}

echo "FieldSight AWS 命名扫描" | tee "$OUTPUT_FILE"
echo "时间: $(date)" | tee -a "$OUTPUT_FILE"
echo "区域: $REGION" | tee -a "$OUTPUT_FILE"
echo "账户: $(aws sts get-caller-identity --query Account --output text)" | tee -a "$OUTPUT_FILE"
echo "匹配模式: ${PATTERNS[*]}" | tee -a "$OUTPUT_FILE"
echo "---" | tee -a "$OUTPUT_FILE"

TOTAL=0

# ============================================================
# 1. Lambda Functions
# ============================================================
section "1. Lambda Functions"

LAMBDAS=$(aws lambda list-functions --region $REGION \
  --query 'Functions[].FunctionName' --output text 2>/dev/null)

if [ -z "$LAMBDAS" ]; then
  echo "  (无 Lambda 函数或无权限)"
else
  for fn in $LAMBDAS; do
    if match_any "$fn"; then
      # 获取详细信息
      CONFIG=$(aws lambda get-function-configuration --function-name "$fn" --region $REGION \
        --query '{Runtime:Runtime,Handler:Handler,Timeout:Timeout,MemorySize:MemorySize,LastModified:LastModified}' \
        --output json 2>/dev/null)
      found "Lambda: $fn"
      echo "         $CONFIG" | tee -a "$OUTPUT_FILE"

      # 扫描环境变量
      ENVVARS=$(aws lambda get-function-configuration --function-name "$fn" --region $REGION \
        --query 'Environment.Variables' --output json 2>/dev/null)
      if [ "$ENVVARS" != "null" ] && [ -n "$ENVVARS" ]; then
        echo "$ENVVARS" | grep -iE "realptt|sitesync" | while read -r line; do
          echo -e "    ${YELLOW}[ENV VAR]${NC} $line"
          echo "    [ENV VAR] $line" >> "$OUTPUT_FILE"
        done
      fi
      ((TOTAL++))
    fi
  done
fi

# ============================================================
# 2. S3 Buckets
# ============================================================
section "2. S3 Buckets"

BUCKETS=$(aws s3api list-buckets --query 'Buckets[].Name' --output text 2>/dev/null)

for bkt in $BUCKETS; do
  if match_any "$bkt"; then
    SIZE=$(aws s3 ls "s3://$bkt" --summarize --recursive 2>/dev/null | tail -2)
    found "S3 Bucket: $bkt"
    echo "         $SIZE" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi
done

# 扫描 bucket 内的关键路径 (config, scripts 目录)
echo ""
echo -e "  ${YELLOW}扫描 bucket 内部关键文件...${NC}"
for bkt in $BUCKETS; do
  # 检查 scripts/ 目录 (Fargate 用)
  SCRIPTS=$(aws s3 ls "s3://$bkt/scripts/" 2>/dev/null)
  if [ -n "$SCRIPTS" ]; then
    echo -e "    ${YELLOW}[BUCKET CONTENT]${NC} s3://$bkt/scripts/" | tee -a "$OUTPUT_FILE"
    echo "$SCRIPTS" | while read -r line; do
      echo "      $line" | tee -a "$OUTPUT_FILE"
    done
  fi
  # 检查 config/ 目录
  CONFIGS=$(aws s3 ls "s3://$bkt/config/" 2>/dev/null)
  if [ -n "$CONFIGS" ]; then
    echo -e "    ${YELLOW}[BUCKET CONTENT]${NC} s3://$bkt/config/" | tee -a "$OUTPUT_FILE"
    echo "$CONFIGS" | while read -r line; do
      echo "      $line" | tee -a "$OUTPUT_FILE"
    done
  fi
done

# ============================================================
# 3. DynamoDB Tables
# ============================================================
section "3. DynamoDB Tables"

TABLES=$(aws dynamodb list-tables --region $REGION \
  --query 'TableNames[]' --output text 2>/dev/null)

for tbl in $TABLES; do
  if match_any "$tbl"; then
    STATUS=$(aws dynamodb describe-table --table-name "$tbl" --region $REGION \
      --query 'Table.{Status:TableStatus,ItemCount:ItemCount,SizeBytes:TableSizeBytes}' \
      --output json 2>/dev/null)
    found "DynamoDB: $tbl"
    echo "         $STATUS" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi
done

# ============================================================
# 4. ECS Clusters & Services & Task Definitions
# ============================================================
section "4. ECS / Fargate"

CLUSTERS=$(aws ecs list-clusters --region $REGION \
  --query 'clusterArns[]' --output text 2>/dev/null)

for cluster_arn in $CLUSTERS; do
  cluster_name=$(echo "$cluster_arn" | awk -F'/' '{print $NF}')
  if match_any "$cluster_name"; then
    STATUS=$(aws ecs describe-clusters --clusters "$cluster_arn" --region $REGION \
      --query 'clusters[0].{Status:status,RunningTasks:runningTasksCount,Services:activeServicesCount}' \
      --output json 2>/dev/null)
    found "ECS Cluster: $cluster_name"
    echo "         $STATUS" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi

  # 扫描 Services
  SERVICES=$(aws ecs list-services --cluster "$cluster_arn" --region $REGION \
    --query 'serviceArns[]' --output text 2>/dev/null)
  for svc_arn in $SERVICES; do
    svc_name=$(echo "$svc_arn" | awk -F'/' '{print $NF}')
    if match_any "$svc_name"; then
      found "ECS Service: $svc_name (cluster: $cluster_name)"
      ((TOTAL++))
    fi
  done
done

# Task Definitions
echo ""
echo "  Task Definitions:"
TASKDEFS=$(aws ecs list-task-definition-families --region $REGION \
  --status ACTIVE --query 'families[]' --output text 2>/dev/null)

for td in $TASKDEFS; do
  if match_any "$td"; then
    LATEST=$(aws ecs describe-task-definition --task-definition "$td" --region $REGION \
      --query 'taskDefinition.{Revision:revision,Cpu:cpu,Memory:memory,Status:status}' \
      --output json 2>/dev/null)
    found "Task Definition: $td"
    echo "         $LATEST" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi
done

# ============================================================
# 5. IAM Roles & Policies
# ============================================================
section "5. IAM Roles"

ROLES=$(aws iam list-roles --query 'Roles[].RoleName' --output text 2>/dev/null)

for role in $ROLES; do
  if match_any "$role"; then
    found "IAM Role: $role"
    # 列出 attached policies
    ATTACHED=$(aws iam list-attached-role-policies --role-name "$role" \
      --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null)
    if [ -n "$ATTACHED" ]; then
      echo "         Attached: $ATTACHED" | tee -a "$OUTPUT_FILE"
    fi
    INLINE=$(aws iam list-role-policies --role-name "$role" \
      --query 'PolicyNames[]' --output text 2>/dev/null)
    if [ -n "$INLINE" ]; then
      echo "         Inline: $INLINE" | tee -a "$OUTPUT_FILE"
    fi
    ((TOTAL++))
  fi
done

# ============================================================
# 6. CloudWatch Log Groups
# ============================================================
section "6. CloudWatch Log Groups"

LOGGROUPS=$(aws logs describe-log-groups --region $REGION \
  --query 'logGroups[].logGroupName' --output text 2>/dev/null)

for lg in $LOGGROUPS; do
  if match_any "$lg"; then
    BYTES=$(aws logs describe-log-groups --region $REGION \
      --log-group-name-prefix "$lg" \
      --query 'logGroups[0].storedBytes' --output text 2>/dev/null)
    found "Log Group: $lg  (${BYTES} bytes)"
    ((TOTAL++))
  fi
done

# ============================================================
# 7. EventBridge Rules
# ============================================================
section "7. EventBridge Rules"

RULES=$(aws events list-rules --region $REGION \
  --query 'Rules[].Name' --output text 2>/dev/null)

for rule in $RULES; do
  if match_any "$rule"; then
    DETAIL=$(aws events describe-rule --name "$rule" --region $REGION \
      --query '{State:State,Schedule:ScheduleExpression,Description:Description}' \
      --output json 2>/dev/null)
    found "EventBridge Rule: $rule"
    echo "         $DETAIL" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  else
    # 即使 rule 名不匹配, 检查 target 是否指向旧资源
    TARGETS=$(aws events list-targets-by-rule --rule "$rule" --region $REGION \
      --query 'Targets[].Arn' --output text 2>/dev/null)
    for tgt in $TARGETS; do
      if match_any "$tgt"; then
        found "EventBridge Rule: $rule → Target: $tgt"
        ((TOTAL++))
      fi
    done
  fi
done

# ============================================================
# 8. CloudWatch Alarms
# ============================================================
section "8. CloudWatch Alarms"

ALARMS=$(aws cloudwatch describe-alarms --region $REGION \
  --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null)

for alarm in $ALARMS; do
  if match_any "$alarm"; then
    found "Alarm: $alarm"
    ((TOTAL++))
  fi
done

# ============================================================
# 9. SNS Topics
# ============================================================
section "9. SNS Topics"

TOPICS=$(aws sns list-topics --region $REGION \
  --query 'Topics[].TopicArn' --output text 2>/dev/null)

for topic_arn in $TOPICS; do
  topic_name=$(echo "$topic_arn" | awk -F':' '{print $NF}')
  if match_any "$topic_name"; then
    found "SNS Topic: $topic_name"
    echo "         ARN: $topic_arn" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi
done

# ============================================================
# 10. CloudFront Distributions
# ============================================================
section "10. CloudFront Distributions"

DISTS=$(aws cloudfront list-distributions \
  --query 'DistributionList.Items[].{Id:Id,Domain:DomainName,Comment:Comment,Origins:Origins.Items[].DomainName}' \
  --output json 2>/dev/null)

if [ "$DISTS" != "null" ] && [ -n "$DISTS" ]; then
  echo "$DISTS" | python3 -c "
import sys, json
dists = json.load(sys.stdin)
if dists:
  for d in dists:
    combo = json.dumps(d)
    lower = combo.lower()
    if any(p in lower for p in ['realptt','sitesync','site-sync','real-ptt']):
      print(f\"  [FOUND] CloudFront: {d['Id']} - {d['Domain']} - {d.get('Comment','')}\")
    else:
      # 列出所有 distribution 供参考
      origins = ', '.join(d.get('Origins', []))
      print(f\"  [INFO]  CloudFront: {d['Id']} | {d['Domain']} | Origins: {origins}\")
" 2>/dev/null | tee -a "$OUTPUT_FILE"
else
  echo "  (无 CloudFront distributions)"
fi

# ============================================================
# 11. Cognito User Pools
# ============================================================
section "11. Cognito User Pools"

POOLS=$(aws cognito-idp list-user-pools --max-results 20 --region $REGION \
  --query 'UserPools[].{Id:Id,Name:Name}' --output json 2>/dev/null)

if [ "$POOLS" != "null" ] && [ -n "$POOLS" ]; then
  echo "$POOLS" | python3 -c "
import sys, json
pools = json.load(sys.stdin)
for p in pools:
  combo = (p['Name'] + p['Id']).lower()
  if any(pat in combo for pat in ['realptt','sitesync','site-sync','real-ptt','fieldsight']):
    print(f\"  [FOUND] Cognito: {p['Id']} - {p['Name']}\")
  else:
    print(f\"  [INFO]  Cognito: {p['Id']} - {p['Name']}\")
" 2>/dev/null | tee -a "$OUTPUT_FILE"
else
  echo "  (无 Cognito User Pools)"
fi

# ============================================================
# 12. CloudFormation Stacks (SAM 部署的)
# ============================================================
section "12. CloudFormation Stacks"

STACKS=$(aws cloudformation list-stacks --region $REGION \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
  --query 'StackSummaries[].StackName' --output text 2>/dev/null)

for stack in $STACKS; do
  if match_any "$stack"; then
    DETAIL=$(aws cloudformation describe-stacks --stack-name "$stack" --region $REGION \
      --query 'Stacks[0].{Status:StackStatus,Updated:LastUpdatedTime,Description:Description}' \
      --output json 2>/dev/null)
    found "CFN Stack: $stack"
    echo "         $DETAIL" | tee -a "$OUTPUT_FILE"
    ((TOTAL++))
  fi
done

# 扫描所有 stack 的资源，看是否有匹配的
echo ""
echo "  扫描所有 Stack 内部资源..."
for stack in $STACKS; do
  RESOURCES=$(aws cloudformation list-stack-resources --stack-name "$stack" --region $REGION \
    --query 'StackResourceSummaries[].{Type:ResourceType,Logical:LogicalResourceId,Physical:PhysicalResourceId}' \
    --output json 2>/dev/null)
  if [ -n "$RESOURCES" ]; then
    echo "$RESOURCES" | python3 -c "
import sys, json
resources = json.load(sys.stdin)
stack='$stack'
for r in resources:
  phys = r.get('Physical','') or ''
  combo = (phys + r.get('Logical','')).lower()
  if any(p in combo for p in ['realptt','sitesync','site-sync']):
    print(f\"  [FOUND] Stack={stack} | {r['Type']} | Logical={r['Logical']} | Physical={phys}\")
" 2>/dev/null | tee -a "$OUTPUT_FILE"
  fi
done

# ============================================================
# 13. ECR Repositories (如果有 Docker 镜像)
# ============================================================
section "13. ECR Repositories"

REPOS=$(aws ecr describe-repositories --region $REGION \
  --query 'repositories[].repositoryName' --output text 2>/dev/null)

if [ -z "$REPOS" ]; then
  echo "  (无 ECR repositories)"
else
  for repo in $REPOS; do
    if match_any "$repo"; then
      found "ECR Repo: $repo"
      ((TOTAL++))
    fi
  done
fi

# ============================================================
# 14. API Gateway (REST & HTTP)
# ============================================================
section "14. API Gateway"

# REST APIs
REST_APIS=$(aws apigateway get-rest-apis --region $REGION \
  --query 'items[].{id:id,name:name}' --output json 2>/dev/null)

if [ "$REST_APIS" != "null" ] && [ -n "$REST_APIS" ]; then
  echo "$REST_APIS" | python3 -c "
import sys, json
apis = json.load(sys.stdin)
for a in apis:
  if any(p in a['name'].lower() for p in ['realptt','sitesync','site-sync','fieldsight']):
    print(f\"  [FOUND] REST API: {a['id']} - {a['name']}\")
  else:
    print(f\"  [INFO]  REST API: {a['id']} - {a['name']}\")
" 2>/dev/null | tee -a "$OUTPUT_FILE"
fi

# HTTP APIs
HTTP_APIS=$(aws apigatewayv2 get-apis --region $REGION \
  --query 'Items[].{ApiId:ApiId,Name:Name}' --output json 2>/dev/null)

if [ "$HTTP_APIS" != "null" ] && [ -n "$HTTP_APIS" ]; then
  echo "$HTTP_APIS" | python3 -c "
import sys, json
apis = json.load(sys.stdin)
for a in apis:
  if any(p in a['Name'].lower() for p in ['realptt','sitesync','site-sync','fieldsight']):
    print(f\"  [FOUND] HTTP API: {a['ApiId']} - {a['Name']}\")
  else:
    print(f\"  [INFO]  HTTP API: {a['ApiId']} - {a['Name']}\")
" 2>/dev/null | tee -a "$OUTPUT_FILE"
fi

# ============================================================
# 15. Transcribe Jobs (最近的)
# ============================================================
section "15. Recent Transcribe Jobs (last 20)"

JOBS=$(aws transcribe list-transcription-jobs --region $REGION \
  --max-results 20 \
  --query 'TranscriptionJobSummaries[].{Name:TranscriptionJobName,Status:TranscriptionJobStatus,Created:CreationTime}' \
  --output json 2>/dev/null)

if [ "$JOBS" != "null" ] && [ -n "$JOBS" ]; then
  echo "$JOBS" | python3 -c "
import sys, json
jobs = json.load(sys.stdin)
old = [j for j in jobs if 'realptt' in j['Name'].lower()]
new = [j for j in jobs if 'fieldsight' in j['Name'].lower()]
other = [j for j in jobs if 'realptt' not in j['Name'].lower() and 'fieldsight' not in j['Name'].lower()]
if old:
  print(f'  旧前缀 (realptt_): {len(old)} 个')
  for j in old[:5]:
    print(f\"    {j['Name']} [{j['Status']}]\")
if new:
  print(f'  新前缀 (fieldsight_): {len(new)} 个')
if other:
  print(f'  其他前缀: {len(other)} 个')
if not old and not new and not other:
  print('  (无最近的 Transcribe jobs)')
" 2>/dev/null | tee -a "$OUTPUT_FILE"
fi

# ============================================================
# 16. Lambda Layers
# ============================================================
section "16. Lambda Layers"

LAYERS=$(aws lambda list-layers --region $REGION \
  --query 'Layers[].LayerName' --output text 2>/dev/null)

for layer in $LAYERS; do
  if match_any "$layer"; then
    found "Lambda Layer: $layer"
    ((TOTAL++))
  else
    echo -e "  [INFO]  Layer: $layer"
  fi
done

# ============================================================
# 17. Secrets Manager
# ============================================================
section "17. Secrets Manager"

SECRETS=$(aws secretsmanager list-secrets --region $REGION \
  --query 'SecretList[].Name' --output text 2>/dev/null)

if [ -n "$SECRETS" ]; then
  for sec in $SECRETS; do
    if match_any "$sec"; then
      found "Secret: $sec"
      ((TOTAL++))
    fi
  done
else
  echo "  (无 Secrets 或无权限)"
fi

# ============================================================
# 18. SSM Parameter Store
# ============================================================
section "18. SSM Parameter Store"

PARAMS=$(aws ssm describe-parameters --region $REGION \
  --query 'Parameters[].Name' --output text 2>/dev/null)

if [ -n "$PARAMS" ]; then
  for param in $PARAMS; do
    if match_any "$param"; then
      found "SSM Param: $param"
      ((TOTAL++))
    fi
  done
else
  echo "  (无 SSM Parameters 或无权限)"
fi

# ============================================================
# SUMMARY
# ============================================================
echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  扫描完成 — 汇总${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""
echo -e "  需要重命名的资源总数: ${RED}${TOTAL}${NC}"
echo ""
echo -e "  完整报告已保存: ${YELLOW}${OUTPUT_FILE}${NC}"
echo ""
echo "  下一步:"
echo "    cat $OUTPUT_FILE                    # 查看完整报告"
echo "    grep '\\[FOUND\\]' $OUTPUT_FILE       # 只看需要改的"
echo "    grep '\\[FOUND\\]' $OUTPUT_FILE | wc -l  # 计数"

echo "" >> "$OUTPUT_FILE"
echo "========================================" >> "$OUTPUT_FILE"
echo "  总计: $TOTAL 个资源需要重命名" >> "$OUTPUT_FILE"
echo "========================================" >> "$OUTPUT_FILE"
