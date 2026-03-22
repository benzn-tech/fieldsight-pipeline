#!/bin/bash
# ============================================================
# FieldSight Migration — Phase 3: SAM Template Update + Deploy
# 更新 template.yaml 中所有命名, 并添加之前手动管理的资源
# 在本地 git repo 中运行 (不是 CloudShell)
# ============================================================
set -euo pipefail

ACCOUNT="509194952652"
NEW_BUCKET="fieldsight-data-${ACCOUNT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  FieldSight Migration Phase 3 — SAM Update${NC}"
echo -e "${CYAN}================================================${NC}"

# ============================================================
# 1. Patch template.yaml — 资源名替换
# ============================================================
echo ""
echo -e "${CYAN}[Step 1/3] 更新 template.yaml 命名${NC}"

TEMPLATE="template.yaml"

if [ ! -f "$TEMPLATE" ]; then
  echo -e "${RED}template.yaml 不存在, 请在项目根目录运行${NC}"
  exit 1
fi

cp "$TEMPLATE" "${TEMPLATE}.bak-$(date +%Y%m%d%H%M%S)"
echo "  备份已创建: ${TEMPLATE}.bak-*"

# ---- Description ----
sed -i 's/REAL PTT Cloud Sync v2/FieldSight Pipeline v2/g' "$TEMPLATE"

# ---- S3 Bucket 名 ----
sed -i "s/realptt-downloads-\${BucketNameSuffix}/fieldsight-data-\${BucketNameSuffix}/g" "$TEMPLATE"
sed -i 's/realptt-downloads-xxx/fieldsight-data-xxx/g' "$TEMPLATE"

# ---- Lambda FunctionName ----
sed -i 's/FunctionName: realptt-orchestrator/FunctionName: fieldsight-orchestrator/g' "$TEMPLATE"
sed -i 's/FunctionName: realptt-downloader/FunctionName: fieldsight-downloader/g' "$TEMPLATE"
sed -i 's/FunctionName: realptt-transcribe/FunctionName: fieldsight-transcribe/g' "$TEMPLATE"
sed -i 's/FunctionName: realptt-report-generator/FunctionName: fieldsight-report-generator/g' "$TEMPLATE"
sed -i 's/FunctionName: realptt-fargate-trigger/FunctionName: fieldsight-fargate-trigger/g' "$TEMPLATE"

# ---- ECS ----
sed -i 's/ClusterName: realptt-downloader-cluster/ClusterName: fieldsight-downloader-cluster/g' "$TEMPLATE"
sed -i 's/Family: realptt-fargate-downloader/Family: fieldsight-fargate-downloader/g' "$TEMPLATE"

# ---- IAM Roles ----
sed -i 's/realptt-fargate-execution/fieldsight-fargate-execution/g' "$TEMPLATE"
sed -i 's/realptt-fargate-task/fieldsight-fargate-task/g' "$TEMPLATE"

# ---- Log Groups ----
sed -i 's|/ecs/realptt-fargate-downloader|/ecs/fieldsight-fargate-downloader|g' "$TEMPLATE"

# ---- SNS + Alarms ----
sed -i 's/TopicName: realptt-pipeline-alerts/TopicName: fieldsight-pipeline-alerts/g' "$TEMPLATE"
sed -i 's/AlarmName: realptt-orchestrator-errors/AlarmName: fieldsight-orchestrator-errors/g' "$TEMPLATE"
sed -i 's/AlarmName: realptt-downloader-errors/AlarmName: fieldsight-downloader-errors/g' "$TEMPLATE"
sed -i 's/AlarmName: realptt-report-errors/AlarmName: fieldsight-report-errors/g' "$TEMPLATE"

# ---- DynamoDB Tables ----
sed -i 's/TableName: sitesync-items/TableName: fieldsight-items/g' "$TEMPLATE"
sed -i 's/TableName: sitesync-reports/TableName: fieldsight-reports/g' "$TEMPLATE"
sed -i 's/TableName: sitesync-audit/TableName: fieldsight-audit/g' "$TEMPLATE"

# ---- Outputs Console URLs ----
sed -i 's/realptt-orchestrator/fieldsight-orchestrator/g' "$TEMPLATE"
sed -i 's/realptt-fargate-downloader/fieldsight-fargate-downloader/g' "$TEMPLATE"
sed -i 's/realptt-downloader-cluster/fieldsight-downloader-cluster/g' "$TEMPLATE"

# ---- Comments 中残留的 REAL PTT 描述 (不改外部 URL) ----
sed -i 's/# REAL PTT company account/# RealPTT platform account (external service)/g' "$TEMPLATE"
sed -i 's/# REAL PTT password/# RealPTT platform password (external service)/g' "$TEMPLATE"

echo -e "  ${GREEN}✓ template.yaml 命名更新完成${NC}"
echo ""

# 验证: 检查残留
echo "  检查残留旧命名..."
REMAINING=$(grep -n "realptt-\|sitesync-" "$TEMPLATE" | grep -v "realptt\.com\|REALPTT_ACCOUNT\|REALPTT_PASSWORD\|record\.realptt" || true)
if [ -n "$REMAINING" ]; then
  echo -e "  ${YELLOW}发现残留 (可能需要手动检查):${NC}"
  echo "$REMAINING" | head -20
else
  echo -e "  ${GREEN}✓ 无残留旧命名${NC}"
fi

# ============================================================
# 2. Patch Python 文件 — 代码内部引用
# ============================================================
echo ""
echo -e "${CYAN}[Step 2/3] 更新 Python 源文件${NC}"

# ---- lambda_orchestrator.py ----
FILE="src/lambda_orchestrator.py"
if [ -f "$FILE" ]; then
  # 只改内部引用, 不改外部 URL
  sed -i "s/'realptt-downloader'/'fieldsight-downloader'/g" "$FILE"
  sed -i 's/REAL PTT File Sync v3/FieldSight Pipeline v3/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_transcribe.py ----
FILE="src/lambda_transcribe.py"
if [ -f "$FILE" ]; then
  sed -i 's/f"realptt_/f"fieldsight_/g' "$FILE"
  sed -i 's/"realptt_/"fieldsight_/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_transcribe_callback.py ----
FILE="src/lambda_transcribe_callback.py"
if [ -f "$FILE" ]; then
  sed -i "s/'sitesync-transcripts'/'fieldsight-transcripts'/g" "$FILE"
  sed -i "s/startswith('realptt_')/startswith(('realptt_', 'fieldsight_'))/g" "$FILE"
  sed -i 's/non-SiteSync job/non-FieldSight job/g' "$FILE"
  sed -i 's/"realptt_MPI3_/"fieldsight_MPI3_/g' "$FILE"
  sed -i 's/realptt_{user}/fieldsight_{user}/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_report_generator.py ----
FILE="src/lambda_report_generator.py"
if [ -f "$FILE" ]; then
  sed -i "s/'sitesync-items'/'fieldsight-items'/g" "$FILE"
  sed -i "s/'sitesync-reports'/'fieldsight-reports'/g" "$FILE"
  sed -i "s/'sitesync-audit'/'fieldsight-audit'/g" "$FILE"
  sed -i 's/sitesync-items table/fieldsight-items table/g' "$FILE"
  sed -i 's/sitesync-reports table/fieldsight-reports table/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_vad.py (如果在 src/ 中) ----
FILE="src/lambda_vad.py"
if [ -f "$FILE" ]; then
  sed -i "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" "$FILE"
  sed -i 's/sitesync/fieldsight/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_meeting_minutes.py (如果在 src/ 中) ----
FILE="src/lambda_meeting_minutes.py"
if [ -f "$FILE" ]; then
  sed -i "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" "$FILE"
  sed -i "s/'sitesync-/'fieldsight-/g" "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- lambda_sitesync_api.py → rename to lambda_fieldsight_api.py ----
FILE="src/lambda_sitesync_api.py"
if [ -f "$FILE" ]; then
  sed -i "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" "$FILE"
  sed -i "s/sitesync-items/fieldsight-items/g" "$FILE"
  sed -i "s/sitesync-reports/fieldsight-reports/g" "$FILE"
  sed -i "s/sitesync-audit/fieldsight-audit/g" "$FILE"
  sed -i "s/sitesync-users/fieldsight-users/g" "$FILE"
  sed -i "s/sitesync-transcripts/fieldsight-transcripts/g" "$FILE"
  sed -i "s/realptt-report-generator/fieldsight-report-generator/g" "$FILE"
  mv "$FILE" "src/lambda_fieldsight_api.py"
  echo -e "  ${GREEN}✓ ${FILE} → src/lambda_fieldsight_api.py${NC}"
fi

# ---- fargate_downloader.py ----
FILE="src/fargate_downloader.py"
if [ -f "$FILE" ]; then
  sed -i "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- transcript_utils.py ----
FILE="src/transcript_utils.py"
if [ -f "$FILE" ]; then
  sed -i 's/sitesync/fieldsight/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ---- config/prompt_templates.json ----
FILE="config/prompt_templates.json"
if [ -f "$FILE" ]; then
  sed -i 's/SiteSync/FieldSight/g' "$FILE"
  sed -i 's/sitesync/fieldsight/g' "$FILE"
  echo -e "  ${GREEN}✓ ${FILE}${NC}"
fi

# ============================================================
# 3. 更新文档
# ============================================================
echo ""
echo -e "${CYAN}[Step 3/3] 更新文档${NC}"

# ---- README.md ----
if [ -f "README.md" ]; then
  sed -i 's/# REAL PTT Cloud Sync Pipeline/# FieldSight Pipeline/g' README.md
  sed -i 's/from REAL PTT Cloud Platform/from RealPTT Cloud Platform/g' README.md
  sed -i 's/realptt-pipeline/fieldsight-pipeline/g' README.md
  sed -i "s/realptt-downloads-YOURSUFFIX/fieldsight-data-YOURSUFFIX/g" README.md
  sed -i 's/realptt-orchestrator/fieldsight-orchestrator/g' README.md
  sed -i 's/realptt-downloader/fieldsight-downloader/g' README.md
  sed -i 's/realptt-transcribe/fieldsight-transcribe/g' README.md
  sed -i 's/realptt-report-generator/fieldsight-report-generator/g' README.md
  sed -i 's/realptt-fargate/fieldsight-fargate/g' README.md
  sed -i 's/realptt_downloader/fieldsight_downloader/g' README.md
  sed -i 's/realptt_api_test/fieldsight_api_test/g' README.md
  sed -i 's/sitesync/fieldsight/g' README.md
  echo -e "  ${GREEN}✓ README.md${NC}"
fi

# ---- ARCHITECTURE.md ----
if [ -f "ARCHITECTURE.md" ]; then
  sed -i 's/realptt-orchestrator/fieldsight-orchestrator/g' ARCHITECTURE.md
  sed -i 's/realptt-downloader/fieldsight-downloader/g' ARCHITECTURE.md
  sed -i 's/realptt-transcribe/fieldsight-transcribe/g' ARCHITECTURE.md
  sed -i 's/realptt-report-generator/fieldsight-report-generator/g' ARCHITECTURE.md
  sed -i 's/realptt-fargate-downloader/fieldsight-fargate-downloader/g' ARCHITECTURE.md
  sed -i 's/realptt-fargate-trigger/fieldsight-fargate-trigger/g' ARCHITECTURE.md
  sed -i 's/sitesync-items/fieldsight-items/g' ARCHITECTURE.md
  sed -i 's/sitesync-reports/fieldsight-reports/g' ARCHITECTURE.md
  sed -i 's/sitesync-audit/fieldsight-audit/g' ARCHITECTURE.md
  sed -i 's/| realptt\.com/| realptt.com/g' ARCHITECTURE.md  # 保留外部 URL
  echo -e "  ${GREEN}✓ ARCHITECTURE.md${NC}"
fi

# ---- MONITORING.md ----
if [ -f "MONITORING.md" ]; then
  sed -i 's/realptt-orchestrator/fieldsight-orchestrator/g' MONITORING.md
  sed -i 's/realptt-downloader-cluster/fieldsight-downloader-cluster/g' MONITORING.md
  sed -i 's/realptt-downloader/fieldsight-downloader/g' MONITORING.md
  sed -i 's/realptt-transcribe/fieldsight-transcribe/g' MONITORING.md
  sed -i 's/realptt-report-generator/fieldsight-report-generator/g' MONITORING.md
  sed -i 's/realptt-fargate-trigger/fieldsight-fargate-trigger/g' MONITORING.md
  sed -i 's/realptt-fargate-downloader/fieldsight-fargate-downloader/g' MONITORING.md
  sed -i 's/realptt-pipeline-alerts/fieldsight-pipeline-alerts/g' MONITORING.md
  sed -i 's/sitesync-items/fieldsight-items/g' MONITORING.md
  sed -i 's/sitesync-reports/fieldsight-reports/g' MONITORING.md
  sed -i 's/sitesync-audit/fieldsight-audit/g' MONITORING.md
  sed -i 's/sitesync-\*/fieldsight-\*/g' MONITORING.md
  # 保留 REAL PTT 外部引用
  echo -e "  ${GREEN}✓ MONITORING.md${NC}"
fi

echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  Phase 3 完成 — 代码/文档更新汇总${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo "  已更新文件:"
echo "    template.yaml (备份: ${TEMPLATE}.bak-*)"
echo "    src/lambda_orchestrator.py"
echo "    src/lambda_transcribe.py"
echo "    src/lambda_transcribe_callback.py"
echo "    src/lambda_report_generator.py"
echo "    src/lambda_vad.py"
echo "    src/lambda_meeting_minutes.py"
echo "    src/lambda_fieldsight_api.py (renamed)"
echo "    src/fargate_downloader.py"
echo "    src/transcript_utils.py"
echo "    config/prompt_templates.json"
echo "    README.md, ARCHITECTURE.md, MONITORING.md"
echo ""
echo -e "  ${YELLOW}SAM 部署命令:${NC}"
echo "    sam build"
echo "    sam deploy --stack-name fieldsight-pipeline --resolve-s3"
echo ""
echo -e "  ${YELLOW}⚠ 注意: SAM deploy 会:${NC}"
echo "    1. 创建新 Lambda 函数 (fieldsight-*)"
echo "    2. 删除旧 Lambda 函数 (realptt-*)"
echo "    3. 创建新 ECS cluster + task def"
echo "    4. 创建新 log groups"
echo "    5. 旧 CloudWatch log groups 不会自动删除"
echo ""
echo -e "  ${YELLOW}完成后运行 phase4-cleanup.sh 清理旧资源${NC}"
