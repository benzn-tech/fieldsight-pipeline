#!/bin/bash
# ============================================================
# cleanup_old_resources.sh
# Remove all realptt-* and sitesync-* legacy resources
# Run AFTER confirming fieldsight-* pipeline is working
# ============================================================

set -e
echo "=== FieldSight Cleanup: Removing legacy resources ==="
echo ""

# ── 1. Delete old Lambda functions ──────────────────────────
echo "--- Deleting old Lambda functions ---"
for fn in \
  realptt-orchestrator \
  realptt-downloader \
  realptt-transcribe \
  realptt-fargate-trigger \
  realptt-report-generator \
  realptt-meeting-minutes \
  sitesync-vad \
  sitesync-transcribe-callback \
  sitesync-api; do
  echo "  Deleting: $fn"
  aws lambda delete-function --function-name "$fn" 2>/dev/null && echo "    ✓ Deleted" || echo "    - Not found (already deleted)"
done

# ── 2. Delete old EventBridge rules ─────────────────────────
echo ""
echo "--- Deleting old EventBridge rules ---"

# First remove targets, then delete rule
for rule in sitesync-transcribe-state-change; do
  echo "  Removing targets for: $rule"
  TARGETS=$(aws events list-targets-by-rule --rule "$rule" --query 'Targets[*].Id' --output text 2>/dev/null)
  if [ -n "$TARGETS" ]; then
    aws events remove-targets --rule "$rule" --ids $TARGETS 2>/dev/null || true
  fi
  echo "  Deleting rule: $rule"
  aws events delete-rule --name "$rule" 2>/dev/null && echo "    ✓ Deleted" || echo "    - Not found"
done

# Also check if realptt-* has any EventBridge rules
echo "  Checking for realptt-* EventBridge rules..."
aws events list-rules --query 'Rules[?starts_with(Name,`realptt`)].Name' --output text 2>/dev/null | tr '\t' '\n' | while read rule; do
  if [ -n "$rule" ]; then
    echo "  Found: $rule"
    TARGETS=$(aws events list-targets-by-rule --rule "$rule" --query 'Targets[*].Id' --output text 2>/dev/null)
    if [ -n "$TARGETS" ]; then
      aws events remove-targets --rule "$rule" --ids $TARGETS 2>/dev/null || true
    fi
    aws events delete-rule --name "$rule" 2>/dev/null && echo "    ✓ Deleted" || echo "    - Failed"
  fi
done

# ── 3. Delete old Cognito pool ──────────────────────────────
echo ""
echo "--- Deleting old Cognito pool ---"
echo "  Deleting: sitesync-users (ap-southeast-2_ps7XIQGHB)"
aws cognito-idp delete-user-pool --user-pool-id ap-southeast-2_ps7XIQGHB 2>/dev/null \
  && echo "    ✓ Deleted" || echo "    - Not found (already deleted)"

# ── 4. Delete old CloudWatch Log Groups ─────────────────────
echo ""
echo "--- Deleting old CloudWatch Log Groups ---"
for prefix in realptt sitesync; do
  aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/${prefix}-" \
    --query 'logGroups[*].logGroupName' --output text 2>/dev/null | tr '\t' '\n' | while read lg; do
    if [ -n "$lg" ]; then
      echo "  Deleting: $lg"
      aws logs delete-log-group --log-group-name "$lg" 2>/dev/null && echo "    ✓" || echo "    - Failed"
    fi
  done
done

# ── 5. Summary ──────────────────────────────────────────────
echo ""
echo "=== Cleanup Complete ==="
echo ""
echo "Remaining Lambda functions:"
aws lambda list-functions --no-paginate \
  --query 'Functions[*].FunctionName' --output text | tr '\t' '\n' | sort
echo ""
echo "Remaining EventBridge rules:"
aws events list-rules --query 'Rules[*].{Name:Name,State:State}' --output table
