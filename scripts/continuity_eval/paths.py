"""Guards the one thing every continuity-eval write shares: run directories can hold raw
session ids (a person's name, via `user_folder`), transcript text and item text pulled from
prod customer recordings (spec S7 "Sessions" allows reading prod, read-only, once the owner
approves a run). None of that may ever land inside the tracked repo tree, so every write this
harness makes must go under `continuity_eval_runs/` -- the gitignored root (see `.gitignore`)
-- not wherever a typo'd `--out`/`--run` flag happens to point.

`results/summary.json` (score.py's `build_summary`, the one file spec S7 says gets committed)
is the sole intentional exception: it holds counts, rates and verdicts only, never a raw
session id or item/transcript text, so it is written straight through `build_summary`'s
`out_path` without calling `require_run_dir` on it. Every other write in
`scripts/continuity_eval/` -- run.py's step files, label.py's todo/done files and transcript
side files, score.py's `session_refs.json` (session_ids ARE raw folder names) -- calls
`require_run_dir` first.
"""
from __future__ import annotations

from pathlib import Path

RUN_DIR_COMPONENT = "continuity_eval_runs"


def require_run_dir(path) -> Path:
    """Resolves `path` and raises `SystemExit` unless one of its path components is
    `continuity_eval_runs` (the gitignored root). Returns the resolved `Path` so a caller can
    use it directly. Every CLI/API entry point in this package that writes run output --
    transcripts, step files, todo/done files, `session_refs.json` -- calls this on the
    directory or file path it is about to write to, before writing anything."""
    resolved = Path(path).resolve()
    if RUN_DIR_COMPONENT not in resolved.parts:
        raise SystemExit(
            f"Refusing to write outside a '{RUN_DIR_COMPONENT}/' directory: {resolved}\n"
            f"Continuity-eval run output (may include prod customer transcript/item text) "
            f"must stay under a '{RUN_DIR_COMPONENT}/' directory -- the gitignored root. "
            f"Pass a path with that component in it.")
    return resolved
