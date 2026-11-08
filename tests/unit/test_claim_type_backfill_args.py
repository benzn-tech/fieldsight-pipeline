"""Unit: scripts/claim_type_backfill.py -- its arguments, and that a dry run never commits.

The recompute's SQL is pinned in tests/integration/test_voiceprint_writer_claim_recompute.py;
here the store is stubbed and the connection is a fake that records commit/rollback.
"""
import datetime
import importlib.util
import os
import uuid

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "claim_type_backfill.py")
_spec = importlib.util.spec_from_file_location("claim_type_backfill", _PATH)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

SITE = "6f1c2a8e-3b7d-4c55-9a51-1f0e2b7c9d10"
CO = "11111111-1111-1111-1111-111111111111"


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, keys):
        self.keys = keys
        self.commits = 0
        self.rollbacks = 0
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "FROM sites" in sql:
            return _Result([(CO,)])
        return _Result([(k,) for k in self.keys])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def recomputes(monkeypatch):
    from repositories import claim_type_store
    seen = []
    monkeypatch.setattr(claim_type_store, "recompute_session",
                        lambda conn, **kw: seen.append(kw) or {"rows": 2, "changed": 1})
    return seen


def test_site_is_required():
    with pytest.raises(SystemExit):
        backfill.parse_args([])


def test_site_must_be_a_uuid():
    with pytest.raises(SystemExit):
        backfill.parse_args(["--site", "not-a-uuid"])


def test_since_must_be_a_date():
    with pytest.raises(SystemExit):
        backfill.parse_args(["--site", SITE, "--since", "yesterday"])


def test_defaults_are_a_dry_run_over_all_dates():
    a = backfill.parse_args(["--site", SITE])
    assert a.site == uuid.UUID(SITE) and a.since is None and a.apply is False
    b = backfill.parse_args(["--site", SITE, "--since", "2026-10-01", "--apply"])
    assert b.since == datetime.date(2026, 10, 1) and b.apply is True


def test_backfill_dry_run_writes_nothing(recomputes):
    sid_a, sid_b = "sid" + "a" * 32, "sid" + "b" * 32
    conn = FakeConn([f"extractions/F/2026-11-02/{sid_a}.json",
                     f"extractions/G/2026-11-03/{sid_b}.json"])
    out = backfill.run(conn, site_id=uuid.UUID(SITE),
                       since=datetime.date(2026, 11, 1), apply=False)
    assert conn.commits == 0, "a dry run must never commit"
    assert conn.rollbacks >= 2
    assert [r["session_base"] for r in recomputes] == [sid_a, sid_b]
    assert {r["company_id"] for r in recomputes} == {CO}
    assert {r["site_id"] for r in recomputes} == {SITE}
    assert out["sessions"] == 2 and out["changed"] == 2 and out["apply"] is False
    # --since reaches the session query.
    assert any(p and datetime.date(2026, 11, 1) in p for _, p in conn.sql)


def test_apply_commits_once_per_session(recomputes):
    conn = FakeConn(["extractions/F/2026-11-02/sid" + "c" * 32 + ".json"])
    backfill.run(conn, site_id=uuid.UUID(SITE), apply=True)
    assert conn.commits == 1


def test_a_failing_session_is_counted_and_rolled_back(monkeypatch):
    from repositories import claim_type_store

    def boom(conn, **kw):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(claim_type_store, "recompute_session", boom)
    conn = FakeConn(["extractions/F/2026-11-02/sid" + "d" * 32 + ".json"])
    out = backfill.run(conn, site_id=uuid.UUID(SITE), apply=True)
    assert out["failed"] == 1 and conn.commits == 0
