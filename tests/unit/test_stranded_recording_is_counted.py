"""A recording whose folder has no directory row is counted, and the deploy can create
the alarm that watches the count.

2026-10-05: 45 minutes of a prod recording reached S3 and never reached Aurora. The
count is an Embedded Metric Format line, not a log metric filter: the deploy role may
not call logs:PutMetricFilter, and the first attempt failed the TEST deploy."""
import json
import pathlib

import pytest

ingest = pytest.importorskip("lambda_ingest")

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def test_unknown_folder_error_emits_one_stranded_metric_line(capsys, monkeypatch):
    monkeypatch.setenv("STAGE", "prod")
    err = ingest.unknown_folder_error("Someone__Else")
    assert "has no directory row" in str(err)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    cw = doc["_aws"]["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "FieldSight"
    assert cw["Dimensions"] == [["Stage"]]
    assert [m["Name"] for m in cw["Metrics"]] == ["StrandedRecording"]
    assert doc["Stage"] == "prod"
    assert doc["StrandedRecording"] == 1


def test_template_uses_no_resource_the_deploy_role_cannot_create():
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "AWS::Logs::MetricFilter" not in body
    assert "AWS::SNS::Subscription" not in body


def test_stranded_alarm_watches_the_emitted_metric_for_its_own_stage():
    body = TEMPLATE.read_text(encoding="utf-8")
    start = body.index("  RecordingStrandedAlarm:")
    block = body[start:body.index("AlarmActions:", start)]
    assert "Namespace: FieldSight\n" in block.replace("\r\n", "\n")
    assert "MetricName: StrandedRecording" in block
    assert "Value: !Ref Stage" in block
