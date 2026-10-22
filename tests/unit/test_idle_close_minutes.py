"""IDLE_CLOSE_MINUTES: how long an `open` session may go without a new chunk
before the sweep infers it closed, and the quiet test group merge uses.

It used to be SESSION_GAP_MINUTES (15) — the "one session" inactivity constant.
The owner moved idle-close to 1 h on every platform (M2 spec §8): idle is
measured on SERVER arrival, and a phone that queues offline delivers its chunks
late, so 15 min closed sessions that were still being recorded. It is a SAM
parameter so a stage can roll back (`PROD_IDLE_CLOSE_MINUTES=15`) without code.
SESSION_GAP_MINUTES itself is unchanged — other readers still use it."""
import importlib
import os
import pathlib
import re

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

import pytest

import lambda_finalize_claim as fc
import session_scope

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def reload_fc(monkeypatch):
    """Reload with a given env; on teardown, restore env and reload again so
    later tests see the module's defaults."""
    def _reload(value):
        if value is None:
            monkeypatch.delenv("IDLE_CLOSE_MINUTES", raising=False)
        else:
            monkeypatch.setenv("IDLE_CLOSE_MINUTES", value)
        return importlib.reload(fc)
    yield _reload
    monkeypatch.undo()
    importlib.reload(fc)


def test_default_is_60_minutes(reload_fc):
    mod = reload_fc(None)
    assert mod.IDLE_CLOSE_MINUTES == 60
    assert mod.IDLE_CLOSE_SECONDS == 3600
    mod = reload_fc("5")
    assert mod.IDLE_CLOSE_MINUTES == 5
    assert mod.IDLE_CLOSE_SECONDS == 300


def test_session_gap_is_untouched():
    assert session_scope.SESSION_GAP_MINUTES == 15


def test_group_scan_uses_idle_close(monkeypatch, reload_fc):
    # A non-default value, so the test cannot pass by SESSION_GAP_MINUTES * 60
    # happening to equal IDLE_CLOSE_SECONDS.
    mod = reload_fc("7")
    from repositories import session_group
    seen = []

    def fake_list_due(conn, idle_grace_seconds):
        seen.append(idle_grace_seconds)
        return []

    monkeypatch.setattr(session_group, "list_due", fake_list_due)
    mod._real_group_scan("CONN")
    assert seen == [mod.IDLE_CLOSE_SECONDS] == [420]


def _function_block(template, name):
    """The text of one top-level resource: from `  Name:` to the next line
    indented exactly two spaces."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S)", template, re.M | re.S)
    assert m, f"{name} not found in template.yaml"
    return m.group(1)


def test_template_and_workflows_wire_the_parameter():
    template = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")
    params = template.split("\nResources:", 1)[0].split("\nParameters:", 1)[1]
    param = _function_block(params + "\n  END:", "IdleCloseMinutes")
    assert "Type: Number" in param
    assert "Default: 60" in param
    assert "MinValue: 1" in param
    resources = template.split("\nResources:", 1)[1]
    assert "IDLE_CLOSE_MINUTES: !Ref IdleCloseMinutes" in _function_block(
        resources, "FinalizeSweepFunction")

    for wf, stage in (("deploy.yml", "TEST"), ("deploy-prod.yml", "PROD")):
        lines = (ROOT / ".github" / "workflows" / wf).read_text(
            encoding="utf-8").splitlines()
        want = ('"IdleCloseMinutes=${{ vars.%s_IDLE_CLOSE_MINUTES || \'60\' }}" \\'
                % stage)
        hits = [i for i, ln in enumerate(lines) if ln.strip() == want]
        assert len(hits) == 1, f"{wf}: IdleCloseMinutes override line missing"
        # Its own continuation line inside --parameter-overrides: the line
        # before must also end in a backslash or the shell drops it.
        assert lines[hits[0] - 1].rstrip().endswith("\\"), wf
