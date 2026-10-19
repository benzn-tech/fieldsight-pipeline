#!/usr/bin/env bash
# ============================================================
# build-pypdf-layer.sh <region> [--publish]
# ------------------------------------------------------------
# Builds the pure-Python pypdf layer that ProjectDocIndexFunction reads PDFs with
# (spec 2026-10-16-project-documents-design, R13). Published ONCE per account by hand;
# the ARN goes into the repo variable TEST_PDF_LAYER_ARN / PROD_PDF_LAYER_ARN.
# Without --publish it only builds the zip and prints its path.
#
# PYPDF_VERSION must equal the pin in requirements-test.txt
# (tests/unit/test_build_pypdf_layer.py): the tests run against the version the layer ships.
# Windows Git Bash: paths handed to native tools (uv, python, aws fileb://) go through
# cygpath (BUG-27); with no `zip` on PATH the archive is written by Python zipfile.
# ============================================================
set -euo pipefail

REGION="${1:?usage: build-pypdf-layer.sh <region> [--publish]}"
PYPDF_VERSION="6.20.1"
WORK="$(mktemp -d)"
mkdir -p "$WORK/python"
ZIP="$WORK/fieldsight-pypdf-${PYPDF_VERSION}.zip"

# Native Windows tools (uv, python) read Git Bash's /tmp/... as C:\tmp\... -- the
# install then lands outside $WORK and the zip comes out EMPTY (measured 2026-10-16).
# Hand them Windows paths; on Linux/macOS cygpath is absent and nothing changes.
WORK_NATIVE="$WORK"; ZIP_NATIVE="$ZIP"
if command -v cygpath >/dev/null 2>&1; then
  WORK_NATIVE="$(cygpath -w "$WORK")"; ZIP_NATIVE="$(cygpath -w "$ZIP")"
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --no-deps --target "$WORK_NATIVE/python" "pypdf==${PYPDF_VERSION}"
else
  python3 -m pip install --no-deps --target "$WORK_NATIVE/python" "pypdf==${PYPDF_VERSION}"
fi
find "$WORK/python" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -f "$WORK/python/.lock"   # uv's install lock, not part of the package
# Never publish a layer without the package in it.
[ -f "$WORK/python/pypdf/__init__.py" ] || { echo "pypdf not found under $WORK/python" >&2; exit 1; }

if command -v zip >/dev/null 2>&1; then
  (cd "$WORK" && zip -qr "$ZIP" python/)
else
  # Windows Git Bash ships no `zip`. Python's zipfile writes '/'-separated entry names
  # (python/pypdf/...), which is what Lambda unpacks under /opt.
  if command -v uv >/dev/null 2>&1; then PY=(uv run --no-project python); else PY=(python3); fi
  "${PY[@]}" - "$WORK_NATIVE" "$ZIP_NATIVE" <<'PYEOF'
import os, sys, zipfile
work, out = sys.argv[1], sys.argv[2]
root = os.path.join(work, "python")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for d, _, files in os.walk(root):
        for f in files:
            full = os.path.join(d, f)
            z.write(full, os.path.relpath(full, work).replace(os.sep, "/"))
PYEOF
fi
echo "LAYER_ZIP=$ZIP"

if [ "${2:-}" = "--publish" ]; then
  ZIP_ARG="$ZIP"
  if command -v cygpath >/dev/null 2>&1; then ZIP_ARG="$(cygpath -w "$ZIP")"; fi
  ARN=$(aws lambda publish-layer-version --layer-name fieldsight-pypdf \
    --description "pypdf ${PYPDF_VERSION} (pure Python) for project documents" \
    --zip-file "fileb://${ZIP_ARG}" --compatible-runtimes python3.12 \
    --region "$REGION" --query LayerVersionArn --output text)
  echo "LAYER_ARN=$ARN"
fi
