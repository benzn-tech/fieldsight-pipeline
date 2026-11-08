"""Shared I/O for the item_support harness: the write guard, deterministic JSON, the turn
round-trip, and the one item enumeration every step uses.

Write guard: session files hold a folder name (a person's name), item text and turn text, so
every write except results/summary.json must land under `item_support_eval/{data,runs,labels}/`
(gitignored by this package's .gitignore). `require_eval_dir` refuses anything else.

Turn round-trip: `assemble_session_turns` gives `abs_start`/`abs_end` as datetimes, and
`item_support._window_turns` only counts a turn whose `abs_start` IS a datetime. A turn stored
as JSON and read back with a string there would silently drop out of every window, so
datetimes are written tagged and parsed back exactly (naive stays naive).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from evidence_match import is_cjk
from item_continuity import KINDS, normalise_children

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
RUNS_DIR = PACKAGE_DIR / "runs"
LABELS_DIR = PACKAGE_DIR / "labels"
RESULTS_DIR = PACKAGE_DIR / "results"
_GUARDED = ("data", "runs", "labels")
_DT = "$dt"

SPLIT_SEED = "item-support-eval-2026-11-09"
OWNER_SEED = "item-support-owner-2026-11-09"


def require_eval_dir(path) -> Path:
    """The resolved path, or SystemExit unless it lies under an `item_support_eval/data|runs|
    labels/` directory."""
    resolved = Path(path).resolve()
    parts = resolved.parts
    for i in range(len(parts) - 1):
        if parts[i] == "item_support_eval" and parts[i + 1] in _GUARDED:
            return resolved
    raise SystemExit(f"Refusing to write outside item_support_eval/{{data,runs,labels}}/: "
                     f"{resolved}")


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_json(path, obj):
    path = require_eval_dir(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8", newline="\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, rows):
    path = require_eval_dir(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(dumps(row) + "\n")


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _enc(v):
    if isinstance(v, datetime):
        return {_DT: v.isoformat()}
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    return v


def _dec(v):
    if isinstance(v, dict):
        if set(v) == {_DT}:
            return datetime.fromisoformat(v[_DT])
        return {k: _dec(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_dec(x) for x in v]
    return v


def encode_turn(turn):
    """JSON-safe copy; every datetime (at any depth) is tagged so `decode_turn` restores it."""
    return _enc(turn)


def decode_turn(turn):
    return _dec(turn)


def session_key(folder, date, base):
    return f"{folder}/{date}/{base}"


def session_hash(key, seed=SPLIT_SEED):
    return hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()


def split_sessions(keys, seed=SPLIT_SEED):
    """{key: "fit"|"heldout"}: sorted by seeded sha256, first ceil(n/2) are fit."""
    ordered = sorted(keys, key=lambda k: session_hash(k, seed))
    n_fit = -(-len(ordered) // 2)
    return {k: ("fit" if i < n_fit else "heldout") for i, k in enumerate(ordered)}


def session_ref(key, seed=SPLIT_SEED):
    """Opaque id used in file names and every run row (never the folder name)."""
    return session_hash(key, seed)[:12]


def item_ref(sref, topic_idx, list_name, child_idx):
    return f"{sref}:{topic_idx}:{list_name}:{child_idx}"


def iter_items(topics):
    """(topic_idx, list_name, child_idx, child, topic) for every child item, in the order
    `item_support.attach_support` visits them. Normalises plain-string decisions/questions in
    place first, as attach_support does, so indices agree on both sides."""
    normalise_children(topics)
    for ti, topic in enumerate(topics if isinstance(topics, list) else []):
        if not isinstance(topic, dict):
            continue
        for list_name in KINDS:
            for ci, child in enumerate(topic.get(list_name) or []):
                if isinstance(child, dict):
                    yield ti, list_name, ci, child, topic


def item_text(list_name, child):
    text = child.get(KINDS[list_name].text_field)
    return text if isinstance(text, str) else ""


def language(text):
    return "zh" if any(is_cjk(ch) for ch in text or "") else "en"


def load_session(path):
    s = read_json(path)
    s["turns"] = [decode_turn(t) for t in s.get("turns") or []]
    return s


def load_sessions(data_dir=DATA_DIR, split=None):
    """Session files under data_dir/sessions, sorted by ref; optionally one split only."""
    out = []
    for p in sorted(Path(data_dir, "sessions").glob("*.json")):
        s = load_session(p)
        if split is None or s["split"] == split:
            out.append(s)
    return out


def manifest_digest(data_dir=DATA_DIR):
    """sha256 of data/manifest.json, or None if there is none. Recorded by every run and by the
    fit, so a rebuilt set (a different split) cannot be scored against a fit made on another."""
    p = Path(data_dir, "manifest.json")
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def require_same_manifest(recorded, data_dir=DATA_DIR, what="run"):
    current = manifest_digest(data_dir)
    if recorded != current:
        raise SystemExit(f"Refusing: the {what} was made on manifest {recorded}, "
                         f"data/ now holds {current}. The split is frozen; rebuild nothing.")
    return current


def item_text_at(session, ref):
    """The item text behind an item_ref, read from the session's own (un-normalised) topics."""
    _sref, ti, list_name, ci = ref.split(":")
    child = session["topics"][int(ti)][list_name][int(ci)]
    if isinstance(child, str):
        return child
    return item_text(list_name, child)
