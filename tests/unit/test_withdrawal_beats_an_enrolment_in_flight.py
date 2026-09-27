"""Unit: a withdrawal is not undone by an enrolment that was already in the air.

## The defect, measured

On TEST, 2026-09-23:

    01:46:13  profile created by a correction
    01:46:xx  profile withdrawn        -> `samplesRemoved: 0`, quite correctly
    01:47:33  the embedder finished its window and the sample LANDED

`withdraw` deletes the samples that exist when it runs. Nothing stopped the one in flight,
and enrolment is slow by nature — the embedder narrows a long window, checks homogeneity,
and embeds it, which took 80 seconds here. Any withdrawal made during that will land behind
it.

## Why it is worth a guard and not a log line

Nothing visible was wrong. A withdrawn profile is excluded by `profiles_for_matching`, so
nobody was named by it, and the endpoint's own report (`samplesRemoved: 0`) was accurate
about the moment it ran.

What was left behind was a **biometric vector on a profile somebody asked to have
destroyed**, discoverable only by reading the table. That is the single thing a withdrawal
exists to accomplish, and the failure has no symptom — which is the shape this subsystem's
schema comments say to design against, because it is un-reconstructable after the fact.

The guard lives in `add_sample` for the reason the agreement guard beside it does: there are
three call sites and a rule enforced in one of them is a rule the other two quietly do
without.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    # A module from this repository: a missing one is a wrong path or a moved file, and both
    # should be red. `importorskip` here would report a passing suite for a guard that never
    # ran, which is how this repo lost a different guard entirely.
    from repositories import voiceprints as vp
except ImportError as exc:  # pragma: no cover - the message IS the point
    raise AssertionError(f"repositories.voiceprints is not importable: {exc}")

VP_ID = "11111111-1111-1111-1111-111111111111"
CO = "22222222-2222-2222-2222-222222222222"
EMB = [0.1] * 192


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if "SELECT status FROM speaker_voiceprints" in sql:
            self._rows = [] if self.conn.status is None else [{"status": self.conn.status}]
        elif "INSERT INTO speaker_voiceprint_samples" in sql:
            self.conn.inserted += 1
            self._rows = [{"id": "sample-1"}]
        else:
            self._rows = []
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records SQL rather than executing it — the house double. It cannot tell us the guard
    is CORRECT, only that it runs and in what order, which is what is at stake here."""

    def __init__(self, status):
        self.status = status
        self.sql = []
        self.inserted = 0

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def test_a_withdrawn_profile_takes_no_more_vectors():
    conn = FakeConn("withdrawn")
    with pytest.raises(vp.EnrolmentAfterWithdrawal):
        vp.add_sample(conn, CO, VP_ID, EMB, source="correction", s3_key="k", window=(0, 10))
    assert conn.inserted == 0, "a vector was written to a withdrawn profile"


def test_a_profile_that_no_longer_exists_takes_no_vectors_either():
    """Deleted rather than withdrawn — a company cascade, or a test fixture torn down mid
    flight. Writing would either fail on the foreign key or, worse, succeed against a stale
    id that a later profile reuses."""
    conn = FakeConn(None)
    with pytest.raises(vp.EnrolmentAfterWithdrawal):
        vp.add_sample(conn, CO, VP_ID, EMB, source="correction", s3_key="k", window=(0, 10))
    assert conn.inserted == 0


def test_a_live_profile_is_unaffected():
    """Guard the guard. A check that refused everything would pass the two tests above and
    silently switch enrolment off for everybody — which is the failure that emptied this
    library once already, from a different cause."""
    conn = FakeConn("tentative")
    row = vp.add_sample(conn, CO, VP_ID, EMB, source="correction", s3_key="k",
                        window=(0, 10))
    assert conn.inserted == 1
    assert row and row["id"] == "sample-1"


def test_the_status_is_checked_before_the_vector_is_compared():
    """Ordering matters for cost, not correctness: `_agreement` fetches every profile in the
    company and scores against each. Doing that first and then discarding the answer pays
    the whole price of an enrolment to decide not to do one."""
    conn = FakeConn("withdrawn")
    with pytest.raises(vp.EnrolmentAfterWithdrawal):
        vp.add_sample(conn, CO, VP_ID, EMB, source="correction", s3_key="k", window=(0, 10))
    joined = " ".join(conn.sql)
    assert "SELECT status FROM speaker_voiceprints" in joined
    assert "JOIN speaker_voiceprint_samples" not in joined, (
        "the candidate fetch ran before the status check")


def test_the_writer_handles_the_refusal_on_every_path_that_stores_one():
    """Three call sites in `lambda_voiceprint_writer`, and an unhandled raise on any of them
    takes down the item write that carries it — trading a working correction for a refused
    enrolment, which is the opposite of the trade the other refusal already makes."""
    src = open(os.path.join(ROOT, "src", "lambda_voiceprint_writer.py"),
               encoding="utf-8").read()
    assert src.count("add_sample(") >= 3, "the call sites moved; re-check this test"
    assert src.count("EnrolmentAfterWithdrawal") >= 4, (
        "a path that calls add_sample does not catch the withdrawal refusal")
