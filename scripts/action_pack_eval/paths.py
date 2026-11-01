"""File helpers shared by every action-pack-eval command.

Real gold sets, labels, recorded packs and blind subsets hold instruction text, conclusion
text and source excerpts from site recordings, so every WRITE this harness makes goes through
`require_run_dir`: the path must sit under an `action_pack_eval_runs/` directory, which
.gitignore excludes (the continuity_eval convention). Reads are unguarded so the committed
SYNTHETIC examples/ can be scored."""
from __future__ import annotations

import json
from pathlib import Path

RUN_DIR_COMPONENT = "action_pack_eval_runs"


def require_run_dir(path) -> Path:
    """The resolved path, or SystemExit unless one of its components is
    `action_pack_eval_runs` (the gitignored root)."""
    resolved = Path(path).resolve()
    if RUN_DIR_COMPONENT not in resolved.parts:
        raise SystemExit(
            f"Refusing to write outside a '{RUN_DIR_COMPONENT}/' directory: {resolved}\n"
            f"Action-pack eval output can carry instruction, conclusion and excerpt text "
            f"from site recordings; pass a path under '{RUN_DIR_COMPONENT}/' (gitignored).")
    return resolved


def read_records(path, key=None):
    """A list of objects from a .jsonl file (one per line), a JSON list, or a JSON object
    holding the list under `key`."""
    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict) and key is not None:
        data = data.get(key)
    if not isinstance(data, list):
        raise SystemExit(f"{path}: expected a list" + (f" (or an object with '{key}')"
                                                       if key else ""))
    return data


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, obj) -> Path:
    p = require_run_dir(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
                 encoding="utf-8")
    return p


def write_jsonl(path, rows) -> Path:
    p = require_run_dir(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def write_text(path, text) -> Path:
    p = require_run_dir(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p
