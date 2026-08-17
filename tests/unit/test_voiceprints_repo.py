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

def _insert(conn):
    """add_sample reads the company's profiles before it writes, so the INSERT is no
    longer call 0. Find it by what it is rather than by where it sits."""
    return next(c for c in conn.calls if c["sql"].startswith("INSERT"))


def test_the_embedding_crosses_as_a_pgvector_literal():
    conn = FakeConn([[], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, _emb(), source="enrolment",
                           s3_key="users/x.wav", window=(0.0, 30.0),
                           created_by="u1", correction_ref="corr-9")
    params = _insert(conn)["params"]
    vec = next(p for p in params if isinstance(p, str) and p.startswith("["))
    assert vec.startswith("[0.0,") and vec.endswith("]")


def test_a_wrong_length_embedding_is_refused_before_it_reaches_the_database():
    """The column is vector(192). Postgres would reject it too, but only at insert time and
    with an error nobody reads — and by then the audio window has been chosen and the
    consent recorded."""
    conn = FakeConn([[], [{"id": "s1"}]])
    with pytest.raises(ValueError):
        voiceprints.add_sample(conn, CO, VP, _emb(128), source="enrolment",
                               s3_key="k", window=(0.0, 1.0), created_by="u1",
                               correction_ref="c")


def test_a_sample_keeps_the_pointer_back_to_the_correction_that_made_it():
    conn = FakeConn([[], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, _emb(), source="correction",
                           s3_key="k", window=(1.0, 4.0), created_by="u1",
                           correction_ref="corr-9")
    assert "corr-9" in _insert(conn)["params"]
    assert "correction_ref" in _insert(conn)["sql"]


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
    conn = FakeConn([[], [], [{"id": "t2"}]])
    voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                 state="confirmed", source="correction",
                                 correction_ref="corr-2", voiceprint_id=VP)
    # The precedence check reads the live row first; what this test is about is that the
    # supersede still happens BEFORE the insert.
    order = [c["sql"].split()[0] for c in conn.calls]
    assert order.index("UPDATE") < order.index("INSERT")
    assert "superseded_at" in next(c for c in conn.calls
                                   if c["sql"].startswith("UPDATE"))["sql"]


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


def test_withdrawal_reaches_names_from_corrections_that_never_enrolled():
    """The gap a tester hits on their first short window.

    org-api creates the profile BEFORE the embedder runs. If the embedder then refuses the
    enrolment — window under ten seconds, not homogeneous, or between voices — the turn
    names are still written, and no sample row exists. Keying the un-naming on the samples'
    `correction_ref` then finds nothing: 200, `samplesRemoved: 0`, and every name still on
    the transcript.

    The names carry `voiceprint_id` for exactly this reason now, so both routes are covered:
    a correction that enrolled is reached through its sample, one that did not through the
    profile itself.
    """
    conn = FakeConn([[], [], [], []])
    voiceprints.withdraw(conn, CO, VP)
    upd = next(c for c in conn.calls if "UPDATE speaker_turn_names" in c["sql"])
    sql = " ".join(upd["sql"].split())
    assert "correction_ref IN" in sql
    assert "voiceprint_id = %s" in sql, (
        "a correction whose enrolment was refused leaves names nothing can reach")


# ---- removing a name without touching a voiceprint ---------------------
#
# Verified live on TEST: a session held seven named turns and withdrawal could reach exactly
# one. The other six came from corrections made WITHOUT consent — no profile was created, so
# there is nothing to withdraw and nothing to point a withdrawal at.
#
# "Delete my voiceprint" and "take my name off this meeting" are different requests, and the
# second is the ordinary one. It had no API at all.


def test_a_name_can_be_removed_from_one_session():
    conn = FakeConn([[{"id": "t1"}, {"id": "t2"}]])
    n = voiceprints.unname(conn, CO, session_base="sid" + "a" * 32, display_name="Ben L")
    assert n == 2
    sql = " ".join(conn.calls[0]["sql"].split())
    assert "UPDATE speaker_turn_names SET superseded_at" in sql
    assert "display_name = %s" in sql
    assert "session_base = %s" in sql


def test_removing_a_name_is_company_scoped():
    conn = FakeConn([[]])
    voiceprints.unname(conn, CO, session_base="s", display_name="Ben L")
    assert "company_id = %s" in conn.calls[0]["sql"]
    assert CO in conn.calls[0]["params"]


def test_removing_a_name_leaves_other_sessions_alone():
    """A person may be named correctly in twenty meetings and wrongly in one. Removing the
    name everywhere would be a different request nobody made."""
    conn = FakeConn([[]])
    voiceprints.unname(conn, CO, session_base="only-this-one", display_name="Ben L")
    assert "only-this-one" in conn.calls[0]["params"]


def test_names_are_superseded_not_deleted():
    """Same reason as everywhere else here: the audit of a removal is partly the record that
    something was shown, and a deleted row cannot say so."""
    conn = FakeConn([[]])
    voiceprints.unname(conn, CO, session_base="s", display_name="Ben L")
    assert "DELETE" not in conn.calls[0]["sql"].upper()


def test_removing_a_name_does_not_touch_the_voiceprint():
    """Deliberately separate. Somebody who wants their name off one transcript has not asked
    for their profile to be destroyed, and doing both would answer a question they did not
    ask."""
    conn = FakeConn([[], []])
    voiceprints.unname(conn, CO, session_base="s", display_name="Ben L")
    assert conn.calls, "nothing ran"
    assert not any("speaker_voiceprint" in c["sql"] for c in conn.calls)


# ---- the same name is not the same person ---------------------------------
#
# `upsert_profile` finds an existing profile by NAME, so two people called "Leo"
# confirmed by the same person land on one profile — and a profile cannot be
# un-poisoned, only the contributing sample deleted.
#
# The guard compares an ORDER, never a threshold. A cross-session absolute cosine
# is not a stable quantity here (Phase 0: the same person varying by >0.2 across
# sessions), so a floor drawn today would reject legitimate second samples and
# would have been drawn from nothing. Both numbers below are measured on the same
# audio, so whatever lowers one lowers the other.

OTHER = "33333333-3333-3333-3333-333333333333"


def _profile_row(pid, vec):
    return {"id": pid, "display_name": "x", "status": "active", "user_id": None,
            "sample_id": "s", "embedding": vec}


def _near(v, k):
    """A vector like v but nudged, so cosine(v, out) falls as k grows."""
    out = list(v)
    for i in range(0, k):
        out[i] = -out[i] - 1.0
    return out


def test_a_sample_closer_to_another_profile_is_refused():
    me = _emb()
    conn = FakeConn([[_profile_row(VP, _near(me, 40)),      # my profile: further
                      _profile_row(OTHER, _near(me, 2))]])  # somebody else: nearer
    with pytest.raises(voiceprints.EnrolmentBelongsToSomebodyElse) as exc:
        voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                               window=(0.0, 4.0))
    assert exc.value.nearest_other_id == OTHER
    assert exc.value.best_other > exc.value.own
    assert not any(c["sql"].startswith("INSERT") for c in conn.calls), \
        "it was refused and stored anyway"


def test_a_sample_nearest_its_own_profile_is_stored():
    me = _emb()
    conn = FakeConn([[_profile_row(VP, _near(me, 2)),
                      _profile_row(OTHER, _near(me, 40))], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                           window=(0.0, 4.0))
    assert _insert(conn)["sql"].startswith("INSERT")


def test_the_first_sample_of_a_profile_is_never_refused():
    """There is nothing to be closer to. Refusing here would make a profile
    impossible to start."""
    me = _emb()
    conn = FakeConn([[_profile_row(OTHER, _near(me, 1))], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                           window=(0.0, 4.0))
    assert _insert(conn)["sql"].startswith("INSERT")


def test_both_agreements_are_recorded_on_the_row():
    """The numbers a threshold would eventually be drawn FROM. Recording them is the
    whole reason a threshold does not have to be invented today."""
    me = _emb()
    conn = FakeConn([[_profile_row(VP, _near(me, 2)),
                      _profile_row(OTHER, _near(me, 40))], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                           window=(0.0, 4.0))
    call = _insert(conn)
    assert "agreement_own" in call["sql"] and "agreement_best_other" in call["sql"]
    nums = [p for p in call["params"] if isinstance(p, float) and -1.0 <= p <= 1.0]
    assert len(nums) >= 2, "the two agreements did not reach the row"


def test_a_lone_profile_records_no_runner_up_rather_than_a_zero():
    """NULL is the honest answer. A fabricated 0.0 would poison the very dataset the
    threshold is meant to come from."""
    me = _emb()
    conn = FakeConn([[_profile_row(VP, _near(me, 2))], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                           window=(0.0, 4.0))
    params = _insert(conn)["params"]
    assert params[-1] is None and params[-2] is None, "a zero was invented"


def test_the_comparison_is_company_scoped():
    me = _emb()
    conn = FakeConn([[], [{"id": "s1"}]])
    voiceprints.add_sample(conn, CO, VP, me, source="correction", s3_key="k",
                           window=(0.0, 4.0))
    assert CO in conn.calls[0]["params"], "profiles were read without a company scope"


# ---- write-time precedence -----------------------------------------------
#
# The supersede has no source predicate and never had one, so a weaker source
# could bury a human correction simply by arriving later. `_SOURCE_RANK` cannot
# help: it ranks at READ time, among rows that both survived.


def _live(source, state="confirmed"):
    return [{"id": "t1", "source": source, "state": state}]


def test_a_matched_name_does_not_bury_a_human_correction():
    conn = FakeConn([_live("correction")])
    out = voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                       state="confirmed", source="voiceprint_match",
                                       display_name="Ben L")
    assert out is None, "the weaker write was accepted"
    assert not any(c["sql"].startswith("UPDATE") for c in conn.calls), \
        "the human row was superseded anyway"
    assert not any(c["sql"].startswith("INSERT") for c in conn.calls)


def test_a_correction_replaces_a_matched_name():
    conn = FakeConn([_live("voiceprint_match"), [], [{"id": "t2"}]])
    out = voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                       state="confirmed", source="correction",
                                       display_name="Ben L")
    assert out == {"id": "t2"}


def test_an_equal_source_still_replaces():
    """A newer match superseding an older match is wanted. Only a write that would move a
    turn DOWN the scale is declined — otherwise re-running a match could never correct
    itself."""
    conn = FakeConn([_live("voiceprint_match"), [], [{"id": "t2"}]])
    out = voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                       state="tentative", source="voiceprint_match",
                                       display_name="Zoe")
    assert out == {"id": "t2"}


def test_an_unknown_live_source_never_blocks_a_known_one():
    """An unrecognised source ranks -1. It must not outrank anything, or a stray value in
    the column would freeze a turn permanently."""
    conn = FakeConn([_live("something_nobody_wrote"), [], [{"id": "t2"}]])
    assert voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                        state="tentative", source="voiceprint_match",
                                        display_name="Zoe") == {"id": "t2"}


def test_the_live_row_is_locked_before_the_decision():
    """Two writers racing on one turn must serialise here rather than at the partial unique
    index, where the loser gets a write failure instead of a decision."""
    conn = FakeConn([[], [], [{"id": "t2"}]])
    voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                 state="confirmed", source="correction")
    assert "FOR UPDATE" in conn.calls[0]["sql"]


def test_the_precedence_read_is_company_scoped():
    conn = FakeConn([[], [], [{"id": "t2"}]])
    voiceprints.record_turn_name(conn, CO, session_base="s1", turn_ref="f.wav@1.0",
                                 state="confirmed", source="correction")
    assert CO in conn.calls[0]["params"]


# ---- a rejection is remembered -------------------------------------------


def test_removing_a_name_records_that_it_was_rejected():
    """Superseding alone records that a name WAS shown, which is also true of one merely
    replaced by a better answer. Only "rejected" should stop a later inference — without
    this, label inheritance re-derives the same name from the same transcriber label on the
    next run and the user deletes it again after every run."""
    conn = FakeConn([[{"id": "t1"}], []])
    voiceprints.unname(conn, CO, session_base="s", display_name="Ben L",
                       rejected_by="u-1")
    sql = " ".join(c["sql"] for c in conn.calls)
    assert "INSERT INTO speaker_name_rejections" in sql
    assert "ON CONFLICT" in sql, "a second rejection of the same name would raise"


def test_a_rejection_is_scoped_to_one_session():
    conn = FakeConn([[], []])
    voiceprints.unname(conn, CO, session_base="s9", display_name="Ben L")
    ins = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    assert "s9" in ins["params"] and CO in ins["params"]


def test_rejected_names_are_readable_and_company_scoped():
    conn = FakeConn([[{"display_name": "Ben L"}, {"display_name": "Zoe"}]])
    out = voiceprints.rejected_names(conn, CO, "s1")
    assert out == {"Ben L", "Zoe"}
    assert CO in conn.calls[0]["params"]


def test_reading_rejections_without_a_company_raises():
    with pytest.raises(ValueError):
        voiceprints.rejected_names(FakeConn(), "", "s1")


# ---- linking an EXISTING profile -----------------------------------------


def test_an_existing_profile_gains_the_identity_too():
    """`upsert_profile` returns early when it finds a profile by name, so passing `user_id`
    links only at CREATION. Every profile in the database predates this, so without the
    update the site-scoped branch stays a no-op for the entire existing population — the
    identical silent-inert failure this work exists to remove."""
    conn = FakeConn([[{"id": "vp-1"}], []])
    out = voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                                     consented_by="u-9", user_id="u-42",
                                     linked_by="u-1", linked_on="folder_name")
    assert out == {"id": "vp-1"}
    upd = next((c for c in conn.calls if c["sql"].startswith("UPDATE")), None)
    assert upd is not None, "the existing profile was left unlinked"
    assert "u-42" in upd["params"] and "vp-1" in upd["params"]
    assert "linked_on" in upd["sql"] and "linked_by" in upd["sql"]


def test_an_existing_link_is_never_overwritten():
    """A person is not re-identified by somebody typing the same name again."""
    conn = FakeConn([[{"id": "vp-1"}], []])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9", user_id="u-42")
    upd = next(c for c in conn.calls if c["sql"].startswith("UPDATE"))
    assert "user_id IS NULL" in upd["sql"]


def test_an_unresolved_name_runs_no_update_at_all():
    conn = FakeConn([[{"id": "vp-1"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9", user_id=None)
    assert not any(c["sql"].startswith("UPDATE") for c in conn.calls)


def test_a_new_profile_records_when_it_was_linked():
    conn = FakeConn([[], [{"id": "vp-2"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Zoe", consent_given=True,
                               consented_by="u-9", user_id="u-7", linked_by="u-1",
                               linked_on="full_name")
    ins = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    assert "linked_at" in ins["sql"] and "u-7" in ins["params"]


# ---- narrowing the pool by site ------------------------------------------
#
# `decide_name` takes the runner-up as the maximum over EVERY other candidate, so
# the size of this result — not the number of people in the room — is what the
# margin has to survive. A company accumulating profiles across many sites makes
# every turn harder to confirm, and the failure is refusal, not misidentification:
# safe, and indistinguishable from the feature being broken.


def _sql_of(conn):
    return " ".join(conn.calls[0]["sql"].split())


def test_a_person_who_belongs_to_no_site_stays_in_the_pool():
    """`upsert_field_only_user` writes only `users` — a directory entry for somebody with no
    login has no membership row. Without this arm, attaching an identity makes that person
    LESS matchable than before: matchable while unlinked, invisible once linked. The fix
    would have introduced the regression."""
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO, site_id="st-1")
    sql = _sql_of(conn)
    assert "NOT EXISTS" in sql, "a field_only person disappears the moment they are linked"


def test_the_membership_check_ignores_archived_memberships():
    """Every other membership query in this repository filters it
    (repositories/memberships.py:29, 52, 71, ...). Without it somebody removed from a site
    keeps being matched there — a guard satisfied and ineffective."""
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO, site_id="st-1")
    sql = _sql_of(conn)
    assert sql.count("m2.archived_at IS NULL") == 1
    assert sql.count("m.archived_at IS NULL") == 1


def test_an_unnamed_profile_is_never_narrowed_away():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO, site_id="st-1")
    assert "p.user_id IS NULL" in _sql_of(conn)


def test_narrowing_still_asks_for_the_site():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO, site_id="st-1")
    assert "st-1" in conn.calls[0]["params"]


def test_no_site_asks_for_no_membership_join_at_all():
    conn = FakeConn([[]])
    voiceprints.profiles_for_matching(conn, CO)
    assert "memberships" not in _sql_of(conn)


def test_a_profile_built_only_from_harvest_has_no_human_sample():
    """Coverage may come from the machine; confidence only from people. A profile can now be
    BUILT entirely from cluster members the machine selected after one human named one turn
    — worth having, since a single corrected window is often under 10 s — but it must not be
    able to earn confidence from inference, or the loop agrees with itself and no later
    evidence can say where it started going wrong."""
    conn = FakeConn([[]])
    assert voiceprints.has_human_sample(conn, CO, "vp-1") is False
    assert "source = 'correction'" in conn.calls[0]["sql"]


def test_a_profile_with_one_vouched_window_has_a_human_sample():
    conn = FakeConn([[{"?column?": 1}]])
    assert voiceprints.has_human_sample(conn, CO, "vp-1") is True


def test_the_human_sample_check_is_company_scoped():
    conn = FakeConn([[]])
    voiceprints.has_human_sample(conn, CO, "vp-1")
    assert CO in conn.calls[0]["params"]


def test_no_parameter_is_used_only_in_an_untyped_null_test():
    """`CASE WHEN %s IS NULL` gives Postgres nothing to infer a type from, and it refuses the
    whole statement: `IndeterminateDatatype: could not determine data type of parameter $7`.

    A real correction on TEST returned 500 for exactly this while 3082 unit tests stayed
    green — the connection double never type-checks, which is what the warning at the top of
    CLAUDE.md's testing section is about. TWO sessions hit it independently within the hour,
    which is the argument for a guard rather than a memory.

    So this asserts on the SQL TEXT, the one thing a double can see. A cast (`%s::uuid IS
    NULL`) is fine — it is the bare form that has no type to infer.
    """
    conn = FakeConn([[], [{"id": "vp-1"}]])
    voiceprints.upsert_profile(conn, CO, display_name="Ben L", consent_given=True,
                               consented_by="u-9", user_id="u-42")
    ins = next(c for c in conn.calls if c["sql"].startswith("INSERT"))
    import re as _re
    bare = _re.search(r"%s\s+IS\s+NULL", ins["sql"])
    assert not bare, f"untyped parameter in a NULL test: {bare.group(0) if bare else ''}"


# ---- the two functions added tonight that only ever ran as stubs ----------
#
# `record_attempt` and `list_profiles` are monkeypatched in the org-api and
# writer suites and were called for real nowhere. That is how a SQL defect
# reached production tonight while 3082 tests passed: the connection double does
# not type-check, so a query is only checked by asserting on its text.


def test_an_attempt_is_recorded_against_one_profile_in_one_company():
    conn = FakeConn([[]])
    voiceprints.record_attempt(conn, CO, "vp-1", "refused", "two voices")
    sql = " ".join(conn.calls[0]["sql"].split())
    assert sql.startswith("UPDATE speaker_voiceprints")
    assert "company_id = %s" in sql and "id = %s" in sql
    assert conn.calls[0]["params"] == ("refused", "two voices", CO, "vp-1")


def test_an_attempt_stamps_its_own_time_rather_than_taking_one():
    """A caller-supplied timestamp is a claim about when something happened; `now()` is a
    record of when the system was told, which is the only one this code can make."""
    conn = FakeConn([[]])
    voiceprints.record_attempt(conn, CO, "vp-1", "stored")
    assert "now()" in conn.calls[0]["sql"]


def test_recording_an_attempt_without_a_company_raises():
    with pytest.raises(ValueError):
        voiceprints.record_attempt(FakeConn(), "", "vp-1", "stored")


def test_no_parameter_in_the_attempt_update_is_untyped_in_a_null_test():
    """Same shape as the defect that returned 500 tonight."""
    conn = FakeConn([[]])
    voiceprints.record_attempt(conn, CO, "vp-1", "refused", None)
    import re as _re
    assert not _re.search(r"%s\s+IS\s+NULL", conn.calls[0]["sql"])


def test_the_listing_counts_human_samples_separately_from_harvested_ones():
    """The distinction the whole harvest design rests on: a profile built only from
    inference must not read as one somebody vouched for."""
    conn = FakeConn([[{"id": "vp-1"}]])
    voiceprints.list_profiles(conn, CO)
    sql = " ".join(conn.calls[0]["sql"].split())
    assert "count(s.id) AS samples" in sql
    assert "FILTER (WHERE s.source = 'correction')" in sql


def test_the_listing_is_a_left_join_so_an_empty_profile_still_appears():
    """A profile with zero samples is exactly the case somebody is looking at when they ask
    why nothing works. An inner join would hide it."""
    conn = FakeConn([[]])
    voiceprints.list_profiles(conn, CO)
    assert "LEFT JOIN" in conn.calls[0]["sql"]


def test_the_listing_never_selects_an_embedding():
    """Biometric data. A listing does not need it, and the one path it may travel is the
    synchronous fetch the matcher makes."""
    conn = FakeConn([[]])
    voiceprints.list_profiles(conn, CO)
    sql = conn.calls[0]["sql"]
    assert "embedding" not in sql.split("FROM")[0]


def test_the_listing_is_company_scoped():
    conn = FakeConn([[]])
    voiceprints.list_profiles(conn, CO)
    assert CO in conn.calls[0]["params"]
    assert "p.company_id = %s" in " ".join(conn.calls[0]["sql"].split())


def test_listing_without_a_company_raises():
    with pytest.raises(ValueError):
        voiceprints.list_profiles(FakeConn(), "")
