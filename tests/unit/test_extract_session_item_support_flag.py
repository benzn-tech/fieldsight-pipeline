"""Track C, Task 6: EMIT_ITEM_SUPPORT is a real switch, default off, final tier only.

Style follows tests/unit/test_extract_session_continuity.py: the FakeS3 double and helpers
come from test_lambda_extract_session, and the flag is flipped with monkeypatch.setattr
rather than a module reload.

The flag-off test compares the WRITTEN BYTES against a golden file captured from the
extractor before this flag existed (tests/fixtures/extract_session_final_golden.json).
Only `extracted_at` (wall clock) is normalised; the model identity is pinned.
"""
import json
import logging
import re
from pathlib import Path

import pytest

from tests.unit.test_lambda_extract_session import (
    BUCKET,
    FakeS3,
    OUT_KEY,
    SEG1_KEY,
    SESSION_BASE,
    les,
    llm_utils,
    make_transcribe_json,
)

import item_support

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "extract_session_final_golden.json"
TEMPLATE = ROOT / "src" / "template.yaml"
DEPLOY_TEST = ROOT / ".github" / "workflows" / "deploy.yml"
DEPLOY_PROD = ROOT / ".github" / "workflows" / "deploy-prod.yml"

TRANSCRIPT = "we need to fix the scaffold on level two before Friday"

PAYLOAD = {
    "topics": [
        {
            "topic_title": "Scaffold",
            "time_range": "10:00 – 10:01",
            "action_items": [
                {"action": "Fix the scaffold on level two", "responsible": "Sam",
                 "deadline": "Friday", "priority": "high"},
            ],
            "findings": [
                {"observation": "Scaffold on level two is unsafe", "domain": "safety",
                 "severity": "major", "entity": {"name": None, "trade": None},
                 "recommended_action": None},
            ],
            # Plain-string form on purpose: attach_support normalises these to dicts,
            # so any run of it on the flag-off path would show in the bytes.
            "decisions": ["Fix before Friday"],
            "questions": [],
        },
    ],
    "declared_site": None,
}

CALIBRATED = {"FLOOR": 0.5, "MARGIN": 0.1, "NOTHING_CLOSE": 0.2}


def _fake_llm(prompt, max_tokens=4096, force_json=False, enable_thinking=None, **kw):
    return json.dumps(PAYLOAD), None


def _setup(monkeypatch, *, flag, evidence=False):
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json(TRANSCRIPT))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "DECLARE_CONTINUITY", False)
    monkeypatch.setattr(les, "STATUS_SUGGESTIONS", False)
    monkeypatch.setattr(les, "EMIT_EVIDENCE", evidence)
    monkeypatch.setattr(les, "EMIT_ITEM_SUPPORT", flag, raising=False)
    monkeypatch.setattr(llm_utils, "LLM_PROVIDER", "golden-provider")
    monkeypatch.setattr(llm_utils, "active_model", lambda enable_thinking=None: "golden-model")
    monkeypatch.setattr(llm_utils, "call_llm", _fake_llm)
    return fake_s3


def _calibrate(monkeypatch):
    for name, value in CALIBRATED.items():
        monkeypatch.setattr(item_support, name, value)


def _normalised_written(fake_s3):
    raw = fake_s3.objects[OUT_KEY]
    at = json.loads(raw)["extracted_at"]
    return raw.replace(at, "<EXTRACTED_AT>")


def _golden():
    # Normalise line endings: a Windows checkout with autocrlf rewrites the fixture.
    return GOLDEN.read_text(encoding="utf-8").replace("\r\n", "\n")


def _children(extraction):
    for topic in extraction.get("topics", []):
        for list_name in ("action_items", "findings", "decisions", "questions"):
            for child in topic.get(list_name) or []:
                yield child


def _record_attach_calls(monkeypatch):
    calls = []
    real = item_support.attach_support

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(item_support, "attach_support", _spy)
    return calls


# ---------------------------------------------------------------------------

def test_flag_off_artifact_is_byte_identical(monkeypatch):
    fake_s3 = _setup(monkeypatch, flag=False)
    _calibrate(monkeypatch)   # calibrated or not, off means never called
    calls = _record_attach_calls(monkeypatch)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert calls == []
    assert _normalised_written(fake_s3) == _golden()


def test_flag_off_is_the_default():
    # Module default read at import from an unset env var.
    import os
    if "EMIT_ITEM_SUPPORT" not in os.environ:
        assert les.EMIT_ITEM_SUPPORT is False


def test_flag_on_final_tier_attaches_support(monkeypatch, caplog):
    fake_s3 = _setup(monkeypatch, flag=True)
    _calibrate(monkeypatch)
    calls = _record_attach_calls(monkeypatch)

    with caplog.at_level(logging.INFO):
        extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                         final=True)

    assert len(calls) == 1
    _, kw = calls[0]
    assert kw == {"floor": 0.5, "margin": 0.1, "nothing_close": 0.2}
    children = list(_children(extraction))
    assert len(children) == 3
    # One turn inside the topic's time_range carrying every item's words: each must
    # link through the window path -- proves turns and the session date are passed right.
    for child in children:
        assert child["support"]["status"] == "linked"
        assert child["support"]["candidate_path"] == "window"
    written = json.loads(fake_s3.objects[OUT_KEY])
    assert [c["support"] for c in _children(written)] == [c["support"] for c in children]
    assert any(r.getMessage().startswith("item_support: {") for r in caplog.records)


def test_flag_on_group_and_live_tiers_do_not_attach(monkeypatch):
    fake_s3 = _setup(monkeypatch, flag=True)
    _calibrate(monkeypatch)
    calls = _record_attach_calls(monkeypatch)

    live = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert calls == []
    assert live["tier"] == les.TIER_LIVE
    assert all("support" not in c for c in _children(live))
    assert all("support" not in c for c in _children(json.loads(fake_s3.objects[OUT_KEY])))

    # The group writer is a separate function with its own write; it must not reach
    # item_support at all (a group artifact's turns are not one session's turns).
    import inspect
    assert "item_support" not in inspect.getsource(les.extract_group)
    assert "item_support" not in inspect.getsource(les.build_group_prompt)


def test_support_failure_is_never_fatal(monkeypatch, caplog):
    # Uncalibrated thresholds (today's state): attach_support raises ValueError.
    fake_s3 = _setup(monkeypatch, flag=True)
    for name in CALIBRATED:
        monkeypatch.setattr(item_support, name, None)

    with caplog.at_level(logging.WARNING):
        extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                         final=True)

    assert all("support" not in c for c in _children(extraction))
    # Written, and exactly as the flag-off path would have written it.
    assert _normalised_written(fake_s3) == _golden()
    warned = [r for r in caplog.records
              if r.levelno >= logging.WARNING and r.getMessage().startswith("item_support")]
    assert warned
    # No item or transcript text in the message: the exception TYPE, no traceback.
    assert any("(ValueError)" in r.getMessage() for r in warned)
    for r in warned:
        assert "scaffold" not in r.getMessage().lower()
        assert r.exc_info is None


def test_a_failure_part_way_leaves_no_partial_support(monkeypatch):
    fake_s3 = _setup(monkeypatch, flag=True)
    _calibrate(monkeypatch)

    def _half(result, *a, **kw):
        item_support.normalise_children(result["topics"])
        result["topics"][0]["action_items"][0]["support"] = {"status": "linked"}
        raise RuntimeError("boom")

    monkeypatch.setattr(item_support, "attach_support", _half)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert _normalised_written(fake_s3) == _golden()


# ---------------------------------------------------------------------------
# Wiring: template Parameter -> ExtractSessionFunction env -> both workflows.
# Same text-level approach as test_declare_continuity_wired.py.
# ---------------------------------------------------------------------------

def test_flag_is_wired_template_to_both_workflows():
    text = TEMPLATE.read_text(encoding="utf-8")

    block = re.search(r"\n  EmitItemSupport:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no EmitItemSupport parameter in the template"
    assert re.search(r"Default: '?false'?\n", block.group(1)), (
        "EmitItemSupport must default to false -- a manual sam deploy would turn it on")
    assert "AllowedValues: ['true', 'false']" in block.group(1)

    start = text.index("\n  ExtractSessionFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    fn = text[start:start + 1 + nxt.start()] if nxt else text[start:]
    assert "EMIT_ITEM_SUPPORT: !Ref EmitItemSupport" in fn, (
        "ExtractSessionFunction does not receive EMIT_ITEM_SUPPORT")

    test = DEPLOY_TEST.read_text(encoding="utf-8")
    prod = DEPLOY_PROD.read_text(encoding="utf-8")
    # SAM keeps a parameter's previous value when it is not passed, so both workflows
    # must always pass it, with an explicit false fallback.
    assert "EmitItemSupport=${{ vars.TEST_EMIT_ITEM_SUPPORT || 'false' }}" in test
    assert "EmitItemSupport=${{ vars.PROD_EMIT_ITEM_SUPPORT || 'false' }}" in prod
