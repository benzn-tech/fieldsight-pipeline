"""`src/` as it is at a git ref, exported once per process. Side-effect free on import,
so both run.py and the Ask v2 harness can share it (run.py imports production
modules at its own import; nothing may import run.py)."""
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent       # tools/retrieval-eval
REPO = HERE.parents[1]

_EXPORTED = {}


def export_src(ref):
    """(directory holding `src/` at `ref`, sha), or this worktree's own src for ref
    "worktree" (sha None). The same ref is exported once per process."""
    if ref == "worktree":
        return REPO / "src", None
    if ref in _EXPORTED:
        return _EXPORTED[ref]
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", ref], capture_output=True,
                         text=True, check=True).stdout.strip()
    blob = subprocess.run(["git", "-C", str(REPO), "archive", sha, "src"], capture_output=True,
                          check=True).stdout
    dest = Path(tempfile.mkdtemp(prefix="retrieval-eval-src-"))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest)
    _EXPORTED[ref] = (dest / "src", sha)
    return _EXPORTED[ref]
