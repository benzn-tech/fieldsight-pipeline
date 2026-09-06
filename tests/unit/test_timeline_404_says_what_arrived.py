"""The "no report" 404 now says what DID arrive that day.

During the 2026-09-02 LLM outage a day holding 53 photos, 5 recordings and 5
readable transcripts answered

    404 {"message": "No report for Neil_Blunden on 2026-09-02"}

which is the same answer a day nobody switched the device on gets. The words
were sitting in S3 the whole time -- transcription does not go through the LLM
provider -- and nothing said so.

TWO OF THE THREE 404s IN THIS FUNCTION MUST STAY BARE, and the tests for that
are the important ones here:

  * the cross-user-clip 404 withholds a target's content from a caller who may
    not see it; "they recorded 53 photos" discloses the fact being withheld;
  * the deleted-sources 404 is a day the customer took back. `range_stats`
    drops their tombstoned sessions, but a photo key carries no session id and
    cannot be excluded at all, so counts there would partially undo a deletion.

Spec: AI/spec-day-view-without-the-llm-2026-09-05.md
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
DATE, USER = "2026-09-02", "Neil_Blunden"


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def body_of(res):
    import json
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    """No Aurora topics, no lake doc, nothing deleted -> the plain 404 path."""
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org, "_day_has_deleted_sources", lambda conn, u, d: False)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_author_filter", lambda conn, caller: None)
    monkeypatch.setattr(org.redactions, "deleted_session_bases",
                        lambda conn, company, a, b: set())
    monkeypatch.setattr(org.recordings, "range_stats",
                        lambda conn, company, a, b, sites, author_ids=None,
                        deleted_bases=(): {"sessions": 1, "duration_s": 412,
                                           "photos": 53, "unmeasured": 0,
                                           "unattributed": 0})
    monkeypatch.setattr(org, "_list_media_objects",
                        lambda prefix, what: iter([{"Key": "t%d" % i} for i in range(5)]))
    # The photo list is a second database read on this path. Defaulted to
    # empty here so the tests above keep describing what they were written to
    # describe; the ones that care about photos override it below.
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: [])
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())
    return monkeypatch


def _render(**kw):
    return org._render_timeline_for_user(FakeConn(), dict(CALLER), DATE, USER, **kw)


# --------------------------------------------------------------------------
# What the plain 404 now carries
# --------------------------------------------------------------------------

def test_the_404_reports_what_arrived(wired):
    """Note the photo count: it is the LENGTH OF THE LIST, not range_stats'
    number. range_stats cannot consult the photo tombstones -- they postdate
    it -- so keeping its count here would print a total that the grid below it
    contradicts, and the difference would be exactly the deleted ones."""
    wired.setattr(org.recordings, "photo_list_for_day",
                  lambda conn, company, folder, date: [
                      {"s3_key": f"users/{USER}/pictures/{DATE}/p%d.jpg" % i,
                       "taken_at": None} for i in range(53)])
    res = _render()
    assert res["statusCode"] == 404
    b = body_of(res)
    assert b["message"] == f"No report for {USER} on {DATE}"     # unchanged
    assert b["date"] == DATE                                      # unchanged
    assert b["uploads"] == {"sessions": 1, "duration_s": 412, "photos": 53}
    assert len(b["photo_filenames"]) == 53
    assert b["transcripts"] == 5
    assert b["day_state"] == "transcribed"


def test_captured_when_media_arrived_but_nothing_was_transcribed(wired):
    wired.setattr(org, "_list_media_objects", lambda prefix, what: iter([]))
    assert body_of(_render())["day_state"] == "captured"


def test_transcribed_wins_even_with_no_rows(wired):
    """Media that arrived through a path which never registered `recordings`
    rows (RealPTT, pre-migration-0009, lake-fed) still has transcripts. Reading
    `none` there would be the false zero `day_stats`'s own comment warns about:
    the ROWS are missing, not the recordings."""
    wired.setattr(org.recordings, "range_stats",
                  lambda *a, **k: {"sessions": 0, "duration_s": 0, "photos": 0,
                                   "unmeasured": 0, "unattributed": 0})
    assert body_of(_render())["day_state"] == "transcribed"


def test_a_genuinely_empty_day_keeps_the_old_body_exactly(wired):
    """No keys full of zeros. A day with nothing to say says nothing."""
    wired.setattr(org.recordings, "range_stats",
                  lambda *a, **k: {"sessions": 0, "duration_s": 0, "photos": 0,
                                   "unmeasured": 0, "unattributed": 0})
    wired.setattr(org, "_list_media_objects", lambda prefix, what: iter([]))
    assert body_of(_render()) == {"message": f"No report for {USER} on {DATE}",
                                  "date": DATE}


def test_it_counts_transcripts_for_the_target_folder_and_date(wired):
    seen = {}

    def _lister(prefix, what):
        seen["prefix"] = prefix
        return iter([])

    wired.setattr(org, "_list_media_objects", _lister)
    _render()
    assert seen["prefix"] == f"transcripts/{USER}/{DATE}/"


# --------------------------------------------------------------------------
# The two 404s that must stay bare
# --------------------------------------------------------------------------

def test_a_deleted_day_gets_no_counts(wired):
    """The customer took this day back.

    `range_stats` drops their tombstoned sessions, but photos cannot be
    excluded -- a photo key carries no session id, so the tombstone and the
    photo share only a folder and a day. Reporting "53 photos" here would
    partially undo the deletion through a route that never used to answer.
    """
    wired.setattr(org, "_day_has_deleted_sources", lambda conn, u, d: True)
    wired.setattr(org, "_day_upload_facts",
                  lambda *a: (_ for _ in ()).throw(
                      AssertionError("upload facts computed for a deleted day")))
    assert body_of(_render()) == {"message": f"No report for {USER} on {DATE}",
                                  "date": DATE}


def test_a_cross_user_clipped_day_gets_no_counts(wired):
    """This 404 exists because the caller may not see the target's content.
    Answering it with the target's upload counts discloses exactly the fact
    being withheld -- that they were recording that day."""
    wired.setattr(org.topics, "has_topics_for_source_prefix",
                  lambda conn, prefix: True)
    wired.setattr(org.topics, "list_topics_for_source_prefix",
                  lambda conn, prefix: [])          # all clipped away
    wired.setattr(org, "_day_upload_facts",
                  lambda *a: (_ for _ in ()).throw(
                      AssertionError("upload facts computed for a clipped day")))
    b = body_of(_render(cross_user_clip=True))
    assert b == {"message": f"No in-scope report for {USER} on {DATE}", "date": DATE}


# --------------------------------------------------------------------------
# The lesson from the last release
# --------------------------------------------------------------------------

def test_the_tombstone_range_is_handed_over_as_text(wired):
    """`deleted_session_bases` compares a text-typed substring; `date` objects
    make it 500. The suite cannot execute the SQL, so the type at the boundary
    is the only assertion available -- and the previous release shipped exactly
    this bug with 3901 tests green."""
    seen = {}
    wired.setattr(org.redactions, "deleted_session_bases",
                  lambda conn, company, a, b: seen.update(t=(type(a), type(b))) or set())
    _render()
    assert seen["t"] == (str, str)


def test_no_site_reach_means_no_counts(wired):
    """A caller who can reach no site must not learn a folder was busy."""
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: set())
    assert body_of(_render()) == {"message": f"No report for {USER} on {DATE}",
                                  "date": DATE}


def test_a_failure_computing_the_extras_still_answers_404(wired, caplog):
    """The 404 is the answer. The counts are an addition to it.

    This path did not touch `recordings` or S3 before, so a new dependency that
    can turn a correct 404 into a 500 is a worse bug than the blank screen it
    set out to fix. Two tests written long before this change went red exactly
    here, which is how it was found -- not by this file.

    It must still LOG. A degrade nobody can see is the failure mode this repo
    keeps re-learning.
    """
    import logging

    def _boom(*a, **k):
        raise RuntimeError("aurora is having a day")

    wired.setattr(org.recordings, "range_stats", _boom)
    with caplog.at_level(logging.ERROR):
        res = _render()
    assert res["statusCode"] == 404
    assert body_of(res) == {"message": f"No report for {USER} on {DATE}", "date": DATE}
    assert any("upload facts unavailable" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# The photo list (the step that makes 360 unreachable photos reachable)
# --------------------------------------------------------------------------

def _photo(name, key=None):
    return {"s3_key": key or f"users/{USER}/pictures/{DATE}/{name}", "taken_at": None}


@pytest.fixture
def with_photos(wired):
    wired.setattr(org.recordings, "photo_list_for_day",
                  lambda conn, company, folder, date: [_photo("a.jpg"), _photo("b.jpg"),
                                                       _photo("c.jpg")])
    wired.setattr(org.redactions, "deleted_photo_keys",
                  lambda conn, company, keys=None: set())
    return wired


def test_the_day_lists_its_photos_by_filename(with_photos):
    b = body_of(_render())
    assert b["photo_filenames"] == ["a.jpg", "b.jpg", "c.jpg"]


def test_a_deleted_photo_is_not_listed(with_photos):
    """Photos now have tombstones of their own. A surface that lists them from
    `recordings` bypasses the topic-hiding that used to remove them, so it has
    to consult those tombstones or it un-deletes."""
    with_photos.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: {f"users/{USER}/pictures/{DATE}/b.jpg"})
    b = body_of(_render())
    assert b["photo_filenames"] == ["a.jpg", "c.jpg"]


def test_the_count_and_the_list_cannot_disagree(with_photos):
    """`range_stats` counts photos without consulting the tombstones -- it
    cannot, they postdate it. Taking the count from there and the list from
    here would print "3 photos" over a grid of 2, and the missing one would be
    exactly the one somebody deleted. Both come from the same filtered list.
    """
    with_photos.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: {f"users/{USER}/pictures/{DATE}/b.jpg"})
    with_photos.setattr(org.recordings, "range_stats",
                        lambda *a, **k: {"sessions": 0, "duration_s": 0, "photos": 99,
                                         "unmeasured": 0, "unattributed": 0})
    b = body_of(_render())
    assert b["uploads"]["photos"] == len(b["photo_filenames"]) == 2


def test_only_the_keys_of_this_day_are_checked_for_tombstones(with_photos):
    """One query for the candidates in hand, not every tombstone the company
    ever wrote."""
    seen = {}
    with_photos.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: seen.update(keys=keys) or set())
    _render()
    assert seen["keys"] == [f"users/{USER}/pictures/{DATE}/{n}"
                            for n in ("a.jpg", "b.jpg", "c.jpg")]


def test_a_day_whose_photos_were_all_deleted_says_nothing(wired):
    """Not "0 photos" -- nothing. The old body back verbatim, because a day
    whose content was withdrawn has nothing to report."""
    wired.setattr(org.recordings, "range_stats",
                  lambda *a, **k: {"sessions": 0, "duration_s": 0, "photos": 7,
                                   "unmeasured": 0, "unattributed": 0})
    wired.setattr(org.recordings, "photo_list_for_day",
                  lambda *a, **k: [_photo("gone.jpg")])
    wired.setattr(org.redactions, "deleted_photo_keys",
                  lambda conn, company, keys=None: set(keys or []))
    wired.setattr(org, "_list_media_objects", lambda prefix, what: iter([]))
    assert body_of(_render()) == {"message": f"No report for {USER} on {DATE}",
                                  "date": DATE}
