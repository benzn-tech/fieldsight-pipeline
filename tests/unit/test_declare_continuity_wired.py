# tests/unit/test_declare_continuity_wired.py
"""Spec 2026-09-30, Task 8: DECLARE_CONTINUITY must be a real switch.

Text-level assertions, same approach as test_template_group_merge_flag.py and
test_template_org_api_media_iam.py: the template is full of CFN intrinsics
(!Sub/!Ref/!ImportValue) that a plain YAML loader cannot resolve, and the
point here is the literal parameter text.

A Parameter with no `--parameter-overrides` line behind it can only ever hold
its template default -- this repo has shipped that gap twice already
(FILTER_AUDIO_EVENT_TAGS, TRANSCRIBE_WHOLE_CHUNK), so the three links -- repo
variable, workflow override, template Parameter -- are each pinned here
rather than trusted.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"
DEPLOY_TEST = ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_PROD = ROOT / ".github" / "workflows" / "deploy-prod.yml"


def test_parameter_exists_and_defaults_off():
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\n  DeclareContinuity:\n(?:.*\n)*?\s*Default: '?(\w+)'?\n", text)
    assert m, "no DeclareContinuity parameter in the template"
    assert m.group(1) == "false", (
        f"DeclareContinuity defaults to {m.group(1)} -- a manual sam deploy "
        f"would turn on continuity claims and change the extraction prompt")


def test_parameter_is_constrained_to_true_false():
    text = TEMPLATE.read_text(encoding="utf-8")
    block = re.search(r"\n  DeclareContinuity:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no DeclareContinuity parameter block in the template"
    assert "AllowedValues: ['true', 'false']" in block.group(1), (
        "DeclareContinuity is not constrained to true/false")


def test_extract_session_receives_the_flag():
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  ExtractSessionFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    block = text[start:start + 1 + nxt.start()] if nxt else text[start:]
    assert "DECLARE_CONTINUITY: !Ref DeclareContinuity" in block, (
        "ExtractSessionFunction does not receive DECLARE_CONTINUITY -- it would "
        "silently take the code default")


def test_both_workflows_pass_the_flag_defaulting_to_false():
    test = DEPLOY_TEST.read_text(encoding="utf-8")
    prod = DEPLOY_PROD.read_text(encoding="utf-8")
    assert "DeclareContinuity=${{ vars.TEST_DECLARE_CONTINUITY || 'false' }}" in test, (
        "TEST does not pass DeclareContinuity with a false fallback -- the "
        "Parameter can only ever hold its template default")
    assert "DeclareContinuity=${{ vars.PROD_DECLARE_CONTINUITY || 'false' }}" in prod, (
        "PROD does not pass DeclareContinuity with a false fallback -- the "
        "Parameter can only ever hold its template default")
