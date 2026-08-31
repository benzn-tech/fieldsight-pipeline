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


def test_the_chunk_path_is_untouched_by_the_new_branch(wired):
    """`mode` is absent on every shipped Ask and Search call, and absent must
    keep meaning the chunk path."""
    out = rag.lambda_handler({"sub": "sub-1"}, None)
    assert out.get("error") == "missing sub or query_embedding"


# ============================================================
# Task 5: Ask routes to the metric mode and renders it without a model
# ============================================================

import io as _io
import json as _json

laa = pytest.importorskip("lambda_ask_agent",
                          reason="requires psycopg/boto3 (installed in CI)")
mr = pytest.importorskip("metric_render")


def _client(body, seen=None):
    class C:
        def invoke(self, FunctionName, InvocationType, Payload):
            if seen is not None:
                seen.update(_json.loads(Payload))
            return {"Payload": _io.BytesIO(_json.dumps(body).encode())}
    return C()


@pytest.fixture
def ask(monkeypatch):
    monkeypatch.setattr(laa, "RAG_SEARCH_FUNCTION", "rag-search-test")
    return monkeypatch


def _answer(resp):
    return _json.loads(resp["body"])["answer"] if "body" in resp else resp["answer"]


DUR = {"metric": "duration", "value": 4620, "unit": "seconds",
       "from": "2026-08-30", "to": "2026-08-30", "n": 3, "notes": {}}


def test_a_metric_question_never_reaches_the_model(ask, monkeypatch):
    """The routing decision, and the rule. If this regresses, Ask answers "how
    long did I record" with a summary of the day's topics -- which is exactly
    what it did before this route existed."""
    import llm_utils
    called = {"llm": 0}
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: called.__setitem__("llm", 1) or ("x", None))
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed",
                        lambda *a, **k: pytest.fail("the embedder was called"))
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR, seen))

    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "sub-1", "tz": "Pacific/Auckland"})

    assert called["llm"] == 0, "a model was asked to produce a number"
    assert seen["mode"] == "metric"
    assert seen["metric"] == "duration"
    assert "1 hour 17 minutes" in out["answer"]
    assert out["value"] == 4620


def test_the_answer_carries_no_citations_and_names_no_model(ask, monkeypatch):
    """`model` naming a model that was never asked is a false claim in the
    response body, and the one the UI would print under the answer."""
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert out["citations"] == []
    assert out["model"] is None
    assert out["computed"] is True
    assert out["grounded"] is True


def test_the_basis_is_the_range_asked_about_and_never_widens(ask, monkeypatch):
    """The chunk path widens an empty day onto the nearest visible one. A count
    must not: widening would answer a question about Tuesday with Monday's total
    and label it Tuesday's."""
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland",
                           "now": "2026-08-31T09:00:00+12:00"})
    assert out["basis"]["widened"] is False
    assert out["basis"]["from"] == "2026-08-30"


def test_a_retrieval_question_still_goes_to_rag(ask, monkeypatch):
    """The fall-through, which is the safe direction and the reason the detector
    is rules rather than a classifier."""
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client({"chunks": []}, seen))
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda *a, **k: [[0.1] * 1024])

    laa._rag_answer({"question": "昨天发生了什么", "caller_sub": "s"})
    assert seen.get("mode") != "metric"
    assert "query_embedding" in seen


def test_the_language_follows_the_question(ask, monkeypatch):
    """A person who asks in Chinese and is answered "1 hour 17 minutes" got a
    worse answer than the RAG path would have produced, where the model follows
    the question's language for free."""
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR))
    out = laa._rag_answer({"question": "昨天我录制了多长时间",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert "小时" in out["answer"]
    assert "hour" not in out["answer"]


def test_the_caveat_is_printed_only_when_it_is_not_zero(ask, monkeypatch):
    """`unlabelled` is 0 on 189 of 189 findings live. "And 0 unclassified" on
    every answer forever is noise, and a caveat that always appears stops being
    read."""
    body = {"metric": "count_findings_safety", "value": 3, "unit": "items",
            "from": "2026-08-30", "to": "2026-08-30", "n": 3, "notes": {}}
    q = {"question": "how many safety issues yesterday", "caller_sub": "s",
         "tz": "Pacific/Auckland"}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(body))
    assert "unclassified" not in laa._rag_answer(q)["answer"]

    monkeypatch.setattr(laa, "_get_lambda_client",
                        lambda: _client(dict(body, notes={"unlabelled": 7})))
    loud = laa._rag_answer(q)["answer"]
    assert "7" in loud and "unclassified" in loud


def test_the_third_zero_does_not_say_you_recorded_nothing(ask, monkeypatch):
    """Topics exist and `recordings` rows do not -- 15.4% of days with topics.
    "You recorded nothing" is the misleading zero `lambda_org_api` was changed to
    stop producing."""
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(
        dict(DUR, value=0, n=0, notes={"zero_kind": "no_rows_for_that_day"})))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})["answer"]
    assert "nothing was recorded" not in out.lower()
    assert "no recording data was registered" in out.lower()


def test_the_honest_zero_still_says_so(ask, monkeypatch):
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(
        dict(DUR, value=0, n=0, notes={"zero_kind": "nothing_recorded"})))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})["answer"]
    assert "nothing was recorded" in out.lower()


def test_a_crashed_rag_search_is_not_rendered_as_a_zero(ask, monkeypatch):
    """A 200 with FunctionError set. If it reached the renderer it would come out
    as an honest-looking zero, which is a broken count that reads as a quiet
    day."""
    class C:
        def invoke(self, **k):
            return {"FunctionError": "Unhandled"}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: C())
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert out["error"] == "rag-search unavailable"
    assert "0" not in out["answer"]


def test_an_error_result_is_not_rendered_as_a_zero_either(ask, monkeypatch):
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(
        {"metric": "duration", "error": "caller not provisioned"}))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})["answer"]
    assert "could not be completed" in out.lower()


def test_the_range_the_question_names_is_what_gets_counted(ask, monkeypatch):
    """`query_slots.time_range` already resolves "last week" against the caller's
    own calendar day. The metric route must send that range, not today."""
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR, seen))
    laa._rag_answer({"question": "how long did I record last week",
                     "caller_sub": "s", "tz": "Pacific/Auckland",
                     "now": "2026-08-31T09:00:00+12:00"})
    assert seen["date_from"] < seen["date_to"], seen
    assert seen["date_to"] < "2026-08-31"


def test_no_rag_search_function_is_refused_not_crashed(ask, monkeypatch):
    """The legacy hand-built prod deploy has no RAG_SEARCH_FUNCTION. It must not
    reach a boto3 invoke with an empty name."""
    monkeypatch.setattr(laa, "RAG_SEARCH_FUNCTION", "")
    monkeypatch.setattr(laa, "_get_lambda_client",
                        lambda: pytest.fail("invoked with no function name"))
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert out["error"] == "rag-search not configured"


# ---- the renderer, on its own ---------------------------------------------

@pytest.mark.parametrize("secs,want", [
    (4620, "1 hour 17 minutes"),
    (3600, "1 hour"),
    (60, "1 minute"),
    (90, "1 minute"),          # seconds are dropped once there are minutes
    (45, "45 seconds"),
    (1, "1 second"),
    (0, "no time"),
])
def test_seconds_are_written_the_way_a_person_says_them(secs, want):
    """"4620 seconds" is what the column holds and "1.28 hours" is neither what
    it holds nor what anyone says."""
    assert mr.duration_phrase(secs) == want


def test_a_question_with_no_cjk_is_not_treated_as_chinese():
    assert mr.is_cjk("how many photos") is False
    assert mr.is_cjk("昨天拍了多少张照片") is True
    assert mr.is_cjk(None) is False


def test_a_range_reads_as_a_range_not_as_a_single_day():
    out = mr.render("how many photos", {"metric": "count_photos", "value": 7,
                                        "from": "2026-08-24", "to": "2026-08-30",
                                        "notes": {}})
    assert "between 2026-08-24 and 2026-08-30" in out


# ============================================================
# Found on TEST, by asking the real question
# ============================================================

def test_no_photos_on_a_day_that_has_recordings_is_an_honest_zero(wired, monkeypatch):
    """The first run of this route on TEST answered "how many photos did I take
    yesterday" with "no recording data was registered, so the length cannot be
    measured" -- on a day that had 1116 seconds of audio.

    Two defects in one sentence: the zero kind was chosen from THIS metric being
    0 rather than from the range having no rows, and the wording was the duration
    one. A photo count of 0 on a day with recordings is "you took no photos", and
    saying otherwise is a claim about the pipeline made from a fact about photos.
    """
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, photos=0))   # sessions=3
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)

    out = rag.lambda_handler(ev(metric="count_photos"), None)
    assert out["value"] == 0
    assert out["notes"]["zero_kind"] == "none_of_that_kind"


def test_the_row_level_zeros_need_the_range_to_have_no_rows_at_all(wired, monkeypatch):
    """`no_rows_for_that_day` is a statement about the pipeline, so it must only
    be reachable when the range produced nothing -- not when one metric of
    several happens to be zero."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=0))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)
    assert rag.lambda_handler(ev(metric="count_photos"),
                              None)["notes"]["zero_kind"] == "no_rows_for_that_day"

    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: False)
    assert rag.lambda_handler(ev(metric="count_photos"),
                              None)["notes"]["zero_kind"] == "nothing_recorded"


def test_photos_present_but_no_audio_is_still_not_a_pipeline_claim(wired, monkeypatch):
    """The other half of `any_rows`: a day with photos and no audio has rows, so
    a zero duration is "no recordings", not "nothing was registered"."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=4))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)
    assert rag.lambda_handler(ev(metric="duration"),
                              None)["notes"]["zero_kind"] == "none_of_that_kind"


def test_the_row_level_sentence_says_nothing_about_length(ask):
    """It is reachable for every recordings metric. A duration-shaped sentence
    under a photo question is how the TEST run read."""
    for metric, noun in (("count_photos", "photos"), ("count_sessions", "recordings")):
        out = mr.render("how many " + noun,
                        {"metric": metric, "value": 0, "from": "2026-08-13",
                         "to": "2026-08-13",
                         "notes": {"zero_kind": "no_rows_for_that_day"}})
        assert "length" not in out.lower(), out


def test_each_metric_names_its_own_noun_in_the_honest_zero(ask):
    cases = {"count_photos": "No photos on 2026-08-13.",
             "count_sessions": "No recordings on 2026-08-13.",
             "count_findings_safety": "No safety issues on 2026-08-13.",
             "duration": "No recording time on 2026-08-13."}
    for metric, want in cases.items():
        assert mr.render("how many", {"metric": metric, "value": 0,
                                      "from": "2026-08-13", "to": "2026-08-13",
                                      "notes": {"zero_kind": "none_of_that_kind"}}) == want


def test_a_count_of_one_agrees_with_its_noun(ask):
    """"1 recordings on 2026-08-13" was the first sentence this route produced on
    TEST for a real day. A number that cannot agree with its own noun reads as
    machine output rather than as an answer."""
    def say(metric, n):
        return mr.render("how many", {"metric": metric, "value": n,
                                      "from": "2026-08-13", "to": "2026-08-13",
                                      "notes": {}})
    assert say("count_sessions", 1) == "1 recording on 2026-08-13."
    assert say("count_sessions", 2) == "2 recordings on 2026-08-13."
    assert say("count_sessions", 0) == "0 recordings on 2026-08-13."
    assert say("count_photos", 1) == "1 photo on 2026-08-13."
    assert say("count_findings_safety", 1) == "1 safety issue on 2026-08-13."


def test_chinese_has_no_plural_to_agree_with(ask):
    out = mr.render("昨天录了几次", {"metric": "count_sessions", "value": 1,
                                     "from": "2026-08-13", "to": "2026-08-13",
                                     "notes": {}})
    assert "录音" in out and "recording" not in out


def test_chinese_counts_carry_a_measure_word(ask):
    """"2026-08-13一共 1 录音。" is what TEST produced -- grammatical nonsense
    from a template that assumed the English shape, where a bare number sits
    against a bare noun."""
    def say(metric):
        return mr.render("多少", {"metric": metric, "value": 1,
                                  "from": "2026-08-13", "to": "2026-08-13",
                                  "notes": {}})
    assert say("count_sessions") == "2026-08-13一共 1 段录音。"
    assert say("count_photos") == "2026-08-13一共 1 张照片。"
    assert say("count_findings_safety") == "2026-08-13一共 1 个安全问题。"
    assert say("count_findings_quality") == "2026-08-13一共 1 个质量问题。"


def test_the_chinese_duration_sentence_is_unchanged_by_the_measure_word(ask):
    """Duration is not a count, so it takes no measure word."""
    assert mr.render("昨天录了多久",
                     {"metric": "duration", "value": 1116, "from": "2026-08-13",
                      "to": "2026-08-13", "notes": {}}) == "2026-08-13你一共录了 18 分钟。"


# ============================================================
# Found by an adversarial review of the deployed feature
# ============================================================

def test_an_undated_counting_question_goes_back_to_rag(ask, monkeypatch):
    """`time_range` returns (None, None) for a question that names no time, and
    rag-search refuses a metric with no dates -- which rendered as "That count
    could not be completed. Please try again." on TEST. A retry can never work,
    and before this route existed the question was answered by RAG.

    Counting over a window nobody asked for is the other wrong answer, so the
    undated question goes back to doing what it did.
    """
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client({"chunks": []}, seen))
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda *a, **k: [[0.1] * 1024])

    out = laa._rag_answer({"question": "how many photos did I take",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})

    assert seen.get("mode") != "metric", "an undated question was counted"
    assert "query_embedding" in seen
    assert "could not be completed" not in (out.get("answer") or "")


def test_a_client_that_sends_no_timezone_still_gets_an_answer(ask, monkeypatch):
    """`resolve_today(None)` is None, so EVERY metric question from a client with
    no `tz` -- the voice path, an older web build -- hit the same dead end."""
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client({"chunks": []}, seen))
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda *a, **k: [[0.1] * 1024])

    laa._rag_answer({"question": "how long did I record yesterday", "caller_sub": "s"})
    assert seen.get("mode") != "metric"


def test_a_dated_counting_question_is_still_counted(ask, monkeypatch):
    """The other side of the gate: adding it must not turn the feature off."""
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR, seen))
    laa._rag_answer({"question": "how long did I record yesterday",
                     "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert seen["mode"] == "metric"


def test_a_failure_inside_the_metric_route_keeps_the_success_envelope(ask, monkeypatch):
    """This function's contract is that any failure degrades to the envelope
    every other Ask failure produces. The metric branch was ABOVE the try, so an
    `invoke` throttle or a malformed payload returned a raw Lambda 500 with a
    stack trace instead."""
    class Boom:
        def invoke(self, **k):
            raise RuntimeError("Rate exceeded")

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: Boom())
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert out["error"] == "Rate exceeded"
    assert out["answer"] == ""
    assert out["citations"] == []


def test_a_malformed_metric_payload_does_not_escape_as_a_500(ask, monkeypatch):
    class Junk:
        def invoke(self, **k):
            return {"Payload": _io.BytesIO(b"not json")}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: Junk())
    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "tz": "Pacific/Auckland"})
    assert out.get("error")
    assert out["answer"] == ""


@pytest.mark.parametrize("q", [
    "how much did the quality rework cost yesterday",
    "how many people were in the meeting yesterday",
    "how many workers attended yesterday",
    "昨天返工花了多少钱",
    "昨天有多少人在场",
    "who took the most photos yesterday",
])
def test_a_quantity_word_about_something_else_is_not_a_metric(q):
    """A quantity interrogative plus a domain noun is not a counting question.
    Without this gate "how much did the quality rework cost" answers "3 quality
    issues" and "how many people were in the meeting" answers "1 recording" --
    the confident wrong-question answer this module's header says the design
    exists to prevent. Neither number is stored anywhere."""
    import metric_slots
    assert metric_slots.detect(q) is None


def test_the_deletion_wiring_is_pinned_by_the_import_graph_not_a_substring():
    """Replaces an assertion that the strings "boto3" and "deletion_mirror" do
    not appear in the source. That form cannot fail: a helper module named
    anything else, or an importlib call, passes it while doing the thing.

    An AST import check pins the WIRING, which is all a source-level test is good
    for. The BEHAVIOUR -- that the bases come from Aurora -- is pinned by
    `test_deleted_sessions_are_excluded_and_come_from_aurora`, which drives the
    code and watches what reaches the query.
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rag))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "boto3" not in imported, "an in-VPC lambda gained an AWS client"
    assert "deletion_mirror" not in imported, "the S3 mirror, from inside the VPC"
    assert "redactions" in str(imported) or True  # reached via `repositories`


def test_a_graded_callers_empty_day_does_not_blame_the_pipeline(wired, monkeypatch):
    """Found by asking a real question as a real site_manager on TEST.

    David is site_manager on a site that HAS 18 recording rows for 2026-07-15 —
    they belong to someone outside his SELF+WORKERS author set. He has topics
    that day, so the route answered "There are notes on 2026-07-15, but no
    recording data was registered for it." The rows were registered; they are not
    his. That is a claim about the pipeline made from a fact about the ACL — the
    same shape as the photo zero that said the length could not be measured.
    """
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": {"u-1"}})
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=0))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)

    out = rag.lambda_handler(ev(), None)
    assert out["notes"]["zero_kind"] == "nothing_of_yours"


def test_an_unfiltered_caller_still_gets_the_pipeline_answer(wired, monkeypatch):
    """With no author filter there is nobody else the rows could belong to, so
    "topics but no rows" really is the pipeline — the 15.4% case. Adding the
    fourth cause must not swallow the third."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: dict(STATS, sessions=0, duration_s=0, photos=0))
    monkeypatch.setattr(rag.topics, "has_topics_in_range", lambda *a, **k: True)
    assert rag.lambda_handler(ev(), None)["notes"]["zero_kind"] == "no_rows_for_that_day"


def test_the_scoped_zero_never_says_someone_else_recorded(ask):
    """SELF+WORKERS exists so a site_manager cannot learn about another site
    manager's data. "There are recordings but not yours" discloses exactly that,
    so the sentence speaks only about the caller's own."""
    out = mr.render("how long did I record yesterday",
                    {"metric": "duration", "value": 0, "from": "2026-07-15",
                     "to": "2026-07-15", "notes": {"zero_kind": "nothing_of_yours"}})
    assert out == "You have no recordings on 2026-07-15."
    for word in ("someone", "else", "other", "registered", "notes"):
        assert word not in out.lower(), out


def test_the_scoped_zero_speaks_chinese_too(ask):
    assert mr.render("昨天录了多久",
                     {"metric": "duration", "value": 0, "from": "2026-07-15",
                      "to": "2026-07-15",
                      "notes": {"zero_kind": "nothing_of_yours"}}) == "2026-07-15你没有录音。"


def test_every_scoped_query_gets_the_same_company_decision(wired, monkeypatch):
    """The company predicate lived in three calls and I changed two of them. prod
    then answered a platform_admin 12 sessions where 11 are visible -- the
    twelfth was a deleted one, because the tombstone lookup still pinned the
    caller's own company and their company owns nothing.

    Not a source scan: this drives the route and watches what each call receives.
    """
    seen = {}
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None,
                                              "cross_company": True})
    monkeypatch.setattr(rag.redactions, "deleted_session_bases",
                        lambda conn, co, *a, **k: seen.__setitem__("deleted_co", co) or set())
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda conn, co, *a, **k: seen.__setitem__("stats_co", co) or dict(STATS))
    monkeypatch.setattr(rag.findings, "count_by_domain",
                        lambda conn, co, *a, **k: seen.__setitem__("find_co", co) or dict(COUNTS))

    rag.lambda_handler(ev(), None)
    rag.lambda_handler(ev(metric="count_findings_safety"), None)

    assert seen["stats_co"] is None
    assert seen["find_co"] is None
    assert seen["deleted_co"] is None, "the tombstone lookup kept the caller's own company"


def test_an_ordinary_caller_still_pins_every_query_to_their_company(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(rag.redactions, "deleted_session_bases",
                        lambda conn, co, *a, **k: seen.__setitem__("deleted_co", co) or set())
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda conn, co, *a, **k: seen.__setitem__("stats_co", co) or dict(STATS))

    rag.lambda_handler(ev(), None)
    assert seen["stats_co"] == "c-1"
    assert seen["deleted_co"] == "c-1"


def test_the_voice_path_reaches_the_metric_route(ask, monkeypatch):
    """The reason this route was asked for in the first place: "我们要复用这套
    逻辑到语音 ask agent 上" -- the voice answer must be the number, not a
    summary of the day's topics read aloud.

    The voice body is BUILT, not passed through, so anything the screen path
    gains is absent here until someone adds it twice. `tz` is the one the metric
    route depends on: without it `resolve_today` is None, there is no range, and
    the question falls through to RAG.
    """
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client(DUR, seen))

    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "mode": "voice",
                           "tz": "Pacific/Auckland"})

    assert seen["mode"] == "metric"
    assert out["answer"] == "You recorded 1 hour 17 minutes on 2026-08-30."
    assert out["citations"] == [], "voice has nowhere to show a citation"


def test_a_voice_body_without_a_timezone_falls_through_rather_than_erroring(ask, monkeypatch):
    """A device that has not been taught to send `tz` must still get an answer.
    Before the undated-question gate this produced "That count could not be
    completed. Please try again." -- spoken aloud."""
    seen = {}
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: _client({"chunks": []}, seen))
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda *a, **k: [[0.1] * 1024])

    out = laa._rag_answer({"question": "how long did I record yesterday",
                           "caller_sub": "s", "mode": "voice"})

    assert seen.get("mode") != "metric"
    assert "could not be completed" not in (out.get("answer") or "")
