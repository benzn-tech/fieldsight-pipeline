"""Contamination rule 2 (spec 2026-10-16, R14): the extraction / report / checklist
pipeline never imports the documents code, so "the spec says X" can never be written down
as "on site they did X".

Three checks: (1) no import of a documents module ANYWHERE in a forbidden file (lazy
imports included); (2) nothing a forbidden module loads AT IMPORT TIME reaches one
(transitively, through src/); (3) lambda_ingest -- imported at module level by
lambda_item_writer -- reaches the documents code only lazily. Lazy imports inside other
modules (checklist_reports -> lambda_org_api) are not followed: they run only on that
function's own path, which is not extraction."""
import ast
import glob
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "src")
FORBIDDEN = (["lambda_extract_session", "lambda_item_writer", "lambda_session_report",
              "lambda_report_generator", "report_template", "session_index", "chunking"]
             + sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(SRC, "checklist*.py"))))


def _is_doc(name):
    return name in ("repositories.project_docs", "repositories.project_docs_sql") or \
        name.split(".")[-1].startswith("project_doc")


def _names(node):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    return []


def _all_imports(tree):
    return [n for node in ast.walk(tree) for n in _names(node)]


def _module_level_imports(tree):
    out = []

    def visit(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                out.extend(_names(s))
                continue
            for field in ("body", "orelse", "finalbody"):
                child = getattr(s, field, None)
                if isinstance(child, list):
                    visit(child)
            for h in getattr(s, "handlers", None) or []:
                visit(h.body)
    visit(tree.body)
    return out


def _path(module):
    p = os.path.join(SRC, *module.split(".")) + ".py"
    return p if os.path.exists(p) else None


def _tree(module):
    return ast.parse(open(_path(module), encoding="utf-8").read())


def _closure(start):
    seen, todo = set(), [start]
    while todo:
        mod = todo.pop()
        if mod in seen or _path(mod) is None:
            continue
        seen.add(mod)
        todo.extend(n for n in _module_level_imports(_tree(mod)) if _path(n))
    return seen


def test_the_forbidden_modules_exist():
    """A rename must not quietly empty this test."""
    assert len(FORBIDDEN) >= 9
    for mod in FORBIDDEN:
        assert _path(mod), mod


def test_no_forbidden_module_imports_documents_code_anywhere():
    for mod in FORBIDDEN:
        bad = [n for n in _all_imports(_tree(mod)) if _is_doc(n)]
        assert not bad, f"{mod} imports {bad}"


def test_nothing_a_forbidden_module_loads_at_import_reaches_documents_code():
    for mod in FORBIDDEN:
        reached = [m for m in _closure(mod) if _is_doc(m)]
        assert not reached, f"{mod} loads {reached} at import"


def test_lambda_ingest_reaches_documents_only_lazily():
    assert not [m for m in _closure("lambda_ingest") if _is_doc(m)]
    assert "project_doc_apply" in _all_imports(_tree("lambda_ingest"))


def test_the_scanner_sees_both_kinds_of_import():
    lazy = ast.parse("def f():\n    from repositories import project_docs\n")
    top = ast.parse("from repositories import project_docs\n")
    assert any(_is_doc(n) for n in _all_imports(lazy))
    assert not any(_is_doc(n) for n in _module_level_imports(lazy))
    assert any(_is_doc(n) for n in _module_level_imports(top))
