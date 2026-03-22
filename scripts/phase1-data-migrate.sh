#!/bin/bash
# ============================================================
# FieldSight Migration — Phase 1: Data Migration
# 创建新 S3 bucket + DynamoDB tables, 迁移数据
# 在 CloudShell (ap-southeast-2) 中运行
# ============================================================
set -euo pipefail

REGION="ap-southeast-2"
ACCOUNT="509194952652"

# ---- 配置 ----
OLD_BUCKET="realptt-downloads-sitesync"
NEW_BUCKET="fieldsight-data-${ACCOUNT}"  # 全局唯一

OLD_DDB_TABLES=("sitesync-items" "sitesync-reports" "sitesync-audit" "sitesync-transcripts" "sitesync-users")
NEW_DDB_TABLES=("fieldsight-items" "fieldsight-reports" "fieldsight-audit" "fieldsight-transcripts" "fieldsight-users")

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

confirm() {
  echo -e "${YELLOW}$1${NC}"
  read -p "继续? (y/n): " answer
  [[ "$answer" == "y" ]] || { echo "已跳过"; return 1; }
}

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  FieldSight Migration Phase 1 — Data Migration${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""

# ============================================================
# 1. Create new S3 bucket
# ============================================================
echo -e "${CYAN}[Step 1/4] 创建新 S3 Bucket: ${NEW_BUCKET}${NC}"

if aws s3api head-bucket --bucket "$NEW_BUCKET" 2>/dev/null; then
  echo -e "${GREEN}Bucket 已存在，跳过创建${NC}"
else
  confirm "将创建 S3 bucket: ${NEW_BUCKET}" && {
    aws s3api create-bucket \
      --bucket "$NEW_BUCKET" \
      --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION"

    # 屏蔽公共访问
    aws s3api put-public-access-block \
      --bucket "$NEW_BUCKET" \
      --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

    # 添加生命周期规则 (与旧 bucket 一致)
    aws s3api put-bucket-lifecycle-configuration \
      --bucket "$NEW_BUCKET" \
      --lifecycle-configuration '{
        "Rules": [
          {
            "ID": "DeleteOldTranscripts",
            "Status": "Enabled",
            "Filter": {"Prefix": "transcripts/"},
            "Expiration": {"Days": 90}
          },
          {
            "ID": "CleanupPendingDownloads",
            "Status": "Enabled",
            "Filter": {"Prefix": "pending_downloads/"},
            "Expiration": {"Days": 7}
          }
        ]
      }'

    # 添加 Transcribe 服务访问策略
    aws s3api put-bucket-policy \
      --bucket "$NEW_BUCKET" \
      --policy "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Sid\": \"AllowTranscribeRead\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"transcribe.amazonaws.com\"},
            \"Action\": \"s3:GetObject\",
            \"Resource\": \"arn:aws:s3:::${NEW_BUCKET}/*\"
          },
          {
            \"Sid\": \"AllowTranscribeWrite\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"transcribe.amazonaws.com\"},
            \"Action\": \"s3:PutObject\",
            \"Resource\": \"arn:aws:s3:::${NEW_BUCKET}/transcripts/*\"
          }
        ]
      }"

    echo -e "${GREEN}✓ Bucket 创建完成${NC}"
  }
fi

# ============================================================
# 2. Sync S3 data
# ============================================================
echo ""
echo -e "${CYAN}[Step 2/4] 同步 S3 数据 (${OLD_BUCKET} → ${NEW_BUCKET})${NC}"
echo "  源 bucket 大小: ~13.5 GB, ~1054 个文件"
echo "  预计时间: 5-15 分钟 (取决于网络)"

confirm "开始 S3 数据同步?" && {
  echo "同步中..."
  aws s3 sync "s3://${OLD_BUCKET}" "s3://${NEW_BUCKET}" \
    --region "$REGION" \
    --no-progress 2>&1 | tail -5

  # 验证
  OLD_COUNT=$(aws s3 ls "s3://${OLD_BUCKET}" --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $3}')
  NEW_COUNT=$(aws s3 ls "s3://${NEW_BUCKET}" --recursive --summarize 2>/dev/null | grep "Total Objects" | awk '{print $3}')
  echo ""
  echo -e "  旧 bucket 文件数: ${OLD_COUNT}"
  echo -e "  新 bucket 文件数: ${NEW_COUNT}"

  if [ "$OLD_COUNT" == "$NEW_COUNT" ]; then
    echo -e "${GREEN}✓ 文件数一致，同步成功${NC}"
  else
    echo -e "${YELLOW}⚠ 文件数不一致，请检查。可能是同步过程中有新文件写入。${NC}"
  fi
}

# ============================================================
# 3. Create new DynamoDB tables
# ============================================================
echo ""
echo -e "${CYAN}[Step 3/4] 创建新 DynamoDB Tables${NC}"

for i in "${!OLD_DDB_TABLES[@]}"; do
  OLD_TABLE="${OLD_DDB_TABLES[$i]}"
  NEW_TABLE="${NEW_DDB_TABLES[$i]}"

  echo ""
  echo -e "  ${CYAN}创建表: ${NEW_TABLE}${NC} (基于 ${OLD_TABLE})"

  # 检查是否已存在
  if aws dynamodb describe-table --table-name "$NEW_TABLE" --region "$REGION" 2>/dev/null | grep -q ACTIVE; then
    echo -e "  ${GREEN}表已存在，跳过${NC}"
    continue
  fi

  # 获取旧表的 schema
  SCHEMA=$(aws dynamodb describe-table --table-name "$OLD_TABLE" --region "$REGION" 2>/dev/null)

  if [ -z "$SCHEMA" ]; then
    echo -e "  ${YELLOW}旧表 ${OLD_TABLE} 不存在，跳过${NC}"
    continue
  fi

  # 提取 key schema 和 attribute definitions
  ATTR_DEFS=$(echo "$SCHEMA" | python3 -c "
import sys, json
t = json.load(sys.stdin)['Table']
attrs = [{'AttributeName': a['AttributeName'], 'AttributeType': a['AttributeType']} for a in t['AttributeDefinitions']]
print(json.dumps(attrs))
")

  KEY_SCHEMA=$(echo "$SCHEMA" | python3 -c "
import sys, json
t = json.load(sys.stdin)['Table']
keys = [{'AttributeName': k['AttributeName'], 'KeyType': k['KeyType']} for k in t['KeySchema']]
print(json.dumps(keys))
")

  # 检查 GSI
  HAS_GSI=$(echo "$SCHEMA" | python3 -c "
import sys, json
t = json.load(sys.stdin)['Table']
gsis = t.get('GlobalSecondaryIndexes', [])
if gsis:
    result = []
    for g in gsis:
        result.append({
            'IndexName': g['IndexName'],
            'KeySchema': g['KeySchema'],
            'Projection': g['Projection']
        })
    print(json.dumps(result))
else:
    print('')
" 2>/dev/null)

  # 创建表
  CREATE_CMD="aws dynamodb create-table \
    --table-name ${NEW_TABLE} \
    --region ${REGION} \
    --billing-mode PAY_PER_REQUEST \
    --attribute-definitions '${ATTR_DEFS}' \
    --key-schema '${KEY_SCHEMA}'"

  if [ -n "$HAS_GSI" ]; then
    CREATE_CMD="${CREATE_CMD} --global-secondary-indexes '${HAS_GSI}'"
  fi

  eval "$CREATE_CMD"

  # 等待表激活
  echo -n "  等待表激活..."
  aws dynamodb wait table-exists --table-name "$NEW_TABLE" --region "$REGION"
  echo -e " ${GREEN}✓${NC}"
done

# ============================================================
# 4. Migrate DynamoDB data
# ============================================================
echo ""
echo -e "${CYAN}[Step 4/4] 迁移 DynamoDB 数据${NC}"

for i in "${!OLD_DDB_TABLES[@]}"; do
  OLD_TABLE="${OLD_DDB_TABLES[$i]}"
  NEW_TABLE="${NEW_DDB_TABLES[$i]}"

  # 检查旧表有多少数据
  ITEM_COUNT=$(aws dynamodb describe-table --table-name "$OLD_TABLE" --region "$REGION" \
    --query 'Table.ItemCount' --output text 2>/dev/null || echo "0")

  if [ "$ITEM_COUNT" == "0" ]; then
    echo -e "  ${GREEN}${OLD_TABLE}: 0 items, 无需迁移${NC}"
    continue
  fi

  echo -e "  ${YELLOW}${OLD_TABLE} → ${NEW_TABLE}: ${ITEM_COUNT} items${NC}"

  # Scan + BatchWrite
  python3 -c "
import boto3, json, sys

region = '${REGION}'
src = '${OLD_TABLE}'
dst = '${NEW_TABLE}'

dynamodb = boto3.resource('dynamodb', region_name=region)
src_table = dynamodb.Table(src)
dst_table = dynamodb.Table(dst)

# Scan all items
response = src_table.scan()
items = response.get('Items', [])

# Handle pagination
while 'LastEvaluatedKey' in response:
    response = src_table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
    items.extend(response.get('Items', []))

print(f'  扫描到 {len(items)} 条记录')

# Batch write
with dst_table.batch_writer() as batch:
    for item in items:
        batch.put_item(Item=item)

print(f'  ✓ 已写入 {len(items)} 条到 {dst}')
"
done

# ============================================================
# Summary
# ============================================================
echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  Phase 1 完成 — 数据迁移汇总${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo -e "  S3:       ${GREEN}${NEW_BUCKET}${NC} ← 已同步"
echo -e "  DynamoDB: ${GREEN}${NEW_DDB_TABLES[*]}${NC}"
echo ""
echo -e "  ${YELLOW}下一步: 运行 phase2-resources.sh${NC}"
