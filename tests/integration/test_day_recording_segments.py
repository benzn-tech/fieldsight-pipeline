"""Integration: repositories.day_recording_segments against a real PostgreSQL.

The unit tests replace this repository with an in-memory double that implements the
monotonic rule in Python. That proves the callers and nothing about the SQL: the
`ON CONFLICT ... DO UPDATE ... WHERE` guard is the entire F4 protection, and a WHERE
on the wrong side of the comparison would shrink a day silently while every unit test
stayed green. This file is where that rule is actually checked.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py). A SKIP IS NOT A PASS:
CI provides the database; locally, run it against a disposable Postgres with pgvector.
"""
import datetime
import uuid

import psycopg
import pytest

from repositories import day_recording_segments as drs

pytestmark = pytest.mark.integration

DAY = datetime.date(2026, 9, 2)
SID = "81a64d850bb34d8bbf01ec11fd2af02f"
SEGS = [{"start": 39318.0, "end": 39323.0, "session_id": SID,
         "key": f"transcripts/Blocks_T/2026-09-02/ben_ucpk2_2026-09-02_10-55-18_sid{SID}_c0001_off0.0_to5.0_srcwav.json"}]
NEWER = SEGS + [{"start": 39902.0, "end": 39930.0, "session_id": SID,
                 "key": f"transcripts/Blocks_T/2026-09-02/ben_ucpk2_2026-09-02_11-05-02_sid{SID}_c0021_off0.0_to28.0_srcwav.json"}]


def _user(db, folder):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    return db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (cid, f"{folder.lower()}@example.test", folder)).fetchone()[0]


def _now():
    """A `listed_at` taken from the app's own clock, not the transaction-frozen SQL
    now() the repository stamps `dirty_since`/`computed_at` with (see the comment on
    test_list_dirty_returns_only_dirty_rows_least_recently_computed_first below)."""
    return datetime.datetime.now(datetime.timezone.utc)


def test_a_first_write_round_trips_segments_as_a_list(db):
    uid = _user(db, "Blocks_T")
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, _now()) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == SEGS
    assert row["source_object_count"] == 5
    assert row["dirty"] is False
    assert row["dirty_since"] is None
    assert row["report_date"] == DAY and row["folder_name"] == "Blocks_T"
    assert row["computed_at"] is not None


def test_a_listing_with_fewer_objects_does_not_shrink_the_day(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, _now())
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4, _now()) is False
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["source_object_count"] == 5


def test_an_equal_or_larger_listing_replaces_the_day_and_clears_dirty(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, _now())
    assert drs.mark_dirty(db, uid, DAY) is True
    # now() is constant inside the test transaction (see the comment further down), so
    # this mark and every `_now()` above/below it share one SQL timestamp; the app-side
    # `_now()` taken here is real wall-clock time and so is always later than it.
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, _now()) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["dirty"] is False
    assert row["dirty_since"] is None


# ---- I1: a refused write still carries a `listed_at` and must not leave a fresh
# LIST's day dirty forever, but must not clear a mark it could not have seen ---------

def test_a_refused_write_with_a_fresh_list_clears_the_dirty_mark(db):
    """I1: `ON CONFLICT ... WHERE count >=` refuses the whole UPDATE (CASE clauses
    included) when the new listing is smaller, so without a second statement the row
    would keep whatever dirty/dirty_since it had FOREVER once refused. A day stuck
    dirty this way keeps its stale `computed_at` and sorts first in `list_dirty`,
    starving newer dirty days once SWEEP_LIMIT of them pile up, and keeps re-flagging
    the trailing pass's own flag every tick -- which keeps connecting to Aurora every
    5 minutes and defeats auto-pause. A refused write whose OWN `listed_at` is fresh
    enough to have seen the mark must still clear it."""
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, _now())
    assert drs.mark_dirty(db, uid, DAY) is True
    listed_at = _now()          # real wall clock: always later than the frozen db now()
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4, listed_at) is False
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["source_object_count"] == 5   # refused: unchanged
    assert row["dirty"] is False
    assert row["dirty_since"] is None


def test_a_refused_write_with_a_mark_newer_than_the_list_keeps_dirty(db):
    """I1, other half: a refused write whose `listed_at` predates the mark must NOT
    clear it -- its view could not have seen whatever raised it."""
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, _now())
    listed_at = _now()
    assert drs.mark_dirty(db, uid, DAY) is True
    # now() is frozen for the whole test transaction (see the file-level comment on
    # `_now()` above), so the mark's dirty_since cannot naturally land after
    # `listed_at` (captured mid-test on the real clock) -- push it forward explicitly,
    # exactly like the accepted-write I1 tests below, to represent a mark genuinely
    # raised after this write's LIST began.
    db.execute("UPDATE day_recording_segments SET dirty_since = %s WHERE user_id = %s",
              (listed_at + datetime.timedelta(seconds=1), uid))
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4, listed_at) is False
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["source_object_count"] == 5
    assert row["dirty"] is True
    assert row["dirty_since"] is not None


# ---- I1: a write's LIST relative to when the mark it might be racing was raised ----

def test_a_mark_raised_after_the_writes_list_began_survives_the_write(db):
    """Reproduces the review round-1 race directly against SQL: a write whose LIST
    started before a mark was raised must not clear that mark, even though the count
    rule alone would allow the write to land."""
    uid = _user(db, "Blocks_T")
    listed_at = _now()
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, listed_at)
    assert drs.mark_dirty(db, uid, DAY) is True
    # now() is frozen at transaction start for the whole test, so this mark's
    # dirty_since cannot naturally land after `listed_at` (captured mid-test, on the
    # real clock) inside one transaction. Push it forward explicitly to simulate a
    # mark genuinely raised after this write's LIST began.
    db.execute("UPDATE day_recording_segments SET dirty_since = %s WHERE user_id = %s",
              (listed_at + datetime.timedelta(seconds=1), uid))
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, listed_at) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER            # the write itself still lands
    assert row["dirty"] is True                # but the mark it raced survives
    assert row["dirty_since"] is not None


def test_a_mark_raised_before_the_writes_list_began_is_cleared(db):
    """The trailing pass's own recompute, whose LIST starts after the mark, is exactly
    the write meant to clear it."""
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, _now())
    assert drs.mark_dirty(db, uid, DAY) is True
    listed_at = _now()          # this LIST starts after the mark above
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, listed_at) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER
    assert row["dirty"] is False
    assert row["dirty_since"] is None


# ---- I2: dirty_since must keep the LATEST mark, not the earliest --------------------

def test_a_second_mark_overwrites_the_first_with_the_latest_timestamp(db):
    """I2: a second call to mark_dirty must UPDATE dirty_since to the latest mark, not
    keep the earliest (the pre-fix `COALESCE(dirty_since, now())` rule). now() is
    frozen for the whole test transaction, so both marks share one SQL timestamp;
    proving the fix means showing the second call overwrites an EARLIER dirty_since
    rather than leaving it alone."""
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, _now())
    assert drs.mark_dirty(db, uid, DAY) is True                    # mark1
    frozen_now = drs.get(db, uid, DAY)["dirty_since"]
    earlier = frozen_now - datetime.timedelta(minutes=5)
    db.execute("UPDATE day_recording_segments SET dirty_since = %s WHERE user_id = %s",
              (earlier, uid))                                       # simulate mark1 being old
    assert drs.mark_dirty(db, uid, DAY) is True                    # mark2, the latest mark
    new_mark = drs.get(db, uid, DAY)["dirty_since"]
    # The old COALESCE rule would have left dirty_since == `earlier` untouched.
    assert new_mark > earlier
    assert new_mark == frozen_now


def test_the_latest_of_two_marks_survives_a_write_whose_list_began_between_them(db):
    """I2 + I1's clearing rule together: mark1, then a write's LIST begins, then mark2
    for a transcript that lands AFTER that LIST started. The write must not clear
    dirty -- its view (listed_at) predates mark2, even though it postdates mark1."""
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5, _now())
    assert drs.mark_dirty(db, uid, DAY) is True                     # mark1
    listed_at = _now()                                               # this write's LIST begins after mark1
    assert drs.mark_dirty(db, uid, DAY) is True                     # mark2 lands afterwards
    # Both marks share one frozen transaction now(); push dirty_since forward past
    # listed_at to represent mark2 genuinely landing after this write's LIST began.
    db.execute("UPDATE day_recording_segments SET dirty_since = %s WHERE user_id = %s",
              (listed_at + datetime.timedelta(seconds=1), uid))
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5, listed_at) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER
    assert row["dirty"] is True
    assert row["dirty_since"] is not None


def test_mark_dirty_without_a_row_creates_nothing(db):
    uid = _user(db, "Blocks_T")
    assert drs.mark_dirty(db, uid, DAY) is False
    assert drs.get(db, uid, DAY) is None


def test_list_dirty_returns_only_dirty_rows_least_recently_computed_first(db):
    older = _user(db, "Blocks_Old")
    newer = _user(db, "Blocks_New")
    clean = _user(db, "Blocks_Clean")
    for uid, folder in ((older, "Blocks_Old"), (newer, "Blocks_New"), (clean, "Blocks_Clean")):
        drs.upsert_monotonic(db, uid, DAY, folder, SEGS, 1, _now())
    drs.mark_dirty(db, older, DAY)
    drs.mark_dirty(db, newer, DAY)
    # now() is constant inside the test transaction, so order is set explicitly.
    db.execute("UPDATE day_recording_segments SET computed_at = now() - interval '10 minutes' "
               "WHERE user_id = %s", (older,))
    rows = drs.list_dirty(db)
    assert [r["folder_name"] for r in rows] == ["Blocks_Old", "Blocks_New"]
    assert rows[0]["report_date"] == DAY and str(rows[0]["user_id"]) == str(older)
    assert [r["folder_name"] for r in drs.list_dirty(db, limit=1)] == ["Blocks_Old"]


def test_a_row_cannot_name_a_user_that_does_not_exist(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        drs.upsert_monotonic(db, uuid.uuid4(), DAY, "Nobody", SEGS, 1, _now())


# ---- deleted merged meetings -------------------------------------------------------
#
# A merged meeting's delete writes one tombstone on `extractions/{lead}/{date}/grp{lead_sid}`.
# These run the real join against meeting_session and redactions: the unit tests of the
# org-api read replace this function, so only here is the SQL itself checked.

LEAD = "a" * 32
MEMBER = "b" * 32
OTHER_MEMBER = "c" * 32
SOLO = "d" * 32


def _group(db, *, lead_row=True):
    """A company, a lead user and a member user on a DIFFERENT folder, one group, one solo."""
    from repositories import meeting_session, redactions

    cid = db.execute("INSERT INTO companies (name) VALUES ('G') RETURNING id").fetchone()[0]
    lead_user = db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, 'lead@example.test', 'worker', 'Grp_Lead') RETURNING id", (cid,)).fetchone()[0]
    member_user = db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, 'member@example.test', 'worker', 'Grp_Member') RETURNING id", (cid,)).fetchone()[0]
    if lead_row:
        meeting_session.ensure_open(db, LEAD, cid, lead_user, None, "audio", None)
    meeting_session.ensure_open(db, MEMBER, cid, member_user, None, "audio", None, group_id=LEAD)
    meeting_session.ensure_open(db, OTHER_MEMBER, cid, lead_user, None, "audio", None, group_id=LEAD)
    meeting_session.ensure_open(db, SOLO, cid, member_user, None, "audio", None)
    return cid, lead_user, redactions


def test_a_deleted_group_hides_the_lead_and_every_member_on_any_folder(db):
    cid, lead_user, redactions = _group(db)
    redactions.create_recording_tombstone(
        db, cid, f"extractions/Grp_Lead/2026-09-02/grp{LEAD}", "removed", lead_user, "admin")
    assert drs.deleted_group_session_ids(db, [LEAD, MEMBER, OTHER_MEMBER, SOLO]) == {
        LEAD, MEMBER, OTHER_MEMBER}
    # a member's day asks only about its own session, and still gets the answer
    assert drs.deleted_group_session_ids(db, [MEMBER]) == {MEMBER}


def test_a_lead_with_no_session_row_is_still_matched_by_its_own_id(db):
    cid, lead_user, redactions = _group(db, lead_row=False)
    redactions.create_recording_tombstone(
        db, cid, f"extractions/Grp_Lead/2026-09-02/grp{LEAD}", "removed", lead_user, "admin")
    assert drs.deleted_group_session_ids(db, [LEAD]) == {LEAD}


def test_a_reverted_group_tombstone_hides_nothing(db):
    cid, lead_user, redactions = _group(db)
    key = f"extractions/Grp_Lead/2026-09-02/grp{LEAD}"
    redactions.create_recording_tombstone(db, cid, key, "removed", lead_user, "admin")
    db.execute("UPDATE redactions SET reverted_at = now() WHERE target_key = %s", (key,))
    assert drs.deleted_group_session_ids(db, [LEAD, MEMBER, OTHER_MEMBER]) == set()


def test_a_solo_sid_tombstone_is_not_read_as_a_group(db):
    cid, lead_user, redactions = _group(db)
    redactions.create_recording_tombstone(
        db, cid, f"extractions/Grp_Lead/2026-09-02/sid{LEAD}", "removed", lead_user, "admin")
    # the sid arm belongs to filter_segments' prefix match; it must not fan out to members
    assert drs.deleted_group_session_ids(db, [LEAD, MEMBER, OTHER_MEMBER]) == set()


def test_no_candidates_asks_nothing(db):
    assert drs.deleted_group_session_ids(db, []) == set()
    assert drs.deleted_group_session_ids(db, [None, ""]) == set()


# ---- group_member_session_ids: exclusion (not deletion) fan-out ---------------------
#
# Companion to deleted_group_session_ids, used when a `grp{lead_sid}` extraction base
# itself (not a tombstone) needs expanding to every device session it covers -- e.g. all
# of its topics were redacted/non_work (recording_blocks §5.4 fix round 1, I1).

def test_a_lead_with_members_expands_to_the_lead_and_every_member(db):
    _cid, _lead_user, _redactions = _group(db)
    assert drs.group_member_session_ids(db, [LEAD]) == {LEAD, MEMBER, OTHER_MEMBER}


def test_a_lead_with_no_members_expands_to_only_itself(db):
    from repositories import meeting_session

    cid = db.execute("INSERT INTO companies (name) VALUES ('GS') RETURNING id").fetchone()[0]
    lone_lead = "e" * 32
    lone_user = db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, 'lone@example.test', 'worker', 'Lone_Lead') RETURNING id", (cid,)).fetchone()[0]
    meeting_session.ensure_open(db, lone_lead, cid, lone_user, None, "audio", None)
    assert drs.group_member_session_ids(db, [lone_lead]) == {lone_lead}


def test_empty_input_expands_to_nothing(db):
    assert drs.group_member_session_ids(db, []) == set()
    assert drs.group_member_session_ids(db, [None, ""]) == set()
