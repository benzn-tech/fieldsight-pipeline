"""Tests for src/repositories/voiceprints.py — the speaker-identity write/read paths.

A FakeConn/FakeCursor double records every execute()'s SQL and params, so behaviour is
assertable without a real Postgres — and without the pgvector extension, because embeddings
cross the boundary as the text literal `'[0.1,0.2,…]'` that the repo builds. The fake only
ever sees a string.

Design: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md §6, §8.

The load-bearing test here is `profiles_for_matching`. It is the one query whose mistakes are
invisible: a profile without consent, or a withdrawn one, would simply keep naming people and
nothing would look wrong.
"""
import pytest

from repositories import voiceprints

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": " ".join(sql.split()), "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def _emb(n=192):
    return [0.01 * i for i in range(n)]


# ---- add_sample ----

def test_the_embedding_crosses_as_a_pgvector_literal():
    conn = FakeConn([[{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, _emb(), source="enrolment",
                           s3_key="users/x.wav", window=(0.0, 30.0),
                           created_by="u1", correction_ref="corr-9")
    params = conn.calls[0]["params"]
    vec = next(p for p in params if isinstance(p, str) and p.startswith("["))
    assert vec.startswith("[0.0,") and vec.endswith("]")


def test_a_wrong_length_embedding_is_refused_before_it_reaches_the_database():
    """The column is vector(192). Postgres would reject it too, but only at insert time and
    with an error nobody reads — and by then the audio window has been chosen and the
    consent recorded."""
    conn = FakeConn([[{"id": "s1"}]])
    with pytest.raises(ValueError):
        voiceprints.add_sample(conn, CO, VP, _emb(128), source="enrolment",
                               s3_key="k", window=(0.0, 1.0), created_by="u1",
                               correction_ref="c")


def test_a_sample_keeps_the_pointer_back_to_the_correction_that_made_it():
    conn = FakeConn([[{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, _emb(), source="correction",
                           s3_key="k", window=(1.0, 4.0), created_by="u1",
                           correction_ref="corr-9")
    assert "corr-9" in conn.calls[0]["params"]
    assert "correction_ref" in conn.calls[0]["sql"]


# ---- profiles_for_matching: the query whose mistakes are invisible ----

def test_only_consented_profiles_are_offered_for_matching():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO)
    sql = conn.calls[0]["sql"]
    assert "consent_at is not null" in sql.lower()


def test_a_withdrawn_profile_is_never_offered():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO)
    assert "withdrawn" in conn.calls[0]["sql"].lower()


def test_the_query_is_scoped_to_a_company():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO)
    assert "company_id = %s" in conn.calls[0]["sql"]
    assert CO in conn.calls[0]["params"]


def test_a_missing_company_raises_rather_than_matching_against_everyone():
    """The empty-list trap, one table along: `[]` and `None` have both meant "no filter"
    somewhere in this codebase, and here that would match a voice against every company's
    profiles at once."""
    for bad in (None, ""):
        with pytest.raises(ValueError):
            voiceprints.profiles_for_matching(FakeConn([[]]), bad)


def test_the_caller_is_told_whether_a_profile_is_only_tentative():
    """A tentative profile must not produce a confirmed name — the caller can only cap that
    if the status comes back with the vector."""
    conn = FakeConn([[{"id": VP, "display_name": "Ben", "status": "tentative",
                       "embedding": "[0.1]"}]])
    rows = voiceprints.profiles_for_matching(conn, CO)
    assert rows[0]["status"] == "tentative"


# ---- withdrawal ----

def test_withdrawing_deletes_the_vectors_and_keeps_the_audit_row():
    """Biometric data goes; the record that it once existed stays, because that is what an
    audit of a withdrawal is."""
    conn = FakeConn([[{"id": "s1"}, {"id": "s2"}], [], []])
    ids = voiceprints.withdraw(conn, CO, VP)
    sqls = " | ".join(c["sql"].lower() for c in conn.calls)
    assert "delete from speaker_voiceprint_samples" in sqls
    assert "status" in sqls and "withdrawn" in sqls
    assert ids == ["s1", "s2"], "the caller needs these to un-name the turns they justified"


def test_withdrawing_is_also_company_scoped():
    conn = FakeConn([[], [], []])
    voiceprints.withdraw(conn, CO, VP)
    assert all("company_id = %s" in c["sql"] for c in conn.calls)


# ---- §6's "N independent confirmations" ----

def test_confirmations_are_counted_over_distinct_sessions():
    """Three corrections inside one meeting are one person clicking three times, not three
    independent confirmations."""
    conn = FakeConn([[{"n": 2}]])
    voiceprints.confirmations_count(conn, CO, VP)
    assert "distinct" in conn.calls[0]["sql"].lower()
    assert "session" in conn.calls[0]["sql"].lower()


# ----------------------------------------------------------
# Pool scope. Every turn is scored against every row this returns, and `decide_name` takes
# the runner-up as the maximum over the rest — so the POOL, not the number of people in the
# room, is what the margin has to survive. Company-wide is the widest possible pool.
#
# The scope argument lands now because `profiles_for_matching` has no callers yet. Once
# Phase 4 and Phase 5 both call it, changing the signature costs both.
# ----------------------------------------------------------


def test_without_a_site_the_pool_is_the_whole_company_as_before():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, "c1")
    sql, params = conn.calls[-1]["sql"], conn.calls[-1]["params"]
    assert "memberships" not in sql, "company-wide must not pay for a join it does not need"
    assert params == ("c1",)


def test_a_site_scoped_pool_reaches_profiles_through_membership():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, "c1", site_id="s1")
    sql, params = conn.calls[-1]["sql"], conn.calls[-1]["params"]
    assert "memberships" in sql
    assert "s1" in params


def test_a_site_scoped_pool_still_offers_the_unnamed_voices():
    """`user_id` is nullable BY DESIGN — 0038's comment: a recurring unnamed voice may hold
    a profile before anyone names it, which is what makes "the same person again" visible.

    Those profiles reach no `memberships` row, so a naive site join drops every one of them
    and silently kills the feature the nullable column exists for. They are kept in scope
    instead, and the reason is that they are safe to keep: a profile with no user and no
    display name cannot produce a NAME. The worst it can do is become the runner-up and push
    a real match down to `tentative` — a refusal, not a wrong answer.
    """
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, "c1", site_id="s1")
    sql = conn.calls[-1]["sql"]
    assert "p.user_id IS NULL" in sql, (
        "unnamed profiles must survive site scoping; dropping them is the "
        "empty-filter-means-no-filter shape this repo has been bitten by")


def test_site_scoping_does_not_weaken_consent_or_withdrawal():
    """The two filters that fail silently must hold under every scope."""
    for kwargs in ({}, {"site_id": "s1"}):
        conn = FakeConn([[]])
        voiceprints.profiles_for_matching(conn, "c1", **kwargs)
        sql = conn.calls[-1]["sql"]
        assert "consent_at IS NOT NULL" in sql
        assert "status <> 'withdrawn'" in sql


# ---- reading a vector without pgvector ----------------------------------
#
# The function that matches voiceprints cannot carry pgvector. It runs on python3.12
# (fieldsight-vad-layer is cp312-only, and that layer is where onnxruntime comes from),
# while the psycopg layer that ships pgvector is cp311-only — the two cannot sit on one
# function. Writes were never affected: `_vector_literal` sends text. Reads were: without
# `register_vector`, psycopg hands a `vector` column back as the STRING '[0.1,0.2,…]',
# and `cosine` on a string is not an error that says "no pgvector" — it is a shape error
# thirty lines away, or worse, a number.


def test_a_vector_column_read_without_pgvector_comes_back_as_numbers():
    import repositories.voiceprints as vr
    got = vr._parse_vector("[0.5,-0.25,0.125]")
    assert [float(x) for x in got] == [0.5, -0.25, 0.125]


def test_a_vector_that_pgvector_already_parsed_is_left_alone():
    """When the layer DOES have pgvector the value arrives as a sequence, and parsing must
    be a no-op rather than a second interpretation of it."""
    import repositories.voiceprints as vr
    assert list(vr._parse_vector([0.5, -0.25])) == [0.5, -0.25]
    assert vr._parse_vector(None) is None


def test_profiles_for_matching_returns_numbers_not_text():
    """The end the caller actually touches. `profiles_for_matching` is the only read of an
    embedding in the codebase, so this is where the conversion has to be — not in each
    caller, which is how one of them ends up without it."""
    import repositories.voiceprints as vr
    rows = [{"id": "p1", "display_name": "Ben", "status": "confirmed", "user_id": None,
             "sample_id": "s1", "embedding": "[1,2,3]"}]
    conn = FakeConn([rows])
    out = vr.profiles_for_matching(conn, CO)
    assert [float(x) for x in out[0]["embedding"]] == [1.0, 2.0, 3.0], (
        "the embedding reached the caller as text; cosine would not have said so")
