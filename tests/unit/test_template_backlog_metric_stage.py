"""Unit: the backlog metric is dimensioned, and BOTH ends agree on the name.

Two stacks publish `ExtractionBacklog` into one namespace. Undimensioned they
write the SAME series, and the failure that creates is not "test pages prod" --
it is that a dead PROD probe stops being detectable, because a live test
publisher keeps the series populated and the alarm's `TreatMissingData:
breaching` never trips. That is exactly the "the checker stopped running"
scenario the probe was written to catch.

The last assertion is the one worth having: a dimension is a contract between
two files. The function decides the name in Python, the alarm asserts it in
YAML, and nothing at deploy time compares them -- a rename on either side
produces an alarm watching a series nobody writes, which reads as permanently
healthy. So the expected name is taken from the deployed code path rather than
written twice.
"""
import os
import re

import pytest

bl = pytest.importorskip("lambda_extraction_backlog")

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _resource(name):
    """The body of one top-level resource, as text.

    Resources sit at 2-space indent under `Resources:`; the block runs to the
    next line at that same indent. Grepping the whole file instead would happily
    match another alarm's dimensions.
    """
    lines = open(TEMPLATE, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"  {name}:"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _published_dimension_name(monkeypatch):
    """What the FUNCTION actually puts on the metric -- not a literal repeated
    from the template, which is what makes this a cross-file check."""
    sent = {}

    class _CW:
        def put_metric_data(self, Namespace=None, MetricData=None):
            sent["data"] = MetricData

    monkeypatch.setattr(bl, "METRIC_STAGE", "prod")
    monkeypatch.setattr(bl, "scan", lambda client, now=None: ([], [], 0))
    monkeypatch.setattr(bl, "_s3", lambda: None)
    monkeypatch.setattr(bl.boto3, "client",
                        lambda name, *a, **k: _CW() if name == "cloudwatch" else None)
    bl.lambda_handler({}, None)
    return sent["data"][0]["Dimensions"][0]["Name"]


def test_the_probe_is_told_which_stack_it_is():
    """Without this env var the code publishes no dimension at all, and the
    alarm below would watch a series that is never written."""
    assert "BACKLOG_METRIC_STAGE: !Ref Stage" in _resource("ExtractionBacklogFunction")


def test_the_alarm_watches_the_dimensioned_series(monkeypatch):
    body = _resource("ExtractionBacklogAlarm")
    assert "Dimensions:" in body
    assert "Value: !Ref Stage" in body
    name = _published_dimension_name(monkeypatch)
    assert f"Name: {name}" in body, (
        f"the function publishes dimension {name!r} but the alarm names another"
    )
