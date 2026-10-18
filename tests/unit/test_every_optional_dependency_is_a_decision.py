"""Two workflows agreeing on a dependency list does not make the list right.

`test_workflow_dependency_parity.py` pins that every workflow running pytest
installs the SAME set. That is necessary and it is not sufficient: PyYAML was
missing from both, so parity held perfectly while four test files were skipped
everywhere. Among them was the guard written after a toggle shipped unwired on
2026-08-08 -- it had never run, in CI or locally, since the day it was written,
and it went red the first time it was given the chance (`EnableTopicTagging`,
declared, read by the code, passed by neither workflow).

So this file asks the other question: is the list COMPLETE? It derives the
answer from the tests rather than from a list somebody maintains, because a
list somebody maintains is what failed. Every module a test guards with
`importorskip` is either

  * one of ours, under src/ -- then it is not a pip dependency at all;
  * installed by every workflow that runs pytest; or
  * named in NOT_INSTALLED below, with the reason it is deliberately absent.

A new `importorskip("something")` therefore cannot quietly disable a test: it
either adds an install or it adds a line here. The install list's own comment
already said "this has been short an entry before, and the miss read as
`skipped`, not `failed`" -- the comment was right and the same thing happened
again, which is the argument for a check that can fail instead.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
WORKFLOWS = REPO / ".github" / "workflows"

PIP_INSTALL = re.compile(r"run:\s*pip install (.+)")
IMPORTORSKIP = re.compile(r"""importorskip\(\s*["']([A-Za-z_][\w.]*)["']""")

#: import name -> the pip token that provides it, where they differ. Matched
#: against the raw token too, so an extra counts: `cryptography` arrives only
#: because PyJWT is installed as `PyJWT[crypto]`, and dropping the extra would
#: silently skip the websocket authorizer tests.
DISTRIBUTION = {
    "yaml": "pyyaml",
    "docx": "python-docx",
    "jwt": "pyjwt",
    "cryptography": "pyjwt[crypto]",
}

#: Modules a test may skip on that no workflow installs, and why. A line here
#: is a decision that some tests do not run in CI -- it should be as
#: uncomfortable to add as it looks.
NOT_INSTALLED = {
    # Ships with Python 3.9+. Guarded because one test also runs on hosts
    # where the tzdata backport is absent.
    "zoneinfo": "standard library",
    # The ONNX runtime is a ~200MB wheel pulled in only by the voiceprint
    # parity test, whose reference fixtures were removed with the real-voice
    # data. Installing it would buy a test that skips for another reason.
    "onnxruntime": "large wheel; the test it gates skips on missing fixtures anyway",
}


def _pytest_workflow_deps():
    """{workflow name: {normalised distribution names it installs}}."""
    out = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = PIP_INSTALL.search(line)
            if not m or "pytest" not in m.group(1):
                continue
            names = set()
            for tok in m.group(1).split():
                tok = tok.strip("\"'").lower()
                names.add(tok)                       # raw, e.g. pyjwt[crypto]>=2.8
                # version dropped, extra kept: -> pyjwt[crypto]
                names.add(re.split(r"[><=!~]", tok, 1)[0].strip())
                # both dropped: -> pyjwt
                names.add(re.split(r"[\[><=!~]", tok, 1)[0].strip())
            out[path.name] = names
    return out


def _is_ours(module):
    """A module imported from this repo rather than from pip.

    src/ is the lambda code, scripts/ the tooling the scripts tests import,
    and a `tests.` prefix is one test file importing another. All three are on
    sys.path during a run and none of them is a dependency.
    """
    if module.split(".")[0] == "tests":
        return True
    rel = module.split(".")
    for root in (SRC, REPO / "scripts"):
        if (root.joinpath(*rel).with_suffix(".py").exists()
                or root.joinpath(*rel).is_dir()):
            return True
    return False


def _external_skips():
    """{top-level package: [files that guard on it]} for what pip must provide.

    Keyed on the TOP-LEVEL package, because a guard on
    `cryptography.hazmat.primitives.serialization` is a guard on cryptography,
    and pip has no opinion about submodules.
    """
    found = {}
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue  # this file names modules in its own prose and messages
        for module in IMPORTORSKIP.findall(path.read_text(encoding="utf-8")):
            if _is_ours(module):
                continue
            found.setdefault(module.split(".")[0], []).append(path.name)
    return found


def test_the_repo_still_guards_on_something_external():
    """If this ever empties, the check below passes by describing nothing."""
    assert _external_skips(), "no external importorskip found -- has the idiom changed?"


def test_every_workflow_that_runs_pytest_was_found():
    """The parity file makes the same assertion; repeated here because every
    check in this file is vacuous if the workflow scan silently finds none."""
    assert len(_pytest_workflow_deps()) >= 2


def test_every_optional_dependency_is_installed_or_written_down():
    deps = _pytest_workflow_deps()
    missing = []
    for module, files in sorted(_external_skips().items()):
        if module in NOT_INSTALLED:
            continue
        dist = DISTRIBUTION.get(module, module).lower()
        for workflow, installed in deps.items():
            if dist not in installed:
                missing.append(
                    f"{module} (needs {dist}; guards {', '.join(sorted(set(files)))}) "
                    f"is not installed by {workflow}")
    assert not missing, (
        "These tests skip instead of running, everywhere, and a skip reads like "
        "a pass in every summary that only reports passed and failed:\n  "
        + "\n  ".join(missing)
        + "\n\nInstall it in every workflow that runs pytest, or add it to "
          "NOT_INSTALLED with the reason.")


def test_nothing_is_written_down_that_is_actually_installed():
    """A stale exemption is worse than none: it says "we decided not to run
    these" about tests that now run, so the next real absence looks routine."""
    deps = _pytest_workflow_deps()
    stale = [m for m in NOT_INSTALLED
             if all(DISTRIBUTION.get(m, m).lower() in d for d in deps.values())]
    assert not stale, f"installed everywhere, so remove from NOT_INSTALLED: {stale}"
