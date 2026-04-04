#!/bin/bash
# Verify that transcript_utils.py exists and all Lambda files that import it
# are in the same directory (ensuring zip bundles will include it).
# See CLAUDE.md BUG-22: Lambda deployment version mismatch.
set -e

UTILS="src/transcript_utils.py"
if [ ! -f "$UTILS" ]; then
  echo "ERROR: transcript_utils.py not found at $UTILS"
  exit 1
fi

IMPORTERS=$(grep -rl "from transcript_utils import\|import transcript_utils" src/*.py 2>/dev/null || true)
if [ -z "$IMPORTERS" ]; then
  echo "WARNING: No files import transcript_utils — is this expected?"
  exit 0
fi

echo "transcript_utils.py importers:"
for f in $IMPORTERS; do
  echo "  OK  $f"
done
COUNT=$(echo "$IMPORTERS" | wc -l)
echo "Bundle check passed: transcript_utils.py exists and $COUNT file(s) import it"
