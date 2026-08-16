"""Unit: the label map has to survive the middle hop, or the feature does not exist.

`label_map` is written by org-api into both request artifacts, and read by the writer out of
its event. The only thing between them is the embedder, which builds the writer's payload —
and it never carried the key across, so `_inherit_labels` received None on every call and
returned immediately. PR #508's whole label-inheritance feature, and the
`0044_speaker_name_rejections` guard it feeds, were unreachable code.

Nothing failed. Each side's tests exercised its own half and passed; this repo's CLAUDE.md
lists that exact shape — "I wrote the function and tested the function and never called it".
"""
import ast
import os

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")


def _module(name):
    return open(os.path.join(SRC, name), encoding="utf-8").read()


def _writer_payload_dicts():
    """Every dict literal the embedder hands to invoke_writer."""
    tree = ast.parse(_module("lambda_speaker_embed.py"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "invoke_writer"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Dict):
                out.append({k.value for k in arg.keys if isinstance(k, ast.Constant)})
            elif isinstance(arg, ast.Name):
                # payload built above and passed by name — resolve it from the assignment
                for a in ast.walk(tree):
                    if (isinstance(a, ast.Assign) and isinstance(a.value, ast.Dict)
                            and any(getattr(t, "id", "") == arg.id for t in a.targets)):
                        out.append({k.value for k in a.value.keys
                                    if isinstance(k, ast.Constant)})
    return out


def test_the_writer_reads_a_key_the_embedder_never_sent():
    """Both ends agreed on the name; the middle never spoke it."""
    writer = _module("lambda_voiceprint_writer.py")
    assert 'event.get("label_map")' in writer, "the writer no longer reads it — update this"

    payloads = _writer_payload_dicts()
    assert payloads, "found no invoke_writer payloads to inspect — the AST walk is stale"

    # Only the ops that lead to _inherit_labels need it; `profiles` is a lookup.
    carrying = [p for p in payloads if "results" in p]
    assert carrying, "no result-bearing payload found"
    for keys in carrying:
        assert "label_map" in keys, (
            "a payload that reaches _inherit_labels does not carry label_map, so it "
            f"receives None and returns 0 — keys were {sorted(keys)}")
