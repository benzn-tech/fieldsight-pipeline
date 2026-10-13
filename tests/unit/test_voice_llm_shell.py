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
