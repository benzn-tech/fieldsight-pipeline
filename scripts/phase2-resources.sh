#!/bin/bash
# ============================================================
# FieldSight Migration — Phase 2: Resource Migration
# 迁移所有手动创建的资源 (非 SAM 管理)
# 前置条件: phase1-data-migrate.sh 已完成
# ============================================================
set -euo pipefail

REGION="ap-southeast-2"
ACCOUNT="509194952652"
NEW_BUCKET="fieldsight-data-${ACCOUNT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

confirm() {
  echo -e "${YELLOW}$1${NC}"
  read -p "继续? (y/n): " answer
  [[ "$answer" == "y" ]] || { echo "已跳过"; return 1; }
}

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  FieldSight Migration Phase 2 — Resources${NC}"
echo -e "${CYAN}================================================${NC}"

# ============================================================
# 1. IAM Roles — 创建新角色
# ============================================================
echo ""
echo -e "${CYAN}[Step 1/7] 创建新 IAM Roles${NC}"

# ---- 1a. fieldsight-lambda-role ----
echo "  创建 fieldsight-lambda-role..."

if aws iam get-role --role-name fieldsight-lambda-role 2>/dev/null | grep -q fieldsight; then
  echo -e "  ${GREEN}已存在，跳过${NC}"
else
  # 获取旧角色的 trust policy
  TRUST=$(aws iam get-role --role-name sitesync-lambda-role \
    --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)

  aws iam create-role \
    --role-name fieldsight-lambda-role \
    --assume-role-policy-document "$TRUST" \
    --description "FieldSight Lambda execution role"

  # 附加 managed policy
  aws iam attach-role-policy \
    --role-name fieldsight-lambda-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  # 复制 inline policies
  for POLICY_NAME in $(aws iam list-role-policies --role-name sitesync-lambda-role \
    --query 'PolicyNames[]' --output text 2>/dev/null); do

    POLICY_DOC=$(aws iam get-role-policy \
      --role-name sitesync-lambda-role \
      --policy-name "$POLICY_NAME" \
      --query 'PolicyDocument' --output json)

    # 替换旧 bucket/table 引用
    UPDATED_DOC=$(echo "$POLICY_DOC" | sed \
      -e "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" \
      -e 's/sitesync-items/fieldsight-items/g' \
      -e 's/sitesync-reports/fieldsight-reports/g' \
      -e 's/sitesync-audit/fieldsight-audit/g' \
      -e 's/sitesync-transcripts/fieldsight-transcripts/g' \
      -e 's/sitesync-users/fieldsight-users/g')

    # 同步更新 policy 名字
    NEW_POLICY_NAME=$(echo "$POLICY_NAME" | sed 's/sitesync/fieldsight/g')

    aws iam put-role-policy \
      --role-name fieldsight-lambda-role \
      --policy-name "$NEW_POLICY_NAME" \
      --policy-document "$UPDATED_DOC"

    echo "    Inline policy: ${POLICY_NAME} → ${NEW_POLICY_NAME}"
  done
  echo -e "  ${GREEN}✓ fieldsight-lambda-role 创建完成${NC}"
fi

# ---- 1b. fieldsight-scheduler-role ----
echo "  创建 fieldsight-scheduler-role..."

if aws iam get-role --role-name fieldsight-scheduler-role 2>/dev/null | grep -q fieldsight; then
  echo -e "  ${GREEN}已存在，跳过${NC}"
else
  TRUST=$(aws iam get-role --role-name sitesync-scheduler-role \
    --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)

  aws iam create-role \
    --role-name fieldsight-scheduler-role \
    --assume-role-policy-document "$TRUST" \
    --description "FieldSight EventBridge Scheduler role"

  for POLICY_NAME in $(aws iam list-role-policies --role-name sitesync-scheduler-role \
    --query 'PolicyNames[]' --output text 2>/dev/null); do
    POLICY_DOC=$(aws iam get-role-policy \
      --role-name sitesync-scheduler-role \
      --policy-name "$POLICY_NAME" \
      --query 'PolicyDocument' --output json)
    NEW_POLICY_NAME=$(echo "$POLICY_NAME" | sed 's/sitesync/fieldsight/g')
    aws iam put-role-policy \
      --role-name fieldsight-scheduler-role \
      --policy-name "$NEW_POLICY_NAME" \
      --policy-document "$POLICY_DOC"
  done
  echo -e "  ${GREEN}✓ fieldsight-scheduler-role 创建完成${NC}"
fi

# ---- 1c. fieldsight-transcribe-callback-role ----
echo "  创建 fieldsight-transcribe-callback-role..."

if aws iam get-role --role-name fieldsight-transcribe-callback-role 2>/dev/null | grep -q fieldsight; then
  echo -e "  ${GREEN}已存在，跳过${NC}"
else
  TRUST=$(aws iam get-role --role-name sitesync-transcribe-callback-role \
    --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)

  aws iam create-role \
    --role-name fieldsight-transcribe-callback-role \
    --assume-role-policy-document "$TRUST" \
    --description "FieldSight Transcribe callback role"

  aws iam attach-role-policy \
    --role-name fieldsight-transcribe-callback-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  for POLICY_NAME in $(aws iam list-role-policies --role-name sitesync-transcribe-callback-role \
    --query 'PolicyNames[]' --output text 2>/dev/null); do
    POLICY_DOC=$(aws iam get-role-policy \
      --role-name sitesync-transcribe-callback-role \
      --policy-name "$POLICY_NAME" \
      --query 'PolicyDocument' --output json)
    UPDATED_DOC=$(echo "$POLICY_DOC" | sed \
      -e "s/realptt-downloads-sitesync/${NEW_BUCKET}/g" \
      -e 's/sitesync-transcripts/fieldsight-transcripts/g')
    NEW_POLICY_NAME=$(echo "$POLICY_NAME" | sed 's/sitesync/fieldsight/g')
    aws iam put-role-policy \
      --role-name fieldsight-transcribe-callback-role \
      --policy-name "$NEW_POLICY_NAME" \
      --policy-document "$UPDATED_DOC"
  done
  echo -e "  ${GREEN}✓ fieldsight-transcribe-callback-role 创建完成${NC}"
fi

# ---- 1d. Fargate roles ----
echo "  创建 fieldsight-fargate-execution / fieldsight-fargate-task..."

for OLD_ROLE in sitesync-fargate-execution sitesync-fargate-task; do
  NEW_ROLE=$(echo "$OLD_ROLE" | sed 's/sitesync/fieldsight/')

  if aws iam get-role --role-name "$NEW_ROLE" 2>/dev/null | grep -q "$NEW_ROLE"; then
    echo -e "  ${GREEN}${NEW_ROLE} 已存在，跳过${NC}"
    continue
  fi

  TRUST=$(aws iam get-role --role-name "$OLD_ROLE" \
    --query 'Role.AssumeRolePolicyDocument' --output json 2>/dev/null)
  aws iam create-role --role-name "$NEW_ROLE" --assume-role-policy-document "$TRUST"

  # Attached policies
  for POLICY_ARN in $(aws iam list-attached-role-policies --role-name "$OLD_ROLE" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam attach-role-policy --role-name "$NEW_ROLE" --policy-arn "$POLICY_ARN"
  done

  # Inline policies
  for POLICY_NAME in $(aws iam list-role-policies --role-name "$OLD_ROLE" \
    --query 'PolicyNames[]' --output text 2>/dev/null); do
    POLICY_DOC=$(aws iam get-role-policy --role-name "$OLD_ROLE" --policy-name "$POLICY_NAME" \
      --query 'PolicyDocument' --output json)
    UPDATED_DOC=$(echo "$POLICY_DOC" | sed "s/realptt-downloads-sitesync/${NEW_BUCKET}/g")
    aws iam put-role-policy --role-name "$NEW_ROLE" --policy-name "$POLICY_NAME" --policy-document "$UPDATED_DOC"
  done
  echo -e "  ${GREEN}✓ ${NEW_ROLE} 创建完成${NC}"
done

echo ""
echo -e "  ${YELLOW}等待 10 秒让 IAM 角色全局传播...${NC}"
sleep 10

# ============================================================
# 2. Lambda Layer — 发布新版本
# ============================================================
echo ""
echo -e "${CYAN}[Step 2/7] Lambda Layer: sitesync-vad-layer → fieldsight-vad-layer${NC}"

LAYER_EXISTS=$(aws lambda list-layers --region "$REGION" \
  --query "Layers[?LayerName=='fieldsight-vad-layer'].LayerName" --output text 2>/dev/null)

if [ -n "$LAYER_EXISTS" ]; then
  echo -e "  ${GREEN}fieldsight-vad-layer 已存在，跳过${NC}"
else
  # 获取旧 layer 最新版本信息
  OLD_LAYER_VERSION=$(aws lambda list-layer-versions --layer-name sitesync-vad-layer --region "$REGION" \
    --query 'LayerVersions[0].Version' --output text 2>/dev/null)
  OLD_LAYER_ARN=$(aws lambda list-layer-versions --layer-name sitesync-vad-layer --region "$REGION" \
    --query 'LayerVersions[0].LayerVersionArn' --output text 2>/dev/null)

  if [ "$OLD_LAYER_VERSION" == "None" ] || [ -z "$OLD_LAYER_VERSION" ]; then
    echo -e "  ${YELLOW}旧 layer 不存在，跳过${NC}"
  else
    echo "  下载旧 layer 内容 (version ${OLD_LAYER_VERSION})..."
    LAYER_URL=$(aws lambda get-layer-version --layer-name sitesync-vad-layer \
      --version-number "$OLD_LAYER_VERSION" --region "$REGION" \
      --query 'Content.Location' --output text)
    curl -sL "$LAYER_URL" -o /tmp/vad-layer.zip

    echo "  发布 fieldsight-vad-layer..."
    aws lambda publish-layer-version \
      --layer-name fieldsight-vad-layer \
      --region "$REGION" \
      --zip-file fileb:///tmp/vad-layer.zip \
      --compatible-runtimes python3.12 \
      --description "FieldSight VAD - Silero model + dependencies"

    rm -f /tmp/vad-layer.zip
    echo -e "  ${GREEN}✓ fieldsight-vad-layer 发布完成${NC}"
  fi
fi

NEW_LAYER_ARN=$(aws lambda list-layer-versions --layer-name fieldsight-vad-layer --region "$REGION" \
  --query 'LayerVersions[0].LayerVersionArn' --output text 2>/dev/null || echo "")

# ============================================================
# 3. 迁移手动创建的 Lambda Functions (4个)
# ============================================================
echo ""
echo -e "${CYAN}[Step 3/7] 迁移手动创建的 Lambda Functions${NC}"

LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/fieldsight-lambda-role"
CALLBACK_ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/fieldsight-transcribe-callback-role"

# 定义迁移映射: OLD_NAME|NEW_NAME|ROLE_ARN
MANUAL_LAMBDAS=(
  "sitesync-vad|fieldsight-vad|${LAMBDA_ROLE_ARN}"
  "sitesync-api|fieldsight-api|${LAMBDA_ROLE_ARN}"
  "sitesync-transcribe-callback|fieldsight-transcribe-callback|${CALLBACK_ROLE_ARN}"
  "realptt-meeting-minutes|fieldsight-meeting-minutes|${LAMBDA_ROLE_ARN}"
)

for MAPPING in "${MANUAL_LAMBDAS[@]}"; do
  IFS='|' read -r OLD_FN NEW_FN ROLE <<< "$MAPPING"

  echo ""
  echo -e "  ${CYAN}${OLD_FN} → ${NEW_FN}${NC}"

  # 检查新函数是否已存在
  if aws lambda get-function --function-name "$NEW_FN" --region "$REGION" 2>/dev/null | grep -q "$NEW_FN"; then
    echo -e "  ${GREEN}${NEW_FN} 已存在，跳过创建${NC}"
    continue
  fi

  # 下载旧函数代码
  echo "    下载 ${OLD_FN} 代码..."
  CODE_URL=$(aws lambda get-function --function-name "$OLD_FN" --region "$REGION" \
    --query 'Code.Location' --output text 2>/dev/null)

  if [ -z "$CODE_URL" ] || [ "$CODE_URL" == "None" ]; then
    echo -e "  ${RED}无法获取 ${OLD_FN} 代码，跳过${NC}"
    continue
  fi

  curl -sL "$CODE_URL" -o "/tmp/${OLD_FN}.zip"

  # 获取旧函数配置
  OLD_CONFIG=$(aws lambda get-function-configuration --function-name "$OLD_FN" --region "$REGION" 2>/dev/null)
  HANDLER=$(echo "$OLD_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['Handler'])")
  RUNTIME=$(echo "$OLD_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['Runtime'])")
  TIMEOUT=$(echo "$OLD_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['Timeout'])")
  MEMSIZE=$(echo "$OLD_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['MemorySize'])")

  # 提取并更新环境变量
  ENV_JSON=$(echo "$OLD_CONFIG" | python3 -c "
import sys, json
config = json.load(sys.stdin)
env = config.get('Environment', {}).get('Variables', {})

# 替换所有旧引用
replacements = {
    'realptt-downloads-sitesync': '${NEW_BUCKET}',
    'sitesync-items': 'fieldsight-items',
    'sitesync-reports': 'fieldsight-reports',
    'sitesync-audit': 'fieldsight-audit',
    'sitesync-transcripts': 'fieldsight-transcripts',
    'sitesync-users': 'fieldsight-users',
    'realptt-report-generator': 'fieldsight-report-generator',
    'realptt-downloader': 'fieldsight-downloader',
    'realptt-downloader-cluster': 'fieldsight-downloader-cluster',
}

for key, val in env.items():
    for old, new in replacements.items():
        if old in str(val):
            env[key] = str(val).replace(old, new)

# TASK_DEF ARN 替换
for key, val in env.items():
    if 'realptt-fargate-downloader' in str(val):
        env[key] = str(val).replace('realptt-fargate-downloader', 'fieldsight-fargate-downloader')

print(json.dumps({'Variables': env}))
")

  # 获取 layers
  LAYERS_JSON=$(echo "$OLD_CONFIG" | python3 -c "
import sys, json
config = json.load(sys.stdin)
layers = [l['Arn'] for l in config.get('Layers', [])]
# 替换旧 layer ARN
layers = [l.replace('sitesync-vad-layer', 'fieldsight-vad-layer') for l in layers]
print(json.dumps(layers) if layers else '[]')
")

  # 创建新函数
  echo "    创建 ${NEW_FN}..."
  CREATE_CMD="aws lambda create-function \
    --function-name ${NEW_FN} \
    --region ${REGION} \
    --runtime ${RUNTIME} \
    --handler ${HANDLER} \
    --role ${ROLE} \
    --timeout ${TIMEOUT} \
    --memory-size ${MEMSIZE} \
    --zip-file fileb:///tmp/${OLD_FN}.zip \
    --environment '${ENV_JSON}'"

  if [ "$LAYERS_JSON" != "[]" ]; then
    CREATE_CMD="${CREATE_CMD} --layers ${LAYERS_JSON}"
  fi

  eval "$CREATE_CMD" > /dev/null

  rm -f "/tmp/${OLD_FN}.zip"
  echo -e "  ${GREEN}✓ ${NEW_FN} 创建完成${NC}"
done

# ============================================================
# 4. 更新 SAM 管理的 Lambda 环境变量 (先于 SAM deploy)
# ============================================================
echo ""
echo -e "${CYAN}[Step 4/7] 更新 SAM Lambda 环境变量 (临时, 指向新资源)${NC}"

SAM_LAMBDAS=("realptt-orchestrator" "realptt-downloader" "realptt-transcribe" "realptt-report-generator" "realptt-fargate-trigger")

for FN in "${SAM_LAMBDAS[@]}"; do
  echo -e "  更新 ${FN} 环境变量..."

  # 获取当前环境变量
  CURRENT_ENV=$(aws lambda get-function-configuration --function-name "$FN" --region "$REGION" \
    --query 'Environment' --output json 2>/dev/null)

  if [ "$CURRENT_ENV" == "null" ] || [ -z "$CURRENT_ENV" ]; then
    echo -e "  ${YELLOW}${FN} 无环境变量，跳过${NC}"
    continue
  fi

  # 更新环境变量中的引用
  UPDATED_ENV=$(echo "$CURRENT_ENV" | python3 -c "
import sys, json
env = json.load(sys.stdin)

replacements = {
    'realptt-downloads-sitesync': '${NEW_BUCKET}',
    'sitesync-items': 'fieldsight-items',
    'sitesync-reports': 'fieldsight-reports',
    'sitesync-audit': 'fieldsight-audit',
    'sitesync-transcripts': 'fieldsight-transcripts',
    'sitesync-users': 'fieldsight-users',
    'realptt-report-generator': 'fieldsight-report-generator',
    'realptt-downloader': 'fieldsight-downloader',
    'realptt-downloader-cluster': 'fieldsight-downloader-cluster',
}

for key, val in env.get('Variables', {}).items():
    for old, new in replacements.items():
        if old in str(val):
            env['Variables'][key] = str(val).replace(old, new)
    # 特殊处理 TASK_DEF ARN
    if 'realptt-fargate-downloader' in str(env['Variables'].get(key, '')):
        env['Variables'][key] = env['Variables'][key].replace('realptt-fargate-downloader', 'fieldsight-fargate-downloader')

print(json.dumps(env))
")

  aws lambda update-function-configuration \
    --function-name "$FN" \
    --region "$REGION" \
    --environment "$UPDATED_ENV" > /dev/null

  echo -e "  ${GREEN}✓ ${FN} 环境变量已更新${NC}"
done

# ============================================================
# 5. EventBridge Rule
# ============================================================
echo ""
echo -e "${CYAN}[Step 5/7] EventBridge: sitesync-transcribe-state-change → fieldsight-transcribe-state-change${NC}"

# 获取旧 rule 配置
OLD_RULE=$(aws events describe-rule --name sitesync-transcribe-state-change --region "$REGION" 2>/dev/null || echo "")

if [ -n "$OLD_RULE" ]; then
  EVENT_PATTERN=$(echo "$OLD_RULE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('EventPattern',''))")
  DESCRIPTION=$(echo "$OLD_RULE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('Description','FieldSight transcribe state change'))")

  # 获取 targets
  TARGETS=$(aws events list-targets-by-rule --rule sitesync-transcribe-state-change --region "$REGION" 2>/dev/null)
  TARGET_LIST=$(echo "$TARGETS" | python3 -c "
import sys, json
targets = json.load(sys.stdin).get('Targets', [])
# 更新 target ARN 中的函数名
for t in targets:
    t['Arn'] = t['Arn'].replace('sitesync-transcribe-callback', 'fieldsight-transcribe-callback')
print(json.dumps(targets))
")

  # 创建新 rule
  aws events put-rule \
    --name fieldsight-transcribe-state-change \
    --region "$REGION" \
    --event-pattern "$EVENT_PATTERN" \
    --state ENABLED \
    --description "$DESCRIPTION" > /dev/null

  # 添加 targets
  aws events put-targets \
    --rule fieldsight-transcribe-state-change \
    --region "$REGION" \
    --targets "$TARGET_LIST" > /dev/null

  # 添加 Lambda 权限让 EventBridge 调用新函数
  aws lambda add-permission \
    --function-name fieldsight-transcribe-callback \
    --region "$REGION" \
    --statement-id fieldsight-eventbridge-invoke \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT}:rule/fieldsight-transcribe-state-change" 2>/dev/null || true

  echo -e "  ${GREEN}✓ fieldsight-transcribe-state-change 创建完成${NC}"
else
  echo -e "  ${YELLOW}旧 rule 不存在，跳过${NC}"
fi

# ============================================================
# 6. API Gateway — 更新名称
# ============================================================
echo ""
echo -e "${CYAN}[Step 6/7] API Gateway: sitesync-api → fieldsight-api${NC}"

API_ID="khfj3p1fkb"

aws apigateway update-rest-api \
  --rest-api-id "$API_ID" \
  --region "$REGION" \
  --patch-operations op=replace,path=/name,value=fieldsight-api > /dev/null 2>&1 && \
  echo -e "  ${GREEN}✓ API Gateway 名称已更新为 fieldsight-api${NC}" || \
  echo -e "  ${YELLOW}API Gateway 更新失败，请手动检查${NC}"

# 更新 Lambda 集成: 如果 API GW 的后端指向 sitesync-api Lambda，需要更新
echo "  检查 API Gateway 集成目标..."
RESOURCES=$(aws apigateway get-resources --rest-api-id "$API_ID" --region "$REGION" \
  --query 'items[].id' --output text 2>/dev/null)

for RES_ID in $RESOURCES; do
  for METHOD in GET POST PUT DELETE PATCH OPTIONS; do
    INTEGRATION=$(aws apigateway get-integration \
      --rest-api-id "$API_ID" \
      --resource-id "$RES_ID" \
      --http-method "$METHOD" \
      --region "$REGION" 2>/dev/null || echo "")
    if echo "$INTEGRATION" | grep -q "sitesync-api"; then
      NEW_URI=$(echo "$INTEGRATION" | python3 -c "
import sys, json
i = json.load(sys.stdin)
uri = i.get('uri', '')
print(uri.replace('sitesync-api', 'fieldsight-api'))
")
      aws apigateway update-integration \
        --rest-api-id "$API_ID" \
        --resource-id "$RES_ID" \
        --http-method "$METHOD" \
        --region "$REGION" \
        --patch-operations "op=replace,path=/uri,value=${NEW_URI}" > /dev/null 2>&1
      echo -e "    ${GREEN}✓ 更新 ${METHOD} 集成 → fieldsight-api${NC}"
    fi
  done
done

# 重新部署 API
STAGE=$(aws apigateway get-stages --rest-api-id "$API_ID" --region "$REGION" \
  --query 'item[0].stageName' --output text 2>/dev/null)
if [ -n "$STAGE" ] && [ "$STAGE" != "None" ]; then
  aws apigateway create-deployment \
    --rest-api-id "$API_ID" \
    --stage-name "$STAGE" \
    --region "$REGION" > /dev/null 2>&1
  echo -e "  ${GREEN}✓ API Gateway 已重新部署到 stage: ${STAGE}${NC}"
fi

# 添加 Lambda 权限让 API GW 调用新函数
aws lambda add-permission \
  --function-name fieldsight-api \
  --region "$REGION" \
  --statement-id apigateway-invoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT}:${API_ID}/*" 2>/dev/null || true

# ============================================================
# 7. CloudFront — 更新 Comment
# ============================================================
echo ""
echo -e "${CYAN}[Step 7/7] CloudFront: 更新 comment sitesync-web → fieldsight-web${NC}"

CF_ID="E12IVML224YUEE"

# 获取当前配置 + ETag
CF_CONFIG=$(aws cloudfront get-distribution-config --id "$CF_ID" 2>/dev/null)
ETAG=$(echo "$CF_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin)['ETag'])")

# 更新 comment 和 origin (如果指向旧 bucket)
echo "$CF_CONFIG" | python3 -c "
import sys, json

data = json.load(sys.stdin)
config = data['DistributionConfig']

# 更新 comment
config['Comment'] = config.get('Comment', '').replace('sitesync', 'fieldsight')

# 更新 origin 如果指向旧 bucket
for origin in config.get('Origins', {}).get('Items', []):
    dn = origin.get('DomainName', '')
    if 'sitesync-web' in dn:
        origin['DomainName'] = dn.replace('sitesync-web', 'fieldsight-web')
        origin['Id'] = origin.get('Id', '').replace('sitesync', 'fieldsight')
    if 'realptt-downloads-sitesync' in dn:
        origin['DomainName'] = dn.replace('realptt-downloads-sitesync', '${NEW_BUCKET}')

# 更新 DefaultCacheBehavior TargetOriginId
target = config.get('DefaultCacheBehavior', {}).get('TargetOriginId', '')
if 'sitesync' in target:
    config['DefaultCacheBehavior']['TargetOriginId'] = target.replace('sitesync', 'fieldsight')

json.dump(config, open('/tmp/cf-config.json', 'w'), indent=2)
print('Config 已保存到 /tmp/cf-config.json')
"

aws cloudfront update-distribution \
  --id "$CF_ID" \
  --distribution-config file:///tmp/cf-config.json \
  --if-match "$ETAG" > /dev/null 2>&1 && \
  echo -e "  ${GREEN}✓ CloudFront 配置已更新${NC}" || \
  echo -e "  ${YELLOW}CloudFront 更新失败，可能需要手动处理 origin 配置${NC}"

# ============================================================
# Summary
# ============================================================
echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  Phase 2 完成 — 资源迁移汇总${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
echo "  ✓ IAM Roles:       5 个新角色创建"
echo "  ✓ Lambda Layer:    fieldsight-vad-layer"
echo "  ✓ Lambda 手动:     4 个新函数创建"
echo "  ✓ Lambda SAM:      5 个环境变量已更新"
echo "  ✓ EventBridge:     fieldsight-transcribe-state-change"
echo "  ✓ API Gateway:     fieldsight-api"
echo "  ✓ CloudFront:      comment 已更新"
echo ""
echo -e "  ${YELLOW}下一步: 更新 template.yaml 并运行 sam deploy (Phase 3)${NC}"
echo -e "  ${YELLOW}        然后运行 phase4-cleanup.sh 清理旧资源${NC}"
