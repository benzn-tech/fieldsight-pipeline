"""action_pack_web: query discipline (S5), the web_search wrapper that drops unverified
answers (D6) and the page snapshot (§3.2, §6) -- spec 2026-11-01 background action agent,
Task 9. general_answer, the fetcher and S3 are fakes: no network, no model."""
import datetime as dt

import pytest

import action_pack_web as apw
import action_pack_view
from ask_tools import RefAssigner

CO = "c0000000-0000-0000-0000-000000000001"
PACK = "33333333-3333-3333-3333-333333333333"
PLAN = [{"id": "q1", "text": "  NZS 3604 bracing requirements for wind zone high  "},
        {"id": "q2", "text": "BRANZ guidance fire door hardware"}]


def verified(answer="Bracing per NZS 3604 [1]. Also see [2,3].", n_sources=2):
    return {"answer": answer, "status": "verified", "kind": "general",
            "sources": [{"url": f"https://s{i}.example/p", "title": f"T{i}",
                         "domain": f"s{i}.example"} for i in range(n_sources)],
            "searched": True, "refused": None, "timed_out": False, "failed": False,
            "conflicts": [], "_trace": {"ms": {}}}


class FakeGeneral:
    def __init__(self, block=None, exc=None):
        self.calls = []
        self.block, self.exc = block, exc

    def __call__(self, question, budget, **kw):
        self.calls.append({"question": question, "budget": budget, **kw})
        if self.exc:
            raise self.exc
        return self.block if self.block is not None else verified()


# ------------------------------------------------------------- query discipline

def test_plan_queries_are_stripped_and_kept_by_id():
    q = apw.WebQueries.from_plan(PLAN)
    assert q.ids == ("q1", "q2")
    assert q.text_for("q1") == "NZS 3604 bracing requirements for wind zone high"


@pytest.mark.parametrize("plan", [None, []])
def test_no_planned_queries_means_no_web(plan):
    q = apw.WebQueries.from_plan(plan)
    assert q.ids == ()


@pytest.mark.parametrize("plan,code", [
    ("q1", "plan_not_list"),
    ({"id": "q1", "text": "x"}, "plan_not_list"),
    ([{"id": "q1", "text": "a"}, {"id": "q2", "text": "b"}, {"id": "q3", "text": "c"},
      {"id": "q4", "text": "d"}], "too_many_queries"),
    (["NZS 3604"], "query_shape"),
    ([{"id": "q1"}], "query_text"),
    ([{"id": "q1", "text": "   "}], "query_text"),
    ([{"id": "q1", "text": "x" * 301}], "query_text"),
    ([{"id": "q1", "text": 7}], "query_text"),
    ([{"text": "x"}], "query_id"),
    ([{"id": "", "text": "x"}], "query_id"),
    ([{"id": "q 1", "text": "x"}], "query_id"),
    ([{"id": "q" * 17, "text": "x"}], "query_id"),
    ([{"id": 1, "text": "x"}], "query_id"),
    ([{"id": "q1", "text": "a"}, {"id": "q1", "text": "b"}], "duplicate_query_id"),
    ([{"id": "q1", "text": "a", "extra": 1}], "query_shape"),
])
def test_bad_plans_are_refused(plan, code):
    with pytest.raises(apw.WebQueryError) as exc:
        apw.WebQueries.from_plan(plan)
    assert exc.value.code == code


@pytest.mark.parametrize("args,kw", [
    (([("q1", "free text from a record")],), {}),
    (([("q1", "free text")],), {"_token": object()}),
    (([("q1", "free text")],), {"_token": None}),
])
def test_queries_cannot_be_built_around_the_plan(args, kw):
    with pytest.raises(TypeError):
        apw.WebQueries(*args, **kw)


def test_300_chars_is_the_cap_and_three_queries_the_most():
    plan = [{"id": f"q{i}", "text": "x" * 300} for i in (1, 2, 3)]
    assert apw.WebQueries.from_plan(plan).ids == ("q1", "q2", "q3")
    assert apw.MAX_QUERY_CHARS == 300 and apw.MAX_QUERIES == 3


@pytest.mark.parametrize("query_id,code", [
    ("NZS 3604 bracing requirements for wind zone high", "unknown_query_id"),
    ("q9", "unknown_query_id"),
    ({"id": "q1"}, "bad_query_id"),
    (["q1"], "bad_query_id"),
    (None, "bad_query_id"),
    (1, "bad_query_id"),
])
def test_web_search_refuses_anything_but_a_planned_id(query_id, code):
    general = FakeGeneral()
    trace = {}
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), query_id, refs=RefAssigner(),
                         budget=30, trace=trace, general_answer=general)
    assert env["items"] == [] and env["meta"]["error"] == code
    assert general.calls == []
    assert trace["web_refused"] == 1 and trace.get("web_searches", 0) == 0


def test_each_id_is_searched_at_most_once_per_run():
    general = FakeGeneral()
    q, refs, trace = apw.WebQueries.from_plan(PLAN), RefAssigner(), {}
    assert apw.web_search(q, "q1", refs=refs, budget=30, trace=trace,
                          general_answer=general)["items"]
    again = apw.web_search(q, "q1", refs=refs, budget=30, trace=trace,
                           general_answer=general)
    assert again["items"] == [] and again["meta"]["error"] == "already_searched"
    assert len(general.calls) == 1


def test_general_answer_receives_exactly_the_planned_texts_and_the_budget():
    general = FakeGeneral()
    q, refs, trace = apw.WebQueries.from_plan(PLAN), RefAssigner(), {}
    apw.web_search(q, "q2", refs=refs, budget=41.5, trace=trace, general_answer=general)
    apw.web_search(q, "q1", refs=refs, budget=12, trace=trace, general_answer=general)
    assert [c["question"] for c in general.calls] == [
        "BRANZ guidance fire door hardware",
        "NZS 3604 bracing requirements for wind zone high"]
    assert [c["budget"] for c in general.calls] == [41.5, 12]
    assert all(c.get("voice") is False and c.get("kind") == "general" for c in general.calls)
    assert trace["web_searches"] == 2


# ---------------------------------------------------------------- the wrapper

def test_verified_answer_becomes_ask_shaped_items():
    refs = RefAssigner()
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), "q1", refs=refs, budget=30,
                         trace={}, general_answer=FakeGeneral(verified(n_sources=6)))
    items = env["items"]
    assert items[0] == {"ref": "W1", "kind": "web_answer", "provenance": "web",
                        "text": "Bracing per NZS 3604. Also see.",
                        "sources": ["W2", "W3", "W4", "W5"]}       # its own source refs
    sources = items[1:]
    assert len(sources) == 4                                       # WEB_MAX_SOURCES
    assert sources[0] == {"ref": "W2", "kind": "web_source", "provenance": "web",
                          "title": "T0", "text": "s0.example", "source": "https://s0.example/p"}
    assert env["tool"] == "web_search" and env["meta"]["status"] == "verified"
    assert env["meta"]["query_id"] == "q1"


def test_answer_is_capped_at_1200_chars():
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), "q1", refs=RefAssigner(),
                         budget=30, trace={},
                         general_answer=FakeGeneral(verified(answer="y" * 5000)))
    assert len(env["items"][0]["text"]) == 1200


def test_two_queries_get_their_own_answer_refs_and_share_source_refs():
    refs = RefAssigner()
    q = apw.WebQueries.from_plan(PLAN)
    a = apw.web_search(q, "q1", refs=refs, budget=30, trace={},
                       general_answer=FakeGeneral(verified(n_sources=1)))
    b = apw.web_search(q, "q2", refs=refs, budget=30, trace={},
                       general_answer=FakeGeneral(verified(n_sources=2)))
    assert [i["ref"] for i in a["items"]] == ["W1", "W2"]
    # q2's answer is new; its first source is the same URL as q1's -> same ref.
    assert [i["ref"] for i in b["items"]] == ["W3", "W2", "W4"]


@pytest.mark.parametrize("status", ["unverified", "too_long", "failed", None, "VERIFIED"])
def test_an_answer_that_is_not_verified_is_dropped_with_its_sources(status):
    """D6: never citable, not even as weak. The block's sources came from the failed
    verify, so they go too."""
    block = verified()
    block["status"] = status
    trace = {}
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), "q1", refs=RefAssigner(),
                         budget=30, trace=trace, general_answer=FakeGeneral(block))
    assert env["items"] == []
    assert env["meta"]["status"] == "dropped_unverified"
    assert trace["web_unverified_dropped"] == 1 and trace["web_sources_dropped"] == 2


def test_verified_but_empty_answer_is_dropped():
    trace = {}
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), "q1", refs=RefAssigner(),
                         budget=30, trace=trace,
                         general_answer=FakeGeneral(verified(answer="  [1] ")))
    assert env["items"] == [] and trace["web_unverified_dropped"] == 1


def test_dropped_answer_consumes_no_ref():
    refs = RefAssigner()
    q = apw.WebQueries.from_plan(PLAN)
    bad = verified()
    bad["status"] = "unverified"
    apw.web_search(q, "q1", refs=refs, budget=30, trace={}, general_answer=FakeGeneral(bad))
    env = apw.web_search(q, "q2", refs=refs, budget=30, trace={},
                         general_answer=FakeGeneral(verified(n_sources=0)))
    assert env["items"][0]["ref"] == "W1"


def test_general_answer_raising_is_a_failed_search_not_a_raise():
    trace = {}
    env = apw.web_search(apw.WebQueries.from_plan(PLAN), "q1", refs=RefAssigner(),
                         budget=30, trace=trace,
                         general_answer=FakeGeneral(exc=RuntimeError("boom")))
    assert env["items"] == [] and env["meta"]["status"] == "failed"
    assert trace["web_failed"] == 1


def test_logs_never_carry_query_or_answer_text(caplog):
    caplog.set_level("DEBUG")
    q = apw.WebQueries.from_plan(PLAN)
    apw.web_search(q, "q1", refs=RefAssigner(), budget=30, trace={},
                   general_answer=FakeGeneral())
    apw.web_search(q, "free text query SECRETQ", refs=RefAssigner(), budget=30, trace={},
                   general_answer=FakeGeneral())
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "NZS" not in joined and "Bracing" not in joined and "SECRETQ" not in joined


# ------------------------------------------------------------------- snapshot

class FakeS3:
    def __init__(self, fail=False):
        self.puts, self.fail = [], fail

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(kw)


class FakeFetcher:
    def __init__(self, result):
        self.result, self.urls = result, []

    def fetch(self, url):
        self.urls.append(url)
        return dict(self.result, url=url)


OK_FETCH = {"ok": True, "final_url": "https://s0.example/p", "status": 200,
            "text": "Page text é", "truncated": False, "reason": None,
            "fetched_at": "2026-11-01T01:02:03+00:00"}
WEB_SOURCE = {"ref": "W2", "kind": "web_source", "provenance": "web", "title": "T0",
              "text": "s0.example", "source": "https://s0.example/p"}


def test_snapshot_key_shape():
    assert action_pack_view.snapshot_key(CO, PACK, "W2") == \
        f"action_packs/{CO}/{PACK}/web/W2.txt"
    for bad in ("../W2", "W2/x", "", "W2.txt", None):
        with pytest.raises(ValueError):
            action_pack_view.snapshot_key(CO, PACK, bad)


def test_snapshot_writes_the_text_to_the_exact_key():
    s3 = FakeS3()
    key = apw.put_snapshot(s3, "lake-bucket", CO, PACK, "W2", "Page text é")
    assert key == f"action_packs/{CO}/{PACK}/web/W2.txt"
    assert s3.puts == [{"Bucket": "lake-bucket", "Key": key,
                        "Body": "Page text é".encode("utf-8"),
                        "ContentType": "text/plain; charset=utf-8"}]


def test_snapshot_source_fetches_once_and_returns_the_store_shape():
    s3, fetcher = FakeS3(), FakeFetcher(OK_FETCH)
    src = apw.snapshot_source(WEB_SOURCE, fetcher=fetcher, s3=s3, bucket="lake-bucket",
                              company_id=CO, pack_id=PACK)
    assert fetcher.urls == ["https://s0.example/p"]
    assert src == {"ref": "W2", "kind": "web_source", "provenance": "web", "title": "T0",
                   "url": "https://s0.example/p", "fetched_at": "2026-11-01T01:02:03+00:00",
                   "snapshot_s3_key": f"action_packs/{CO}/{PACK}/web/W2.txt"}
    # Store-ready: the org-api shape check accepts it as is.
    assert action_pack_view.payload_shape_error({"sources": [src]}) is None


def test_failed_fetch_means_no_snapshot():
    s3 = FakeS3()
    src = apw.snapshot_source(WEB_SOURCE, fetcher=FakeFetcher(
        {"ok": False, "text": None, "reason": "robots", "fetched_at": None}),
        s3=s3, bucket="lake-bucket", company_id=CO, pack_id=PACK)
    assert src["snapshot_s3_key"] is None and src["fetched_at"] is None
    assert src["url"] == "https://s0.example/p" and s3.puts == []


def test_failed_put_means_no_snapshot():
    src = apw.snapshot_source(WEB_SOURCE, fetcher=FakeFetcher(OK_FETCH), s3=FakeS3(fail=True),
                              bucket="lake-bucket", company_id=CO, pack_id=PACK)
    assert src["snapshot_s3_key"] is None


def test_snapshot_source_refuses_a_non_web_item():
    with pytest.raises(ValueError):
        apw.snapshot_source({"ref": "R1", "kind": "topic", "provenance": "records"},
                            fetcher=FakeFetcher(OK_FETCH), s3=FakeS3(), bucket="b",
                            company_id=CO, pack_id=PACK)


def test_caps_and_cleanup_match_asks_web_tool():
    """§6: a pack's web items are Ask's shape. If Ask's caps move, these must too."""
    import ask_orchestrator
    assert apw.WEB_ANSWER_MAX_CHARS == ask_orchestrator.WEB_ANSWER_MAX_CHARS
    assert apw.WEB_MAX_SOURCES == ask_orchestrator.WEB_MAX_SOURCES
    assert apw._NUMERIC_GROUP.pattern == ask_orchestrator._NUMERIC_GROUP.pattern
    assert apw._MULTI_BLANK.pattern == ask_orchestrator._MULTI_BLANK.pattern
