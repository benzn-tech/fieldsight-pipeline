"""Unit: the hand-assembled prod deploy bundles an import graph, not a list.

`scripts/deploy-lambda-code.sh` is the only path to the nine prod lambdas that
were never put in a CloudFormation stack. It used to zip each handler with three
named modules::

    SHARED=("src/transcript_utils.py" "src/llm_utils.py" "src/elevenlabs_utils.py")

Measured on 2026-09-11, the handlers in its own MAP imported twenty further local
modules between them -- `output_language`, `nz_time`, `weather`, `site_coords`,
`deletion_mirror`, `agent_turn_filter`, `batch_stitch`, `batch_ledger`,
`batch_seal`, `answer_language`, `corroboration`, `metric_render`,
`metric_slots`, `query_slots`, `dashscope_utils`, `report_sections` and more --
none of them in that list.

`update-function-code` validates a zip, not an import graph. So the script
printed a success table for every function and left an ImportError waiting for
the next cold start. Nothing in this repository would have said so.

A list that has to be edited whenever an import is added will be wrong again in
exactly the same silent way, so the fix is not a longer list: the script globs
`src/*.py`. This test pins that it stays a glob, and that the graph it has to
cover is in fact all inside `src/`.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRIPT = ROOT / "scripts" / "deploy-lambda-code.sh"


def _local_modules():
    return {p.stem for p in SRC.glob("*.py")}


def _handlers_the_script_deploys():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split("declare -A MAP=(", 1)[1].split(")", 1)[0]
    return re.findall(r"\[[a-z-]+\]=(\w+)", body)


def _imports(module, local):
    """Direct local imports of one module, by reading it -- no execution."""
    tree = ast.parse((SRC / (module + ".py")).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & local


def _closure(start, local):
    seen, queue = set(), list(start)
    while queue:
        m = queue.pop()
        if m in seen:
            continue
        seen.add(m)
        queue.extend(_imports(m, local) - seen)
    return seen


def test_the_bundle_is_a_glob_not_a_hand_kept_list():
    """The specific regression: a literal SHARED list falls behind an import and
    fails only at cold start, after a successful-looking deploy."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^SHARED=\(\)\s*$", text, re.M), \
        "SHARED must start empty and be filled from a glob"
    assert "ls src/*.py" in text, "the bundle must come from src/*.py"


def test_every_handler_the_script_deploys_is_reachable_by_that_glob():
    local = _local_modules()
    handlers = _handlers_the_script_deploys()
    assert len(handlers) >= 9, handlers
    for h in handlers:
        assert h in local, "%s is deployed but not a src/ module" % h


def test_the_whole_import_closure_lives_in_src():
    """The glob covers `src/*.py` and nothing deeper. If a handler grows a
    dependency on a package directory (`repositories/`, `db/`), the flat zip
    stops being enough and this fails rather than deploying a broken function.
    """
    local = _local_modules()
    handlers = _handlers_the_script_deploys()
    closure = _closure(handlers, local)
    # Every module in the closure is, by construction, a src/*.py file.
    for m in closure:
        assert (SRC / (m + ".py")).exists(), m
    # And the packages are not reachable from these nine.
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    for h in handlers:
        tree = ast.parse((SRC / (h + ".py")).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                assert n not in packages, (
                    "%s imports the package %s, which a flat `zip -j src/*.py` "
                    "does not carry" % (h, n))


def test_report_sections_is_carried_because_the_report_now_imports_it():
    """The change that prompted this: `lambda_report_generator` gained
    `report_sections`, which gained `due_dates`, which gained `deadline_parse`.
    Three modules, none of them in the old list of three."""
    local = _local_modules()
    closure = _closure(["lambda_report_generator"], local)
    for m in ("report_sections", "due_dates", "deadline_parse", "output_language"):
        assert m in closure, m
        assert (SRC / (m + ".py")).exists(), m
