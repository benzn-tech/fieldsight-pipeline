"""
Integration tests for the multi-device group SQL against a real PostgreSQL.

The unit tests drive these through FakeConn, which records the SQL string and
returns canned rows. That proves the callers' logic and proves nothing about
what Postgres does with the query — and this repo's CLAUDE.md lists several
defects that a full green unit suite walked straight past for exactly that
reason.

Two of the queries here have a failure direction that is silent rather than
loud, which is why they are worth the round trip:

  * `list_group_members` must include the LEAD, which carries no group_id of its
    own. Get that wrong and the merged report quietly omits the person holding
    the meeting — the output still reads perfectly.
  * `end_group` must mark nothing for a solo session. Get that wrong and every
    ordinary End tells its own device to stop recording, in the only path most
    users ever take.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import meeting_session

pytestmark = pytest.mark.integration

LEAD = "a" * 32
J1 = "b" * 32
J2 = "c" * 32
SOLO = "d" * 32
LATE = "e" * 32


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _session(db, cid, uid, sid, *, group_id=None, minutes_ago=0, ended=False):
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, group_id, group_ended_at) "
        "VALUES (%s,%s,%s,'audio','open', now() - make_interval(mins => %s), %s, "
        "CASE WHEN %s THEN now() ELSE NULL END)",
        (sid, cid, uid, minutes_ago, group_id, ended))


def test_the_lead_is_returned_as_a_member_of_its_own_group(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30)          # no group_id of its own
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=25)
    _session(db, cid, uid, J2, group_id=LEAD, minutes_ago=20)
    _session(db, cid, uid, SOLO, minutes_ago=1)           # unrelated

    rows = meeting_session.list_group_members(db, LEAD)

    assert [r["session_id"] for r in rows] == [LEAD, J1, J2], (
        "oldest first, and the lead must be in it despite having no group_id")


def test_ending_a_solo_session_marks_nothing(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, SOLO)

    assert meeting_session.end_group(db, SOLO) == 0
    assert meeting_session.group_is_ended(db, SOLO) is False


def test_ending_a_group_marks_every_member_once(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=25)
    _session(db, cid, uid, J2, group_id=LEAD, minutes_ago=20)

    assert meeting_session.end_group(db, LEAD) == 3
    # Second call marks nobody: the timestamp records when the meeting ended,
    # not when the last device got round to asking.
    assert meeting_session.end_group(db, LEAD) == 0
    assert meeting_session.group_is_ended(db, LEAD) is True


def test_a_device_that_joined_after_the_end_still_reads_ended(db):
    """Its own row carries no timestamp — it was written after the update. Read
    only that row and it would keep recording into a finished meeting."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30, ended=True)
    _session(db, cid, uid, LATE, group_id=LEAD, minutes_ago=0)

    own = meeting_session.get(db, LATE)
    assert own["group_ended_at"] is None, "precondition: the late row is unmarked"
    assert meeting_session.group_is_ended(db, own["group_id"]) is True


def test_settling_waits_for_the_lead(db):
    """A group settled without the lead would finalize the meeting while the
    person holding the code is still recording it."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=0)           # lead still active
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=120)

    assert meeting_session.group_is_settled(db, LEAD, 900) is False


def test_the_group_is_only_filled_never_cleared(db):
    """/open is best-effort and arrives more than once. A second call that omits
    the group must not orphan a device that already joined — this is what lets
    the group travel on both /open and the upload without them fighting."""
    cid, uid = _seed(db)
    meeting_session.ensure_open(db, J1, cid, uid, None, "audio", None, group_id=LEAD)
    row = meeting_session.ensure_open(db, J1, cid, uid, None, "audio", None, group_id=None)

    assert row["group_id"] == LEAD
