#!/bin/bash
# ============================================================
# FieldSight Migration — Phase 4: Cleanup Old Resources
# ⚠️ 仅在验证新资源完全正常后运行
# ⚠️ 每一步都有确认提示, 不可逆操作
# ============================================================
set -euo pipefail

REGION="ap-southeast-2"
ACCOUNT="509194952652"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

confirm() {
  echo -e "${RED}⚠ $1${NC}"
  read -p "确认删除? (输入 DELETE 确认): " answer
  [[ "$answer" == "DELETE" ]] || { echo "已跳过"; return 1; }
}

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  FieldSight Migration Phase 4 — Cleanup${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo -e "${RED}⚠ 此脚本删除旧资源, 操作不可逆!${NC}"
echo -e "${RED}  请确认新资源已完全正常运行后再执行${NC}"
echo ""

# ============================================================
# Pre-flight: 验证新资源存在
# ============================================================
echo -e "${CYAN}[Pre-flight] 验证新资源...${NC}"

ERRORS=0
for FN in fieldsight-orchestrator fieldsight-downloader fieldsight-transcribe \
          fieldsight-report-generator fieldsight-fargate-trigger \
          fieldsight-vad fieldsight-api fieldsight-transcribe-callback fieldsight-meeting-minutes; do
  if aws lambda get-function --function-name "$FN" --region "$REGION" 2>/dev/null | grep -q "$FN"; then
    echo -e "  ${GREEN}✓ Lambda: ${FN}${NC}"
  else
    echo -e "  ${RED}✗ Lambda: ${FN} 不存在!${NC}"
    ((ERRORS++))
  fi
done

for TABLE in fieldsight-items fieldsight-reports fieldsight-audit fieldsight-transcripts fieldsight-users; do
  if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" 2>/dev/null | grep -q ACTIVE; then
    echo -e "  ${GREEN}✓ DynamoDB: ${TABLE}${NC}"
  else
    echo -e "  ${RED}✗ DynamoDB: ${TABLE} 不存在!${NC}"
    ((ERRORS++))
  fi
done

if [ "$ERRORS" -gt 0 ]; then
  echo ""
  echo -e "${RED}发现 ${ERRORS} 个新资源缺失, 终止清理!${NC}"
  echo -e "${RED}请先完成 Phase 2 和 Phase 3 部署${NC}"
  exit 1
fi

echo -e "  ${GREEN}✓ 所有新资源验证通过${NC}"
echo ""

# ============================================================
# 1. 删除旧 Lambda Functions (手动创建的)
# ============================================================
echo -e "${CYAN}[Step 1/7] 删除旧 Lambda Functions (手动创建的)${NC}"

for OLD_FN in sitesync-vad sitesync-api sitesync-transcribe-callback realptt-meeting-minutes; do
  if aws lambda get-function --function-name "$OLD_FN" --region "$REGION" 2>/dev/null | grep -q "$OLD_FN"; then
    confirm "删除 Lambda: ${OLD_FN}" && {
      aws lambda delete-function --function-name "$OLD_FN" --region "$REGION"
      echo -e "  ${GREEN}✓ 已删除 ${OLD_FN}${NC}"
    }
  else
    echo -e "  已不存在: ${OLD_FN}"
  fi
done

# ============================================================
# 2. 删除旧 EventBridge Rule
# ============================================================
echo ""
echo -e "${CYAN}[Step 2/7] 删除旧 EventBridge Rule${NC}"

OLD_RULE="sitesync-transcribe-state-change"
if aws events describe-rule --name "$OLD_RULE" --region "$REGION" 2>/dev/null | grep -q "$OLD_RULE"; then
  confirm "删除 EventBridge Rule: ${OLD_RULE}" && {
    # 先移除 targets
    TARGET_IDS=$(aws events list-targets-by-rule --rule "$OLD_RULE" --region "$REGION" \
      --query 'Targets[].Id' --output text 2>/dev/null)
    if [ -n "$TARGET_IDS" ]; then
      aws events remove-targets --rule "$OLD_RULE" --region "$REGION" \
        --ids $TARGET_IDS
    fi
    aws events delete-rule --name "$OLD_RULE" --region "$REGION"
    echo -e "  ${GREEN}✓ 已删除 ${OLD_RULE}${NC}"
  }
fi

# ============================================================
# 3. 删除旧 CloudWatch Log Groups
# ============================================================
echo ""
echo -e "${CYAN}[Step 3/7] 删除旧 CloudWatch Log Groups${NC}"

OLD_LOG_GROUPS=(
  "/aws/lambda/realptt-downloader"
  "/aws/lambda/realptt-fargate-trigger"
  "/aws/lambda/realptt-meeting-minutes"
  "/aws/lambda/realptt-orchestrator"
  "/aws/lambda/realptt-report-generator"
  "/aws/lambda/realptt-transcribe"
  "/aws/lambda/sitesync-api"
  "/aws/lambda/sitesync-transcribe-callback"
  "/aws/lambda/sitesync-vad"
  "/ecs/realptt-fargate-downloader"
)

confirm "删除 ${#OLD_LOG_GROUPS[@]} 个旧 Log Groups?" && {
  for LG in "${OLD_LOG_GROUPS[@]}"; do
    aws logs delete-log-group --log-group-name "$LG" --region "$REGION" 2>/dev/null && \
      echo -e "  ${GREEN}✓ ${LG}${NC}" || \
      echo -e "  ${YELLOW}跳过 ${LG} (可能已不存在)${NC}"
  done
}

# ============================================================
# 4. 删除旧 DynamoDB Tables
# ============================================================
echo ""
echo -e "${CYAN}[Step 4/7] 删除旧 DynamoDB Tables${NC}"

for OLD_TABLE in sitesync-items sitesync-reports sitesync-audit sitesync-transcripts sitesync-users; do
  ITEM_COUNT=$(aws dynamodb describe-table --table-name "$OLD_TABLE" --region "$REGION" \
    --query 'Table.ItemCount' --output text 2>/dev/null || echo "N/A")

  if [ "$ITEM_COUNT" != "N/A" ]; then
    confirm "删除 DynamoDB: ${OLD_TABLE} (${ITEM_COUNT} items)" && {
      aws dynamodb delete-table --table-name "$OLD_TABLE" --region "$REGION" > /dev/null
      echo -e "  ${GREEN}✓ 已删除 ${OLD_TABLE}${NC}"
    }
  fi
done

# ============================================================
# 5. 删除旧 IAM Roles
# ============================================================
echo ""
echo -e "${CYAN}[Step 5/7] 删除旧 IAM Roles${NC}"

for OLD_ROLE in sitesync-lambda-role sitesync-scheduler-role sitesync-transcribe-callback-role \
                sitesync-fargate-execution sitesync-fargate-task; do

  if ! aws iam get-role --role-name "$OLD_ROLE" 2>/dev/null | grep -q "$OLD_ROLE"; then
    echo -e "  已不存在: ${OLD_ROLE}"
    continue
  fi

  confirm "删除 IAM Role: ${OLD_ROLE}" && {
    # 分离 managed policies
    for POLICY_ARN in $(aws iam list-attached-role-policies --role-name "$OLD_ROLE" \
      --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      aws iam detach-role-policy --role-name "$OLD_ROLE" --policy-arn "$POLICY_ARN"
    done

    # 删除 inline policies
    for POLICY_NAME in $(aws iam list-role-policies --role-name "$OLD_ROLE" \
      --query 'PolicyNames[]' --output text 2>/dev/null); do
      aws iam delete-role-policy --role-name "$OLD_ROLE" --policy-name "$POLICY_NAME"
    done

    aws iam delete-role --role-name "$OLD_ROLE"
    echo -e "  ${GREEN}✓ 已删除 ${OLD_ROLE}${NC}"
  }
done

# ============================================================
# 6. 删除旧 Lambda Layer
# ============================================================
echo ""
echo -e "${CYAN}[Step 6/7] 删除旧 Lambda Layer${NC}"

OLD_LAYER_VERSIONS=$(aws lambda list-layer-versions --layer-name sitesync-vad-layer --region "$REGION" \
  --query 'LayerVersions[].Version' --output text 2>/dev/null)

if [ -n "$OLD_LAYER_VERSIONS" ]; then
  confirm "删除 Lambda Layer: sitesync-vad-layer (all versions)" && {
    for VER in $OLD_LAYER_VERSIONS; do
      aws lambda delete-layer-version --layer-name sitesync-vad-layer \
        --version-number "$VER" --region "$REGION"
    done
    echo -e "  ${GREEN}✓ 已删除 sitesync-vad-layer${NC}"
  }
fi

# ============================================================
# 7. S3 Bucket 清理 (最后执行, 最高风险)
# ============================================================
echo ""
echo -e "${CYAN}[Step 7/7] S3 Bucket 清理${NC}"

OLD_BUCKET="realptt-downloads-sitesync"
NEW_BUCKET="fieldsight-data-${ACCOUNT}"

echo -e "  ${YELLOW}旧 bucket: ${OLD_BUCKET}${NC}"
echo -e "  ${YELLOW}新 bucket: ${NEW_BUCKET}${NC}"

# 验证新 bucket 数据完整性
OLD_COUNT=$(aws s3 ls "s3://${OLD_BUCKET}" --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $3}')
NEW_COUNT=$(aws s3 ls "s3://${NEW_BUCKET}" --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $3}')

echo ""
echo "  旧 bucket 文件数: ${OLD_COUNT}"
echo "  新 bucket 文件数: ${NEW_COUNT}"

if [ "$OLD_COUNT" != "$NEW_COUNT" ]; then
  echo -e "  ${RED}⚠ 文件数不一致! 建议先运行 aws s3 sync 再删除${NC}"
  echo "  运行: aws s3 sync s3://${OLD_BUCKET} s3://${NEW_BUCKET}"
fi

echo ""
echo -e "  ${RED}⚠ S3 bucket 删除是不可逆操作!${NC}"
echo -e "  ${RED}  13.5GB 数据将永久丢失!${NC}"
confirm "删除旧 S3 bucket: ${OLD_BUCKET}" && {
  echo "  清空 bucket..."
  aws s3 rm "s3://${OLD_BUCKET}" --recursive --region "$REGION"
  echo "  删除 bucket..."
  aws s3api delete-bucket --bucket "$OLD_BUCKET" --region "$REGION"
  echo -e "  ${GREEN}✓ 已删除 ${OLD_BUCKET}${NC}"
}

# 前端 S3 bucket
OLD_WEB_BUCKET="sitesync-web-${ACCOUNT}"
if aws s3api head-bucket --bucket "$OLD_WEB_BUCKET" 2>/dev/null; then
  confirm "删除旧前端 S3 bucket: ${OLD_WEB_BUCKET}" && {
    aws s3 rm "s3://${OLD_WEB_BUCKET}" --recursive --region "$REGION"
    aws s3api delete-bucket --bucket "$OLD_WEB_BUCKET" --region "$REGION"
    echo -e "  ${GREEN}✓ 已删除 ${OLD_WEB_BUCKET}${NC}"
  }
fi

# ============================================================
# Final Verification
# ============================================================
echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  Phase 4 完成 — 最终验证扫描${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo "  运行残留扫描..."

FOUND=0
for PATTERN in realptt sitesync; do
  # Lambda
  for FN in $(aws lambda list-functions --region "$REGION" --query 'Functions[].FunctionName' --output text 2>/dev/null); do
    if echo "$FN" | grep -qi "$PATTERN"; then
      echo -e "  ${RED}[残留] Lambda: ${FN}${NC}"
      ((FOUND++))
    fi
  done
  # DynamoDB
  for TBL in $(aws dynamodb list-tables --region "$REGION" --query 'TableNames[]' --output text 2>/dev/null); do
    if echo "$TBL" | grep -qi "$PATTERN"; then
      echo -e "  ${RED}[残留] DynamoDB: ${TBL}${NC}"
      ((FOUND++))
    fi
  done
  # IAM
  for ROLE in $(aws iam list-roles --query 'Roles[].RoleName' --output text 2>/dev/null); do
    if echo "$ROLE" | grep -qi "$PATTERN"; then
      echo -e "  ${RED}[残留] IAM Role: ${ROLE}${NC}"
      ((FOUND++))
    fi
  done
done

if [ "$FOUND" -eq 0 ]; then
  echo -e "  ${GREEN}✓ 清理完成! 无残留旧命名资源${NC}"
else
  echo -e "  ${YELLOW}发现 ${FOUND} 个残留资源 (可能是 SAM 管理的, 需要 sam deploy 处理)${NC}"
fi

echo ""
echo -e "${GREEN}================================================${NC}"
echo -e "${GREEN}  FieldSight 迁移全部完成!${NC}"
echo -e "${GREEN}================================================${NC}"
