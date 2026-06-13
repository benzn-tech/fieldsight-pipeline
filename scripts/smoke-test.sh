#!/usr/bin/env bash
# smoke-test.sh <stack-name> <region>
# Reads the ApiEndpoint output from the deployed stack and curls /api/health.
set -euo pipefail
STACK="${1:?usage: smoke-test.sh <stack> <region>}"
REGION="${2:?missing region}"

API=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)
if [ -z "$API" ] || [ "$API" = "None" ]; then
  echo "❌ no ApiEndpoint output on stack $STACK"; exit 1
fi
# ApiEndpoint already ends in /api  →  health is /api/health
echo "GET ${API}/health"
for i in 1 2 3 4 5 6; do
  code=$(curl -s -o /tmp/health.json -w '%{http_code}' "${API}/health" || echo 000)
  if [ "$code" = "200" ]; then
    echo "✅ /api/health 200"; cat /tmp/health.json; echo; exit 0
  fi
  echo "attempt $i: HTTP $code (API may still be warming up; retrying in 5s)"
  sleep 5
done
echo "❌ smoke test failed for $STACK"; cat /tmp/health.json 2>/dev/null || true
exit 1
