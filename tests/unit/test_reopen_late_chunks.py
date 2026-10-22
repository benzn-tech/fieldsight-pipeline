"""REOPEN_LATE_CHUNKS / REOPEN_MAX / REOPEN_WINDOW_HOURS: the finalize sweep reopens an idle-closed,
emailed session when a later chunk arrives (M2 owner decision). The SQL is
covered in tests/integration/test_reopen_late_chunks_sql.py; this covers the
env defaults, the sweep ordering and the SAM/workflow wiring."""
import importlib
import logging
import os
import pathlib
import re

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import pytest

import lambda_finalize_claim as fc

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def reload_fc(monkeypatch):
    def _reload(**env):
        for k in ("REOPEN_LATE_CHUNKS", "REOPEN_MAX", "REOPEN_WINDOW_HOURS"):
            if env.get(k) is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, env[k])
        return importlib.reload(fc)
    yield _reload
    monkeypatch.undo()
    importlib.reload(fc)


def test_defaults_off_and_three(reload_fc):
    mod = reload_fc()
    assert mod.REOPEN_LATE_CHUNKS is False
    assert mod.REOPEN_MAX == 3
    assert mod.REOPEN_WINDOW_HOURS == 48
    mod = reload_fc(REOPEN_LATE_CHUNKS="true", REOPEN_MAX="5", REOPEN_WINDOW_HOURS="12")
    assert mod.REOPEN_LATE_CHUNKS is True
    assert mod.REOPEN_MAX == 5
    assert mod.REOPEN_WINDOW_HOURS == 12


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    ms = fc.meeting_session
    monkeypatch.setattr(ms, "reopen_late", lambda conn, mx, hours: calls.append(
        ("reopen", mx, hours)) or [{"session_id": "s1", "reopen_count": 2}])
    monkeypatch.setattr(ms, "list_reopen_capped", lambda conn, mx, hours: calls.append(
        ("capped", mx, hours)) or ["s2"])
    monkeypatch.setattr(fc, "infer_idle_closes",
                        lambda conn, idle: calls.append(("infer", idle)) or 0)
    monkeypatch.setattr(fc, "revisit_active_batch_sessions", lambda: [])
    monkeypatch.setattr(ms, "list_due_finalize", lambda conn, grace: [])
    return calls


def test_sweep_reopens_before_inferring_idle(recorded, caplog):
    with caplog.at_level(logging.INFO):
        fc.sweep("CONN", reopen=True, infer_idle=True)
    assert [c[0] for c in recorded] == ["reopen", "capped", "infer"]
    assert recorded[0] == ("reopen", fc.REOPEN_MAX, fc.REOPEN_WINDOW_HOURS)
    assert recorded[1] == ("capped", fc.REOPEN_MAX, fc.REOPEN_WINDOW_HOURS)
    msgs = [r.getMessage() for r in caplog.records]
    assert "finalize: reopened s1 after a late chunk (reopen 2)" in msgs
    assert "finalize: s2 has a late chunk but reached REOPEN_MAX" in msgs


def test_sweep_default_follows_the_flag(recorded, monkeypatch):
    monkeypatch.setattr(fc, "REOPEN_LATE_CHUNKS", False)
    fc.sweep("CONN", infer_idle=False)
    assert recorded == []
    monkeypatch.setattr(fc, "REOPEN_LATE_CHUNKS", True)
    fc.sweep("CONN", infer_idle=False)
    assert [c[0] for c in recorded] == ["reopen", "capped"]


def _block(text, name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S)", text, re.M | re.S)
    assert m, f"{name} not found in template.yaml"
    return m.group(1)


def test_template_and_workflows_wire_the_parameter():
    template = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")
    params = template.split("\nResources:", 1)[0].split("\nParameters:", 1)[1]
    param = _block(params + "\n  END:", "ReopenLateChunks")
    assert "Type: String" in param
    assert "Default: 'false'" in param
    assert "AllowedValues: ['true', 'false']" in param
    sweep_fn = _block(template.split("\nResources:", 1)[1], "FinalizeSweepFunction")
    assert "REOPEN_LATE_CHUNKS: !Ref ReopenLateChunks" in sweep_fn
    assert "REOPEN_MAX: '3'" in sweep_fn
    assert "REOPEN_WINDOW_HOURS: '48'" in sweep_fn

    for wf, stage, default in (("deploy.yml", "TEST", "true"),
                               ("deploy-prod.yml", "PROD", "false")):
        lines = (ROOT / ".github" / "workflows" / wf).read_text(
            encoding="utf-8").splitlines()
        want = ('"ReopenLateChunks=${{ vars.%s_REOPEN_LATE_CHUNKS || \'%s\' }}" \\'
                % (stage, default))
        hits = [i for i, ln in enumerate(lines) if ln.strip() == want]
        assert len(hits) == 1, f"{wf}: ReopenLateChunks override line missing"
        assert lines[hits[0] - 1].rstrip().endswith("\\"), wf
        assert lines[hits[0] + 1].strip().startswith('"'), (
            f"{wf}: the line after must still be an override (continuation kept)")
