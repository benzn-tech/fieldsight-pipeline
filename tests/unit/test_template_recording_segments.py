"""Unit: RecordingSegmentsFunction is declared the way design §5.4 and this repo's traps require.

Parses the template text by resource block rather than grepping the whole file:
`State:`, `s3:ListBucket` and `transcripts/` all appear under other resources.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {
    "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml"),
    "test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
}
GATE = "State: !If [ShouldEnableRecordingBlocks, ENABLED, DISABLED]"


def _text():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def _function_block(text, name):
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _event_block(block, event_name):
    """One event's body: from `        {event_name}:` to the next 8-space-indented key."""
    start = block.index(f"\n        {event_name}:\n")
    nxt = re.search(r"\n        [A-Za-z][A-Za-z0-9]*:\n", block[start + 1:])
    return block[start:start + 1 + nxt.start()] if nxt else block[start:]


def test_it_is_an_in_vpc_database_function_with_capped_concurrency():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "Condition: HasDb" in block
    assert "Handler: lambda_recording_segments.lambda_handler" in block
    assert "- !Ref PsycopgLayer" in block
    assert "VpcConfig:" in block and "SubnetIds: !Ref DbSubnetIds" in block
    assert "PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'" in block
    assert "S3_BUCKET: !Ref IngestBucketName" in block
    assert re.search(r"^\s*ReservedConcurrentExecutions: 2\s*$", block, re.M)


def test_the_object_rule_watches_transcripts_and_follows_the_switch():
    event = _event_block(_function_block(_text(), "RecordingSegmentsFunction"), "TranscriptLanded")
    assert "Type: EventBridgeRule" in event
    assert GATE in event
    assert "- Object Created" in event
    assert "- !Ref IngestBucketName" in event
    assert "- prefix: transcripts/" in event


def test_the_trailing_pass_runs_every_five_minutes_on_the_same_switch():
    event = _event_block(_function_block(_text(), "RecordingSegmentsFunction"), "DirtyDaySweep")
    assert "Type: Schedule" in event
    assert "Schedule: rate(5 minutes)" in event
    assert GATE in event
    assert not re.search(r"^\s*Enabled:", event, re.M)


def test_it_may_list_transcripts_and_read_or_write_no_object():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "Action: s3:ListBucket" in block
    assert re.search(r"s3:prefix:\s*\n\s*- transcripts/\*\s*\n", block)
    for forbidden in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                      "lambda:InvokeFunction", "secretsmanager:GetSecretValue"):
        assert forbidden not in block, f"RecordingSegmentsFunction must not be granted {forbidden}"


def test_its_only_dynamodb_access_is_the_sweep_flag_in_the_items_table():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "SWEEP_STATE_TABLE: !Ref ItemsTableName" in block
    assert "STAGE: !Ref Stage" in block
    assert re.search(r"- DynamoDBCrudPolicy:\s*\n\s*TableName: !Ref ItemsTableName", block)
    assert block.count("DynamoDB") == block.count("DynamoDBCrudPolicy") + block.count(
        "DynamoDB is reachable"), "no other DynamoDB grant"


def test_it_is_given_no_threshold():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "REPORT_BLOCK_GAP_SECONDS:" not in block
    assert "REPORT_LONG_BLOCK_SECONDS:" not in block


def test_the_finalize_path_function_is_untouched():
    # Comment lines are dropped: a block runs to the next resource NAME, so the
    # banner comment above RecordingSegmentsFunction falls inside this one.
    block = _function_block(_text(), "SessionActivityFunction")
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "recording" not in code.lower()
    assert "Handler: session_activity.lambda_handler" in block


def test_a_throttle_alarm_watches_the_function():
    """Reserved concurrency 2 means throttles are expected under a burst, and a throttled
    async invocation writes NO log line. This metric is the only place one shows."""
    block = _function_block(_text(), "RecordingSegmentsThrottleAlarm")
    assert "Type: AWS::CloudWatch::Alarm" in block
    assert "Condition: ShouldAlarmRecordingSegments" in block
    assert "Namespace: AWS/Lambda" in block and "MetricName: Throttles" in block
    assert re.search(r"- Name: FunctionName\s*\n\s*Value: !Ref RecordingSegmentsFunction", block)
    assert "Threshold: 1" in block and "ComparisonOperator: GreaterThanOrEqualToThreshold" in block
    assert re.search(r"AlarmActions:\s*\n\s*- !Ref AlertTopic", block)


def test_the_alarm_rides_the_same_no_alerts_switch_and_needs_the_database():
    """Same sentinel as every failure alarm (AlertEmail 'none' or '' creates nothing),
    and HasDb because the function it names only exists with a database -- an alarm on a
    resource that does not exist fails the whole stack (ShouldAlarmItemWriter precedent)."""
    cond = re.search(r"\n  ShouldAlarmRecordingSegments: !And\n((?:    - .*\n)+)", _text())
    assert cond, "no ShouldAlarmRecordingSegments condition"
    assert sorted(cond.group(1).split()) == sorted(
        "- !Condition ShouldCreateAlerts - !Condition HasDb".split())


def test_missing_throttle_data_is_never_treated_as_a_fault():
    """notBreaching, unconditionally. Throttles has no datapoint in a period with no
    throttle, on every stack, whether EnableRecordingBlocks is on or off -- so missing data
    is never a fault here, and a real throttle is a datapoint >= 1 that notBreaching cannot
    hide. The conditional `breaching` of ExtractionBacklogAlarm is for a series its function
    publishes on EVERY run; applied here it would page every quiet period."""
    block = _function_block(_text(), "RecordingSegmentsThrottleAlarm")
    [line] = [ln.strip() for ln in block.splitlines() if ln.strip().startswith("TreatMissingData:")]
    assert line == "TreatMissingData: notBreaching"


def test_the_switch_is_a_boolean_parameter_behind_a_condition():
    text = _text()
    assert "ShouldEnableRecordingBlocks: !Equals [!Ref EnableRecordingBlocks, 'true']" in text
    param = re.search(r"\n  EnableRecordingBlocks:\n(.*?)(?=\n  \w+:\n)", text, re.S).group(1)
    assert "Default: 'false'" in param
    assert "AllowedValues: ['true', 'false']" in param
