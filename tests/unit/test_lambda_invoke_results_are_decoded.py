"""A synchronous lambda invoke must not hand its caller boto3's envelope.

`invoke()` returns `{"StatusCode", "ExecutedVersion", "Payload": <StreamingBody>, …}`. The
callee's actual answer is inside `Payload`, and reading it is a separate step that is easy to
forget — because forgetting it produces a dict, not an error.

`lambda_speaker_embed.invoke_writer` forgot it. Every caller then read `.get("profiles")` and
`.get("written")` off the envelope, got None and 0 every time, and the whole speaker-match
path did nothing in production while 3158 tests passed. The log line it produced —
"no consented profiles for this company" — reads as an ordinary empty state, so it did not
look broken either.

The suite could not have caught it: twenty tests monkeypatch `invoke_writer` with a stub
returning a plain dict, and none exercised the function. A stubbed seam is a seam nobody
tested.

This scans the source instead. Every `InvocationType="RequestResponse"` either reads its
`Payload`, or provably does not use the result for anything but a failure check.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _enclosing_function(tree, lineno):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno and (best is None or node.lineno > best.lineno):
                best = node
    return best


def _synchronous_invokes(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "invoke":
            continue
        for kw in node.keywords:
            if (kw.arg == "InvocationType"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "RequestResponse"):
                yield node


def _returns_the_envelope(fn):
    """Does this function hand back the object `invoke()` returned?

    Keyed on the RETURN, not on whether `Payload` appears anywhere in the body — the failure
    branch reads `resp['Payload'].read()` to build an error message, so "mentions Payload"
    is true even when the success path never decodes anything. A first version of this test
    used that and could not be made to fail by the very defect it was written for.
    """
    invoked = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "attr", None) == "invoke"):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    invoked.add(t.id)
    if not invoked:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
            if node.value.id in invoked:
                return True
    return False


def test_every_synchronous_invoke_decodes_its_payload_or_ignores_the_result():
    offenders = []
    for path in sorted(SRC.glob("lambda_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _synchronous_invokes(tree):
            fn = _enclosing_function(tree, call.lineno)
            if fn is None:
                continue
            # Returning the invoke() result itself is the defect, whatever else the
            # function does with `Payload` elsewhere. A function that uses the response only
            # to detect failure and returns its own values is fine — the programme matcher
            # does exactly that.
            if _returns_the_envelope(fn):
                offenders.append(f"{path.name}:{call.lineno} in {fn.name}()")
    assert not offenders, (
        "these return boto3's envelope as if it were the callee's answer, so every key the "
        "caller asks for is missing and no error is raised: " + ", ".join(offenders))
