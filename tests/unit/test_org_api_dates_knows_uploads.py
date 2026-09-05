"""`GET /api/org/dates?uploads=1` — the calendar learns that media arrived.

Why this endpoint and not the day view. A date is in this index only if
extraction produced topics for it. On 2026-09-02 the LLM provider's account
went into arrears; extraction has failed on every attempt since, so days
holding real work stopped appearing at all. Measured on prod: 432 photos in
S3, 72 reachable in the UI, 296 of the lost ones on days that never got a
topic. There was no dot to click, so no later screen could be reached — which
is why the calendar, not the day view, had to know first.

The compatibility rule these tests exist to hold: WITHOUT `uploads=1` the
response must be exactly what it was before, and the upload query must not run
at all. Nine frontend callers read this map. Eight filter on `hasReport`;
`today.js:_fallbackCandidates` does not — it takes `Object.keys(...)` and
probes the newest few days for a report. Report-less dates in the default
response would spend that budget on days that 404 and hide the most recent
real report.

Spec: AI/spec-day-view-without-the-llm-2026-09-05.md
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self, *a, **k):
        raise AssertionError("no test here may reach real SQL")


def make_event(params=None):
    return {
        "httpMethod": "GET",
        "path": "/api/org/dates",
        "queryStringParameters": params,
        "body": None,
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}},
    }


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_author_filter", lambda conn, caller: None)
    monkeypatch.setattr(org.topics, "report_date_counts",
                        lambda conn, sites, since, author_ids=None: [
                            {"report_date": "2026-08-13", "topics": 4, "safety": 1},
                        ])
    monkeypatch.setattr(org.redactions, "deleted_session_bases",
                        lambda conn, company, d_from, d_to: set())
    monkeypatch.setattr(org.recordings, "upload_date_counts",
                        lambda conn, company, sites, since, author_ids=None,
                        deleted_bases=(): [
                            {"date": "2026-09-02", "sessions": 1, "photos": 53},
                        ])
    return monkeypatch


# --------------------------------------------------------------------------
# The compatibility guarantee
# --------------------------------------------------------------------------

def test_without_the_flag_the_body_is_exactly_what_it_was(wired):
    """No new key, no new date, and — the part that matters — no new query.

    Asserted by making the upload query fail loudly rather than by trusting
    that it wasn't called: a mock that records a call still lets a regression
    return the right body for the wrong reason.
    """
    def _must_not_run(*a, **k):
        raise AssertionError("upload_date_counts ran without ?uploads=1")

    wired.setattr(org.recordings, "upload_date_counts", _must_not_run)
    wired.setattr(org.redactions, "deleted_session_bases", _must_not_run)

    res = org.lambda_handler(make_event(), None)
    assert res["statusCode"] == 200
    assert body_of(res) == {
        "dates": {"2026-08-13": {"hasReport": True, "topics": 4, "safety": 1}}
    }


@pytest.mark.parametrize("params", [None, {}, {"months": "3"}, {"uploads": "0"},
                                    {"uploads": ""}, {"uploads": "true"}])
def test_only_the_literal_1_opts_in(wired, params):
    """`uploads=true` must NOT opt in.

    A client that guesses the spelling and gets the wide index by accident
    would hit the today.js regression this flag exists to avoid, and would do
    it in production without anyone choosing to.
    """
    wired.setattr(org.recordings, "upload_date_counts",
                  lambda *a, **k: (_ for _ in ()).throw(
                      AssertionError("opted in on: %r" % (params,))))
    res = org.lambda_handler(make_event(params), None)
    assert "2026-09-02" not in body_of(res)["dates"]


# --------------------------------------------------------------------------
# The new behaviour
# --------------------------------------------------------------------------

def test_a_day_with_uploads_and_no_report_appears(wired):
    res = org.lambda_handler(make_event({"uploads": "1"}), None)
    dates = body_of(res)["dates"]
    assert dates["2026-09-02"] == {
        "hasReport": False, "topics": 0, "safety": 0,
        "hasUploads": True, "sessions": 1, "photos": 53,
    }


def test_hasReport_stays_true_and_untouched_on_a_report_day(wired):
    """The existing contract survives the merge — a client reading only
    `hasReport` sees the same days it always did."""
    res = org.lambda_handler(make_event({"uploads": "1"}), None)
    d = body_of(res)["dates"]["2026-08-13"]
    assert d["hasReport"] is True and d["topics"] == 4 and d["safety"] == 1


def test_a_report_day_with_no_recordings_says_so_rather_than_staying_silent(wired):
    """`hasUploads: False`, not a missing key.

    A day whose media arrived through a path that never registered rows
    (RealPTT, pre-migration-0009, lake-fed files) is a real case. Omitting the
    field there would leave a client that just asked about uploads unable to
    tell "no uploads" from "this endpoint didn't answer".
    """
    res = org.lambda_handler(make_event({"uploads": "1"}), None)
    assert body_of(res)["dates"]["2026-08-13"]["hasUploads"] is False


def test_a_day_with_both_keeps_the_report_and_gains_the_counts(wired):
    wired.setattr(org.recordings, "upload_date_counts",
                  lambda conn, company, sites, since, author_ids=None,
                  deleted_bases=(): [{"date": "2026-08-13", "sessions": 3, "photos": 7}])
    d = body_of(org.lambda_handler(make_event({"uploads": "1"}), None))["dates"]["2026-08-13"]
    assert d == {"hasReport": True, "topics": 4, "safety": 1,
                 "hasUploads": True, "sessions": 3, "photos": 7}


# --------------------------------------------------------------------------
# Deletion and the clock
# --------------------------------------------------------------------------

def test_deleted_sessions_are_handed_to_the_upload_query(wired):
    """A dot the customer took back must not come back.

    This asserts the tombstones REACH the query. Whether the SQL then honours
    them is a database question and belongs in tests/integration — a fake
    connection records SQL, it does not execute it.
    """
    seen = {}
    wired.setattr(org.redactions, "deleted_session_bases",
                  lambda conn, company, d_from, d_to: {"sid" + "a" * 32})
    wired.setattr(org.recordings, "upload_date_counts",
                  lambda conn, company, sites, since, author_ids=None,
                  deleted_bases=(): seen.update(deleted=deleted_bases) or [])
    org.lambda_handler(make_event({"uploads": "1"}), None)
    assert seen["deleted"] == {"sid" + "a" * 32}


def test_the_range_end_is_todays_nz_date_not_utc(wired):
    """For thirteen hours every night UTC is still on yesterday.

    Closing the tombstone range on a UTC 'today' would leave a session deleted
    this evening outside the range, so the calendar would keep a dot for a day
    the customer had just withdrawn. Same family as BUG-37.

    THE CLOCK IS FROZEN INSIDE THAT WINDOW ON PURPOSE. Comparing against a
    live `now()` only distinguishes the two derivations while UTC and NZ
    happen to be on different days — for most of the day a UTC-derived
    implementation passes, which is a test that reports the bug is absent
    because it looked at the wrong hour. Caught by mutating the source and
    watching this pass.
    """
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 22:30 UTC on the 5th == 11:30 NZ on the 6th.
            return datetime(2026, 9, 5, 22, 30, tzinfo=timezone.utc)

    wired.setattr(org, "datetime", _FrozenDatetime)
    seen = {}
    wired.setattr(org.redactions, "deleted_session_bases",
                  lambda conn, company, d_from, d_to: seen.update(to=d_to) or set())
    org.lambda_handler(make_event({"uploads": "1"}), None)
    assert seen["to"].isoformat() == "2026-09-06"      # NZ, not 2026-09-05


def test_cross_company_caller_lifts_the_company_pin(wired):
    """platform_admin reaches sites in other companies; pinning to its own
    operator company would count zero rows and report an empty calendar for
    tenants that recorded all day. Mirrors the metric route's rule."""
    seen = {}
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: dict(CALLER, global_role="platform_admin"))
    wired.setattr(org.recordings, "upload_date_counts",
                  lambda conn, company, sites, since, author_ids=None,
                  deleted_bases=(): seen.update(company=company) or [])
    org.lambda_handler(make_event({"uploads": "1"}), None)
    assert seen["company"] is None


def test_ordinary_caller_keeps_the_company_pin(wired):
    seen = {}
    wired.setattr(org.recordings, "upload_date_counts",
                  lambda conn, company, sites, since, author_ids=None,
                  deleted_bases=(): seen.update(company=company) or [])
    org.lambda_handler(make_event({"uploads": "1"}), None)
    assert seen["company"] == "c-uuid-1"
