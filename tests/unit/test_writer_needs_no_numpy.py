"""Unit: the in-VPC voiceprint writer must work with numpy absent.

The enrolment library was empty for weeks and the last suspect was the homogeneity guard. It
was not the guard. The embedder logged

    enrolment accepted (frames=2 spread=0.198 limit=0.35)

and then the function that stores the accepted sample died with `ModuleNotFoundError: No
module named 'numpy'`. `repositories.voiceprints._agreement` imported `voiceprint_utils` for
**one dot product**, and `lambda_voiceprint_writer` is in-VPC with the psycopg layer and no
numpy. So the guard passed, the caller was told "accepted", and nothing was written.

That is the third time in this feature that a passing guard and a working feature turned out
to be different claims, and it is why the fix is a test rather than a layer. Adding numpy to
the writer would have worked and would have pinned its runtime to the layer's ABI —
contagious, documented, and 40 MB attached to a function whose whole job is one INSERT.

**The absence is simulated in a subprocess, and that is not fastidiousness.** The first
version of this file blocked numpy in-process with a fixture. It worked, and it turned 73
unrelated tests red: numpy cannot be removed from `sys.modules` and put back — half-imported
submodules leave it in a state where later `np` calls fail in tests that never mentioned this
one. A test that damages the process it runs in gets deleted rather than fixed, and then the
defect it was holding comes back.
"""
import os
import subprocess
import sys
import textwrap

import pytest

SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")

# A finder that refuses numpy before any real one is consulted. `sys.modules` is untouched:
# in a fresh interpreter numpy was never imported, so there is nothing to remove.
BLOCK = """
import sys
class _NoNumpy:
    def find_module(self, name, path=None):
        return self.find_spec(name, path)
    def find_spec(self, name, path=None, target=None):
        if name == 'numpy' or name.startswith('numpy.'):
            raise ModuleNotFoundError("No module named 'numpy'")
        return None
sys.meta_path.insert(0, _NoNumpy())
sys.path.insert(0, %r)
"""


def _run(body):
    """Run `body` in a fresh interpreter with numpy unavailable."""
    script = BLOCK % SRC + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)


def test_the_agreement_check_runs_without_numpy():
    """The exact call that died: `add_sample` → `_agreement` → cosine.

    Driven through the repository rather than through the helper, because the defect was not
    in the arithmetic — it was in WHICH module the arithmetic was imported from, and only
    this path can tell those apart. `_agreement` is deliberately NOT stubbed: stubbing it
    would remove the only line the defect was on, and this suite carries a note about twenty
    tests that monkeypatched the broken function and never ran it.
    """
    res = _run("""
        import repositories.voiceprints as vp

        class _Cur:
            def __init__(self): self.sql = []
            def execute(self, sql, params=None):
                self.sql.append(" ".join(sql.split())); return self
            def fetchone(self): return {"id": "sample-1"}
            def fetchall(self): return []

        class _Conn:
            def __init__(self): self.cur = _Cur()
            def cursor(self, row_factory=None): return self.cur

        conn = _Conn()
        row = vp.add_sample(conn, "11111111-1111-1111-1111-111111111111", "vp-1",
                            [0.1] * vp.EMBEDDING_DIMS, source="correction", s3_key="k",
                            window=(0.0, 8.0))
        assert row == {"id": "sample-1"}, row
        assert any(s.startswith("INSERT INTO speaker_voiceprint_samples")
                   for s in conn.cur.sql), conn.cur.sql
        print("OK")
    """)
    assert "OK" in res.stdout, (
        f"the writer's enrolment path still needs numpy:\n{res.stderr[-2000:]}")


def test_the_blocker_actually_blocks():
    """The guard on the guard. If the meta-path hook stopped working, the test above would
    pass by importing the real numpy and would say nothing about the writer at all — which
    is the exact failure it exists to catch, one level up."""
    res = _run("""
        try:
            import numpy
        except ModuleNotFoundError:
            print("BLOCKED")
    """)
    assert "BLOCKED" in res.stdout, res.stdout + res.stderr


def test_one_definition_of_cosine():
    """`voiceprint_utils` re-exports rather than reimplements. Two similarity functions that
    drift apart would move every threshold in this feature by an amount nobody can see —
    scores would simply be a little different, and no test compares them."""
    import vector_math
    import voiceprint_utils

    assert voiceprint_utils.cosine is vector_math.cosine


def test_the_arithmetic_is_the_same_arithmetic():
    """Pure Python against numpy, on the shapes this actually sees. A rewrite that is
    subtly different is worse than the ModuleNotFoundError it replaced: the error stopped
    the write, a drifted score would silently move every decision the feature makes."""
    import numpy as np

    import vector_math

    rng = np.random.default_rng(7)
    for _ in range(20):
        a, b = rng.normal(size=192), rng.normal(size=192)
        want = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        assert vector_math.cosine(a.tolist(), b.tolist()) == pytest.approx(want, abs=1e-12)


def test_mismatched_lengths_are_an_error_not_a_zero():
    """numpy's `dot` raises on mismatched shapes; a hand-written loop over `zip` would
    silently score the shorter prefix. Two embedding models, or a truncated row, must not
    read as "never the same person"."""
    import vector_math

    with pytest.raises(ValueError):
        vector_math.cosine([1.0, 2.0], [1.0, 2.0, 3.0])
