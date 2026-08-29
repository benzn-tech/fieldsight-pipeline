"""Unit: who a named speaker works for, and the two ways that fact can be quietly lost.

`speaker_voiceprints.company_id` is the TENANT — which customer's data the row is. "ABC Ltd"
in *"Andy M is from ABC"* is Andy's **employer**, a subcontractor, and the schema had no such
concept. Putting one into `companies` would have made every subcontractor a tenant shell with
sites, memberships and an ACL, so it is plain nullable text with no foreign key.

Two rules carry the whole feature, and both fail silently if they go:

- **the pair travels together.** A name with no source cannot be audited; a source with no
  name records nothing. Enforced here *and* by a CHECK in migration 0050, because this table
  has three writers and a rule enforced in one caller is a rule until somebody adds a second.
- **an update never NULLs an employer it did not set.** Most corrections carry no employer.
  `SET employer_name = %s` with a NULL parameter is the obvious way to write the statement and
  it erases the answer somebody gave last week — with no error, and nobody looks at a field
  that was already filled in.
"""
import pytest

vp = pytest.importorskip("repositories.voiceprints")

CO = "11111111-1111-1111-1111-111111111111"
SUBJ = "22222222-2222-2222-2222-222222222222"
SETTER = "33333333-3333-3333-3333-333333333333"


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


def _ok(**kw):
    """A correction that is allowed to create a profile, so the employer has somewhere to go."""
    base = dict(display_name="Andy M", consent_given=True, consented_by=SUBJ)
    base.update(kw)
    return base


def test_a_name_without_a_source_is_refused():
    """The audit trail is the point. An employer with no provenance is a string somebody can
    neither defend nor correct — and `employer_source` is what will separate a typed answer
    from a register's answer when a subcontractor changes."""
    with pytest.raises(ValueError, match="travel together"):
        vp.upsert_profile(_Conn(), CO, **_ok(employer_name="ABC Ltd"))


def test_a_source_without_a_name_is_refused():
    with pytest.raises(ValueError, match="travel together"):
        vp.upsert_profile(_Conn(), CO, **_ok(employer_source="typed"))


def test_an_unknown_source_is_refused():
    """A typo outside the closed list would read as provenance while meaning nothing, and the
    CHECK in 0050 would reject it at the database — later, in a background lambda."""
    with pytest.raises(ValueError, match="employer_source"):
        vp.upsert_profile(_Conn(), CO,
                          **_ok(employer_name="ABC Ltd", employer_source="guessed"))


def test_the_closed_list_matches_the_migration():
    """Two places, one list. The last time an enum lived in two files, one side sent `notice`
    and the other understood only `attestation`; the 400 read as a configuration problem for
    two days. This reads the migration rather than restating the values."""
    import os
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sql = open(os.path.join(root, "src", "migrations", "0050_speaker_employer.sql"),
               encoding="utf-8").read()
    m = re.search(r"employer_source IN \(([^)]*)\)", sql)
    assert m, "the CHECK is gone from 0050; the repository is now the only guard"
    in_sql = {v.strip().strip("'") for v in m.group(1).split(",")}

    conn = _Conn()
    for src in sorted(in_sql):
        vp.upsert_profile(conn, CO, **_ok(employer_name="ABC Ltd", employer_source=src))
    assert in_sql, "no values parsed out of the CHECK"


def test_an_empty_string_is_not_an_employer():
    """A form that submits `""` for an untouched field is the ordinary case, not the odd one.
    Treating it as a value would store an employer nobody typed and pair it with a source."""
    conn = _Conn()
    vp.upsert_profile(conn, CO, **_ok(employer_name="  ", employer_source=""))
    insert = next(i for i, s in enumerate(conn.cur.sql) if s.startswith("INSERT"))
    assert None in conn.cur.params[insert]


def test_a_new_profile_records_who_set_it_and_when():
    conn = _Conn()
    vp.upsert_profile(conn, CO, **_ok(employer_name="ABC Ltd", employer_source="typed",
                                      employer_set_by=SETTER))
    insert = next(s for s in conn.cur.sql if s.startswith("INSERT"))
    assert "employer_name, employer_source, employer_set_by, employer_set_at" in insert
    assert SETTER in conn.cur.params[next(i for i, s in enumerate(conn.cur.sql)
                                          if s.startswith("INSERT"))]


# ---- the update rule, which is where the silent loss lives --------------------


def test_a_correction_carrying_no_employer_leaves_the_stored_one_alone():
    """The defect this test exists for: most corrections carry no employer, and the obvious
    `SET employer_name = %s` would blank the stored value on every one of them. Nothing would
    error, and nobody re-checks a field that was already filled in."""
    conn = _Conn(found={"id": "vp-1"})
    vp.upsert_profile(conn, CO, **_ok())
    assert not any("employer_name =" in s for s in conn.cur.sql), (
        "a correction with no employer wrote to employer_name; it has just erased whatever "
        "somebody recorded earlier")


def test_a_different_employer_overwrites_and_says_who():
    """People change subcontractors. The overwrite is wanted; what must come with it is the
    record that it happened, which is the only reason `employer_set_by`/`_at` exist."""
    conn = _Conn(found={"id": "vp-1"})
    vp.upsert_profile(conn, CO, **_ok(employer_name="XYZ Ltd", employer_source="typed",
                                      employer_set_by=SETTER))
    upd = next(s for s in conn.cur.sql if "employer_name =" in s)
    assert "employer_set_by = %s" in upd and "employer_set_at = now()" in upd
    assert "XYZ Ltd" in conn.cur.params[conn.cur.sql.index(upd)]


def test_the_employer_update_is_not_conditioned_on_the_link():
    """`user_id` gates its own UPDATE (`AND user_id IS NULL`, so a link is never overwritten).
    The employer must not inherit that gate: naming an employer for somebody already linked to
    an account is the ordinary case, not an edge one."""
    conn = _Conn(found={"id": "vp-1"})
    vp.upsert_profile(conn, CO, **_ok(employer_name="ABC Ltd", employer_source="typed"))
    upd = next(s for s in conn.cur.sql if "employer_name =" in s)
    assert "user_id IS NULL" not in upd
