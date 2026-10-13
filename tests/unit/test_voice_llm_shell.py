"""The ElevenLabs voice-agent endpoint (spec 2026-10-13), shell phase.

Two things are pinned here, and neither is about the handler's logic -- that
is tested in Node, next to the code (voice_llm/index.test.mjs):

  * it exists on TEST only. Prod gets no VoiceLlmFunction until the shell has
    been measured, and the mechanism is that deploy-prod.yml never passes the
    bearer, so the HasVoiceLlm condition is false there. A later edit that
    copies the TEST line into the prod workflow would ship a public endpoint
    to prod with nothing reviewing that decision.
  * the Node tests actually run. CI is pytest; a node --test suite nobody
    invokes is a suite that is green forever. This runs it when node exists
    and says so loudly when it does not.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TEMPLATE = os.path.join(ROOT, "src", "template.yaml")
DEPLOY_TEST = os.path.join(ROOT, ".github", "workflows", "deploy.yml")
DEPLOY_PROD = os.path.join(ROOT, ".github", "workflows", "deploy-prod.yml")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_test_passes_the_bearer_from_its_own_secret():
    text = _read(DEPLOY_TEST)
    assert re.search(r"^\s*VOICE_LLM_BEARER:\s*\$\{\{\s*secrets\.VOICE_LLM_BEARER_TEST\s*\}\}\s*$",
                     text, re.MULTILINE), "TEST must bind the bearer from VOICE_LLM_BEARER_TEST"
    assert "$VOICE_LLM_PARAM" in text, "the guarded override must reach sam deploy"


def test_prod_does_not_create_the_endpoint():
    text = _read(DEPLOY_PROD)
    assert "VoiceLlmBearer" not in text and "VOICE_LLM_BEARER" not in text, (
        "deploy-prod.yml passes the voice-agent bearer -- that creates a public "
        "endpoint on prod; decide that on purpose, not by copying a TEST line")
    assert "VoiceSessionSigningKey" not in text and "VOICE_SESSION" not in text, (
        "deploy-prod.yml passes the voice session signing key -- prod has no "
        "endpoint to sign for; decide that on purpose, not by copying a TEST line")


def test_every_voice_llm_resource_is_behind_the_condition():
    text = _read(TEMPLATE)
    for name in ("VoiceLlmFunction", "VoiceLlmUrlInvokePermission"):
        block = text[text.index("\n  %s:\n" % name):]
        block = block[:re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", block[1:]).start() + 1]
        assert "Condition: HasVoiceLlm" in block, "%s is not conditional" % name


def test_the_endpoint_streams():
    # Buffered mode would hand ElevenLabs the restatement and the answer in
    # one lump at the end -- exactly the silence the restatement exists to fill.
    text = _read(TEMPLATE)
    block = text[text.index("\n  VoiceLlmFunction:\n"):]
    assert "InvokeMode: RESPONSE_STREAM" in block[:2500]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed; Node tests did NOT run")
def test_the_node_suite_passes():
    r = subprocess.run(["node", "--test"], cwd=os.path.join(ROOT, "voice_llm"),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_the_package_is_one_npm_will_pack():
    """sam build packs a Node function with `npm pack`, which refuses a
    package.json without BOTH name and version ("Invalid package, must have
    name and version"). The first deploy of this endpoint (2026-10-13, run
    38385150559) failed at SAM build for exactly that -- and because it
    failed in the build step, every later merge to develop would have failed
    the TEST deploy the same way, for every session, until fixed. The Node
    tests could not see it: node --test never packs.
    """
    import json
    with open(os.path.join(ROOT, "voice_llm", "package.json"), encoding="utf-8") as fh:
        pkg = json.load(fh)
    assert pkg.get("name"), "npm pack needs a name"
    assert pkg.get("version"), "npm pack needs a version"


def _block(name):
    text = _read(TEMPLATE)
    block = text[text.index("\n  %s:\n" % name):]
    return block[:re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", block[1:]).start() + 1]


def test_the_signing_key_parameter_is_secret_and_defaults_empty():
    # Empty default keeps prod's stack valid with nothing passed; NoEcho keeps
    # the key out of describe-stacks.
    block = _block("VoiceSessionSigningKey")
    assert "NoEcho: true" in block
    assert re.search(r"^\s*Default: ''\s*$", block, re.MULTILINE)


def test_the_voice_function_reaches_ask_and_the_model_vendor():
    block = _block("VoiceLlmFunction")
    assert "VOICE_SESSION_SIGNING_KEY: !Ref VoiceSessionSigningKey" in block
    assert "ASK_AGENT_FUNCTION: !Ref AskAgentFunction" in block
    # Same expression AskAgentFunction uses for QWEN_API_KEY: Node must call the
    # vendor Python resolved the request for.
    assert ("OPENROUTER_API_KEY: !If [UsesSeparateChatVendor, !Ref QwenChatApiKey, "
            "!Ref DashScopeApiKey]") in block
    assert "OPENROUTER_BASE_URL: !Ref QwenBaseUrl" in block
    assert "VOICE_LLM_PROBES: 'false'" in block
    assert "VOICE_RESTATE_ENABLED: 'false'" in block
    assert re.search(r"LambdaInvokePolicy:\s*\n\s*FunctionName: !Ref AskAgentFunction", block)


def test_org_api_gets_the_signing_key_only_where_the_endpoint_exists():
    block = _block("OrgApiFunction")
    assert "VOICE_SESSION_SIGNING_KEY: !If [HasVoiceLlm, !Ref VoiceSessionSigningKey, !Ref AWS::NoValue]" in block


def test_test_passes_the_signing_key_from_its_own_secret():
    text = _read(DEPLOY_TEST)
    assert re.search(r"^\s*VOICE_SESSION_SIGNING_KEY:\s*\$\{\{\s*secrets\.VOICE_SESSION_SIGNING_KEY_TEST\s*\}\}\s*$",
                     text, re.MULTILINE)
    assert 'VOICE_SESSION_PARAM="VoiceSessionSigningKey=$VOICE_SESSION_SIGNING_KEY"' in text
    assert re.search(r"^\s*\$VOICE_SESSION_PARAM \\\s*$", text, re.MULTILINE)
