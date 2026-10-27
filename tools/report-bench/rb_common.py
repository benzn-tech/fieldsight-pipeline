"""Shared helpers for the report / extraction bench.

Spec: docs/superpowers/specs/2026-10-28-report-extraction-prompt-model-bench-design.md
(rev 2.1). Every module in this directory is named `rb_*`: the web bench
(tools/web-answer-bench) registers bare `ledger`, `select_freeze`, `prompts` in
`sys.modules`, and both suites run in one pytest interpreter. Web-bench modules
are loaded by path under a `web_` name via `load_web`, never by bare import.

Privacy: prompt / transcript / answer text lives only in files under `--out`;
nothing here prints or logs it.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")
WEB_BENCH = os.path.join(REPO, "tools", "web-answer-bench")
DROPBOX = "C:/Users/camil/Dropbox"

# Spec §2: read at import by llm_utils and the lambdas. LLM_TEMPERATURE is
# deliberately absent (prod sends none).
IMPORT_ENV = {
    "LLM_PROVIDER": "qwen",
    "QWEN_BASE_URL": "https://openrouter.ai/api/v1",
    "LLM_REASONING_EFFORT": "high",
    "LLM_REASONING_HEADROOM": "8000",
    "EMIT_EVIDENCE": "false",
    "DECLARE_CONTINUITY": "false",
    "STATUS_SUGGESTIONS": "false",
    "S3_BUCKET": "dummy",
    "AWS_DEFAULT_REGION": "ap-southeast-2",
}
_PASSTHROUGH = ("SYSTEMROOT", "PATH", "TEMP", "TMP")

LOCK_NAME = "bench.lock"


def subprocess_env() -> dict:
    """The render worker's whole environment: IMPORT_ENV plus the few OS
    variables Windows needs to start Python. Never inherited."""
    env = dict(IMPORT_ENV)
    for k in _PASSTHROUGH:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    return env


def _norm(p):
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def _inside(path, root):
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + os.sep)


def check_out_dir(path: str) -> str:
    """Absolute `path`; ValueError when it is inside the repo or Dropbox (bench
    data must never be committed or synced)."""
    ap = os.path.abspath(path)
    for root, what in ((REPO, "the repo"), (DROPBOX, "Dropbox")):
        if _inside(ap, root):
            raise ValueError(f"--out must be outside {what}: {ap}")
    return ap


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def append_jsonl(path, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def write_text(path, text) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def write_json(path, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@contextlib.contextmanager
def bench_lock(out: str, name: str):
    """`<out>/bench.lock`, created exclusively. The report and web benches share
    `anthropic_ledger.json` (read-modify-write), so they must never run at the
    same time: the report bench takes this lock; the owner runbook says the web
    bench is not started while it exists."""
    path = os.path.join(out, LOCK_NAME)
    os.makedirs(out, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"{path} exists: another bench run holds --out "
                           "(delete it only if no bench is running)") from None
    try:
        os.write(fd, json.dumps({"bench": name, "pid": os.getpid()}).encode("utf-8"))
        os.close(fd)
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def load_web(name: str):
    """A tools/web-answer-bench module, loaded by path as `web_<name>`."""
    key = "web_" + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(WEB_BENCH, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def load_rb(name: str):
    """A sibling `rb_*` module by path (the dir has a hyphen)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def ensure_src_on_path() -> None:
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
