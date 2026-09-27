"""A name that resolves to an account adopts an EMPTY unlinked profile of the same name.

On TEST (2026-09-27) one person had two profiles: a rename by someone not on his site
roster could not resolve "Sam Yu" to his account and created an unlinked profile; a
later rename by someone who could resolve it looked up by account only, missed the
first, and created a second. The Voices page showed "Sam Yu" twice, one of them empty.

The lookup was verified on fieldsight_test: with Sam's account id it returns his linked
profile; with an account that has none it returns the unlinked "Sam Yu". These tests pin
the statement's text, which is all a connection double can see.
"""
from repositories import voiceprints

CO = "11111111-1111-1111-1111-111111111111"
USER = "33333333-3333-3333-3333-333333333333"


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.calls.append((" ".join(sql.split()), params))
        return self

    def fetchone(self):
        return self.conn.answers.pop(0) if self.conn.answers else None


class _Conn:
    def __init__(self, answers):
        self.calls, self.answers = [], list(answers)

    def cursor(self, row_factory=None):
        return _Cur(self)


def _lookup(answers=({"id": "p-old"},)):
    conn = _Conn(answers)
    found = voiceprints.upsert_profile(conn, CO, display_name="Sam Yu", user_id=USER,
                                       consent_basis="notice", asserted_by="u-namer")
    return conn, found


def test_the_account_lookup_falls_back_to_an_unlinked_profile_of_the_same_name():
    conn, _ = _lookup()
    sql, params = conn.calls[0]
    assert ("OR (user_id IS NULL AND external_ref IS NULL AND display_name = %s "
            "AND NOT EXISTS (SELECT 1 FROM speaker_voiceprint_samples s") in sql
    assert params == (CO, USER, "Sam Yu")


def test_the_persons_own_linked_profile_wins_over_an_unlinked_one():
    sql, _ = _lookup()[0].calls[0]
    assert "ORDER BY (user_id IS NOT NULL) DESC, created_at" in sql


def test_an_adopted_profile_is_linked_to_the_account():
    conn, found = _lookup()
    assert found == {"id": "p-old"}
    link = [c for c in conn.calls if c[0].startswith("UPDATE speaker_voiceprints SET user_id")]
    assert link and link[0][1][0] == USER and link[0][1][-1] == "p-old"
