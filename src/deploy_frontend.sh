#!/bin/bash
# ============================================================
# SiteSync 前端基础设施部署 v1.0 (CloudShell / CLI)
# ============================================================
#
# 部署前端所需的全部 AWS 资源:
#   1. Cognito User Pool + App Client
#   2. S3 前端 Bucket (sitesync-web)
#   3. CloudFront Distribution (OAC + SPA routing)
#   4. API Gateway REST API + Cognito Authorizer
#   5. Lambda: sitesync-api (后端 API)
#   6. DynamoDB: fieldsight-users 表
#   7. 连接 CloudFront → API Gateway (/api/*)
#
# 前置条件:
#   - Backend pipeline 已部署 (deploy_sitesync.sh)
#   - lambda_sitesync_api.py 已上传到 CloudShell
#
# 用法:
#   chmod +x deploy_frontend.sh
#   ./deploy_frontend.sh
#
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════╗"
echo "║  SiteSync 前端基础设施部署 v1.0              ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Config ───────────────────────────────────────────────────
REGION=$(aws configure get region 2>/dev/null || echo "ap-southeast-2")
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
DATA_BUCKET="fieldsight-data-509194952652"
WEB_BUCKET="sitesync-web-${ACCOUNT_ID}"
LAMBDA_ROLE="sitesync-lambda-role"  # reuse from backend deploy
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"

echo -e "  Region:       ${GREEN}${REGION}${NC}"
echo -e "  Account:      ${GREEN}${ACCOUNT_ID}${NC}"
echo -e "  Data Bucket:  ${GREEN}${DATA_BUCKET}${NC}"
echo -e "  Web Bucket:   ${GREEN}${WEB_BUCKET}${NC}"

# Check required file
if [ ! -f "lambda_sitesync_api.py" ]; then
    echo -e "${RED}❌ lambda_sitesync_api.py not found. Upload it first.${NC}"
    exit 1
fi
echo -e "  ✅ lambda_sitesync_api.py"

# ── 1. Cognito User Pool ────────────────────────────────────
echo ""
echo -e "${YELLOW}[1/7] Cognito User Pool${NC}"

# Check if already exists
POOL_ID=$(aws cognito-idp list-user-pools --max-results 20 --region "${REGION}" \
    --query "UserPools[?Name=='fieldsight-users'].Id" --output text 2>/dev/null || echo "")

if [ -n "$POOL_ID" ] && [ "$POOL_ID" != "None" ]; then
    echo -e "  ${GREEN}User Pool 已存在: ${POOL_ID}${NC}"
else
    POOL_RESPONSE=$(aws cognito-idp create-user-pool \
        --pool-name "fieldsight-users" \
        --auto-verified-attributes email \
        --username-attributes email \
        --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
        --schema '[{"Name":"email","Required":true,"Mutable":true},{"Name":"name","Required":true,"Mutable":true}]' \
        --region "${REGION}" --output json)

    POOL_ID=$(echo "$POOL_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['UserPool']['Id'])")
    echo -e "  ${GREEN}✅ User Pool: ${POOL_ID}${NC}"
fi

# App Client
CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "${POOL_ID}" --region "${REGION}" \
    --query "UserPoolClients[?ClientName=='sitesync-web'].ClientId" --output text 2>/dev/null || echo "")

if [ -n "$CLIENT_ID" ] && [ "$CLIENT_ID" != "None" ]; then
    echo -e "  ${GREEN}App Client 已存在: ${CLIENT_ID}${NC}"
else
    CLIENT_RESPONSE=$(aws cognito-idp create-user-pool-client \
        --user-pool-id "${POOL_ID}" \
        --client-name "sitesync-web" \
        --no-generate-secret \
        --explicit-auth-flows ALLOW_USER_SRP_AUTH ALLOW_REFRESH_TOKEN_AUTH ALLOW_USER_PASSWORD_AUTH \
        --supported-identity-providers COGNITO \
        --region "${REGION}" --output json)

    CLIENT_ID=$(echo "$CLIENT_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['UserPoolClient']['ClientId'])")
    echo -e "  ${GREEN}✅ App Client: ${CLIENT_ID}${NC}"
fi

# Create admin user
echo -e "  创建管理员用户..."
read -p "  Admin Email: " ADMIN_EMAIL
if [ -n "$ADMIN_EMAIL" ]; then
    aws cognito-idp admin-create-user \
        --user-pool-id "${POOL_ID}" \
        --username "${ADMIN_EMAIL}" \
        --user-attributes Name=email,Value="${ADMIN_EMAIL}" Name=email_verified,Value=true Name=name,Value=Admin \
        --temporary-password "SiteSync2026!" \
        --region "${REGION}" 2>/dev/null || echo "  (用户可能已存在)"
    echo -e "  ${GREEN}Admin: ${ADMIN_EMAIL} (临时密码: SiteSync2026!)${NC}"
fi

# ── 2. DynamoDB: fieldsight-users ──────────────────────────────
echo ""
echo -e "${YELLOW}[2/7] DynamoDB fieldsight-users${NC}"

if aws dynamodb describe-table --table-name "fieldsight-users" --region "${REGION}" 2>/dev/null > /dev/null; then
    echo -e "  ${GREEN}表已存在${NC}"
else
    aws dynamodb create-table --table-name "fieldsight-users" \
        --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
        --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
        --billing-mode PAY_PER_REQUEST --region "${REGION}" > /dev/null
    echo -e "  ${GREEN}✅ 已创建${NC}"
fi

# ── 3. S3 前端 Bucket ────────────────────────────────────────
echo ""
echo -e "${YELLOW}[3/7] S3 前端 Bucket${NC}"

if aws s3 ls "s3://${WEB_BUCKET}" 2>/dev/null; then
    echo -e "  ${GREEN}已存在: ${WEB_BUCKET}${NC}"
else
    aws s3 mb "s3://${WEB_BUCKET}" --region "${REGION}"
    aws s3api put-public-access-block --bucket "${WEB_BUCKET}" \
        --public-access-block-configuration \
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
    echo -e "  ${GREEN}✅ 已创建 (私有, OAC 访问)${NC}"
fi

# Upload placeholder index.html
cat > /tmp/index.html <<'HTML'
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SiteSync</title>
<style>body{font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#F2F5FA;color:#1A2332}
.card{background:white;padding:40px;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.1);text-align:center}
h1{color:#1B3A5C;margin-bottom:8px}p{color:#5A6B7F}</style></head>
<body><div class="card"><h1>SiteSync</h1><p>Frontend deploying soon...</p><p style="font-size:12px;color:#94A5B9">API: /api/health</p></div></body></html>
HTML
aws s3 cp /tmp/index.html "s3://${WEB_BUCKET}/index.html" --content-type "text/html"
echo -e "  ${GREEN}placeholder index.html 已上传${NC}"

# ── 4. sitesync-api Lambda ───────────────────────────────────
echo ""
echo -e "${YELLOW}[4/7] sitesync-api Lambda${NC}"

zip -j /tmp/lambda_sitesync_api.zip lambda_sitesync_api.py > /dev/null
aws s3 cp /tmp/lambda_sitesync_api.zip "s3://${DATA_BUCKET}/code/lambda_sitesync_api.zip" > /dev/null

API_ENV=$(python3 -c "
import json
print(json.dumps({'Variables': {
    'S3_BUCKET': '${DATA_BUCKET}',
    'REPORT_PREFIX': 'reports/',
    'ITEMS_TABLE': 'fieldsight-items',
    'REPORTS_TABLE': 'fieldsight-reports',
    'AUDIT_TABLE': 'fieldsight-audit',
    'USERS_TABLE': 'fieldsight-users',
    'REPORT_FUNCTION': 'realptt-report-generator',
}}))
")

if aws lambda get-function --function-name "sitesync-api" --region "${REGION}" 2>/dev/null > /dev/null; then
    aws lambda update-function-code --function-name "sitesync-api" \
        --s3-bucket "${DATA_BUCKET}" --s3-key "code/lambda_sitesync_api.zip" \
        --region "${REGION}" > /dev/null
    aws lambda wait function-updated --function-name "sitesync-api" --region "${REGION}" 2>/dev/null || sleep 5
    aws lambda update-function-configuration --function-name "sitesync-api" \
        --environment "${API_ENV}" --region "${REGION}" > /dev/null
    echo -e "  🔄 已更新"
else
    aws lambda create-function --function-name "sitesync-api" \
        --runtime python3.12 --handler "lambda_sitesync_api.lambda_handler" \
        --role "${LAMBDA_ROLE_ARN}" \
        --code "S3Bucket=${DATA_BUCKET},S3Key=code/lambda_sitesync_api.zip" \
        --timeout 60 --memory-size 256 \
        --architectures x86_64 --environment "${API_ENV}" \
        --region "${REGION}" > /dev/null
    echo -e "  🆕 已创建"
fi
echo -e "  ${GREEN}✅ sitesync-api (60s, 256MB)${NC}"

# ── 5. API Gateway ───────────────────────────────────────────
echo ""
echo -e "${YELLOW}[5/7] API Gateway${NC}"

# Check existing
API_ID=$(aws apigateway get-rest-apis --region "${REGION}" \
    --query "items[?name=='sitesync-api'].id" --output text 2>/dev/null || echo "")

if [ -n "$API_ID" ] && [ "$API_ID" != "None" ]; then
    echo -e "  ${GREEN}API 已存在: ${API_ID}${NC}"
else
    # Create REST API
    API_ID=$(aws apigateway create-rest-api \
        --name "sitesync-api" \
        --endpoint-configuration '{"types":["REGIONAL"]}' \
        --region "${REGION}" \
        --query "id" --output text)
    echo -e "  ${GREEN}✅ API 已创建: ${API_ID}${NC}"
fi

# Get root resource ID
ROOT_ID=$(aws apigateway get-resources --rest-api-id "${API_ID}" --region "${REGION}" \
    --query "items[?path=='/'].id" --output text)

# Create /api resource (if not exists)
API_RESOURCE_ID=$(aws apigateway get-resources --rest-api-id "${API_ID}" --region "${REGION}" \
    --query "items[?path=='/api'].id" --output text 2>/dev/null || echo "")

if [ -z "$API_RESOURCE_ID" ] || [ "$API_RESOURCE_ID" = "None" ]; then
    API_RESOURCE_ID=$(aws apigateway create-resource \
        --rest-api-id "${API_ID}" --parent-id "${ROOT_ID}" \
        --path-part "api" --region "${REGION}" \
        --query "id" --output text)
fi

# Create /api/{proxy+} resource (if not exists)
PROXY_ID=$(aws apigateway get-resources --rest-api-id "${API_ID}" --region "${REGION}" \
    --query "items[?path=='/api/{proxy+}'].id" --output text 2>/dev/null || echo "")

if [ -z "$PROXY_ID" ] || [ "$PROXY_ID" = "None" ]; then
    PROXY_ID=$(aws apigateway create-resource \
        --rest-api-id "${API_ID}" --parent-id "${API_RESOURCE_ID}" \
        --path-part "{proxy+}" --region "${REGION}" \
        --query "id" --output text)
fi

# Create Cognito Authorizer
AUTH_ID=$(aws apigateway get-authorizers --rest-api-id "${API_ID}" --region "${REGION}" \
    --query "items[?name=='sitesync-cognito'].id" --output text 2>/dev/null || echo "")

if [ -z "$AUTH_ID" ] || [ "$AUTH_ID" = "None" ]; then
    AUTH_ID=$(aws apigateway create-authorizer \
        --rest-api-id "${API_ID}" \
        --name "sitesync-cognito" \
        --type COGNITO_USER_POOLS \
        --provider-arns "arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}" \
        --identity-source "method.request.header.Authorization" \
        --region "${REGION}" \
        --query "id" --output text)
    echo -e "  ${GREEN}✅ Cognito Authorizer${NC}"
else
    echo -e "  ${GREEN}Authorizer 已存在${NC}"
fi

# Lambda ARN for integration
API_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:sitesync-api"

# Create ANY method on /api/{proxy+}
aws apigateway put-method \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method ANY --authorization-type COGNITO_USER_POOLS \
    --authorizer-id "${AUTH_ID}" \
    --region "${REGION}" 2>/dev/null || true

# Lambda integration
aws apigateway put-integration \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method ANY --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${API_LAMBDA_ARN}/invocations" \
    --region "${REGION}" 2>/dev/null || true

# Also create ANY on /api (for /api/health etc without trailing path)
aws apigateway put-method \
    --rest-api-id "${API_ID}" --resource-id "${API_RESOURCE_ID}" \
    --http-method ANY --authorization-type NONE \
    --region "${REGION}" 2>/dev/null || true

aws apigateway put-integration \
    --rest-api-id "${API_ID}" --resource-id "${API_RESOURCE_ID}" \
    --http-method ANY --type AWS_PROXY \
    --integration-http-method POST \
    --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${API_LAMBDA_ARN}/invocations" \
    --region "${REGION}" 2>/dev/null || true

# Grant API Gateway permission to invoke Lambda
aws lambda add-permission --function-name "sitesync-api" \
    --statement-id "apigateway-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "apigateway.amazonaws.com" \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    --region "${REGION}" 2>/dev/null || true

# Enable CORS on proxy resource - create OPTIONS method
aws apigateway put-method \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method OPTIONS --authorization-type NONE \
    --region "${REGION}" 2>/dev/null || true

aws apigateway put-integration \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method OPTIONS --type MOCK \
    --request-templates '{"application/json":"{\"statusCode\":200}"}' \
    --region "${REGION}" 2>/dev/null || true

aws apigateway put-method-response \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{"method.response.header.Access-Control-Allow-Headers":false,"method.response.header.Access-Control-Allow-Methods":false,"method.response.header.Access-Control-Allow-Origin":false}' \
    --region "${REGION}" 2>/dev/null || true

aws apigateway put-integration-response \
    --rest-api-id "${API_ID}" --resource-id "${PROXY_ID}" \
    --http-method OPTIONS --status-code 200 \
    --response-parameters '{"method.response.header.Access-Control-Allow-Headers":"'"'"'Content-Type,Authorization'"'"'","method.response.header.Access-Control-Allow-Methods":"'"'"'GET,POST,PATCH,OPTIONS'"'"'","method.response.header.Access-Control-Allow-Origin":"'"'"'*'"'"'"}' \
    --region "${REGION}" 2>/dev/null || true

# Deploy API
aws apigateway create-deployment \
    --rest-api-id "${API_ID}" --stage-name "prod" \
    --region "${REGION}" > /dev/null 2>/dev/null || true

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/prod"
echo -e "  ${GREEN}✅ API Gateway deployed: ${API_URL}${NC}"

# ── 6. CloudFront Distribution ───────────────────────────────
echo ""
echo -e "${YELLOW}[6/7] CloudFront Distribution${NC}"

# Check existing
CF_DIST_ID=$(aws cloudfront list-distributions --region "${REGION}" \
    --query "DistributionList.Items[?Comment=='sitesync-web'].Id" --output text 2>/dev/null || echo "")

if [ -n "$CF_DIST_ID" ] && [ "$CF_DIST_ID" != "None" ]; then
    CF_DOMAIN=$(aws cloudfront get-distribution --id "${CF_DIST_ID}" \
        --query "Distribution.DomainName" --output text)
    echo -e "  ${GREEN}已存在: ${CF_DOMAIN}${NC}"
else
    # Create OAC
    OAC_ID=$(aws cloudfront create-origin-access-control \
        --origin-access-control-config "{
            \"Name\":\"sitesync-web-oac\",
            \"OriginAccessControlOriginType\":\"s3\",
            \"SigningBehavior\":\"always\",
            \"SigningProtocol\":\"sigv4\"
        }" --query "OriginAccessControl.Id" --output text --region "${REGION}" 2>/dev/null || echo "")

    if [ -z "$OAC_ID" ] || [ "$OAC_ID" = "None" ]; then
        OAC_ID=$(aws cloudfront list-origin-access-controls \
            --query "OriginAccessControlList.Items[?Name=='sitesync-web-oac'].Id" \
            --output text 2>/dev/null || echo "")
    fi

    # Create distribution config
    cat > /tmp/cf-config.json <<CFJSON
{
    "CallerReference": "sitesync-$(date +%s)",
    "Comment": "sitesync-web",
    "DefaultRootObject": "index.html",
    "Enabled": true,
    "Origins": {
        "Quantity": 2,
        "Items": [
            {
                "Id": "s3-web",
                "DomainName": "${WEB_BUCKET}.s3.${REGION}.amazonaws.com",
                "OriginAccessControlId": "${OAC_ID}",
                "S3OriginConfig": {"OriginAccessIdentity": ""}
            },
            {
                "Id": "api-gateway",
                "DomainName": "${API_ID}.execute-api.${REGION}.amazonaws.com",
                "OriginPath": "/prod",
                "CustomOriginConfig": {
                    "HTTPPort": 80,
                    "HTTPSPort": 443,
                    "OriginProtocolPolicy": "https-only"
                }
            }
        ]
    },
    "DefaultCacheBehavior": {
        "TargetOriginId": "s3-web",
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
        "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
        "ForwardedValues": {"QueryString": false, "Cookies": {"Forward": "none"}},
        "MinTTL": 0, "DefaultTTL": 86400, "MaxTTL": 31536000,
        "Compress": true
    },
    "CacheBehaviors": {
        "Quantity": 1,
        "Items": [
            {
                "PathPattern": "/api/*",
                "TargetOriginId": "api-gateway",
                "ViewerProtocolPolicy": "https-only",
                "AllowedMethods": {"Quantity": 7, "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"]},
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
                "ForwardedValues": {
                    "QueryString": true,
                    "Cookies": {"Forward": "none"},
                    "Headers": {"Quantity": 2, "Items": ["Authorization", "Content-Type"]}
                },
                "MinTTL": 0, "DefaultTTL": 0, "MaxTTL": 0
            }
        ]
    },
    "CustomErrorResponses": {
        "Quantity": 2,
        "Items": [
            {"ErrorCode": 403, "ResponsePagePath": "/index.html", "ResponseCode": "200", "ErrorCachingMinTTL": 10},
            {"ErrorCode": 404, "ResponsePagePath": "/index.html", "ResponseCode": "200", "ErrorCachingMinTTL": 10}
        ]
    },
    "PriceClass": "PriceClass_All"
}
CFJSON

    CF_RESULT=$(aws cloudfront create-distribution \
        --distribution-config file:///tmp/cf-config.json \
        --output json --region "${REGION}")

    CF_DIST_ID=$(echo "$CF_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Distribution']['Id'])")
    CF_DOMAIN=$(echo "$CF_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['Distribution']['DomainName'])")
    echo -e "  ${GREEN}✅ Distribution: ${CF_DOMAIN}${NC}"
    echo -e "  ${YELLOW}⏳ 部署中 (约 5-10 分钟)${NC}"
fi

# ── 7. S3 Bucket Policy (allow CloudFront OAC) ──────────────
echo ""
echo -e "${YELLOW}[7/7] S3 Bucket Policy + 配置输出${NC}"

cat > /tmp/web-bucket-policy.json <<POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowCloudFrontServicePrincipal",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::${WEB_BUCKET}/*",
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": "arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${CF_DIST_ID}"
                }
            }
        }
    ]
}
POLICY

aws s3api put-bucket-policy --bucket "${WEB_BUCKET}" \
    --policy file:///tmp/web-bucket-policy.json
echo -e "  ${GREEN}✅ Bucket Policy 已设置${NC}"

# ── Output config ────────────────────────────────────────────
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗"
echo -e "║        ✅ 前端基础设施部署完成!              ║"
echo -e "╚══════════════════════════════════════════════╝${NC}"
echo ""

CONFIG_FILE="/tmp/sitesync_frontend_config.json"
cat > "${CONFIG_FILE}" <<CONF
{
  "cloudfront_domain": "${CF_DOMAIN}",
  "cloudfront_dist_id": "${CF_DIST_ID}",
  "api_url": "",
  "api_gateway_url": "${API_URL}",
  "api_gateway_id": "${API_ID}",
  "cognito_user_pool_id": "${POOL_ID}",
  "cognito_client_id": "${CLIENT_ID}",
  "cognito_region": "${REGION}",
  "web_bucket": "${WEB_BUCKET}",
  "data_bucket": "${DATA_BUCKET}"
}
CONF

echo "  🌐 Frontend Config (保存这些值!):"
echo "  ───────────────────────────────────"
echo "  CloudFront:    https://${CF_DOMAIN}"
echo "  API Gateway:   ${API_URL}"
echo "  Cognito Pool:  ${POOL_ID}"
echo "  Cognito Client: ${CLIENT_ID}"
echo "  Web Bucket:    ${WEB_BUCKET}"
echo ""
echo "  📄 Config JSON: ${CONFIG_FILE}"
cat "${CONFIG_FILE}"
echo ""
echo ""
echo "  验证:"
echo "    curl https://${CF_DOMAIN}                    # 应返回 placeholder HTML"
echo "    curl ${API_URL}/api/health                   # 应返回 JSON"
echo ""
echo "  下一步:"
echo "    1. 等 CloudFront 部署完成 (5-10 min)"
echo "    2. 构建 React 前端 (npm run build)"
echo "    3. 上传到 S3: aws s3 sync dist/ s3://${WEB_BUCKET}/"
echo "    4. 清缓存: aws cloudfront create-invalidation --distribution-id ${CF_DIST_ID} --paths '/*'"
echo ""
