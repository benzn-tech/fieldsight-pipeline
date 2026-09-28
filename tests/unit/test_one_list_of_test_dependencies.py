"""The test dependencies are listed once, and every place that installs them reads that list.

A list copied three times with nothing that goes red when the copies drift will drift. On
2026-09-25 this one had: `test.yml`, `deploy-prod.yml` and the local command all differed,
the local run was 31 tests short while reporting "full suite passed", and all three lacked
PyYAML, so 86 guard tests skipped in CI for as long as they had existed.

`test.yml` already carried a comment warning about exactly this. A comment is not the thing
that goes red; this file is.

Two failure shapes are pinned, because they are the two ways the list comes apart again:

  * a workflow that installs test packages INLINE instead of from the file -- the second
    copy, reborn;
  * a test that `importorskip`s a third-party package the file does not list -- the failure
    this all came from, where a missing line reads as a skip instead of a failure.
"""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIST = os.path.join(REPO, "requirements-test.txt")
TEST_WORKFLOWS = ("test.yml", "deploy-prod.yml")


def _listed():
    names = set()
    for line in open(LIST, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # `psycopg[binary]>=3.1` -> `psycopg`
        names.add(re.split(r"[\[<>=!~ ]", line, 1)[0].lower())
    return names


def test_the_list_exists_and_is_not_empty():
    assert os.path.exists(LIST), "requirements-test.txt is the single source; it is gone"
    assert len(_listed()) >= 5


@pytest.mark.parametrize("workflow", TEST_WORKFLOWS)
def test_every_workflow_that_runs_pytest_installs_from_the_list(workflow):
    body = open(os.path.join(REPO, ".github", "workflows", workflow), encoding="utf-8").read()
    assert "pip install -r requirements-test.txt" in body, (
        f"{workflow} runs the tests but does not install from requirements-test.txt, so it "
        f"is running them against a different set of packages than everybody else")


@pytest.mark.parametrize("workflow", TEST_WORKFLOWS)
def test_no_workflow_carries_its_own_copy_of_the_list(workflow):
    """The second copy, reborn. An inline `pip install pytest ...` beside the `-r` line is
    how a package gets added in one place and not the other."""
    body = open(os.path.join(REPO, ".github", "workflows", workflow), encoding="utf-8").read()
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("- run: pip install") and not s.startswith("run: pip install"):
            continue
        if "-r requirements-test.txt" in s:
            continue
        # cfn-lint is a LINTER pinned for the template job, not a test dependency.
        if "cfn-lint" in s:
            continue
        pytest.fail(f"{workflow} installs packages inline: {s!r}. Add them to "
                    f"requirements-test.txt instead, or they will diverge from it")


def _is_first_party(top):
    """A module that lives in this repository, which requirements-test.txt could never list.

    Answered by looking in src/ and scripts/, NOT by a hand-written list of names: the first
    version of this file had one, it was missing `corroboration_client` and
    `elevenlabs_utils` on the day it was written, and a hand-maintained list of what is
    first-party drifts for exactly the reason the dependency list did."""
    if os.path.isdir(os.path.join(REPO, top)):     # e.g. `tests` itself
        return True
    for base in ("src", "scripts"):
        root = os.path.join(REPO, base)
        if os.path.exists(os.path.join(root, top + ".py")) or                 os.path.isdir(os.path.join(root, top)):
            return True
    return False
# Packages a test may legitimately skip on because CI must NOT install them: the ONNX
# runtime is a ~200 MB wheel, excluded by decision (see test_voiceprint_onnx_parity.py).
_EXCLUDED_BY_DECISION = {"onnxruntime", "torch", "speechbrain"}
# Import names that differ from the distribution name.
_IMPORT_TO_DIST = {"yaml": "pyyaml", "docx": "python-docx", "jwt": "pyjwt"}
# Installed as an EXTRA of a listed package rather than named on its own line.
# `PyJWT[crypto]` pulls in `cryptography`; listing it separately would be a second place
# to keep its version in step.
_PROVIDED_BY_EXTRA = {"cryptography": "pyjwt"}


def test_every_package_a_test_may_skip_on_is_listed():
    """`importorskip` is what made a missing package invisible. Any third-party package a
    test skips on must either be in the list -- so it is installed and the test runs -- or
    be excluded by an explicit, named decision."""
    listed = _listed()
    missing = {}
    tests_dir = os.path.join(REPO, "tests")
    for root, _dirs, files in os.walk(tests_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            src = open(os.path.join(root, f), encoding="utf-8").read()
            for mod in re.findall(r'importorskip\(\s*["\']([\w.]+)["\']', src):
                top = mod.split(".")[0]
                if _is_first_party(top):
                    continue
                if top in _EXCLUDED_BY_DECISION:
                    continue
                if top in sys.stdlib_module_names:          # zoneinfo, and so on
                    continue
                if _PROVIDED_BY_EXTRA.get(top) in listed:
                    continue
                dist = _IMPORT_TO_DIST.get(top, top).lower()
                if dist not in listed:
                    missing.setdefault(dist, []).append(f)
    assert not missing, (
        "tests skip on packages requirements-test.txt does not list, so in CI they SKIP "
        "rather than run: " + "; ".join(f"{d} ({', '.join(sorted(set(fs))[:3])})"
                                         for d, fs in sorted(missing.items())))
