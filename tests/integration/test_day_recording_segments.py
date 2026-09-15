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


def test_a_first_write_round_trips_segments_as_a_list(db):
    uid = _user(db, "Blocks_T")
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == SEGS
    assert row["source_object_count"] == 5
    assert row["dirty"] is False
    assert row["report_date"] == DAY and row["folder_name"] == "Blocks_T"
    assert row["computed_at"] is not None


def test_a_listing_with_fewer_objects_does_not_shrink_the_day(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5)
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4) is False
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["source_object_count"] == 5


def test_an_equal_or_larger_listing_replaces_the_day_and_clears_dirty(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5)
    assert drs.mark_dirty(db, uid, DAY) is True
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["dirty"] is False


def test_a_refused_write_leaves_the_dirty_mark_in_place(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5)
    drs.mark_dirty(db, uid, DAY)
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4) is False
    assert drs.get(db, uid, DAY)["dirty"] is True


def test_mark_dirty_without_a_row_creates_nothing(db):
    uid = _user(db, "Blocks_T")
    assert drs.mark_dirty(db, uid, DAY) is False
    assert drs.get(db, uid, DAY) is None


def test_list_dirty_returns_only_dirty_rows_least_recently_computed_first(db):
    older = _user(db, "Blocks_Old")
    newer = _user(db, "Blocks_New")
    clean = _user(db, "Blocks_Clean")
    for uid, folder in ((older, "Blocks_Old"), (newer, "Blocks_New"), (clean, "Blocks_Clean")):
        drs.upsert_monotonic(db, uid, DAY, folder, SEGS, 1)
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
        drs.upsert_monotonic(db, uuid.uuid4(), DAY, "Nobody", SEGS, 1)


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
