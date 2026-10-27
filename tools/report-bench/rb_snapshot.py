"""Cold snapshots of src/ at a sha, and the render call into them (spec §2).

`git archive <sha> src` (zip, extracted with zipfile: no tar binary needed) into
`<cache>/<sha>/src`, reused once a `.complete` marker exists. Rendering runs
rb_render_worker.py under `python -I -X utf8` with rb_common.subprocess_env():
nothing from this interpreter's modules or environment leaks into the render.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402

WORKER = os.path.join(_HERE, "rb_render_worker.py")
RENDER_TIMEOUT_S = 300


class RenderError(RuntimeError):
    pass


def have_sha(sha: str, repo: str = rb_common.REPO) -> bool:
    r = subprocess.run(["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"],
                       capture_output=True)
    return r.returncode == 0


def resolve(sha: str, repo: str = rb_common.REPO) -> str:
    r = subprocess.run(["git", "-C", repo, "rev-parse", sha + "^{commit}"],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def build(sha: str, cache_dir: str, repo: str = rb_common.REPO) -> str:
    """`<cache>/<full sha>/src`, extracted once."""
    cache_dir = rb_common.check_out_dir(cache_dir)
    full = resolve(sha, repo)
    root = os.path.join(cache_dir, full)
    marker = os.path.join(root, ".complete")
    src = os.path.join(root, "src")
    if os.path.exists(marker):
        return src
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    zpath = os.path.join(root, "src.zip")
    subprocess.run(["git", "-C", repo, "archive", "--format=zip", "-o", zpath, full, "src"],
                   check=True, capture_output=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(root)
    os.remove(zpath)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(full + "\n")
    return src


def render(src_dir: str, task: str, inputs: dict, python: str = sys.executable) -> str:
    req = json.dumps({"task": task, "inputs": inputs}, ensure_ascii=False).encode("utf-8")
    r = subprocess.run([python, "-I", "-X", "utf8", WORKER, src_dir], input=req,
                       capture_output=True, env=rb_common.subprocess_env(),
                       cwd=os.path.dirname(src_dir), timeout=RENDER_TIMEOUT_S)
    if r.returncode != 0:
        lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RenderError(lines[-1] if lines else f"worker exit {r.returncode}")
    return json.loads(r.stdout.decode("utf-8"))["prompt"]
