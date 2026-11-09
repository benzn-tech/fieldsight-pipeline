"""Item quote locator, Task 2: the gated `said_at` prompt block (spec 2026-11-10 §4.1).

With the locator off, every extraction prompt -- live, final and group -- must equal today's,
byte for byte. The golden strings under fixtures/locator_prompt_golden/ were captured from the
prompt builders BEFORE the locator parameter existed (synthetic text only). Regenerate them only
on purpose, with `python tests/unit/test_extraction_locator_prompt.py --write-golden`, and only
when a prompt change unrelated to the locator lands.
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":   # golden capture outside pytest: mirror conftest's path setup
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import item_continuity as ic
import lambda_extract_session as les
import status_suggestions

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "locator_prompt_golden"
TEMPLATE = ROOT / "src" / "template.yaml"
DEPLOY_TEST = ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_PROD = ROOT / ".github" / "workflows" / "deploy-prod.yml"

KINDS = ("action_items", "findings", "decisions", "questions")
GID = "b" * 32


def _turns():
    return [
        {"abs_start_str": "09:00:05", "speaker": "spk_0", "source_filename": "a.wav",
         "text": "Morning, we need to book the crane for Tuesday"},
        {"abs_start_str": "09:00:41", "speaker": "spk_1", "source_filename": "a.wav",
         "text": "The handrail on level two is loose near the stairs"},
        {"abs_start_str": "09:02:10", "speaker": "spk_0", "source_filename": "a.wav",
         "text": "We will pour the slab on Thursday, decided"},
        {"abs_start_str": "09:03:30", "speaker": "spk_1", "source_filename": "a.wav",
         "text": "Who is signing off the formwork?"},
    ]


def _prior_block():
    prior = [ic.PriorItem("A1", "action_items", "Book the crane for Tuesday",
                          "00000000-0000-4000-8000-000000000001")]
    return ic.render_block(prior, status_suggestions.INSTRUCTION)


def _group_artifact():
    return {"groupId": GID, "leadSessionId": GID,
            "members": [{"userFolder": "Synthetic_User", "date": "2026-11-10",
                        "sessionBase": "sid" + GID}]}


def _group_sources():
    return [{"session_id": "sid" + GID, "turns": _turns()[:2]},
            {"session_id": "sid" + "c" * 32, "turns": _turns()[2:]}]


def _build(name, **locator_kw):
    """One named prompt. `locator_kw` is empty for the golden capture (the parameter did not
    exist yet) and `locator=...` afterwards. The module flags are set explicitly so the result
    does not depend on the environment the test runs in."""
    les.DECLARE_CONTINUITY = False
    if name == "live":
        # A live pass with a prior pass and status suggestions: the richest live prompt.
        les.EMIT_EVIDENCE = False
        return les.build_extraction_prompt(
            "Synthetic_User", "2026-11-10", "sid_synthetic", _turns(), 1,
            continuity_block=_prior_block(), **locator_kw)[0]
    if name == "final":
        # A final pass (no prior block, linker on) with evidence on and confirmed names.
        les.EMIT_EVIDENCE = True
        names = {("a.wav", "spk_0"): "Sam"}
        return les.build_extraction_prompt(
            "Synthetic_User", "2026-11-10", "sid_synthetic", _turns(), 2,
            speaker_names=names, continuity_block="", **locator_kw)[0]
    if name == "final_no_evidence":
        les.EMIT_EVIDENCE = False
        return les.build_extraction_prompt(
            "Synthetic_User", "2026-11-10", "sid_synthetic", _turns(), 1, **locator_kw)[0]
    if name == "group":
        les.EMIT_EVIDENCE = True
        return les.build_group_prompt(_group_artifact(), _group_sources())
    raise KeyError(name)


GOLDEN_NAMES = ("live", "final", "final_no_evidence", "group")


def _golden(name):
    # Normalise line endings: a Windows checkout with autocrlf rewrites the fixture.
    return (GOLDEN_DIR / f"{name}.txt").read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture(autouse=True)
def _restore_flags(monkeypatch):
    # _build assigns module flags directly; register them so monkeypatch restores them.
    for flag in ("DECLARE_CONTINUITY", "EMIT_EVIDENCE"):
        monkeypatch.setattr(les, flag, getattr(les, flag))
    monkeypatch.setattr(les, "EMIT_ITEM_LOCATOR", getattr(les, "EMIT_ITEM_LOCATOR", False),
                        raising=False)


def _schema_kind_slices(prompt):
    """The schema text of each child kind inside the prompt's topics schema."""
    start = prompt.index('"topics": [')
    end = prompt.index('"declared_site": {"stated"')
    schema = prompt[start:end]
    bounds = [(k, schema.index(f'"{k}": [')) for k in KINDS]
    out = {}
    for i, (k, pos) in enumerate(bounds):
        stop = bounds[i + 1][1] if i + 1 < len(bounds) else len(schema)
        out[k] = schema[pos:stop]
    return out


# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_locator_off_prompts_are_byte_identical(name):
    assert _build(name) == _golden(name)
    if name != "group":
        assert _build(name, locator=False) == _golden(name)


def test_locator_off_is_the_default():
    import os
    if "EMIT_ITEM_LOCATOR" not in os.environ:
        assert les.EMIT_ITEM_LOCATOR is False
    assert les.LOCATOR_FIELD == "said_at"


@pytest.mark.parametrize("name", ("live", "final", "final_no_evidence"))
def test_locator_on_adds_said_at_to_all_four_kinds(name):
    prompt = _build(name, locator=True)
    slices = _schema_kind_slices(prompt)
    for kind in KINDS:
        assert slices[kind].count('"said_at"') == 1, kind
    assert ('"said_at": "HH:MM:SS of the line where this item was FIRST raised, or null"'
            in prompt)
    # Nothing outside the four child kinds gains the field in the schema.
    start = prompt.index("{\n  \"location_markers\"")
    end = prompt.index('"declared_site": {"stated"')
    assert prompt[start:end].count('"said_at"') == 4


def test_locator_on_only_adds_the_block():
    """On = off plus the schema lines and the one numbered rule; nothing else moves."""
    off = _build("final")
    on = _build("final", locator=True)
    removed = re.sub(r',\n          "said_at": "[^"\n]*"', "", on)
    removed = removed.replace(
        ', "said_at": "HH:MM:SS of the line where this item was FIRST raised, or null"', "")
    removed = re.sub(r"\n8\. SAID_AT\..*?(?=\n\n## Output Format)", "", removed, flags=re.S)
    assert removed == off


def test_locator_on_wording():
    les.EMIT_EVIDENCE = True
    block = les._instructions_block(locator=True)
    assert "FIRST raised" in block
    assert "null" in block
    sentences = re.split(r"(?<=[.:])\s+", " ".join(block.split()))
    assert any("status_suggestion" in s and "said_at" in s and "not" in s and "`at`" in s
               for s in sentences), "no sentence says said_at is not status_suggestion's at"
    assert "Do NOT put evidence inside action_items or findings" in block
    # A stamp, not a quote: the block never asks for words inside said_at.
    assert "not evidence" in block or "not a quote" in block


def test_locator_block_independent_of_evidence():
    les.EMIT_EVIDENCE = False
    block = les._instructions_block(locator=True)
    assert "8. SAID_AT." in block
    assert block.count('"said_at"') == 4


def test_group_prompt_never_has_locator(monkeypatch):
    monkeypatch.setattr(les, "EMIT_ITEM_LOCATOR", True)
    prompt = _build("group")
    assert "said_at" not in prompt
    assert prompt == _golden("group")


# ---------------------------------------------------------------------------
# The call site: final tier and flag on only. The live prompt must not change with the flag on.
# ---------------------------------------------------------------------------

def _prompts_sent(monkeypatch, *, flag, final):
    from tests.unit.test_lambda_extract_session import (
        FakeS3, SEG1_KEY, SESSION_BASE, BUCKET, llm_utils, make_transcribe_json)
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json(
        "we need to fix the scaffold on level two before Friday"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", False)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", False)
    monkeypatch.setattr(les, "EMIT_EVIDENCE", False)
    monkeypatch.setattr(les, "EMIT_ITEM_SUPPORT", False)
    monkeypatch.setattr(les, "EMIT_ITEM_LOCATOR", flag)
    sent = []

    def _fake_llm(prompt, max_tokens=4096, force_json=False, enable_thinking=None, **kw):
        sent.append(prompt)
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", _fake_llm)
    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=final)
    assert len(sent) == 1
    return sent[0]


def test_call_site_live_tier_unchanged_with_flag_on(monkeypatch):
    on = _prompts_sent(monkeypatch, flag=True, final=False)
    off = _prompts_sent(monkeypatch, flag=False, final=False)
    assert on == off
    assert "said_at" not in on


def test_call_site_final_tier_gets_locator_only_with_flag_on(monkeypatch):
    off = _prompts_sent(monkeypatch, flag=False, final=True)
    assert "said_at" not in off
    on = _prompts_sent(monkeypatch, flag=True, final=True)
    assert on.count('"said_at"') == 4 and "8. SAID_AT." in on


# ---------------------------------------------------------------------------
# Wiring: template Parameter -> ExtractSessionFunction env -> both workflows.
# ---------------------------------------------------------------------------

def test_flag_wired():
    text = TEMPLATE.read_text(encoding="utf-8")

    block = re.search(r"\n  EmitItemLocator:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no EmitItemLocator parameter in the template"
    assert re.search(r"Default: '?false'?\n", block.group(1)), (
        "EmitItemLocator must default to false -- a manual sam deploy would turn it on")
    assert "AllowedValues: ['true', 'false']" in block.group(1)

    start = text.index("\n  ExtractSessionFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    fn = text[start:start + 1 + nxt.start()] if nxt else text[start:]
    assert "EMIT_ITEM_LOCATOR: !Ref EmitItemLocator" in fn, (
        "ExtractSessionFunction does not receive EMIT_ITEM_LOCATOR")
    # On ExtractSessionFunction only.
    assert text.count("EMIT_ITEM_LOCATOR: !Ref EmitItemLocator") == 1

    test = DEPLOY_TEST.read_text(encoding="utf-8")
    prod = DEPLOY_PROD.read_text(encoding="utf-8")
    # SAM keeps a parameter's previous value when it is not passed, so both workflows
    # must always pass it, with an explicit false fallback.
    assert "EmitItemLocator=${{ vars.TEST_EMIT_ITEM_LOCATOR || 'false' }}" in test
    assert "EmitItemLocator=${{ vars.PROD_EMIT_ITEM_LOCATOR || 'false' }}" in prod


if __name__ == "__main__" and "--write-golden" in sys.argv:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for _name in GOLDEN_NAMES:
        (GOLDEN_DIR / f"{_name}.txt").write_text(_build(_name), encoding="utf-8", newline="\n")
        print("wrote", _name)
