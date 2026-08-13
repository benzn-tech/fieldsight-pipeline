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


# ---- the loop that would have confirmed profiles from machine output ----
#
# `confirmations_count` counts DISTINCT sessions with a `confirmed` turn name as the
# "N independent confirmations" that promote a profile from tentative (§6). Propagation
# writes confirmed rows too. Without a source filter the machine satisfies its own promotion
# criterion with its own output, across sessions, and the profile that then names people was
# confirmed by nothing.
#
# Nothing in the schema shows this: the loop exists only because two features share one
# table. Two independent cuts, because either alone is one edit from being undone.


def test_only_human_corrections_count_towards_promotion():
    conn = FakeConn([[{"n": 2}]])
    voiceprints.confirmations_count(conn, CO, VP)
    sql = conn.calls[0]["sql"]
    assert "source = 'correction'" in sql, (
        "propagated rows count as human confirmations — the system promotes profiles with "
        "its own output")


def test_a_propagated_row_carries_no_profile_id():
    """The second cut. `n.voiceprint_id = %s` can never match NULL, so propagation rows are
    outside the count even if the source filter is ever removed."""
    conn = FakeConn([[{"id": "t1"}]])
    voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                 state="confirmed", source="correction_propagation",
                                 correction_ref="corr-1", cluster_ref="C1",
                                 cluster_threshold=0.85)
    params = conn.calls[-1]["params"]
    assert None in params, "a propagated row was given a voiceprint_id"


def test_a_correction_row_supersedes_the_live_row_for_that_turn_first():
    """Supersede-then-insert, in the caller's transaction. S3 events are unordered, so two
    runs can overlap; without the supersede first the partial unique index turns a race into
    a write failure instead of a replacement."""
    conn = FakeConn([[], [{"id": "t2"}]])
    voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                 state="confirmed", source="correction",
                                 correction_ref="corr-2", voiceprint_id=VP)
    assert "UPDATE speaker_turn_names" in conn.calls[0]["sql"]
    assert "superseded_at" in conn.calls[0]["sql"]
    assert "INSERT INTO speaker_turn_names" in conn.calls[1]["sql"]


def test_the_live_overlay_excludes_superseded_rows():
    conn = FakeConn([[]])
    voiceprints.live_turn_names(conn, CO, "s1")
    sql = conn.calls[0]["sql"]
    assert "superseded_at IS NULL" in sql
    assert "company_id = %s" in sql


# ---- creating the profile a name attaches to ----------------------------
#
# §6: a voiceprint is biometric information, and consent must come from THE PERSON WHOSE
# VOICE IT IS — not the wearer, not the employer, not the person doing the labelling. So a
# profile that carries a NAME cannot be created without it; a profile with no name can,
# because an unnamed recurring voice identifies nobody.


def test_a_named_profile_cannot_be_created_without_consent():
    with pytest.raises(ValueError, match="consent"):
        voiceprints.upsert_profile(FakeConn([[]]), CO, display_name="Ben L",
                                   consented_by="u-9")


def test_an_unnamed_profile_needs_no_consent():
    """A recurring voice may hold a profile before anyone names it — that is what makes
    'the same person again' visible without knowing who they are, and it links no biometric
    to an identity."""
    # One query, not two: with no name there is nothing to look up first.
    conn = FakeConn([[{"id": "vp1"}]])
    got = voiceprints.upsert_profile(conn, CO, display_name=None)
    assert got["id"] == "vp1"
    assert len(conn.calls) == 1


def test_consent_is_stamped_by_the_server_not_taken_from_the_caller():
    """A client-supplied timestamp is a claim about when somebody agreed. This one is a
    record of when the system was told.

    Asserting that the SQL merely CONTAINS `now()` was vacuous — mutation showed an
    unconditional `now()` (stamping consent on profiles nobody consented to) kept it green.
    The stamp has to be conditional on the flag, so both branches are exercised.
    """
    conn = FakeConn([[], [{"id": "vp1"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9")
    consented = conn.calls[-1]
    assert True in consented["params"], "the consent flag never reached the statement"

    conn2 = FakeConn([[{"id": "vp2"}]])
    voiceprints.upsert_profile(conn2, CO, display_name=None)
    unconsented = conn2.calls[-1]
    assert False in unconsented["params"], (
        "an unnamed profile was stamped as consented; a consent_at it never earned makes it "
        "matchable, and matchable is the whole point of the filter")
    assert "CASE WHEN" in unconsented["sql"], (
        "the stamp is unconditional, so every profile carries consent whether or not anyone "
        "gave it")


def test_an_existing_profile_for_the_same_name_is_reused_not_duplicated():
    """Two corrections naming the same person must feed ONE profile. Two profiles for one
    voice make that person his own runner-up — the failure `aggregate_scores` exists for."""
    conn = FakeConn([[{"id": "vp-existing"}]])
    got = voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                                     consented_by="u-9")
    assert got["id"] == "vp-existing"
    assert len(conn.calls) == 1, "an existing profile was looked up and then inserted anyway"


def test_the_lookup_is_company_scoped():
    conn = FakeConn([[{"id": "vp1"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9")
    assert "company_id = %s" in conn.calls[0]["sql"]
    assert CO in conn.calls[0]["params"]


def test_a_withdrawn_profile_is_not_reused():
    """Re-using it would resurrect a withdrawal by the back door.

    Asserting the SQL contains the word "withdrawn" was vacuous: mutation flipped the
    comparison to `=` — reusing ONLY withdrawn profiles — and the test stayed green. The
    operator is the behaviour, so the operator is what is asserted.
    """
    conn = FakeConn([[], [{"id": "vp-new"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9")
    sql = " ".join(conn.calls[0]["sql"].split())
    assert "status <> 'withdrawn'" in sql, (
        f"the lookup does not exclude withdrawn profiles: {sql}")


def test_two_people_with_the_same_name_get_two_profiles():
    """Matching on `display_name` alone merges them — one person's voice stored under the
    other's consent, and no way to tell afterwards which samples belong to whom. That is the
    UNSAFE direction: duplicate profiles merely degrade into refusals (a person becomes his
    own runner-up and the margin refuses), while a merge is a wrong confident answer about
    somebody's biometric data.

    `consented_by` is the anchor, and it is required whenever a name is given, so it exists
    exactly when it is needed.
    """
    conn = FakeConn([[], [{"id": "vp-b"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="person-B")
    lookup = conn.calls[0]
    assert "person-B" in lookup["params"], (
        "the lookup matched on the name alone, so two different people sharing one merge")


def test_the_same_person_correcting_twice_reuses_one_profile():
    """The other side of it: two profiles for one voice make that person his own runner-up,
    which is the failure `aggregate_scores` was written for."""
    conn = FakeConn([[{"id": "vp-existing"}]])
    got = voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                                     consented_by="person-A")
    assert got["id"] == "vp-existing"
    assert len(conn.calls) == 1


def test_withdrawing_also_un_names_the_turns_the_profile_justified():
    """§6: a withdrawal must reach "everything it justified". Removing the vectors while the
    names stay on the transcript is a withdrawal in the database and not in the product —
    and on TEST the endpoint returned 200 with seven rows still reading "Ben L".

    The rows are superseded rather than deleted: the audit of a withdrawal is partly the
    record of what it removed, and a deleted row cannot say a name was ever shown.
    """
    conn = FakeConn([[{"id": "s1"}], [], [], []])
    voiceprints.withdraw(conn, CO, VP)
    sqls = " | ".join(" ".join(c["sql"].split()) for c in conn.calls)
    assert "UPDATE speaker_turn_names SET superseded_at" in sqls, (
        "the names this profile justified were left standing")
    assert "correction_ref IN" in sqls, (
        "the un-naming is keyed on voiceprint_id, and every propagated row carries NULL "
        "there by design — it would match nothing and look like a fix")


def test_un_naming_is_company_scoped_like_everything_else():
    conn = FakeConn([[], [], [], []])
    voiceprints.withdraw(conn, CO, VP)
    for c in conn.calls:
        assert "company_id = %s" in c["sql"], c["sql"]


def test_a_named_profile_needs_the_consenter_named_too():
    """The docstring said this was required and the code did not check — so the layer that
    actually stores the row would have accepted what the endpoint refuses. Three fixtures in
    this very file created named, consented profiles with no consenter, which is how the
    review demonstrated the claim was false."""
    with pytest.raises(ValueError, match="consented_by"):
        voiceprints.upsert_profile(FakeConn([[]]), CO, display_name="Ben L",
                                   consent_given=True)
