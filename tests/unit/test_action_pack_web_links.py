"""Action packs, first live TEST runs (2026-11-01): web sources were never snapshotted.

Two breaks, both fixed here:
1. The writer cites the web_answer (W1: the verified text) and never the web_source items
   (W2..: a domain line), and only cited web_source items were snapshotted. Now a web_answer
   carries the refs of its own sources, a cited web_answer brings them onto the claim, and a
   claim resting on a web_answer with no archived source stays at most weak.
2. Every source url the corroboration client returns is a Google grounding redirect
   (vertexaisearch.cloud.google.com/grounding-api-redirect/...), and that host's robots.txt
   disallows the path, so every fetch failed `robots`. The redirect hop alone is now
   resolved without its robots check (its Location only -- a body is never stored); the
   real page is vetted and robots-checked as before, and its url is what the pack keeps.

Fakes only: no network, no AWS."""
import pytest

import action_pack_stages as st
import action_pack_verify as apv
import action_pack_web as apw
import action_pack_view
import ask_tools
import web_fetch as wf
from tests.unit.test_action_pack_stages import (CO, PACK, WRITER, Fetcher, Model, Verify,
                                                deps, make_ctx, write_obj)
from tests.unit.test_action_pack_verify import Fake, entry
from tests.unit.test_action_pack_verify import fixture as vfixture
from tests.unit.test_action_pack_verify import line
from tests.unit.test_action_pack_verify import make_ctx as vctx
from tests.unit.test_web_fetch import PUBLIC, FakeResponse, html, make

GROUNDING = "vertexaisearch.cloud.google.com"
REDIRECT = f"https://{GROUNDING}/grounding-api-redirect/AUZIYQabc"
def robots_grounding():
    """The redirector's real robots.txt (fetched 2026-11-01). A fresh response each time:
    a FakeResponse is read once."""
    return FakeResponse(200, {"Content-Type": "text/plain"},
                        b"User-agent: *\nDisallow: /grounding-api-redirect\n"
                        b"Disallow: /grounding-redirect\n")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACTION_WRITER_MODEL", WRITER)
    monkeypatch.delenv("ACTION_TOKEN_CAP", raising=False)
    monkeypatch.setenv("ACTION_VERIFIER_MODEL", "google/gemini-3.8-flash")
    monkeypatch.delenv("ACTION_VERIFIER_FALLBACKS", raising=False)


# ---- 1. web_answer -> its sources ----------------------------------------------------------

class Refs(ask_tools.RefAssigner):
    pass


def block(n=2):
    return {"status": "verified", "answer": "Cure 7 days [1].",
            "sources": [{"url": f"https://example.org/p{i}", "title": f"t{i}",
                         "domain": "example.org"} for i in range(n)]}


def test_a_verified_web_answer_lists_the_refs_of_its_own_sources():
    q = apw.WebQueries.from_plan([{"id": "q1", "text": "NZS 3109 curing"}])
    env = apw.web_search(q, "q1", refs=Refs(), budget=30,
                         general_answer=lambda *a, **k: block(2))
    answer, *srcs = env["items"]
    assert answer["kind"] == "web_answer"
    assert [s["kind"] for s in srcs] == ["web_source", "web_source"]
    assert answer["sources"] == [s["ref"] for s in srcs]


def test_a_source_shared_by_two_answers_keeps_one_ref_listed_by_both():
    q = apw.WebQueries.from_plan([{"id": "q1", "text": "a"}, {"id": "q2", "text": "b"}])
    refs = Refs()
    e1 = apw.web_search(q, "q1", refs=refs, budget=30, general_answer=lambda *a, **k: block(1))
    e2 = apw.web_search(q, "q2", refs=refs, budget=30, general_answer=lambda *a, **k: block(1))
    assert e1["items"][0]["sources"] == e2["items"][0]["sources"] == [e1["items"][1]["ref"]]


WEB_ROUND = [{"calls": [{"tool": "web_search", "args": {"query_id": "q1"}}], "done": True}]


def test_a_cited_web_answer_brings_its_sources_onto_the_claim_and_they_are_snapshotted():
    write = write_obj(conclusion=[{"text": "NZS 3109 says cure 7 days.", "refs": ["D1", "W1"],
                                   "confidence": "supported"}])
    ctx = make_ctx()
    fetcher = Fetcher()
    verify = Verify()
    payload = st.run(ctx, deps(model=Model(rounds=WEB_ROUND, write=[write]), fetcher=fetcher,
                               verify=verify))
    [c] = payload["conclusion"]
    assert c["refs"] == ["D1", "W1", "W2"]
    by_ref = {s["ref"]: s for s in payload["sources"]}
    assert by_ref["W2"]["kind"] == "web_source"
    assert by_ref["W2"]["snapshot_s3_key"] == action_pack_view.snapshot_key(CO, PACK, "W2")
    assert fetcher.urls == ["https://www.branz.co.nz/curing"]
    assert [p["Key"] for p in ctx.s3.puts] == [by_ref["W2"]["snapshot_s3_key"]]
    trace = payload["run_trace"]
    assert trace["web_refs_added"] == 1 and trace["snapshots"] == 1
    # the verifier saw the expanded claim and the page text
    assert verify.calls[0]["draft"]["conclusion"][0]["refs"] == ["D1", "W1", "W2"]
    assert verify.calls[0]["registry"]["W2"]["snapshot_text"] == "page text"
    assert action_pack_view.payload_shape_error(payload) is None
    assert action_pack_view.snapshot_key_error(payload, CO, PACK) is None


def test_an_action_citing_a_web_answer_gets_its_sources_too():
    write = write_obj(actions=[{"type": "note", "target_stable_id": None,
                                "proposed": {"text": "Check curing records."},
                                "rationale": "The standard needs 7 days.",
                                "refs": ["W1", "D1"]}])
    payload = st.run(make_ctx(), deps(model=Model(rounds=WEB_ROUND, write=[write])))
    [a] = payload["actions"]
    assert a["refs"] == ["W1", "D1", "W2"]


def test_a_claim_without_a_web_answer_is_unchanged():
    payload = st.run(make_ctx(), deps(model=Model(rounds=WEB_ROUND)))
    assert payload["conclusion"][0]["refs"] == ["R1"]
    assert "web_refs_added" not in payload["run_trace"]


def test_the_page_url_replaces_the_redirect_on_the_stored_row():
    class Resolving(Fetcher):
        def fetch(self, url):
            self.urls.append(url)
            return {"ok": True, "text": "page text", "final_url": "https://www.branz.co.nz/real",
                    "fetched_at": "2026-11-01T00:00:00+00:00"}
    write = write_obj(conclusion=[{"text": "Cure 7 days.", "refs": ["W1"],
                                   "confidence": "uncertain"}])
    payload = st.run(make_ctx(), deps(model=Model(rounds=WEB_ROUND, write=[write]),
                                      fetcher=Resolving()))
    w2 = {s["ref"]: s for s in payload["sources"]}["W2"]
    assert w2["url"] == "https://www.branz.co.nz/real" and w2["snapshot_s3_key"]


def test_a_failed_fetch_keeps_the_original_url():
    write = write_obj(conclusion=[{"text": "Cure 7 days.", "refs": ["W1"],
                                   "confidence": "uncertain"}])
    payload = st.run(make_ctx(), deps(model=Model(rounds=WEB_ROUND, write=[write]),
                                      fetcher=Fetcher(ok=False)))
    w2 = {s["ref"]: s for s in payload["sources"]}["W2"]
    assert w2["url"] == "https://www.branz.co.nz/curing" and w2["snapshot_s3_key"] is None


# ---- 1b. the verifier: a web_answer is only as good as its archived sources ---------------

def _verify(draft, registry, sources, monkeypatch):
    monkeypatch.setattr(apv, "call_json", Fake())
    ctx = vctx()
    _, items, _ = vfixture()
    return apv.verify(draft, registry=registry, items=items, sources=sources, ctx=ctx,
                      writer_model=WRITER, deadline=ctx.deadline_for("verify"))


def _web_fixture(snapshotted):
    registry, _, sources = vfixture(extra_registry={
        "W1": entry("W1", "web_answer", "NZS 3109 says cure for 7 days.",
                    item={"ref": "W1", "kind": "web_answer", "sources": ["W2", "W3"]}),
        "W3": entry("W3", "web_source", "scribd.com")})
    sources["W2"]["snapshot_s3_key"] = "k/W2.txt" if "W2" in snapshotted else None
    sources["W3"]["snapshot_s3_key"] = "k/W3.txt" if "W3" in snapshotted else None
    return registry, sources


def conf(result):
    return {e["index"]: e["confidence"] for e in result["conclusion"]}


def test_a_web_answer_with_no_archived_source_caps_the_claim(monkeypatch):
    registry, sources = _web_fixture(snapshotted=())
    draft = {"conclusion": [line("Cure 7 days.", ["D1", "W1", "W2", "W3"])], "actions": []}
    result = _verify(draft, registry, sources, monkeypatch)
    assert conf(result) == {0: "uncertain"}
    assert result["counts"]["no_snapshot_capped"] == 1


def test_one_archived_source_is_enough_for_a_web_answer_claim(monkeypatch):
    """A source of the answer that could not be archived (scribd 403) does not cap the
    claim when another source of the same answer was archived: the answer is backed."""
    registry, sources = _web_fixture(snapshotted=("W2",))
    draft = {"conclusion": [line("Cure 7 days.", ["D1", "W1", "W2", "W3"])], "actions": []}
    result = _verify(draft, registry, sources, monkeypatch)
    assert conf(result) == {0: "supported"}
    assert "no_snapshot_capped" not in result["counts"]


def test_a_web_answer_without_sources_caps_the_claim(monkeypatch):
    registry, _, sources = vfixture()            # W1 lists no sources
    draft = {"conclusion": [line("Cure 7 days.", ["D1", "W1"])], "actions": []}
    result = _verify(draft, registry, sources, monkeypatch)
    assert conf(result) == {0: "uncertain"}
    assert result["counts"]["no_snapshot_capped"] == 1


def test_a_directly_cited_source_without_a_snapshot_still_caps(monkeypatch):
    registry, sources = _web_fixture(snapshotted=("W2",))
    draft = {"conclusion": [line("Cure 7 days.", ["D1", "W3"])], "actions": []}
    result = _verify(draft, registry, sources, monkeypatch)
    assert conf(result) == {0: "uncertain"}


# ---- 2. grounding redirects ---------------------------------------------------------------

DNS = {GROUNDING: ["142.250.70.10"], "www.branz.co.nz": [PUBLIC], "example.org": [PUBLIC]}


def test_a_grounding_redirect_is_resolved_and_the_page_is_fetched():
    f, server, _ = make({
        (GROUNDING, "/robots.txt"): robots_grounding(),
        (GROUNDING, "/grounding-api-redirect/AUZIYQabc"): FakeResponse(
            302, {"Location": "https://www.branz.co.nz/curing"}),
        ("www.branz.co.nz", "/curing"): html("<p>Cure for seven days.</p>")}, dns=DNS)
    r = f.fetch(REDIRECT)
    assert r["ok"] is True and r["text"] == "Cure for seven days."
    assert r["final_url"] == "https://www.branz.co.nz/curing"
    hosts_paths = [(q["host"], q["path"]) for q in server.requests]
    # the redirector's robots.txt is not consulted; the page's host's is
    assert (GROUNDING, "/robots.txt") not in hosts_paths
    assert ("www.branz.co.nz", "/robots.txt") in hosts_paths


def test_the_real_pages_robots_still_decides():
    f, _, _ = make({
        (GROUNDING, "/grounding-api-redirect/AUZIYQabc"): FakeResponse(
            302, {"Location": "https://www.branz.co.nz/curing"}),
        ("www.branz.co.nz", "/robots.txt"): FakeResponse(
            200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow: /\n")}, dns=DNS)
    assert f.fetch(REDIRECT)["reason"] == "robots"


def test_a_redirector_that_answers_with_a_page_is_refused():
    f, _, _ = make({(GROUNDING, "/grounding-api-redirect/AUZIYQabc"): html("<p>hi</p>")},
                   dns=DNS)
    r = f.fetch(REDIRECT)
    assert r["ok"] is False and r["reason"] == "redirector_no_redirect" and r["text"] is None


def test_the_exemption_is_only_for_the_first_hop_and_the_redirect_path():
    # another path on the same host: robots applies
    f, _, _ = make({(GROUNDING, "/robots.txt"): robots_grounding(),
                    (GROUNDING, "/grounding-redirect/x"): FakeResponse(
                        302, {"Location": "https://www.branz.co.nz/curing"})}, dns=DNS)
    assert f.fetch(f"https://{GROUNDING}/grounding-redirect/x")["reason"] == "robots"
    # a page that redirects INTO the redirector: robots applies to that hop
    f, _, _ = make({("example.org", "/a"): FakeResponse(302, {"Location": REDIRECT}),
                    (GROUNDING, "/robots.txt"): robots_grounding()}, dns=DNS)
    assert f.fetch("https://example.org/a")["reason"] == "robots"


def test_the_redirect_target_is_still_ssrf_vetted():
    f, server, _ = make({(GROUNDING, "/grounding-api-redirect/AUZIYQabc"): FakeResponse(
        302, {"Location": "https://inside.example/x"})},
        dns=dict(DNS, **{"inside.example": ["10.0.0.5"]}))
    r = f.fetch(REDIRECT)
    assert r["ok"] is False
    assert all(c[0] != "inside.example" for c in server.connects)


def test_the_redirector_is_matched_on_the_vetted_host_not_a_lookalike():
    assert wf.is_citation_redirect(f"https://{GROUNDING}/grounding-api-redirect/a") is True
    for url in (f"https://{GROUNDING}.evil.example/grounding-api-redirect/a",
                f"https://evil.example/{GROUNDING}/grounding-api-redirect/a",
                f"https://{GROUNDING}/other/grounding-api-redirect/a",
                f"http://{GROUNDING}/grounding-api-redirect/a"):
        assert wf.is_citation_redirect(url) is False


def test_the_pages_behind_one_claim_share_a_bounded_prompt_budget():
    """A claim now carries every page behind its web answers (up to 8 archived pages of up
    to 200 kB): their text in one verifier prompt shares WEB_PAGES_MAX_CHARS."""
    big = "x" * 100_000
    reg = {f"W{i}": entry(f"W{i}", "web_source", "example.org", snapshot_text=big)
           for i in range(2, 10)}
    reg["D1"] = entry("D1", "document", "d" * 30_000)
    el = {"text": "c", "refs": ["D1"] + list(reg)[:8], "confidence": "supported"}
    prompt = apv.build_prompt("conclusion", el, registry=reg, items={})
    assert prompt.count("x") <= apv.WEB_PAGES_MAX_CHARS + 100
    assert "d" * apv.REF_MAX_CHARS in prompt                 # a document keeps its share
    one = {"text": "c", "refs": ["W2"], "confidence": "supported"}
    assert apv.build_prompt("conclusion", one, registry=reg, items={}).count("x") >= \
        apv.REF_MAX_CHARS
