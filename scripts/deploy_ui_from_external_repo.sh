#!/bin/bash
# ============================================================
# Deploy FieldSight UI from external repo (fieldsight-ui)
# Companion to INTEGRATION_PLAN.md stage B.2 / B.6.
# ============================================================
# Usage:
#   bash scripts/deploy_ui_from_external_repo.sh \
#     --env test|prod \
#     --ui-repo-path /path/to/fieldsight-ui \
#     [--ui-repo-ref main|claude/sprint8|<sha>] \
#     [--bucket <override>] [--cf-id <override>] \
#     [--dry-run]
#
# Looks up bucket + CloudFront distribution ID from environment-aware defaults
# unless explicitly overridden. Defaults match the GitHub Actions secrets used
# by deploy.yml (TEST_FRONTEND_BUCKET / TEST_CLOUDFRONT_ID, etc.) — supply them
# as env vars or pass --bucket / --cf-id at the CLI.
# ============================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

usage() {
  sed -n '1,18p' "$0"
  exit 1
}

ENV=""
UI_REPO_PATH=""
UI_REPO_REF=""
BUCKET=""
CF_ID=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV="$2"; shift 2 ;;
    --ui-repo-path) UI_REPO_PATH="$2"; shift 2 ;;
    --ui-repo-ref) UI_REPO_REF="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --cf-id) CF_ID="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo -e "${RED}Unknown arg: $1${NC}"; usage ;;
  esac
done

if [[ -z "$ENV" || -z "$UI_REPO_PATH" ]]; then
  echo -e "${RED}--env and --ui-repo-path are required${NC}"
  usage
fi

if [[ "$ENV" != "test" && "$ENV" != "prod" ]]; then
  echo -e "${RED}--env must be 'test' or 'prod', got: $ENV${NC}"
  exit 1
fi

if [[ ! -d "$UI_REPO_PATH/.git" ]]; then
  echo -e "${RED}UI repo path is not a git checkout: $UI_REPO_PATH${NC}"
  exit 1
fi

# Resolve bucket + distribution id from env-aware defaults if not overridden.
if [[ -z "$BUCKET" ]]; then
  if [[ "$ENV" == "test" ]]; then
    BUCKET="${TEST_FRONTEND_BUCKET:-}"
  else
    BUCKET="${PROD_FRONTEND_BUCKET:-}"
  fi
fi
if [[ -z "$CF_ID" ]]; then
  if [[ "$ENV" == "test" ]]; then
    CF_ID="${TEST_CLOUDFRONT_ID:-}"
  else
    CF_ID="${PROD_CLOUDFRONT_ID:-}"
  fi
fi
if [[ -z "$BUCKET" || -z "$CF_ID" ]]; then
  echo -e "${RED}Missing bucket or CloudFront distribution.${NC}"
  echo "  Pass --bucket and --cf-id, or set env vars:"
  echo "    TEST_FRONTEND_BUCKET / TEST_CLOUDFRONT_ID  (for --env test)"
  echo "    PROD_FRONTEND_BUCKET / PROD_CLOUDFRONT_ID  (for --env prod)"
  exit 1
fi

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}  FieldSight UI deploy — env=$ENV${NC}"
echo -e "${CYAN}================================================${NC}"
echo "  Source repo : $UI_REPO_PATH"
echo "  Target ref  : ${UI_REPO_REF:-<current checkout>}"
echo "  S3 bucket   : $BUCKET"
echo "  CloudFront  : $CF_ID"
echo "  Region      : $REGION"
echo "  Dry run     : $([[ $DRY_RUN -eq 1 ]] && echo yes || echo no)"
echo ""

# Step 1 — repo state checks
pushd "$UI_REPO_PATH" >/dev/null
if [[ -n "$(git status --porcelain)" ]]; then
  echo -e "${RED}UI repo has uncommitted changes. Commit or stash before deploying.${NC}"
  git status --short
  exit 1
fi

if [[ -n "$UI_REPO_REF" ]]; then
  echo -e "${CYAN}[1/4] git fetch + checkout $UI_REPO_REF${NC}"
  if [[ $DRY_RUN -eq 0 ]]; then
    git fetch origin "$UI_REPO_REF"
    git checkout "$UI_REPO_REF"
  else
    echo "  (dry run) skipped"
  fi
fi

CURRENT_SHA="$(git rev-parse --short HEAD)"
CURRENT_REF="$(git rev-parse --abbrev-ref HEAD)"
echo "  Deploying $CURRENT_REF @ $CURRENT_SHA"
popd >/dev/null

# Step 2 — sync to S3 (exclude VCS + planning docs from web bucket)
echo ""
echo -e "${CYAN}[2/4] aws s3 sync${NC}"
SYNC_FLAGS=(
  --delete
  --region "$REGION"
  --exclude ".git/*"
  --exclude ".github/*"
  --exclude "*.md"
  --exclude ".gitignore"
  --exclude ".DS_Store"
)
if [[ $DRY_RUN -eq 1 ]]; then
  SYNC_FLAGS+=(--dryrun)
fi

aws s3 sync "$UI_REPO_PATH/" "s3://$BUCKET/" "${SYNC_FLAGS[@]}"

# Step 3 — CloudFront invalidation
echo ""
echo -e "${CYAN}[3/4] CloudFront invalidation${NC}"
if [[ $DRY_RUN -eq 0 ]]; then
  INV_ID=$(aws cloudfront create-invalidation \
    --distribution-id "$CF_ID" \
    --paths "/*" \
    --query 'Invalidation.Id' \
    --output text)
  echo "  Invalidation: $INV_ID"
else
  echo "  (dry run) would invalidate /* on $CF_ID"
fi

# Step 4 — print URL
echo ""
echo -e "${CYAN}[4/4] CloudFront domain${NC}"
DOMAIN=$(aws cloudfront get-distribution \
  --id "$CF_ID" \
  --query 'Distribution.DomainName' \
  --output text 2>/dev/null || echo "<lookup failed>")
echo ""
echo -e "${GREEN}Deploy complete.${NC}"
echo "  URL: https://$DOMAIN/app-shell-preview.html"
echo ""
echo "  Smoke-test query strings:"
echo "    ?mocks=0&baseUrl=https://<api-gw-id>.execute-api.$REGION.amazonaws.com/prod"
echo ""
