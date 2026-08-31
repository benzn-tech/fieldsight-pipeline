"""The metric mode: ACL, dispatch, and the three zeros.

What is asserted here is the ROUTING and the SHAPE. The SQL is exercised against
a real database in tests/integration/test_metric_queries.py -- a fake connection
records SQL without parsing it, and asserting on the string it recorded would
prove only that a string was passed.
"""
import pytest

rag = pytest.importorskip("lambda_rag_search",
                          reason="requires psycopg (installed in CI)")


class FakeConn:
    pass


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "a@x.nz", "first_name": "A", "last_name": "B",
          "global_role": "pm"}

STATS = {"sessions": 3, "duration_s": 4620, "unmeasured": 0,
         "unattributed": 0, "photos": 7}
COUNTS = {"count": 4, "unlabelled": 0, "null_author": 0, "from_fallback": 0}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag, "close_cached_connection", lambda *a, **k: None)
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    monkeypatch.setattr(rag.recordings, "range_stats", lambda *a, **k: dict(STATS))
    monkeypatch.setattr(rag.findings, "count_by_domain", lambda *a, **k: dict(COUNTS))
    monkeypatch.setattr(rag.redactions, "deleted_session_bases", lambda *a, **k: set())
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: False)
    return monkeypatch


def ev(**kw):
    e = {"mode": "metric", "sub": "sub-1", "metric": "duration",
         "date_from": "2026-08-30", "date_to": "2026-08-30"}
    e.update(kw)
    return e


def test_duration_comes_back_as_a_number_and_a_unit(wired):
    out = rag.lambda_handler(ev(), None)
    assert out["metric"] == "duration"
    assert out["value"] == 4620
    assert out["unit"] == "seconds"
    assert out["from"] == "2026-08-30"


def test_the_metric_route_never_asks_for_an_embedding(wired):
    """The chunk path returns `{"error": "missing sub or query_embedding"}` when
    there is none. A metric question carries none and must not be refused for
    lacking one."""
    out = rag.lambda_handler(ev(), None)
    assert "error" not in out
    assert "chunks" not in out


def test_no_model_is_ever_called(wired, monkeypatch):
    """The rule the whole route exists to enforce. rag-search has never had an
    LLM and must not gain one here -- a model asked how long you recorded says
    'about two hours' with the fluency of a fact."""
    import sys
    monkeypatch.setitem(sys.modules, "llm_utils", None)
    monkeypatch.setitem(sys.modules, "dashscope_utils", None)
    assert rag.lambda_handler(ev(), None)["value"] == 4620


def test_photos_are_a_different_metric_from_sessions(wired):
    assert rag.lambda_handler(ev(metric="count_photos"), None)["value"] == 7
    assert rag.lambda_handler(ev(metric="count_sessions"), None)["value"] == 3


def test_a_findings_metric_reaches_the_other_repository(wired):
    assert rag.lambda_handler(ev(metric="count_findings_safety"), None)["value"] == 4
    assert rag.lambda_handler(ev(metric="count_findings_quality"), None)["value"] == 4


def test_the_domain_is_taken_from_the_metric_name(wired, monkeypatch):
    """`count_findings_safety` must ask for 'safety', not for the whole metric
    name. Passing the name through would match no domain and answer 0 for
    every findings question, which looks exactly like a quiet site."""
    seen = {}

    def spy(conn, company_id, domain, *a, **k):
        seen["domain"] = domain
        return dict(COUNTS)

    monkeypatch.setattr(rag.findings, "count_by_domain", spy)
    rag.lambda_handler(ev(metric="count_findings_quality"), None)
    assert seen["domain"] == "quality"


def test_an_unknown_metric_is_refused_rather_than_guessed(wired):
    out = rag.lambda_handler(ev(metric="astrology"), None)
    assert out.get("error")
    assert "value" not in out


def test_a_missing_date_range_is_refused(wired):
    out = rag.lambda_handler(ev(date_from=None, date_to=None), None)
    assert out.get("error")
    assert "value" not in out


def test_the_caller_cannot_name_the_sites_or_the_authors(wired, monkeypatch):
    """The ACL comes from `sub` through `visible_scope`, never from the event.
    An event-supplied site or author list would be an ACL bypass wearing a
    parameter -- the caller naming whose recordings to count."""
    seen = {}

    def spy(conn, company_id, date_from, date_to, site_ids, **k):
        seen["sites"] = list(site_ids)
        seen["authors"] = k.get("author_ids")
        return dict(STATS)

    monkeypatch.setattr(rag.recordings, "range_stats", spy)
    rag.lambda_handler(ev(site_ids=["evil"], author_ids=["evil"], folders=["Ben"]), None)
    assert seen["sites"] == ["s-1"]
    assert seen["authors"] is None


def test_a_graded_caller_carries_their_author_filter_through(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": {"u-1"}})
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: seen.update(k) or dict(STATS))
    out = rag.lambda_handler(ev(), None)
    assert seen["author_ids"] == ["u-1"]
    assert out["scope"]["authors"] == 1


def test_an_unprovisioned_caller_gets_no_number(wired, monkeypatch):
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: None)
    out = rag.lambda_handler(ev(), None)
    assert out.get("error")
    assert "value" not in out


# ---- the three zeros ------------------------------------------------------

def test_zero_because_nothing_was_recorded(wired, monkeypatch):
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=0))
    out = rag.lambda_handler(ev(), None)
    assert out["value"] == 0
    assert out["notes"]["zero_kind"] == "nothing_recorded"


def test_zero_because_the_caller_can_see_nothing(wired, monkeypatch):
    """Not an answer about recordings -- an answer about the account. Reporting
    it as "you recorded nothing" tells a locked-out user a fact about the site
    instead of a fact about their access."""
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": set(), "author_ids": None})
    out = rag.lambda_handler(ev(), None)
    assert out["value"] == 0
    assert out["notes"]["zero_kind"] == "nothing_visible"


def test_zero_because_that_day_has_no_rows_at_all(wired, monkeypatch):
    """The third zero, and the one the shipped KPI deliberately refuses to report
    as zero: the RealPTT path never registers `recordings` rows, days before
    migration 0009 have none, lake-fed environments have none. Measured at 15.4%
    of days that carry topics."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=0))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)
    out = rag.lambda_handler(ev(), None)
    assert out["notes"]["zero_kind"] == "no_rows_for_that_day"


def test_no_safety_issues_is_not_the_same_as_a_quiet_day(wired, monkeypatch):
    """"No safety issues yesterday" is a real and welcome answer. "Nothing
    reached the system yesterday" is a different one, and delivering them in the
    same words tells a manager the site was clean when it was silent."""
    monkeypatch.setattr(rag.findings, "count_by_domain",
                        lambda *a, **k: dict(COUNTS, count=0))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)
    assert rag.lambda_handler(ev(metric="count_findings_safety"),
                              None)["notes"]["zero_kind"] == "none_in_domain"

    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: False)
    assert rag.lambda_handler(ev(metric="count_findings_safety"),
                              None)["notes"]["zero_kind"] == "no_topics"


def test_a_non_zero_answer_carries_no_zero_kind(wired):
    assert "zero_kind" not in rag.lambda_handler(ev(), None)["notes"]


# ---- what the number does not include -------------------------------------

def test_a_short_total_arrives_with_its_reason(wired, monkeypatch):
    """`unmeasured` and `unattributed` are only present when non-zero, so a clean
    answer stays clean; when they are present the number is short and says so."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, unmeasured=2, unattributed=28))
    notes = rag.lambda_handler(ev(), None)["notes"]
    assert notes["unmeasured"] == 2
    assert notes["unattributed"] == 28


def test_a_clean_answer_carries_an_empty_notes(wired):
    assert rag.lambda_handler(ev(), None)["notes"] == {}


def test_findings_caveats_are_carried_too(wired, monkeypatch):
    monkeypatch.setattr(rag.findings, "count_by_domain",
                        lambda *a, **k: dict(COUNTS, null_author=3, from_fallback=5))
    notes = rag.lambda_handler(ev(metric="count_findings_safety"), None)["notes"]
    assert notes["null_author"] == 3
    assert notes["from_fallback"] == 5


def test_deleted_sessions_are_excluded_and_come_from_aurora(wired, monkeypatch):
    """From the database, not the S3 mirror. The mirror exists for the non-VPC
    readers that hold no connection; rag-search holds one and is inside the VPC
    with no egress, where an S3 read black-holes until timeout (BUG-36) rather
    than failing."""
    seen = {}
    monkeypatch.setattr(rag.redactions, "deleted_session_bases",
                        lambda *a, **k: {"sid" + "a" * 32})
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: seen.update(k) or dict(STATS))
    rag.lambda_handler(ev(), None)
    assert "sid" + "a" * 32 in (seen.get("deleted_bases") or set())


def test_rag_search_still_holds_no_s3_client(wired):
    """The structural half of the rule above. A future edit that reaches for
    boto3 here would pass every behavioural test in this file."""
    import inspect
    src = inspect.getsource(rag)
    assert "boto3" not in src
    assert "deletion_mirror" not in src


def test_the_chunk_path_is_untouched_by_the_new_branch(wired):
    """`mode` is absent on every shipped Ask and Search call, and absent must
    keep meaning the chunk path."""
    out = rag.lambda_handler({"sub": "sub-1"}, None)
    assert out.get("error") == "missing sub or query_embedding"
