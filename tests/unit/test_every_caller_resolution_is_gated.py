"""Every place that resolves the CALLER from a Cognito sub must pass through the company
gate (spec 2026-11-01-company-offboarding §5.1); lookups of OTHER users must not.

AST-scans every .py under src/ for calls to `get_user_by_sub` (bare name or attribute).
Each call is classified by (path relative to src/, innermost enclosing function name) --
never by line number, which drifts. All calls inside one function share its class.

  caller  -- resolves the person making the request: a `caller_blocked(...)` call must
             follow it (higher line) in the same function.
  target  -- looks up someone else (the member being edited, the invitee): the function
             must not call `caller_blocked` at all; gating a target would refuse the
             CALLER for the target's company.
  exempt  -- resolves the caller but deliberately not gated (owner ruling); same rule
             as target, with the ruling as the reason.

A new call site fails until it is classified here; a table entry whose function no
longer calls `get_user_by_sub` fails as stale.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

CALLER, TARGET, EXEMPT = "caller", "target", "exempt"

CLASSIFIED = {
    ("lambda_org_api.py", "dispatch"): (CALLER, "the org-api request's authorizer sub"),
    ("lambda_org_api.py", "patch_member_folder"): (
        TARGET, "the member whose folder is being changed (path {sub}), not the caller"),
    ("lambda_org_api.py", "_resolve_staffing"): (
        TARGET, "the member being staffed onto a site (path {sub}), not the caller"),
    ("lambda_org_api.py", "create_member"): (
        TARGET, "the invitee's existing row (sub from Cognito admin_create_user), not the caller"),
    ("lambda_org_api.py", "resend_member_invite"): (
        TARGET, "the member whose invite is resent (path {sub}), not the caller"),
    ("lambda_rag_search.py", "_metric"): (CALLER, "Ask metric route; event sub is the caller"),
    ("lambda_rag_search.py", "_tools"): (CALLER, "Ask tools batch; event sub is the caller"),
    ("lambda_rag_search.py", "_documents"): (CALLER, "project-documents search; the caller"),
    ("lambda_rag_search.py", "_search"): (CALLER, "records search; the caller"),
    ("lambda_ws_connect.py", "lambda_handler"): (CALLER, "WebSocket $connect authorizer sub"),
    ("lambda_voice_resolve.py", "lambda_handler"): (CALLER, "Site Voice sender sub"),
    ("lambda_check_gaps_db.py", "_facts"): (CALLER, "check-done presser's caller_sub"),
    ("checklist_reports.py", "auto_generate"): (
        CALLER, "the recorder acts as the caller of report generation"),
    ("lambda_voice_audit.py", "lambda_handler"): (
        EXEMPT, "audit write, not caller access: records what was asked; serves no data"),
}

_GATE = "caller_blocked"
_LOOKUP = "get_user_by_sub"


def _called_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _scan():
    """{(relpath, function): {"lookups": [lineno...], "gates": [lineno...]}} for every
    function (innermost) that calls get_user_by_sub. Module-level calls key as '<module>'."""
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            # A one-off script this interpreter cannot parse (e.g. patch_report_generator.py,
            # 3.12 f-string syntax). Never imported by a Lambda; but if it ever mentions the
            # lookup it cannot be verified, so it fails rather than being skipped.
            assert _LOOKUP not in text, "%s mentions %s but does not parse" % (rel, _LOOKUP)
            continue
        per_func = {}

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if isinstance(child, ast.Call):
                    name = _called_name(child)
                    if name in (_LOOKUP, _GATE):
                        rec = per_func.setdefault(func, {"lookups": [], "gates": []})
                        rec["lookups" if name == _LOOKUP else "gates"].append(child.lineno)
                visit(child, func)

        visit(tree, "<module>")
        for func, rec in per_func.items():
            if rec["lookups"]:
                found[(rel, func)] = rec
    return found


def test_the_scan_finds_the_known_call_sites():
    # Guards the scanner itself: if it silently found nothing, every check below would pass.
    found = _scan()
    assert ("lambda_org_api.py", "dispatch") in found
    assert len(found[("lambda_org_api.py", "patch_member_folder")]["lookups"]) == 2
    assert ("repositories/users.py", "get_user_by_sub") not in found   # the def, not a call


def test_every_get_user_by_sub_call_is_classified():
    unclassified = sorted(set(_scan()) - set(CLASSIFIED))
    assert not unclassified, (
        "get_user_by_sub called from an unclassified function; add it to CLASSIFIED as "
        "caller (and call tenancy_status.caller_blocked after it) or target/exempt "
        "(with the reason): %s" % unclassified)


def test_no_stale_entries():
    stale = sorted(set(CLASSIFIED) - set(_scan()))
    assert not stale, "CLASSIFIED entries with no get_user_by_sub call any more: %s" % stale


def test_every_classification_has_a_reason():
    for key, (kind, reason) in CLASSIFIED.items():
        assert kind in (CALLER, TARGET, EXEMPT), key
        assert reason and reason.strip(), key


def test_caller_resolutions_are_followed_by_the_gate():
    found = _scan()
    missing = []
    for key, (kind, _) in CLASSIFIED.items():
        if kind != CALLER or key not in found:
            continue
        rec = found[key]
        for line in rec["lookups"]:
            if not any(g > line for g in rec["gates"]):
                missing.append((key, line))
    assert not missing, "caller resolution without a later caller_blocked: %s" % missing


def test_target_and_exempt_lookups_are_not_gated():
    found = _scan()
    gated = [key for key, (kind, _) in CLASSIFIED.items()
             if kind in (TARGET, EXEMPT) and key in found and found[key]["gates"]]
    assert not gated, "caller_blocked inside a target/exempt lookup: %s" % gated
