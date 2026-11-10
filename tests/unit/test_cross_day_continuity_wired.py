# tests/unit/test_cross_day_continuity_wired.py
"""Spec 2026-11-09 (cross-day continuity) X1: CROSS_DAY_CONTINUITY must be a real switch.

Same text-level approach as test_declare_continuity_wired.py: the template is full of CFN
intrinsics a plain YAML loader cannot resolve, and the point is the literal text. A Parameter
with no `--parameter-overrides` line behind it can only ever hold its template default
(memory: fieldsight-unwired-toggle-trap), so all three links -- repo variable, workflow
override, template Parameter -- are pinned, plus the item writer's S3 grant on the prefix.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"
DEPLOY_TEST = ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_PROD = ROOT / ".github" / "workflows" / "deploy-prod.yml"


def _block(text, header):
    start = text.index(f"\n  {header}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_parameter_exists_defaults_off_and_is_constrained():
    text = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"\n  CrossDayContinuity:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no CrossDayContinuity parameter block in the template"
    assert "Type: String" in block.group(1)
    assert "Default: 'off'" in block.group(1), (
        "CrossDayContinuity must default to off -- a manual sam deploy would start writing "
        "cross-day requests")
    assert "AllowedValues: ['off', 'shadow', 'on']" in block.group(1)


def test_only_the_item_writer_and_the_xday_linker_receive_the_flag():
    text = TEMPLATE.read_text(encoding="utf-8")
    for fn in ("ItemWriterFunction", "ContinuityXdayLinkerFunction"):
        assert "CROSS_DAY_CONTINUITY: !Ref CrossDayContinuity" in _block(text, fn), (
            f"{fn} does not receive CROSS_DAY_CONTINUITY -- it would silently take "
            "the code default")
    assert text.count("CROSS_DAY_CONTINUITY:") == 2, "CROSS_DAY_CONTINUITY is set elsewhere too"


def test_linker_reads_the_same_env_name_and_defaults_off():
    src = (ROOT / "src" / "lambda_continuity_xday_linker.py").read_text(encoding="utf-8")
    assert re.search(r'os\.environ\.get\(\s*"CROSS_DAY_CONTINUITY",\s*"off"\s*\)', src)


def test_item_writer_may_put_on_the_request_prefix():
    block = _block(TEMPLATE.read_text(encoding="utf-8"), "ItemWriterFunction")
    assert re.search(
        r"Action: s3:PutObject\n\s+Resource: !Sub arn:aws:s3:::\$\{IngestBucketName\}"
        r"/continuity_xday_requests/\*\n", block), (
        "ItemWriterFunction has no PutObject on continuity_xday_requests/* -- every emit "
        "would be AccessDenied (and swallowed)")


def test_both_workflows_pass_the_flag_defaulting_to_off():
    test = DEPLOY_TEST.read_text(encoding="utf-8")
    prod = DEPLOY_PROD.read_text(encoding="utf-8")
    assert "CrossDayContinuity=${{ vars.TEST_CROSS_DAY_CONTINUITY || 'off' }}" in test, (
        "TEST does not pass CrossDayContinuity with an off fallback")
    assert "CrossDayContinuity=${{ vars.PROD_CROSS_DAY_CONTINUITY || 'off' }}" in prod, (
        "PROD does not pass CrossDayContinuity with an off fallback")


def test_writer_reads_the_same_env_name_and_defaults_off():
    src = (ROOT / "src" / "lambda_item_writer.py").read_text(encoding="utf-8")
    assert re.search(r'os\.environ\.get\(\s*"CROSS_DAY_CONTINUITY",\s*"off"\s*\)', src), (
        "lambda_item_writer must read CROSS_DAY_CONTINUITY with an off default")
