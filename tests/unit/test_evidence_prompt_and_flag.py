"""Unit: asking the extraction to cite, behind a flag that is off on prod
(P1-2 Task 3).

Three things are load-bearing:

  * the schema must ask for evidence, or the model never produces any;
  * the prompt must say VERBATIM, or the model regularises more and every
    regularisation drops to the fuzzy tier or below — inflating the
    false-unverified rate the whole measurement exists to keep low;
  * the flag must be settable from a repo variable in BOTH workflows. A value
    set only on the live Lambda is erased by the next CloudFormation reconcile,
    and the failure surfaces days later as "we turned it off and it came back".
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"
ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")


def test_the_schema_asks_for_per_topic_evidence():
    assert '"evidence"' in ex.EXTRACTION_SCHEMA, \
        "the model is never asked to cite anything"
    # Inside the topic object, not at the top level and not inside action_items.
    topics_block = ex.EXTRACTION_SCHEMA.split('"action_items"')[0]
    assert '"evidence"' in topics_block, "evidence must be a per-TOPIC field"


def test_the_schema_names_both_fields_the_matcher_needs():
    ev = ex.EXTRACTION_SCHEMA[ex.EXTRACTION_SCHEMA.index('"evidence"'):]
    assert '"at"' in ev[:400] and '"quote"' in ev[:400]


def test_the_prompt_demands_verbatim_quotes(monkeypatch):
    monkeypatch.setattr(ex, "EMIT_EVIDENCE", True)
    prompt, _ = ex.build_extraction_prompt("Ben_UCPK", "2026-08-07",
                                           "sid" + "a" * 32, [], 0)
    assert "verbatim" in prompt.lower(), \
        "without this the model tidies quotes and honest citations fail to match"


def test_the_prompt_forbids_evidence_below_the_topic_level(monkeypatch):
    monkeypatch.setattr(ex, "EMIT_EVIDENCE", True)
    prompt, _ = ex.build_extraction_prompt("Ben_UCPK", "2026-08-07",
                                           "sid" + "a" * 32, [], 0)
    low = prompt.lower()
    assert "action_items" in low and "evidence" in low


def test_the_flag_off_leaves_the_prompt_untouched(monkeypatch):
    # prod ships with it off. The prompt there must be byte-identical to today's.
    monkeypatch.setattr(ex, "EMIT_EVIDENCE", False)
    off, _ = ex.build_extraction_prompt("Ben_UCPK", "2026-08-07",
                                        "sid" + "a" * 32, [], 0)
    assert "EVIDENCE" not in off


def test_parameter_defaults_off():
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\n  EmitEvidence:\n(?:.*\n)*?\s*Default: '?(\w+)'?\n", text)
    assert m, "no EmitEvidence parameter in the template"
    assert m.group(1) == "false", \
        f"defaults to {m.group(1)} — a manual sam deploy would change the prompt"


def test_both_workflows_pass_it():
    prod = (ROOT / ".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    test = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "EmitEvidence=${{ vars.PROD_EMIT_EVIDENCE || 'false' }}" in prod
    assert "EmitEvidence=${{ vars.TEST_EMIT_EVIDENCE || 'true' }}" in test


def test_extract_session_receives_the_flag():
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  ExtractSessionFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    block = text[start:start + 1 + nxt.start()] if nxt else text[start:]
    assert "EMIT_EVIDENCE: !Ref EmitEvidence" in block
