#!/usr/bin/env bash
# template-audit-stats.sh [--env test|prod] [--months N]
# Month by month tally of template upload audits, from the stats lines the worker keeps
# beside every audit: template_digitise_audit_stats/{YYYY-MM}/{company}/{id}.json. Those
# hold counts and labels only (no customer text) and, unlike the full records under
# template_digitise_audits/ (90 days), are never expired -- so this is the long view;
# template-audits.sh is the one to read a single upload. Read-only: sync down, count.
#
# One row per month and route: uploads, then how many got each verdict. Profile
# fieldsight-deployer unless AWS_PROFILE is set; set PYTHON to pick the interpreter.
set -euo pipefail

ENV_NAME=test
MONTHS=12
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_NAME="${2:?--env needs test or prod}"; shift 2 ;;
    --months) MONTHS="${2:?--months needs a number}"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$MONTHS" in ''|*[!0-9]*) echo "--months must be a whole number" >&2; exit 2 ;; esac
case "$ENV_NAME" in
  test) BUCKET=fieldsight-data-test-509194952652 ;;
  prod) BUCKET=fieldsight-data-509194952652 ;;
  *) echo "--env must be test or prod" >&2; exit 2 ;;
esac
PROFILE="${AWS_PROFILE:-fieldsight-deployer}"
REGION="${AWS_REGION:-ap-southeast-2}"
PREFIX=template_digitise_audit_stats/

PY=""
for c in "${PYTHON:-}" python3 python; do
  if [ -n "$c" ] && "$c" -c 'import json' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "no working python found; set PYTHON" >&2; exit 1; }

DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT
# Git Bash on Windows: native aws and python misread /tmp/... (aws writes under C:\tmp),
# so hand both the Windows path.
PYDIR="$DIR"
command -v cygpath >/dev/null 2>&1 && PYDIR="$(cygpath -w "$DIR")"
# A denied or expired credential fails here (set -e) instead of reading as "no uploads".
aws s3 sync "s3://$BUCKET/$PREFIX" "$PYDIR" --profile "$PROFILE" --region "$REGION" \
  --only-show-errors

"$PY" - "$PYDIR" "$MONTHS" <<'EOF'
import collections, json, os, sys
root, months = sys.argv[1], int(sys.argv[2])
VERDICTS = ["ok", "problems", "missed", "unsure", "not_a_checklist", "skipped"]
rows = collections.defaultdict(collections.Counter)
for base, _, files in os.walk(root):
    for f in files:
        try:
            r = json.load(open(os.path.join(base, f), encoding="utf-8"))
        except ValueError:
            continue
        key = ((r.get("auditedAt") or "?")[:7], r.get("route") or "-")
        rows[key]["uploads"] += 1
        rows[key][r.get("verdict") or "?"] += 1
if not rows:
    print("no stats lines under the prefix")
    sys.exit(0)
keep = sorted({m for m, _ in rows}, reverse=True)[:months]
print("\t".join(["month", "route", "uploads"] + VERDICTS))
for (month, route) in sorted(rows, reverse=True):
    if month in keep:
        c = rows[(month, route)]
        print("\t".join(str(v) for v in [month, route, c["uploads"]] + [c[v] for v in VERDICTS]))
EOF
