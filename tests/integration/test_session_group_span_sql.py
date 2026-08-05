"""
Integration tests for group_span_ok against a real PostgreSQL.

The unit tests hand this function a canned `span_seconds` row, so they pin the
comparison and say nothing about what the query measures. That distinction is
the whole defect: the guard is documented as the thing that makes "yesterday
never merges into today" unconditional, and it was measuring the joiners
against each other with the anchor left out.

Kept in its own file (rather than added to test_session_group_sql.py) so two
branches touching this feature do not collide on the same end-of-file.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import meeting_session

pytestmark = pytest.mark.integration

LEAD = "a" * 32
J1 = "b" * 32
FOUR_HOURS = 4 * 3600


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (company_id, email, global_role) "
        "VALUES (%s,'a@b.c','worker') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid


def _session(db, cid, uid, sid, *, group_id=None, hours_ago=0):
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, "
        "opened_at, group_id) "
        "VALUES (%s,%s,%s,'audio','open', now() - make_interval(hours => %s), %s)",
        (sid, cid, uid, hours_ago, group_id))


def test_a_group_carried_overnight_is_refused(db):
    """THE guarantee, measured rather than asserted.

    The lead opened yesterday morning; a device kept the group overnight and
    joins now. Matching on group_id alone sees ONE member — the joiner — which
    spans zero seconds and passes. Including the lead measures the 23 hours that
    are actually there.
    """
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, hours_ago=23)
    _session(db, cid, uid, J1, group_id=LEAD, hours_ago=0)

    assert meeting_session.group_span_ok(db, LEAD, FOUR_HOURS) is False


def test_an_ordinary_meeting_still_merges(db):
    """The normal case, and the reason the window is generous: a break in the
    middle of a long meeting must not split it."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, hours_ago=2)
    _session(db, cid, uid, J1, group_id=LEAD, hours_ago=1)

    assert meeting_session.group_span_ok(db, LEAD, FOUR_HOURS) is True


def test_a_lead_with_no_joiners_spans_nothing(db):
    """Every solo recording reaches this if the guard is ever wired in front of
    one. It must pass trivially rather than block."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, hours_ago=0)

    assert meeting_session.group_span_ok(db, LEAD, FOUR_HOURS) is True


def test_an_unknown_group_is_not_a_violation(db):
    """No rows at all — the joiner arrived before the lead. There is nothing to
    merge yet, and refusing here would block a group that is merely empty."""
    _seed(db)
    assert meeting_session.group_span_ok(db, LEAD, FOUR_HOURS) is True


# ---- lead_is_joinable ------------------------------------------------------
#
# Unlike group_span_ok this one is WIRED: session_open calls it and answers 409.
# A wrong result here refuses live joins or lets stale ones through, today.


def test_a_lead_opened_recently_can_still_be_joined(db):
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, hours_ago=1)

    assert meeting_session.lead_is_joinable(db, LEAD, FOUR_HOURS) is True


def test_a_lead_from_this_morning_cannot(db):
    """The overnight-carry shape, judged on the server's clock because the
    device's is what is in question (BUG-37: these ROMs run 12 hours out)."""
    cid, uid = _seed(db)
    _session(db, cid, uid, LEAD, hours_ago=5)

    assert meeting_session.lead_is_joinable(db, LEAD, FOUR_HOURS) is False


def test_a_lead_with_no_open_time_is_not_this_guard_s_call(db):
    """opened_at is nullable and genuinely ends up NULL: a session inferred from
    the chunk stream has only whatever the filename yielded. The comparison is
    then NULL, which must read as "no opinion" — refusing on NULL would block
    joins for a reason that has nothing to do with staleness."""
    cid, uid = _seed(db)
    db.execute(
        "INSERT INTO meeting_session (session_id, company_id, user_id, kind, status, opened_at) "
        "VALUES (%s,%s,%s,'audio','open', NULL)", (LEAD, cid, uid))

    assert meeting_session.lead_is_joinable(db, LEAD, FOUR_HOURS) is True


def test_an_unknown_lead_is_not_refused(db):
    """The joiner reached us before the lead. Refusing would put the merge back
    on call ordering, which the offline-first design exists to avoid."""
    _seed(db)
    assert meeting_session.lead_is_joinable(db, LEAD, FOUR_HOURS) is True

