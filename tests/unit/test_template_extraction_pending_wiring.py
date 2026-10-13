"""The extraction_pending loop is only as real as its grants and its alarm (spec D4/D5).

Each assertion names one way this ships green and does nothing: a missing grant is a
403 that the code logs and swallows, a missing schedule is a re-driver that never runs,
an alarm on the wrong series watches nothing.
"""
import os
import re

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _resource(name):
    lines = open(TEMPLATE, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"  {name}:"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^  \S", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _statement(body, action, resource_fragment):
    """Is there a statement granting `action` whose block mentions `resource_fragment`?"""
    for block in re.split(r"\n\s+- Effect: Allow", body):
        if re.search(rf"(?<![\w:]){re.escape(action)}\b", block) and resource_fragment in block:
            return block
    return None


def test_extract_session_can_write_read_list_and_delete_the_marker():
    body = _resource("ExtractSessionFunction")
    assert _statement(body, "s3:PutObject", "/extraction_pending/*")
    assert _statement(body, "s3:GetObject", "/extraction_pending/*")
    assert _statement(body, "s3:DeleteObject", "/extraction_pending/*")
    assert "- extraction_pending/*" in body          # ListBucket: absent is 404, not 403


def test_the_backlog_can_list_read_and_rewrite_markers_and_re_put_requests():
    body = _resource("ExtractionBacklogFunction")
    assert "- extraction_pending/*" in body
    assert _statement(body, "s3:GetObject", "/extraction_pending/*")
    assert _statement(body, "s3:PutObject", "/extraction_pending/*")
    assert _statement(body, "s3:PutObject", "/extraction_requests/*")


def test_the_backlog_runs_the_re_drive_every_five_minutes_and_still_scans_hourly():
    body = _resource("ExtractionBacklogFunction")
    assert "rate(5 minutes)" in body and '"task": "redrive"' in body
    assert "rate(1 hour)" in body


def test_the_finalize_worker_can_read_the_marker_and_record_the_email():
    body = _resource("SessionFinalizeFunction")
    assert _statement(body, "s3:GetObject", "/extraction_pending/*")
    assert _statement(body, "s3:PutObject", "/extraction_pending/*")
    assert "- extraction_pending/*" in body


def test_the_alarm_watches_the_series_the_backlog_emits():
    import lambda_extraction_backlog as bl
    alarm = _resource("ExtractionPendingAlarm")
    assert "AlarmName: !Sub [\"${P}-extraction-pending\"" in alarm
    assert "ExtractionPending" in alarm and "Namespace: FieldSight/Pipeline" in alarm
    assert bl.METRIC_NAMESPACE == "FieldSight/Pipeline"
    assert "Name: Stage" in alarm and "Value: !Ref Stage" in alarm
    assert "Threshold: 1" in alarm and "Period: 300" in alarm
    assert "GreaterThanOrEqualToThreshold" in alarm and "TreatMissingData: notBreaching" in alarm
    assert "!Ref AlertTopic" in alarm and "Condition: ShouldCreateAlerts" in alarm


def test_no_log_metric_filter_was_added():
    """The deploy role cannot create one; the metric is EMF."""
    assert "AWS::Logs::MetricFilter" not in open(TEMPLATE, encoding="utf-8").read()


def test_the_finalize_worker_can_run_the_race_re_check():
    body = _resource("SessionFinalizeFunction")
    assert _statement(body, "s3:GetObject", "/extractions/*")
    assert _statement(body, "s3:PutObject", "/extractions/*")
    assert _statement(body, "s3:DeleteObject", "/extraction_pending/*")
    # absent extraction is the ordinary case: ListBucket, or S3 answers 403 for it
    assert re.search(r"s3:prefix:[\s\S]*?- extractions/\*", body)


CHAIN_BUDGET_FUNCTIONS = ("ExtractSessionFunction", "ReportGeneratorFunction",
                          "MeetingMinutesFunction", "SessionReportFunction",
                          "SessionFinalizeFunction", "RollingSummaryFunction",
                          "MatcherFunction")


def test_every_model_calling_function_has_a_chain_budget_a_minute_under_its_timeout():
    for name in CHAIN_BUDGET_FUNCTIONS:
        body = _resource(name)
        timeout = int(re.search(r"^\s+Timeout:\s*(\d+)", body, re.M).group(1))
        budget = int(re.search(r"LLM_CHAIN_BUDGET_SECONDS:\s*'(\d+)'", body).group(1))
        assert budget == timeout - 60, name
