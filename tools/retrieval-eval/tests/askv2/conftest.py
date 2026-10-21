"""Offline tests for the Ask v2 mode. Production `src/` is exported at EVAL_SRC_REF
(default origin/main) and imported from there -- never this worktree's older src,
which pyproject also puts on sys.path. No network, no AWS: every client is a fake.

No __init__.py here on purpose: the repo root has an importable `tests` package."""
import os
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2]            # tools/retrieval-eval
sys.path.insert(0, str(TOOL))

from askv2 import harness  # noqa: E402

FAMILY = "report"
SRC, SRC_SHA = harness.prepare_imports(os.environ.get("EVAL_SRC_REF", "origin/main"), FAMILY)

import ask_orchestrator  # noqa: E402

assert Path(ask_orchestrator.__file__).resolve().is_relative_to(SRC.resolve()), \
    "imported ask_orchestrator from %s, not the export" % ask_orchestrator.__file__
