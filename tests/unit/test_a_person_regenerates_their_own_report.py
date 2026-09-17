"""Unit: POST /api/org/reports/regenerate queues the caller's OWN report, and only it.

Owner rule (2026-09-15): each person regenerates only their own reports; no role may
regenerate another person's; nobody regenerates a summary by hand.

org-api is in-VPC and cannot invoke the generator (BUG-36), so the route writes one
request artifact, report_requests/<folder>/<rid>.json. The generator, S3-triggered,
regenerates exactly that report or nothing (see
test_a_regenerate_request_names_one_report.py).

The folder is the caller's own folder_name. It is NEVER read from the request: there
is no body field that can widen scope, because there is no scope field at all.

The legacy POST /api/reports/generate is closed in the same change. Its only caller
was the frontend. Left open, it is a company-wide, any-date, forced regenerate
reachable with any token -- the shape of the 2026-09-14 prod incident, where one click
rewrote four reports.
"""
import json
import os
import pathlib

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

from tests.unit.test_org_api_sessions import (  # noqa: E402
    CALLER, body_of, make_event, org, wired,  # noqa: F401
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def s3(wired):
    """Record puts; answer listings from a set of existing transcript prefixes."""
    state = {"puts": [], "lists": [], "transcripts": {"transcripts/Ada_L/2026-09-03/"}}

    class _FakeS3:
        def put_object(self, **kw):
            state["puts"].append(kw)
            return {}

        def list_objects_v2(self, **kw):
            state["lists"].append(kw)
            prefix = kw.get("Prefix", "")
            hit = any(p.startswith(prefix) or prefix.startswith(p) for p in state["transcripts"])
            return {"KeyCount": 1, "Contents": [{"Key": prefix + "x.json"}]} if hit else {"KeyCount": 0}

    wired.setattr(org, "s3", lambda: _FakeS3())
    return state


def _post(body, sub="sub-1"):
    ev = make_event("POST", "/api/org/reports/regenerate", sub=sub)
    ev["body"] = json.dumps(body) if body is not None else None
    return org.lambda_handler(ev, None)


def _artifact(state):
    assert len(state["puts"]) == 1, state["puts"]
    put = state["puts"][0]
    return put["Key"], json.loads(put["Body"])


# ---- the rule ------------------------------------------------------------------

def test_a_daily_regenerate_queues_the_callers_own_report(s3):
    """THE test."""
    res = _post({"report_type": "daily", "date": "2026-09-03"})
    assert res["statusCode"] == 202, body_of(res)
    key, art = _artifact(s3)
    assert key.startswith("report_requests/Ada_L/") and key.endswith(".json")
    assert art["user"] == "Ada_L"
    assert art["report_type"] == "daily"
    assert art["date"] == "2026-09-03"
    assert art["triggered_by"] == CALLER["email"]
    assert body_of(res)["status"] == "queued"


def test_a_body_naming_someone_else_is_ignored(s3):
    """There is no scope field. Anything in the body that looks like one changes
    nothing: the request is still for the caller's own folder."""
    _post({"report_type": "daily", "date": "2026-09-03",
           "user": "Neil_Blunden", "folder": "Neil_Blunden", "users_filter": ["Neil_Blunden"]})
    key, art = _artifact(s3)
    assert "/Ada_L/" in key
    assert art["user"] == "Ada_L"


def test_an_account_without_a_recording_folder_is_told_why(wired, s3):
    """403 with the reason, never a guessed first_last folder (the trailing-
    underscore bug) and never a silent no-op."""
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: dict(CALLER, folder_name=None))
    res = _post({"report_type": "daily", "date": "2026-09-03"})
    assert res["statusCode"] == 403
    assert "folder" in body_of(res)["error"].lower()
    assert s3["puts"] == []


def test_a_day_with_no_recordings_is_not_queued(s3):
    """Queueing it would run the generator, write nothing, and leave the user
    waiting on a report that can never arrive."""
    res = _post({"report_type": "daily", "date": "2026-09-04"})
    assert res["statusCode"] == 404
    assert s3["puts"] == []


# ---- periods -------------------------------------------------------------------

def test_a_weekly_regenerate_is_the_week_ending_on_the_date(s3):
    s3["transcripts"].add("transcripts/Ada_L/")
    res = _post({"report_type": "weekly", "date": "2026-09-06"})
    assert res["statusCode"] == 202, body_of(res)
    _, art = _artifact(s3)
    assert (art["start_date"], art["end_date"]) == ("2026-08-31", "2026-09-06")
    assert "date" not in art


def test_a_monthly_regenerate_is_the_month_to_the_date(s3):
    s3["transcripts"].add("transcripts/Ada_L/")
    res = _post({"report_type": "monthly", "date": "2026-09-30"})
    assert res["statusCode"] == 202, body_of(res)
    _, art = _artifact(s3)
    assert (art["start_date"], art["end_date"]) == ("2026-09-01", "2026-09-30")


# ---- refusals ------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    None,
    {},
    {"report_type": "daily"},
    {"report_type": "daily", "date": "yesterday"},
    {"report_type": "daily", "date": "2026-13-40"},
    {"report_type": "summary", "date": "2026-09-03"},
    {"report_type": "combined", "date": "2026-09-03"},
    {"report_type": ["daily"], "date": "2026-09-03"},
])
def test_a_malformed_request_queues_nothing(s3, body):
    res = _post(body)
    assert res["statusCode"] == 400, (body, body_of(res))
    assert s3["puts"] == []


# ---- the plumbing the route depends on ---------------------------------------------

def _resource_body(name):
    import re
    text = (ROOT / "src" / "template.yaml").read_text(encoding="utf-8")
    start = text.index(f"\n  {name}:\n")
    m = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + m.start()] if m else text[start:]


def test_org_api_may_write_the_request_prefix():
    body = _resource_body("OrgApiFunction")
    assert "${IngestBucketName}/report_requests/*" in body


def test_the_generator_is_wired_to_the_request_prefix():
    script = (ROOT / "scripts" / "wire-s3-events.sh").read_text(encoding="utf-8")
    assert '"Id":"fs-report-requests"' in script
    assert '"Value":"report_requests/"' in script
    assert "${PREFIX}-report-generator" in script


# ---- the legacy route is closed ----------------------------------------------------

def test_the_legacy_generate_route_is_closed(monkeypatch):
    api = pytest.importorskip("lambda_fieldsight_api")
    invoked = []

    class _Lambda:
        def invoke(self, **kw):
            invoked.append(kw)

    monkeypatch.setattr(api, "lambda_client", _Lambda())
    resp = api.trigger_report_generation({"report_type": "daily", "date": "2026-09-03", "force": True},
                                         {"role": "admin", "company_id": "c"})
    assert resp["statusCode"] == 410
    assert invoked == [], "a closed route must not invoke the generator"
