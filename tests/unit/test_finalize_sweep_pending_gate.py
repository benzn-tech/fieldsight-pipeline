"""The finalize sweep's pending gate: when it may skip, and when it may forget.

Two behaviours are pinned here, and the second is the subtle one.

1. With the gate OFF the handler is byte-for-byte what it was — it does not even
   ask the flag, and it does not run the extra liveness query. "Deploys inert"
   has to mean the tick touches nothing new.

2. With the gate ON the flag is cleared ONLY on a tick that did nothing at all,
   never merely because no session is live. `session_group.list_due` deems a
   group mergeable exactly when its last member reaches sent/failed — which is
   the same instant `count_live()` reaches zero. Clearing on "no live sessions"
   would therefore retire the flag on the very tick a group first becomes
   mergeable, pushing the merge and its updated email to the hourly safety pass.
   That race is invisible in production: the email still goes, just an hour late,
   and nothing logs an error.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import lambda_finalize_claim as fc  # noqa: E402
import sweep_state  # noqa: E402


class _Conn:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


@pytest.fixture
def wired(monkeypatch):
    """Handler with its collaborators stubbed; returns a dict of recorded calls."""
    seen = {"cleared": 0, "marked": 0, "is_pending_calls": 0, "pending": True}

    monkeypatch.setitem(sys.modules, "db.connection",
                        type("M", (), {"get_connection": staticmethod(lambda: _Conn())}))
    monkeypatch.setattr(fc, "flush_pending_enqueues", lambda: None)

    def _is_pending(stage):
        seen["is_pending_calls"] += 1
        return seen["pending"]

    monkeypatch.setattr(sweep_state, "is_pending", _is_pending)
    monkeypatch.setattr(sweep_state, "clear_pending",
                        lambda stage: seen.__setitem__("cleared", seen["cleared"] + 1))
    return seen


def _quiet_tick(monkeypatch, *, swept=(), reconciled=(), groups=(), live=0):
    monkeypatch.setattr(fc, "sweep", lambda conn: list(swept))
    monkeypatch.setattr(fc, "reconcile", lambda conn, r: list(reconciled))
    monkeypatch.setattr(fc, "_sweep_groups_contained", lambda conn: list(groups))
    monkeypatch.setattr(fc.meeting_session, "count_live", lambda conn: live)


def _not_safety_minute(monkeypatch):
    monkeypatch.setattr(fc, "_is_safety_minute", lambda now: False)


def _safety_minute(monkeypatch):
    monkeypatch.setattr(fc, "_is_safety_minute", lambda now: True)


# --- gate OFF: nothing changes ----------------------------------------------

def test_gate_off_never_consults_the_flag(monkeypatch, wired):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", False)
    _quiet_tick(monkeypatch)
    fc.lambda_handler({}, None)
    assert wired["is_pending_calls"] == 0
    assert wired["cleared"] == 0


def test_gate_off_does_not_run_the_liveness_query(monkeypatch, wired):
    """The extra query is pointless while inert — and the group-merge tests drive
    this handler with a bare connection double, which would blow up."""
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", False)
    monkeypatch.setattr(fc, "sweep", lambda conn: [])
    monkeypatch.setattr(fc, "reconcile", lambda conn, r: [])
    monkeypatch.setattr(fc, "_sweep_groups_contained", lambda conn: [])

    def _explode(conn):
        raise AssertionError("count_live must not run while the gate is off")

    monkeypatch.setattr(fc.meeting_session, "count_live", _explode)
    fc.lambda_handler({}, None)


# --- gate ON: skipping -------------------------------------------------------

def test_skips_without_connecting_when_flag_says_idle(monkeypatch, wired):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _not_safety_minute(monkeypatch)
    wired["pending"] = False
    monkeypatch.setattr(fc, "sweep", lambda conn: pytest.fail("must not connect"))
    out = fc.lambda_handler({}, None)
    assert out["skipped"] == "no-pending"


def test_skip_is_logged_or_it_cannot_be_verified(monkeypatch, wired, caplog):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _not_safety_minute(monkeypatch)
    wired["pending"] = False
    with caplog.at_level("INFO"):
        fc.lambda_handler({}, None)
    assert any("skipped" in r.getMessage() for r in caplog.records)


def test_safety_minute_connects_even_when_flag_says_idle(monkeypatch, wired):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _safety_minute(monkeypatch)
    wired["pending"] = False
    _quiet_tick(monkeypatch)
    out = fc.lambda_handler({}, None)
    assert "skipped" not in out


# --- gate ON: when the flag may be cleared -----------------------------------

def test_clears_on_a_fully_quiet_tick(monkeypatch, wired):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _not_safety_minute(monkeypatch)
    _quiet_tick(monkeypatch, live=0)
    fc.lambda_handler({}, None)
    assert wired["cleared"] == 1


def test_does_not_clear_while_a_session_is_live(monkeypatch, wired):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _not_safety_minute(monkeypatch)
    _quiet_tick(monkeypatch, live=1)
    fc.lambda_handler({}, None)
    assert wired["cleared"] == 0


@pytest.mark.parametrize("work", [
    {"swept": [{"sessionId": "a"}]},
    {"reconciled": [("a", "sent")]},
    {"groups": ["g1"]},
])
def test_does_not_clear_on_a_tick_that_did_work_even_with_no_live_sessions(
        monkeypatch, wired, work):
    """THE group race. reconcile flipping the last member to `sent` is exactly
    what makes its group mergeable, and it leaves count_live() at zero. If the
    flag were cleared here, the next tick would skip and the merge would wait for
    the hourly pass."""
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _not_safety_minute(monkeypatch)
    _quiet_tick(monkeypatch, live=0, **work)
    fc.lambda_handler({}, None)
    assert wired["cleared"] == 0, (
        "cleared the flag on a tick that did work — the next tick will skip, and "
        "anything that became actionable *because* of this tick waits an hour")


def test_flag_miss_on_the_safety_pass_is_an_error(monkeypatch, wired, caplog):
    monkeypatch.setattr(fc, "SWEEP_REQUIRE_PENDING", True)
    _safety_minute(monkeypatch)
    wired["pending"] = False                     # flag denied any work...
    _quiet_tick(monkeypatch, live=0, swept=[{"sessionId": "a"}])   # ...but there was
    with caplog.at_level("ERROR"):
        fc.lambda_handler({}, None)
    assert any("FLAG MISS" in r.getMessage() for r in caplog.records)


# --- the stuck-session trap --------------------------------------------------

def test_count_live_sql_excludes_open_sessions_with_no_activity_anchor():
    """A permanently stuck `open` row must not pin the flag on forever.

    Both stages carry one today (created 2026-08-04, segment_count 0, both
    timestamps NULL). `list_idle_open` cannot infer a close on a row with no
    anchor, so the sweep can never act on it. Counting it as live would keep the
    pending flag raised for good and the cluster would never sleep — a feature
    that deploys clean, logs nothing, and whose only symptom is that the bill
    does not move.
    """
    from repositories import meeting_session as ms

    class _Cur:
        def __init__(self): self.sql = None
        def execute(self, sql, params=None):
            self.sql = " ".join(sql.split())
            return self
        def fetchone(self): return {"n": 0}

    class _Conn:
        def __init__(self): self.cur = _Cur()
        def cursor(self, **kw): return self.cur

    c = _Conn()
    ms.count_live(c)
    sql = c.cur.sql
    assert "COALESCE(last_segment_at, opened_at) IS NOT NULL" in sql, (
        "count_live must ignore open sessions the sweep can never close")
    # pending_close / finalizing stay unconditional — they are always actionable.
    assert "'pending_close', 'finalizing'" in sql
