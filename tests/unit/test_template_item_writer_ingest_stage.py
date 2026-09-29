"""Unit: Ruling R22 -- ItemWriterFunction and IngestFunction must carry STAGE.

carry_forward_apply.py's OrphanedHumanEdits EMF line reads
`os.environ.get("STAGE", "unknown")`. Without STAGE wired into these two
functions' Environment, Task 8's live TEST check found every emission tagged
Stage=unknown instead of test/prod -- indistinguishable from a probe that
never ran, on the CloudWatch side. This is a template-text assertion (env
wiring, not runtime behaviour) -- same shape as
test_template_backlog_metric_stage.py's `_resource` helper.
"""
import os
import re

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _resource(name):
    """The body of one top-level resource, as text (2-space-indented resources
    under `Resources:`, block runs to the next line at that same indent)."""
    lines = open(TEMPLATE, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"  {name}:"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def test_item_writer_function_carries_stage():
    assert "STAGE: !Ref Stage" in _resource("ItemWriterFunction")


def test_ingest_function_carries_stage():
    assert "STAGE: !Ref Stage" in _resource("IngestFunction")
