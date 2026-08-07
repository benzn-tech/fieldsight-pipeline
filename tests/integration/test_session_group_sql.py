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
    assert meeting_session.group_ended_at(db, SOLO) is None


def test_ending_a_group_marks_every_member_once(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=25)
    _session(db, cid, uid, J2, group_id=LEAD, minutes_ago=20)

    assert meeting_session.end_group(db, LEAD) == 3
    # Second call marks nobody: the timestamp records when the meeting ended,
    # not when the last device got round to asking.
    assert meeting_session.end_group(db, LEAD) == 0
    assert meeting_session.group_ended_at(db, LEAD) is not None


def test_a_device_that_joined_after_the_end_still_reads_ended(db):
    """Its own row carries no timestamp — it was written after the update. Read
    only that row and it would keep recording into a finished meeting."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30, ended=True)
    _session(db, cid, uid, LATE, group_id=LEAD, minutes_ago=0)

    own = meeting_session.get(db, LATE)
    assert own["group_ended_at"] is None, "precondition: the late row is unmarked"
    assert meeting_session.group_ended_at(db, own["group_id"]) is not None


def test_settling_waits_for_the_lead(db):
    """A group settled without the lead would finalize the meeting while the
    person holding the code is still recording it."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=0)           # lead still active
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=120)

    assert meeting_session.group_is_settled(db, LEAD, 900) is False


def test_a_joiner_can_arrive_before_the_lead_exists(db):
    """THE case the offline-first design rests on, and the one a FakeConn cannot
    see.

    The group id is generated on the LEAD's device, so a group forms with no
    network. The joiner may therefore reach the server first — the lead's /open
    is best-effort and on a site can be hours late or lost. 0031 declared
    group_id REFERENCES meeting_session(session_id), which made that insert
    raise 23503 and answered the joiner's /open with a 500: joining before the
    lead never worked at all, while the unit suite stayed green. 0034 drops it.
    """
    cid, uid = _seed(db)
    row = meeting_session.ensure_open(
        db, J1, cid, uid, None, "audio", None, group_id="f" * 32)  # lead unknown

    assert row["group_id"] == "f" * 32


def test_the_group_is_only_filled_never_cleared(db):
    """/open is best-effort and arrives more than once. A second call that omits
    the group must not orphan a device that already joined — this is what lets
    the group travel on both /open and the upload without them fighting."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)      # the lead exists in this one
    meeting_session.ensure_open(db, J1, cid, uid, None, "audio", None, group_id=LEAD)
    row = meeting_session.ensure_open(db, J1, cid, uid, None, "audio", None, group_id=None)

    assert row["group_id"] == LEAD


def test_a_recording_started_after_the_end_is_not_in_the_meeting(db):
    """The loop, at the SQL layer.

    A device that rejoined after the meeting ended was told to stop on its first
    upload; that stop wrote another end, which poisoned the next rejoin. Seen in
    production on 2026-08-07 — two rejoins, killed 37s and 2m17s in.

    The late session is placed a minute in the FUTURE rather than "now",
    because `now()` inside a transaction is the transaction's start time, not
    the wall clock: every statement here sees the same instant, so a row
    inserted after end_group still carries an identical timestamp. Written the
    obvious way, this test passed for the wrong reason — CI caught it.
    """
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=25)
    assert meeting_session.end_group(db, LEAD) == 2

    ended = meeting_session.group_ended_at(db, LEAD)
    assert ended is not None

    _session(db, cid, uid, LATE, group_id=LEAD, minutes_ago=-1)   # after the end
    late = meeting_session.get(db, LATE)
    assert late["opened_at"] > ended


def test_the_end_time_is_the_first_one_not_the_last(db):
    """end_group only fills rows that are still NULL, so a straggler marked
    later carries a later stamp. MIN keeps "when did the meeting end" stable —
    otherwise each late arrival would move the boundary and could pull itself
    back inside it.

    The first end is written explicitly rather than through end_group: two
    end_group calls in one transaction would both stamp the same frozen now(),
    and the test would prove nothing."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=30)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=25)
    db.execute(
        "UPDATE meeting_session SET group_ended_at = now() - interval '10 minutes' "
        "WHERE session_id IN (%s, %s)", (LEAD, J1))
    first = meeting_session.group_ended_at(db, LEAD)

    _session(db, cid, uid, J2, group_id=LEAD, minutes_ago=0)
    assert meeting_session.end_group(db, LEAD) == 1      # only the new row

    assert meeting_session.group_ended_at(db, LEAD) == first
    assert meeting_session.get(db, J2)["group_ended_at"] > first
