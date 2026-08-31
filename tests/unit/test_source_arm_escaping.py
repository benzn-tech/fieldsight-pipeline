"""The deletion source arm dies silently if its query is executed without params.

`DELETED_SOURCE_PREDICATE` ends in `LIKE r.target_key || '%%'`. The doubled `%`
is correct ONLY because psycopg is doing %-interpolation, which it does only
when the execute() call is given parameters. Execute the same SQL with no
params and psycopg passes the string through untouched: the LIKE pattern stays
a literal `%%`, matches no key, and the source arm quietly stops hiding
anything -- no error, no log line, every unit test still green, and the rows a
customer deleted come back the next time the pipeline re-creates them.

There is no such call site today (this test was written after checking). It
exists so that adding one is a red test rather than a silent leak.

Related: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# The tables whose visibility depends on the source arm.
GUARDED_TABLES = re.compile(r"FROM\s+(topics|report_chunks)\b")


def _execute_calls(text):
    """(line, argument-source) for every `.execute(...)` in a module."""
    for m in re.finditer(r"\.execute\(", text):
        i = m.end()
        depth, j = 1, i
        while j < len(text) and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        yield text[:i].count("\n") + 1, text[i:j - 1]


def _passes_params(arg):
    """A top-level comma means a second argument, i.e. params were passed."""
    depth = 0
    for ch in arg:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return True
    return False


def test_no_guarded_read_executes_without_params():
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line, arg in _execute_calls(text):
            if _passes_params(arg):
                continue
            # Follow a bare name back to what it was assigned, so an f-string
            # built above the call is examined too.
            body = arg
            name = arg.strip()
            if re.fullmatch(r"[A-Za-z_]\w*", name):
                for a in re.finditer(rf"\b{name}\s*=", text):
                    body += text[a.end():a.end() + 2000]
            if GUARDED_TABLES.search(body):
                offenders.append(f"{path.relative_to(SRC)}:{line}")
    assert not offenders, (
        "These read topics/report_chunks with no execute() params, so the "
        "deletion source arm's `LIKE ... || '%%'` stays a literal `%%` and "
        "matches nothing. Pass the query's params to execute(): " + ", ".join(offenders)
    )
