"""The sweep's pending flag — and above all, that it fails OPEN.

A flag that wrongly reads "nothing pending" silently stops confirmation emails.
Every error path must therefore answer "pending" (do the work). These tests exist
because that direction is the entire safety argument for the feature; a
regression here would not fail any other test and would not raise any alarm in
production — it would just quietly stop sending email.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import sweep_state  # noqa: E402

TABLE = "fieldsight-test-items"


class FakeDdb:
    """Minimal DynamoDB double. `raises` makes every call blow up, which is the
    case that matters most here."""

    def __init__(self, item=None, raises=False):
        self.item = item
        self.raises = raises
        self.puts = []

    def get_item(self, **kw):
        if self.raises:
            raise RuntimeError("dynamodb unavailable")
        return {"Item": self.item} if self.item else {}

    def put_item(self, **kw):
        if self.raises:
            raise RuntimeError("dynamodb unavailable")
        self.puts.append(kw)


def _item(pending):
    return {"PK": {"S": "SWEEP_STATE#test"}, "SK": {"S": "flag"},
            "pending": {"BOOL": pending}}


# --- fail-open: every one of these must answer "there is work" ---------------

def test_is_pending_true_when_dynamodb_errors():
    assert sweep_state.is_pending("test", client=FakeDdb(raises=True), table=TABLE) is True


def test_is_pending_true_when_item_absent():
    assert sweep_state.is_pending("test", client=FakeDdb(item=None), table=TABLE) is True


def test_is_pending_true_when_attribute_missing():
    stripped = {"PK": {"S": "SWEEP_STATE#test"}, "SK": {"S": "flag"}}
    assert sweep_state.is_pending("test", client=FakeDdb(item=stripped), table=TABLE) is True


def test_is_pending_true_when_table_not_configured(monkeypatch):
    monkeypatch.delenv("SWEEP_STATE_TABLE", raising=False)
    assert sweep_state.is_pending("test", client=FakeDdb(item=_item(False))) is True


def test_is_pending_true_when_flag_says_pending():
    assert sweep_state.is_pending("test", client=FakeDdb(item=_item(True)), table=TABLE) is True


# --- the ONLY way to get False ----------------------------------------------

def test_is_pending_false_only_on_explicit_successful_false_read():
    assert sweep_state.is_pending("test", client=FakeDdb(item=_item(False)), table=TABLE) is False


def test_read_is_consistent():
    """A stale read costs an email up to an hour of delay, so the read must be
    strongly consistent."""
    ddb = FakeDdb(item=_item(False))
    captured = {}
    ddb.get_item = lambda **kw: captured.update(kw) or {"Item": _item(False)}
    sweep_state.is_pending("test", client=ddb, table=TABLE)
    assert captured["ConsistentRead"] is True


# --- writes are best-effort and never raise into the caller ------------------

def test_mark_pending_swallows_errors_but_reports_failure():
    assert sweep_state.mark_pending("test", client=FakeDdb(raises=True), table=TABLE) is False


def test_clear_pending_swallows_errors_but_reports_failure():
    assert sweep_state.clear_pending("test", client=FakeDdb(raises=True), table=TABLE) is False


def test_mark_pending_writes_true():
    ddb = FakeDdb()
    assert sweep_state.mark_pending("test", client=ddb, table=TABLE) is True
    assert ddb.puts[0]["Item"]["pending"] == {"BOOL": True}


def test_clear_pending_writes_false():
    ddb = FakeDdb()
    assert sweep_state.clear_pending("test", client=ddb, table=TABLE) is True
    assert ddb.puts[0]["Item"]["pending"] == {"BOOL": False}


def test_stages_do_not_share_a_key():
    """prod and test share one Aurora cluster; they must not share one flag."""
    prod, test = FakeDdb(), FakeDdb()
    sweep_state.mark_pending("prod", client=prod, table=TABLE)
    sweep_state.mark_pending("test", client=test, table=TABLE)
    assert prod.puts[0]["Item"]["PK"] != test.puts[0]["Item"]["PK"]


def test_write_failure_is_logged_not_silent(caplog):
    """A swallowed exception here is how the safety net would end up carrying
    traffic it was never designed for (CLAUDE.md BUG-40)."""
    with caplog.at_level("ERROR"):
        sweep_state.mark_pending("test", client=FakeDdb(raises=True), table=TABLE)
    assert any("sweep_state" in r.message or "sweep_state" in r.getMessage()
               for r in caplog.records)
