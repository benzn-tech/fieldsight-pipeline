#!/usr/bin/env bash
# template-audits.sh [--env test|prod] [--days N]
# One line per template upload audit (spec 2026-11-06 A5), newest first, read straight
# from the lake: template_digitise_audits/{company}/{id}.json. No API -- the audit has
# none, and the listing must still show `skipped` and `unsure` audits, which are never
# emailed. Read-only: list + get, nothing else.
#
# The prefix expires after 90 days (wire-bucket-lifecycle.sh), so --days beyond 90 finds
# nothing more. Profile fieldsight-deployer unless AWS_PROFILE is set (the default
# profile's credentials expire and then list nothing, silently). JSON is parsed with
# Python: set PYTHON to pick the interpreter (jq is not assumed).
set -euo pipefail

ENV_NAME=test
DAYS=30
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_NAME="${2:?--env needs test or prod}"; shift 2 ;;
    --days) DAYS="${2:?--days needs a number}"; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$DAYS" in ''|*[!0-9]*) echo "--days must be a whole number" >&2; exit 2 ;; esac
case "$ENV_NAME" in
  test) BUCKET=fieldsight-data-test-509194952652 ;;
  prod) BUCKET=fieldsight-data-509194952652 ;;
  *) echo "--env must be test or prod" >&2; exit 2 ;;
esac
PROFILE="${AWS_PROFILE:-fieldsight-deployer}"
REGION="${AWS_REGION:-ap-southeast-2}"
PREFIX=template_digitise_audits/

PY=""
for c in "${PYTHON:-}" python3 python; do
  if [ -n "$c" ] && "$c" -c 'import json' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || { echo "no working python found; set PYTHON" >&2; exit 1; }

SINCE="$("$PY" -c 'import sys, datetime as d
print((d.datetime.now(d.timezone.utc) - d.timedelta(days=int(sys.argv[1]))).strftime("%Y-%m-%dT%H:%M:%S"))' "$DAYS")"

# LastModified sorts as text; the CLI pages through the whole prefix before --query runs.
# The listing is its own step so a denied or expired credential fails the script here
# instead of reading as "no audits".
LISTED="$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" \
  --profile "$PROFILE" --region "$REGION" \
  --query "Contents[?LastModified>=\`$SINCE\`].[LastModified,Key]" --output text)"
KEYS="$(printf '%s\n' "$LISTED" | grep -v -e '^None$' -e '^$' | sort -r | cut -f2 || true)"

if [ -z "$KEYS" ]; then
  echo "no audits in s3://$BUCKET/$PREFIX in the last $DAYS days"
  exit 0
fi

printf 'date\tcompany\tname\tverdict\troute\titems\tsource_items\tmissing\textra\treworded\tdropped\n'
while IFS= read -r key; do
  [ -n "$key" ] || continue
  aws s3 cp "s3://$BUCKET/$key" - --profile "$PROFILE" --region "$REGION" --only-show-errors \
    | "$PY" -c 'import json, sys
key = sys.argv[1]
try:
    r = json.load(sys.stdin)
except ValueError:
    print("?\t?\t%s\tunreadable" % key)
    sys.exit(0)
counts = r.get("counts") or {}
n = lambda f: len(r.get(f) or [])
verdict = r.get("verdict", "?")
if verdict == "skipped":
    verdict = "skipped(%s)" % r.get("reason", "?")
name = " ".join(str(r.get("name") or "(no name)").split())
print("\t".join(str(v) for v in (
    (r.get("auditedAt") or "?")[:19], r.get("companyId", "?"), name, verdict, r.get("route"),
    counts.get("items"), r.get("source_items"), n("missing"), n("extra"), n("reworded"),
    n("dropped_claims"))))' "$key"
done <<< "$KEYS"
