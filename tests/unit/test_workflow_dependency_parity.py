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
            if m and "pytest" in m.group(1):
                out[path.name] = frozenset(
                    tok.strip('"\'') for tok in m.group(1).split()
                )
    return out


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
