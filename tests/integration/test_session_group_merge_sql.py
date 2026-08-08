"""
Integration tests for the Phase C merge-state SQL against a real PostgreSQL.

The unit tests drive session_group through FakeConn, which records the SQL
string and returns canned rows. That proves the callers' logic and proves
nothing about what Postgres does — and every one of the properties below is a
semantic the double cannot demonstrate:

  * `claim` must be exactly-once under two concurrent connections. The whole
    design rests on it: a second claim means the group merges twice, deletes the
    members' topics twice, and emails everyone twice.
  * `list_due` must EXCLUDE a group nobody recorded in. Get that wrong and the
    group is claimed, produces nothing, and — because the claim already set
    merged_at — is never looked at again.
  * `list_due` must WAIT for a member that is still recording, including when
    that member is the LEAD (which carries no group_id of its own).
  * `rearm` must be conditional, or two late arrivals landing together re-arm
    twice and burn the cap on one merge.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import session_group

pytestmark = pytest.mark.integration

LEAD = "a" * 32
J1 = "b" * 32
J2 = "c" * 32


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _session(db, cid, uid, sid, *, group_id=None, minutes_ago=60,
             status='sent', segments=5):
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, last_segment_at, segment_count, group_id) "
        "VALUES (%s,%s,%s,'audio',%s, now() - make_interval(mins => %s), "
        "        now() - make_interval(mins => %s), %s, %s)",
        (sid, cid, uid, status, minutes_ago, minutes_ago, segments, group_id))


def _group(db, cid, gid=LEAD):
    session_group.ensure_row(db, gid, cid)


def test_a_settled_group_with_content_is_due(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=60)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=55)
    _group(db, cid)

    rows = session_group.list_due(db, 900)
    assert [r["group_id"] for r in rows] == [LEAD]


def test_a_group_nobody_recorded_in_is_not_due(db):
    # The 'empty' case. Without the segment_count test this group is claimed,
    # produces nothing, and merged_at is already set so it never returns.
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, segments=0)
    _session(db, cid, uid, J1, group_id=LEAD, segments=0)
    _group(db, cid)

    assert session_group.list_due(db, 900) == []


def test_a_group_whose_LEAD_is_still_recording_is_not_due(db):
    # The lead carries no group_id of its own, so a membership test written as
    # `group_id = %s` alone would not see it — and the merge would run while the
    # person holding the meeting was still talking.
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=0, status='open')   # still going
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=55)
    _group(db, cid)

    assert session_group.list_due(db, 900) == []


def test_a_group_whose_joiner_is_still_recording_is_not_due(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=60)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=0, status='open')
    _group(db, cid)

    assert session_group.list_due(db, 900) == []


def test_a_quiet_but_non_terminal_member_does_not_hold_the_group(db):
    # An inspector who forgot to press stop, or walked off and will sync hours
    # later, must not hold everyone else's report hostage.
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, minutes_ago=60)
    _session(db, cid, uid, J1, group_id=LEAD, minutes_ago=60, status='open')
    _group(db, cid)

    assert [r["group_id"] for r in session_group.list_due(db, 900)] == [LEAD]


def test_a_resolved_group_leaves_the_candidate_set(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)
    _session(db, cid, uid, J1, group_id=LEAD)
    _group(db, cid)

    session_group.mark_result(db, LEAD, "merged")
    assert session_group.list_due(db, 900) == [], \
        "a resolved group must not be rescanned every minute forever"


def test_claim_is_exactly_once(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)
    _group(db, cid)

    first = session_group.claim(db, LEAD, "extractions/A/d/grp.json")
    second = session_group.claim(db, LEAD, "extractions/A/d/other.json")

    assert (first, second) == (True, False), "two claims on one group"
    row = session_group.get(db, LEAD)
    assert row["merge_count"] == 1, "the cap must count merges, not attempts"
    assert row["merged_key"] == "extractions/A/d/grp.json", \
        "the losing claim must not overwrite the winner's key"


def test_rearm_is_conditional(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)
    _group(db, cid)
    session_group.claim(db, LEAD, "k")

    assert session_group.rearm(db, LEAD) is True
    assert session_group.rearm(db, LEAD) is False, \
        "a second late arrival must not re-arm again"
    assert session_group.get(db, LEAD)["merge_count"] == 1, \
        "re-arming must not touch the count"


def test_a_rearmed_group_is_due_again(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)
    _session(db, cid, uid, J1, group_id=LEAD)
    _group(db, cid)
    session_group.claim(db, LEAD, "k")

    assert session_group.list_due(db, 900) == [], "claimed groups are not due"
    session_group.rearm(db, LEAD)
    assert [r["group_id"] for r in session_group.list_due(db, 900)] == [LEAD], \
        "the standing scan is what makes the late-arrival re-merge possible at all"


def test_ensure_row_is_idempotent_and_does_not_reset_state(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD)
    _group(db, cid)
    session_group.claim(db, LEAD, "k")

    session_group.ensure_row(db, LEAD, cid)      # a second joiner arrives late
    row = session_group.get(db, LEAD)
    assert row["merged_at"] is not None and row["merge_count"] == 1, \
        "a late joiner's /open must not un-claim a merge in flight"
