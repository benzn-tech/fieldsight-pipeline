"""Unit: a voiceprint may now be created on an attested basis, and the two are never confused.

The library was empty, and the reason was not the guard everybody suspected. A named profile
required `consented_by` — the SUBJECT's uuid — so it could only exist for someone with an
account, and on a construction site the people most often named are subcontractors who have
none. The strongest basis excluded exactly the population the feature is for.

So `attestation` is added: naming a speaker is taken as the claim that they agreed, recorded
with `asserted_by` naming who made the claim. It records a claim. It does not verify one, and
nothing in this code can.

What these tests protect is the **distinction**, because that is the part that cannot be
repaired later. One column says "this person agreed"; the other says "this person says they
agreed". Collapsing them would make every existing row ambiguous and every audit unanswerable
— and it is the cheap shortcut, since `consented_by` was already there and already a uuid.
"""
import pytest

vp = pytest.importorskip("repositories.voiceprints")


class _Cur:
    def __init__(self, found=None):
        self.found, self.sql, self.params = found, [], []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchone(self):
        if self.sql and self.sql[-1].startswith("SELECT"):
            return self.found
        return {"id": "vp-new"}


class _Conn:
    def __init__(self, found=None):
        self.cur = _Cur(found)

    def cursor(self, row_factory=None):
        return self.cur


CO = "11111111-1111-1111-1111-111111111111"
ASSERTER = "22222222-2222-2222-2222-222222222222"
SUBJECT = "33333333-3333-3333-3333-333333333333"


def test_a_named_profile_still_cannot_be_created_out_of_nothing():
    """The old rule survives untouched for callers that do not opt in. Attestation is a
    basis somebody chooses, not the absence of one."""
    with pytest.raises(ValueError):
        vp.upsert_profile(_Conn(), CO, display_name="Clement")


def test_an_attested_profile_needs_somebody_to_have_made_the_claim():
    """A claim with nobody attached to it is not a record of anything — it is the same
    silence the `consented_by` column was added to end, wearing a new column's name."""
    with pytest.raises(ValueError, match="asserted_by"):
        vp.upsert_profile(_Conn(), CO, display_name="Clement",
                          consent_basis="attestation")


def test_the_asserter_never_lands_in_the_subject_column():
    """The whole design in one assertion.

    `consented_by` means "the person whose voice this is, and they agreed". Borrowing it for
    the labeller is the obvious shortcut — it is already there, already a uuid, already
    required — and it would make every row in the table ambiguous about which of the two it
    records, retrospectively, with no way to separate them again.
    """
    conn = _Conn()
    vp.upsert_profile(conn, CO, display_name="Clement",
                      consent_basis="attestation", asserted_by=ASSERTER)
    insert = next(i for i, s in enumerate(conn.cur.sql) if s.startswith("INSERT"))
    params = conn.cur.params[insert]
    assert ASSERTER in params, "the asserter was not recorded at all"
    consented_by_idx = 4          # (company, user_id, display_name, consent_given, consented_by, ...)
    assert params[consented_by_idx] is None, (
        "the asserter was written into consented_by, which says the SUBJECT agreed")
    assert "attestation" in params, "the basis was not recorded, so nothing marks these rows"


def test_the_basis_is_stored_so_this_population_can_be_found_again():
    """A later decision — a stricter standard, a purge, an opt-out register — has to be able
    to select exactly the profiles created this way. That is one query only if the basis is
    on the row."""
    conn = _Conn()
    vp.upsert_profile(conn, CO, display_name="Clement",
                      consent_basis="attestation", asserted_by=ASSERTER)
    insert = next(s for s in conn.cur.sql if s.startswith("INSERT"))
    assert "consent_basis" in insert and "asserted_by" in insert


def test_a_confirmed_profile_is_unchanged_by_any_of_this():
    """The strong path keeps its meaning and its column."""
    conn = _Conn()
    vp.upsert_profile(conn, CO, display_name="Clement",
                      consent_given=True, consented_by=SUBJECT)
    insert = next(i for i, s in enumerate(conn.cur.sql) if s.startswith("INSERT"))
    assert conn.cur.params[insert][4] == SUBJECT


# ---- the deduplication key ----------------------------------------------


def test_a_resolved_person_is_keyed_on_their_identity():
    """Two people who share a display name stay two profiles, and one person named twice
    stays one. It is the only key that is right in both directions, and it is available
    exactly when the name resolved to somebody in the directory."""
    conn = _Conn(found={"id": "vp-existing"})
    vp.upsert_profile(conn, CO, display_name="Clement", user_id=SUBJECT,
                      consent_basis="attestation", asserted_by=ASSERTER)
    select = next(s for s in conn.cur.sql if s.startswith("SELECT"))
    assert "user_id = %s" in select and "display_name" not in select


def test_an_unresolved_name_duplicates_rather_than_merges():
    """No identity to key on, so the fallback is (name, whoever vouched). Two people each
    naming the same worker produce two profiles; two different workers who share a name never
    become one.

    That direction is chosen, not accidental. A duplicate degrades into a REFUSAL — the
    person becomes his own runner-up and the margin declines to confirm — while a merge is a
    wrong confident answer about somebody's biometric data.
    """
    conn = _Conn()
    vp.upsert_profile(conn, CO, display_name="Clement",
                      consent_basis="attestation", asserted_by=ASSERTER)
    select = next(s for s in conn.cur.sql if s.startswith("SELECT"))
    assert "display_name = %s" in select
    assert "user_id IS NULL" in select, (
        "without this, an unresolved name could match a profile that HAS an identity and "
        "attach one person's voice to another person's record")
    assert "coalesce(consented_by, asserted_by)" in select


# ---- the two lists that have to agree ------------------------------------


def test_every_basis_a_company_may_settle_is_one_enrolment_understands():
    """Two lists in two files, and they disagreed on their first live run.

    `companies.VOICEPRINT_BASES` says what a company may settle. `upsert_profile` decides
    which of those are enough to create a profile without the subject's own id. The second
    read `== "attestation"` while the first had grown `notice`, so setting the basis the
    owner actually uses produced the *pre-existing strict-rule error* — indistinguishable
    from a company that had settled nothing.

    Neither half was wrong on its own and both were tested apart. This is the assertion that
    only exists between them.
    """
    import repositories.companies as co
    import repositories.voiceprints as vp

    for basis in co.VOICEPRINT_BASES:
        conn = _Conn()
        if basis == "confirmed":
            # The one that still needs the subject: it says they agreed themselves.
            vp.upsert_profile(conn, CO, display_name="Clement",
                              consent_given=True, consented_by=SUBJECT,
                              consent_basis=basis)
        else:
            # A standing basis: no subject id, and it must NOT raise.
            vp.upsert_profile(conn, CO, display_name="Clement",
                              consent_basis=basis, asserted_by=ASSERTER)
        assert any(s.startswith("INSERT") for s in conn.cur.sql), (
            f"a company may settle {basis!r} and enrolment refuses it — the two lists have "
            f"drifted apart again")


def test_a_basis_no_company_can_settle_is_still_refused():
    """The pair must agree in both directions. Accepting a value the company endpoint would
    reject would make `upsert_profile` the more permissive of the two, and a caller reaching
    it directly would bypass the closed list entirely."""
    import repositories.voiceprints as vp
    with pytest.raises(ValueError):
        vp.upsert_profile(_Conn(), CO, display_name="Clement",
                          consent_basis="whatever", asserted_by=ASSERTER)
