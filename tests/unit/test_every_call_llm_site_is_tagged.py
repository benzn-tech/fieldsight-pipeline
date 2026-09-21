"""Every production call to `call_llm` or `corroboration_client.call` must
carry a `caller=` tag.

`llm_utils.call_llm`'s `caller` kwarg exists so the LLM_USAGE CloudWatch line
can attribute cost to the call SITE, not just the function it lives in --
`lambda_ask_agent.py` alone has four call shapes (rewrite, answer, retry,
verdict) sharing one log group, and only `caller` can tell them apart there.
`corroboration_client.call` grew the same kwarg for the same reason: its three
production call sites (the ask-rewrite, the web verdict, the web answer) are
the exact comparison the LLM_USAGE line was commissioned to make possible, and
an untagged one collapsing back into "unknown" would make that comparison
unanswerable while looking instrumented.

A kwarg nobody threads through is worse than no kwarg: every line reads
`caller=unknown` and looks like deliberate measurement while being none at
all. This is the guard that would have caught the previous round's revert --
`caller` was added to `call_llm`'s signature but never passed at a single real
call site, so it was invisible to any test that did not go looking for it.

Parsed with `ast`, not a text grep, so a `call_llm(` inside a comment or a
docstring (lambda_rolling_summary.py's own docstring shows the signature as an
example) can never produce a false positive or a false sense of coverage.
"""
import ast
import os

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")

# The module that OWNS call_llm. Its `def call_llm(...)` line is not a call
# site, and its internal dispatch (`_call_anthropic`/`_call_qwen`) is reached
# by name, not through `call_llm(`, so nothing there needs excluding beyond
# the def itself -- which ast.Call simply never matches.
OWNER_MODULE = "llm_utils.py"

# The module that OWNS corroboration_client.call. Its `def call(...)` line is
# not a call site.
CORROBORATION_OWNER_MODULE = "corroboration_client.py"


def _iter_source_files():
    for name in sorted(os.listdir(SRC_DIR)):
        if name.endswith(".py"):
            yield os.path.join(SRC_DIR, name)


def _call_llm_sites(path):
    """(lineno, has_caller) for every `call_llm(...)` / `x.call_llm(...)` CALL
    node in this file -- never a substring match, so text that merely mentions
    `call_llm(` in a docstring or comment is invisible to this walk."""
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name != "call_llm":
            continue
        has_caller = (
            any(kw.arg == "caller" for kw in node.keywords)
            or any(kw.arg is None for kw in node.keywords)  # **kwargs passthrough
        )
        sites.append((node.lineno, has_caller))
    return sites


def _corroboration_client_aliases(tree):
    """Names this file binds `corroboration_client` to, e.g. `{"client"}` for
    `import corroboration_client as client`, `{"corroboration_client"}` for a
    plain `import corroboration_client`. A file that never imports the module
    has none, and a `.call(` found there belongs to something else entirely
    (a mock, a boto3 client, an unrelated object) -- which is exactly why this
    walk is alias-scoped rather than matching the bare name `call` anywhere."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "corroboration_client":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _corroboration_call_sites(path):
    """(lineno, has_caller) for every `<alias>.call(...)` CALL node in this
    file, where `<alias>` is bound to `corroboration_client` by an import in
    the SAME file. Scoped this way on purpose: `ask_rewrite.standalone_question`
    receives its transport as an injected `call` parameter rather than
    importing `corroboration_client` at all (the module is kept PURE, see its
    own docstring), so a bare `call(` there is not something this alias-scoped
    walk can or should catch -- that call site's `caller="rewrite"` tag is
    covered by a direct behavioural test instead
    (test_ask_rewrite.py::test_the_rewrite_call_is_tagged).
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    aliases = _corroboration_client_aliases(tree)
    if not aliases:
        return []

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "call":
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id in aliases):
            continue
        has_caller = (
            any(kw.arg == "caller" for kw in node.keywords)
            or any(kw.arg is None for kw in node.keywords)  # **kwargs passthrough
        )
        sites.append((node.lineno, has_caller))
    return sites


def test_no_production_call_llm_site_is_missing_a_caller_tag():
    """Fails loudly, naming file:line, the moment a new (or edited) call site
    reaches `call_llm` without saying who it is. Passing `caller=` explicitly
    is required -- relying on the default is exactly the silent-`unknown`
    failure this guard exists to catch."""
    untagged = []
    for path in _iter_source_files():
        if os.path.basename(path) == OWNER_MODULE:
            continue
        for lineno, has_caller in _call_llm_sites(path):
            if not has_caller:
                untagged.append(f"{os.path.relpath(path, SRC_DIR)}:{lineno}")

    assert not untagged, (
        "call_llm() invoked without caller= at:\n  " + "\n  ".join(untagged)
        + "\nEvery production call site must pass caller=\"<tag>\" so the "
        "LLM_USAGE log line can attribute cost to it. Add the kwarg at the "
        "call site -- never rely on the caller=\"unknown\" default.")


def test_the_guard_itself_can_see_a_missing_tag():
    """A guard nobody has seen fail is a guard nobody can trust. Proves the
    walk actually flags an untagged call, using a throwaway module on disk --
    not a hand-built AST -- so this exercises the exact same `ast.parse` path
    the real check runs."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.py")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("import llm_utils\n"
                    "def f():\n"
                    "    return llm_utils.call_llm('p', max_tokens=10)\n")
        sites = _call_llm_sites(bad)
        assert sites == [(3, False)]

        good = os.path.join(d, "good.py")
        with open(good, "w", encoding="utf-8") as f:
            f.write("import llm_utils\n"
                    "def f():\n"
                    "    return llm_utils.call_llm('p', max_tokens=10, caller='x')\n")
        sites = _call_llm_sites(good)
        assert sites == [(3, True)]


def test_no_production_corroboration_call_site_is_missing_a_caller_tag():
    """The same guard, for the second production LLM client this repo
    carries. Fails loudly, naming file:line, the moment a new (or edited)
    `corroboration_client.call(...)` site reaches the vendor without saying
    who it is."""
    untagged = []
    for path in _iter_source_files():
        if os.path.basename(path) == CORROBORATION_OWNER_MODULE:
            continue
        for lineno, has_caller in _corroboration_call_sites(path):
            if not has_caller:
                untagged.append(f"{os.path.relpath(path, SRC_DIR)}:{lineno}")

    assert not untagged, (
        "corroboration_client.call() invoked without caller= at:\n  "
        + "\n  ".join(untagged)
        + "\nEvery production call site must pass caller=\"<tag>\" so the "
        "LLM_USAGE log line can attribute cost to it. Add the kwarg at the "
        "call site -- never rely on the caller=\"unknown\" default.")


def test_the_corroboration_guard_itself_can_see_a_missing_tag():
    """A guard nobody has seen fail is a guard nobody can trust. Proves the
    alias-scoped walk actually flags an untagged `client.call(...)` site,
    using a throwaway module on disk -- not a hand-built AST."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.py")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("import corroboration_client as client\n"
                    "def f():\n"
                    "    return client.call('p', timeout=5)\n")
        sites = _corroboration_call_sites(bad)
        assert sites == [(3, False)]

        good = os.path.join(d, "good.py")
        with open(good, "w", encoding="utf-8") as f:
            f.write("import corroboration_client as client\n"
                    "def f():\n"
                    "    return client.call('p', timeout=5, caller='x')\n")
        sites = _corroboration_call_sites(good)
        assert sites == [(3, True)]


def test_an_unrelated_dot_call_is_not_mistaken_for_the_llm_client():
    """A file with no `corroboration_client` import has no aliases, so a
    `.call(...)` on some unrelated object (a mock, a boto3 client) must never
    be flagged -- the walk is alias-scoped precisely to avoid that false
    positive on the generic method name `call`."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        unrelated = os.path.join(d, "unrelated.py")
        with open(unrelated, "w", encoding="utf-8") as f:
            f.write("def f(some_mock):\n"
                    "    return some_mock.call('p', timeout=5)\n")
        assert _corroboration_call_sites(unrelated) == []
