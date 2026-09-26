"""Every workflow that runs pytest must install the same dependency set.

The dependency list is duplicated across workflows (test.yml runs the PR
checks, deploy-prod.yml re-runs them as a release gate). When `numpy` was
added for the scripts/ tooling tests, only test.yml was updated -- so PRs went
green and then the merge to main failed collection, which SKIPPED deploy-prod
and silently blocked the prod release path. Nothing else notices: the deploy
job reports "skipped", not "failed".
"""

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PIP_INSTALL = re.compile(r"run:\s*pip install (.+)")


def _pytest_workflows():
    out = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "pytest" not in text:
            continue
        for line in text.splitlines():
            m = PIP_INSTALL.search(line)
            if not m:
                continue
            deps = _expand(m.group(1))
            if "pytest" in deps:
                out[path.name] = deps
    return out


def _expand(args):
    """The packages a `pip install ...` line installs, following `-r FILE`.

    The workflows now install from requirements-test.txt instead of listing packages
    inline. Before this learned to follow `-r` it found no inline list at all, saw zero
    pytest workflows and failed -- the right outcome for a guard that could no longer see
    what it guards. It follows the file rather than being deleted, so the parity it pins
    still holds if a second requirements file ever appears.

    Why this test did not catch the 2026-09-25 drift: it compares the workflows with EACH
    OTHER, and both were identically missing PyYAML. What the tests actually NEED is pinned
    separately, in test_one_list_of_test_dependencies.py.
    """
    toks = [t.strip('"\'') for t in args.split()]
    deps = set()
    i = 0
    while i < len(toks):
        if toks[i] == "-r" and i + 1 < len(toks):
            req = WORKFLOWS.parents[1] / toks[i + 1]
            for ln in req.read_text(encoding="utf-8").splitlines():
                ln = ln.split("#", 1)[0].strip()
                if ln:
                    deps.add(ln)
            i += 2
            continue
        deps.add(toks[i])
        i += 1
    return frozenset(deps)


def test_more_than_one_workflow_runs_pytest():
    # If this ever drops to one, the parity check below is vacuous.
    assert len(_pytest_workflows()) >= 2


def test_all_pytest_workflows_install_identical_dependencies():
    found = _pytest_workflows()
    sets = set(found.values())
    assert len(sets) == 1, (
        "workflows disagree on test dependencies: "
        + "; ".join(f"{k}={sorted(v)}" for k, v in found.items())
    )


def test_numpy_is_installed_wherever_pytest_runs():
    # tests/unit/test_multichannel_probe.py imports numpy at module scope,
    # so a missing numpy is a collection error, not a skipped test.
    for name, deps in _pytest_workflows().items():
        assert "numpy" in deps, f"{name} runs pytest without numpy"
