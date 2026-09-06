"""Unit: a rendered day carries its photos, and a scoped caller does not.

Measured on prod the morning this was written, as a real user against the real
bucket: of the photos stored on days that DO have a report, **71 of 90 were
unreachable from any screen**. 2026-09-02 is one topic and 53 photos, of which
10 come back (`PHOTOS_PER_TOPIC_CAP`); the other days lose theirs to binding,
because 37% of topic time windows are a single instant and 76% are narrower than
the +/-2 minute tolerance. A photo taken while nobody was talking binds to
nothing, and an unbound photo was invisible everywhere.

So the day carries the list and topic binding stays what it always was -- an
enrichment, not the only door.

The load-bearing test here is NOT the one that proves the field appears. It is
`test_a_scoped_caller_viewing_someone_elses_day_gets_no_photo_list`: the review
that produced this change found that a pm/site_manager viewing SOMEONE ELSE's
day does reach this 200 whenever a single topic survives the site ACL, and for
that caller the 200 granted a site-clipped slice of the day rather than the day.
The photo query is folder+date with no site clip, and a filename carries a name,
a date and a time -- so an ungated field says "the target was somewhere at
14:32" about sites the caller cannot see.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
OTHER_SITE = "b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2"
DATE, USER = "2026-09-02", "Neil_Blunden"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}

PREFIX = "users/%s/pictures/%s/" % (USER, DATE)
THREE_PHOTOS = [{"s3_key": PREFIX + "a.jpg", "taken_at": None},
                {"s3_key": PREFIX + "b.jpg", "taken_at": None},
                {"s3_key": PREFIX + "c.jpg", "taken_at": None}]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    """One in-scope Aurora topic -> the rendered 200 path."""
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix: [{"site_id": SITE_ID}])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org, "render_report_shape",
                        lambda rows, doc, date, user, conn=None, company_id=None:
                        {"report_date": date, "topics": [], "_report_metadata": {}})
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: list(THREE_PHOTOS))
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())
    return monkeypatch


def _render(caller=None, **kw):
    return org._render_timeline_for_user(
        FakeConn(), dict(caller or CALLER), DATE, USER, **kw)


# --------------------------------------------------------------------------

def test_the_rendered_day_lists_every_photo_not_only_the_bound_ones(wired):
    res = _render()
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["photo_filenames"] == ["a.jpg", "b.jpg", "c.jpg"]
    assert body["uploads"] == {"photos": 3}


def test_a_deleted_photo_is_not_in_the_rendered_day(wired):
    """`photo_list_for_day` does NO tombstone filtering of its own -- it is one
    SELECT. The filter lives in the shared block, and this is the test that says
    so: remove it and the count goes from two to three, which on prod means
    deleted photos back on screen on the hottest read path."""
    wired.setattr(org.redactions, "deleted_photo_keys",
                  lambda conn, company, keys=None: {PREFIX + "b.jpg"})
    body = body_of(_render())
    assert body["photo_filenames"] == ["a.jpg", "c.jpg"]
    assert body["uploads"] == {"photos": 2}


def test_a_scoped_caller_viewing_someone_elses_day_gets_no_photo_list(wired):
    """THE one that matters.

    A graded pm/site_manager viewing another user's day reaches this same 200 as
    soon as one topic survives the ACL -- the shape is tried before the
    cross-user 404. The topics are site-clipped and the prose is suppressed for
    exactly this caller; the photo list is neither, so it must not be sent.
    """
    res = _render(cross_user_clip=True)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "photo_filenames" not in body
    assert "uploads" not in body
    # ...and the topics still came through, so this is a gate on the field and
    # not an accidental short-circuit of the whole path.
    assert body["report_date"] == DATE


def test_the_count_is_the_length_of_the_list_it_ships_with(wired):
    """Two numbers for one fact is how a grid of 41 ends up captioned '53'."""
    body = body_of(_render())
    assert body["uploads"]["photos"] == len(body["photo_filenames"])


def test_uploads_carries_photos_and_nothing_else(wired):
    """The 404's `uploads` also holds sessions/duration from `range_stats`
    (site-clipped, author-filtered, tombstoned). This body already reports those
    from `day_stats` (folder-wide, unclipped) under `_report_metadata`, so
    copying the 404's shape here would put two disagreeing session counts in one
    document."""
    assert set(body_of(_render())["uploads"]) == {"photos"}


def test_a_photo_query_failure_does_not_take_down_the_day(wired):
    """The report is the answer; the photos are an addition to it. A new read
    that can turn a correct 200 into a 500 is a worse bug than the blank grid it
    set out to fix."""
    def _boom(conn, company, folder, date):
        raise RuntimeError("relation does not exist")

    wired.setattr(org.recordings, "photo_list_for_day", _boom)
    res = _render()
    assert res["statusCode"] == 200
    body = body_of(res)
    assert body["report_date"] == DATE
    assert "photo_filenames" not in body


def test_a_cross_company_caller_is_not_pinned_to_its_own_company(wired):
    """`None` means UNRESTRICTED; the folder+date predicate does the pinning.

    Passing `caller["company_id"]` unconditionally is the exact bug that made an
    admin's day come back empty on prod twice (#738/#740) -- a platform_admin
    sits in its own operator company, which matches none of the customer rows.
    The trap is live: the obvious attach point, `render_report_shape`, is
    already handed `company_id=caller["company_id"]`.
    """
    seen = {}

    def _record(conn, company, folder, date):
        seen["company"] = company
        return list(THREE_PHOTOS)

    wired.setattr(org.recordings, "photo_list_for_day", _record)

    _render(caller=dict(CALLER, global_role="platform_admin"))
    assert seen["company"] is None

    _render(caller=dict(CALLER, global_role="admin"))
    assert seen["company"] == "c-1"


def test_a_day_with_no_photos_still_answers_with_an_empty_list(wired):
    """Absent and empty are different facts. The client must be able to say
    'no photos that day' rather than 'unknown', which is what a missing key
    means everywhere else in this response."""
    wired.setattr(org.recordings, "photo_list_for_day",
                  lambda conn, company, folder, date: [])
    body = body_of(_render())
    assert body["photo_filenames"] == []
    assert body["uploads"] == {"photos": 0}


def test_render_report_shape_itself_stays_free_of_day_photos():
    """The attach belongs in the timeline closure, not in the renderer.

    `render_report_shape` has two other callers: the session-report preview,
    which is SESSION scoped and must not gain a whole day's photos, and
    reindex's embedding builder, where it would change indexed content and spend
    a pointless query per topic.
    """
    row = {"id": "t-1", "site_id": SITE_ID, "site_name": "Alpha",
           "user_name": "Neil Blunden", "category": "safety", "title": "T",
           "summary": "", "time_range": None, "participants": [],
           "action_items": [], "safety_observations": [], "findings": [],
           "photos": []}
    out = org.render_report_shape([row], None, DATE, USER,
                                  conn=None, company_id="c-1")
    assert "photo_filenames" not in out
    assert "uploads" not in out
