"""Unit: a regenerate request regenerates the requester's own report, and nothing else.

On 2026-09-14 one click on one person's 3 Sep report in the dev UI became a single
prod invocation that rewrote four reports across two days and two people: the
gateway's user filter was sent under a key the generator never read, and every
manual regenerate also ran the seven-day stale backfill.

The owner's rule is binding: each person regenerates only their own reports; no
role may regenerate another person's; nobody regenerates a summary by hand.

This change is the generator half, and it is inert on its own -- nothing writes a
request yet. It pins three things:

  A REQUEST NAMES ONE PERSON. A `Records` event carries report_requests/<folder>/
  <rid>.json. The artifact's `user` must be a single non-empty folder string equal
  to the key's folder segment. Daily regenerates that one folder with force and no
  backfill.

  A BAD REQUEST GENERATES NOTHING. Today's handler, given an S3 event, would fall
  through to its schedule defaults -- yesterday, every user, plus backfill. So any
  artifact that cannot be read, has no user, a list of users, a user that is not
  the key's folder, or a bad date or period, returns a rejection and makes zero
  generation calls. Never the schedule path.

  A PERSONAL WEEKLY IS BUILT FROM THAT PERSON ONLY. With `user` set the periodic
  report reads that person's own dailies directly, never borrows the company-wide
  day summary for a day they have no daily, and never writes a site or combined
  report.
"""
import json

import pytest

rg = pytest.importorskip("lambda_report_generator", reason="requires boto3")

KEY = "report_requests/Ben_UCPK2/req-123.json"


class _CapturingS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw["Key"])
        return {}


def _event(key=KEY):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


@pytest.fixture
def handler(monkeypatch):
    """The handler with the generators and the backfill replaced by recorders."""
    calls = {"daily": [], "periodic": [], "stale": 0}
    artifacts = {}

    monkeypatch.setattr(rg, "S3_BUCKET", "b")
    monkeypatch.setattr(rg, "s3_client", _CapturingS3())
    monkeypatch.setattr(rg, "download_json_from_s3", lambda b, k: artifacts.get(k))
    monkeypatch.setattr(rg, "generate_daily_report",
                        lambda *a, **k: calls["daily"].append((a, k)) or {"status": "success"})
    monkeypatch.setattr(rg, "generate_periodic_report",
                        lambda *a, **k: calls["periodic"].append((a, k)) or {"status": "success"})

    def _stale(*a, **k):
        calls["stale"] += 1
        return []

    monkeypatch.setattr(rg, "check_stale_reports", _stale)
    return calls, artifacts


def _nothing_generated(calls):
    return calls["daily"] == [] and calls["periodic"] == [] and calls["stale"] == 0


# ---- a good request ---------------------------------------------------------

def test_a_daily_request_regenerates_exactly_that_person(handler):
    """THE test. One folder, forced, and never the backfill."""
    calls, artifacts = handler
    artifacts[KEY] = {"report_type": "daily", "user": "Ben_UCPK2", "date": "2026-09-03",
                      "triggered_by": "benlin.chch+ucpk2@gmail.com"}
    rg.lambda_handler(_event(), None)

    assert len(calls["daily"]) == 1
    args, kwargs = calls["daily"][0]
    passed = dict(zip(["target_date", "hidden_topic_ids", "triggered_by"], args), **kwargs)
    assert passed["target_date"] == "2026-09-03"
    assert passed["users_filter"] == ["Ben_UCPK2"]
    assert passed["force"] is True
    assert passed["triggered_by"] == "benlin.chch+ucpk2@gmail.com"
    assert calls["stale"] == 0, "a manual regenerate must never run the seven-day backfill"


def test_a_weekly_request_passes_the_person_and_the_period(handler):
    calls, artifacts = handler
    artifacts[KEY] = {"report_type": "weekly", "user": "Ben_UCPK2",
                      "start_date": "2026-08-31", "end_date": "2026-09-06",
                      "triggered_by": "x@y.z"}
    rg.lambda_handler(_event(), None)

    assert calls["daily"] == []
    (args, kwargs), = calls["periodic"]
    assert args[:3] == ("weekly", "2026-08-31", "2026-09-06")
    assert kwargs["user"] == "Ben_UCPK2"
    assert kwargs["triggered_by"] == "x@y.z"


# ---- a bad request generates nothing ------------------------------------------

@pytest.mark.parametrize("artifact", [
    None,                                                                  # unreadable / absent
    {},                                                                    # empty
    {"report_type": "daily", "date": "2026-09-03"},                        # no user
    {"report_type": "daily", "user": "", "date": "2026-09-03"},            # blank user
    {"report_type": "daily", "user": ["Ben_UCPK2", "Neil_Blunden"], "date": "2026-09-03"},  # a list
    {"report_type": "daily", "user": "Neil_Blunden", "date": "2026-09-03"},  # not the key's folder
    {"report_type": "daily", "user": "Ben_UCPK2"},                         # no date
    {"report_type": "daily", "user": "Ben_UCPK2", "date": "yesterday"},    # not a date
    {"report_type": "weekly", "user": "Ben_UCPK2", "end_date": "2026-09-06"},  # half a period
    {"report_type": "summary", "user": "Ben_UCPK2", "date": "2026-09-03"},  # not regenerable
    {"report_type": "daily", "user": "../Neil_Blunden", "date": "2026-09-03"},  # a path
])
def test_a_bad_request_generates_nothing(handler, artifact):
    """Falls through to nothing -- never to the schedule defaults, which are
    yesterday, every user, and the backfill."""
    calls, artifacts = handler
    if artifact is not None:
        artifacts[KEY] = artifact
    rg.lambda_handler(_event(), None)
    assert _nothing_generated(calls), (artifact, calls)


@pytest.mark.parametrize("key", [
    "reports/2026-09-03/Ben_UCPK2/daily_report.json",   # some other prefix
    "report_requests/req-123.json",                      # no folder segment
    "report_requests/Ben_UCPK2/nested/req.json",         # extra depth
])
def test_a_key_outside_the_request_shape_generates_nothing(handler, key):
    calls, artifacts = handler
    artifacts[key] = {"report_type": "daily", "user": "Ben_UCPK2", "date": "2026-09-03"}
    rg.lambda_handler(_event(key), None)
    assert _nothing_generated(calls), key


def test_the_schedule_path_is_unchanged(handler):
    """No Records: the EventBridge schedule payload still runs as before."""
    calls, _ = handler
    rg.lambda_handler({"report_type": "daily", "date": "2026-09-03"}, None)
    assert len(calls["daily"]) == 1
    assert calls["stale"] == 1, "the nightly run keeps its backfill"


# ---- a personal weekly is built from that person only ---------------------------

@pytest.fixture
def periodic(monkeypatch):
    reads, dailies = [], {}
    s3 = _CapturingS3()
    monkeypatch.setattr(rg, "S3_BUCKET", "b")
    monkeypatch.setattr(rg, "s3_client", s3)
    monkeypatch.setattr(rg, "load_prompt_templates", lambda b: None)
    monkeypatch.setattr(rg, "get_user_site_mapping",
                        lambda b: ({"Ben_UCPK2": "site-1"}, {"Ben_UCPK2": ["site-1"]},
                                   {"Ben_UCPK2": "site_manager"}, {"site-1": {"name": "UC PK"}}))

    def _download(bucket, key):
        reads.append(key)
        return dailies.get(key)

    monkeypatch.setattr(rg, "download_json_from_s3", _download)
    listed = []
    monkeypatch.setattr(rg, "list_s3_objects",
                        lambda b, prefix: listed.append(prefix) or [])
    monkeypatch.setattr(rg, "build_weekly_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(rg, "build_monthly_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(rg, "call_claude_structured", lambda p, max_tokens=None: ('{"summary": "ok"}', None))
    monkeypatch.setattr(rg, "generate_word_document", lambda report, title: None)
    monkeypatch.setattr(rg, "write_report_to_dynamodb", lambda *a, **k: None)
    monkeypatch.setattr(rg, "write_audit_entry", lambda *a, **k: None)
    return s3, reads, listed, dailies


def test_a_personal_weekly_writes_only_that_persons_report(periodic):
    s3, reads, listed, dailies = periodic
    dailies["reports/2026-09-01/Ben_UCPK2/daily_report.json"] = {"user_name": "Ben_UCPK2", "report_date": "2026-09-01"}
    rg.generate_periodic_report("weekly", "2026-08-31", "2026-09-06",
                                user="Ben_UCPK2", triggered_by="x@y.z")
    assert s3.puts == ["reports/2026-09-06/Ben_UCPK2/weekly_report.json"], s3.puts


def test_a_personal_weekly_never_borrows_the_company_summary(periodic):
    """A day with no personal daily must contribute nothing, not the whole
    company's day. Borrowing it would put other people's content in 'my' report."""
    s3, reads, listed, dailies = periodic
    dailies["reports/2026-09-01/Ben_UCPK2/daily_report.json"] = {"user_name": "Ben_UCPK2"}
    dailies["reports/2026-09-02/summary_report.json"] = {"executive_summary": "someone else's day"}
    rg.generate_periodic_report("weekly", "2026-08-31", "2026-09-06",
                                user="Ben_UCPK2", triggered_by="x@y.z")
    assert not any(k.endswith("summary_report.json") for k in reads), reads


def test_a_personal_weekly_reads_only_that_persons_folder(periodic):
    s3, reads, listed, dailies = periodic
    rg.generate_periodic_report("weekly", "2026-08-31", "2026-09-06",
                                user="Ben_UCPK2", triggered_by="x@y.z")
    assert all("/Ben_UCPK2/" in k for k in reads), reads
    assert listed == [], "no whole-prefix listing of other people's reports"


def test_a_personal_report_records_who_asked(periodic):
    s3, reads, listed, dailies = periodic
    dailies["reports/2026-09-01/Ben_UCPK2/daily_report.json"] = {"user_name": "Ben_UCPK2"}
    captured = {}
    rg.s3_client.put_object = lambda **kw: captured.setdefault(kw["Key"], json.loads(kw["Body"]))
    rg.generate_periodic_report("weekly", "2026-08-31", "2026-09-06",
                                user="Ben_UCPK2", triggered_by="x@y.z")
    body = captured["reports/2026-09-06/Ben_UCPK2/weekly_report.json"]
    assert body["_report_metadata"]["generated_by"] == "x@y.z"


def test_a_personal_monthly_does_not_list_every_weekly_in_the_bucket(periodic):
    s3, reads, listed, dailies = periodic
    dailies["reports/2026-09-01/Ben_UCPK2/daily_report.json"] = {"user_name": "Ben_UCPK2"}
    rg.generate_periodic_report("monthly", "2026-09-01", "2026-09-30",
                                user="Ben_UCPK2", triggered_by="x@y.z")
    assert listed == []
    assert s3.puts == ["reports/2026-09-30/Ben_UCPK2/monthly_report.json"], s3.puts
