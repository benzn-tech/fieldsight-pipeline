"""Unit: ContactFunction's template wiring (spec 2026-11-08-contact-fieldsight-design).

Text-level assertions on the resource block, the same approach as
test_template_org_api_media_iam.py: the template is full of CFN intrinsics a plain
YAML loader cannot resolve, and the point is the literal grant text. The Parameter
is read with the same tag-tolerant loader the wiring test uses."""
import io
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "src" / "template.yaml"

_yaml = pytest.importorskip("yaml")


class _Loader(_yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _block():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "\n  ContactFunction:\n" in text, "no ContactFunction resource"
    start = text.index("\n  ContactFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _resource():
    tpl = _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)
    return tpl["Resources"]["ContactFunction"], tpl


def test_function_outside_vpc():
    res, _ = _resource()
    props = res["Properties"]
    assert res["Type"] == "AWS::Serverless::Function"
    assert "VpcConfig" not in props
    assert "VpcConfig" not in _block()
    assert props["Handler"] == "lambda_contact.lambda_handler"
    assert props["Timeout"] == 28
    assert props["MemorySize"] == 256


def test_listbucket_prefix_condition():
    block = _block()
    m = re.search(r"Action:\s*s3:ListBucket\s*\n\s*Resource:\s*!Sub\s*'arn:aws:s3:::\$\{DataBucketName\}'"
                  r"\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n\s*s3:prefix:\s*\['contact_messages/\*'\]",
                  block)
    assert m, "ContactFunction has no ListBucket grant scoped to contact_messages/*"


def test_s3_grants_scoped_to_contact_prefix():
    block = _block()
    assert "arn:aws:s3:::${DataBucketName}/contact_messages/*" in block
    arns = re.findall(r"arn:aws:s3:::[^'\"\s]+", block)
    for arn in arns:
        assert arn in ("arn:aws:s3:::${DataBucketName}",
                       "arn:aws:s3:::${DataBucketName}/contact_messages/*"), arn
    assert "s3:DeleteObject" not in block
    assert "ses:SendRawEmail" in block
    assert "ses:SendEmail\n" not in block


def test_event_path_and_api():
    res, _ = _resource()
    events = res["Properties"]["Events"]
    assert len(events) == 1
    (ev,) = events.values()
    assert ev["Type"] == "Api"
    p = ev["Properties"]
    assert p["Path"] == "/api/contact/{proxy+}"
    assert p["Method"] == "ANY"
    assert p["RestApiId"] == "FieldSightApi"
    # default Cognito authorizer: no Auth override
    assert "Auth" not in p


def test_flag_param_default_false():
    _, tpl = _resource()
    param = tpl["Parameters"]["ContactFieldSight"]
    assert param["Default"] == "false"
    assert param["AllowedValues"] == ["true", "false"]
    assert "CONTACT_FIELDSIGHT: !Ref ContactFieldSight" in _block()


def test_ask_key_and_timeout_env():
    res, _ = _resource()
    env = res["Properties"]["Environment"]["Variables"]
    assert env["ELEVENLABS_ASK_TIMEOUT_SECONDS"] == "12"
    block = _block()
    for line in ("ELEVENLABS_ASK_API_KEY: !Ref ElevenLabsAskApiKey",
                 "S3_BUCKET: !Ref DataBucketName",
                 "SENDER_EMAIL: !Ref SenderEmail",
                 "STAGE: !Ref Stage"):
        assert line in block, line
    # never the batch recording key
    assert "ElevenLabsApiKey\n" not in block and "ELEVENLABS_API_KEY:" not in block


def test_workflows_pass_the_flag_defaulting_off():
    for wf, var in (("deploy.yml", "TEST_CONTACT_FIELDSIGHT"),
                    ("deploy-prod.yml", "PROD_CONTACT_FIELDSIGHT")):
        text = (REPO / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert f"\"ContactFieldSight=${{{{ vars.{var} || 'false' }}}}\"" in text, wf
