"""Ask routing: records for the project, model-then-web for everything else.

Spec: docs/superpowers/specs/2026-10-06-ask-knowledge-then-verify-design.md

These drive the real `_rag_answer` (fake rag-search, scripted vendor client, fake
synthesis model) and read what the reader would get. The vendor client is
scripted per `caller` tag, so which model call ran is read off the tag rather
than inferred from the answer. No test here makes a network call.
"""
import json
import os
from collections import Counter

import pytest

os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

import ask_rewrite                # noqa: E402
import corroboration_client as client   # noqa: E402
import dashscope_utils            # noqa: E402
import lambda_ask_agent as laa    # noqa: E402
import llm_utils                  # noqa: E402
import web_answer                 # noqa: E402
from tests.unit.test_ask_scoped import (   # noqa: E402
    CHUNK, FakeLambdaClient, NOW, PINNED, SITE_ID, TOPIC_ID)

SPRINKLER_Q = "Which NZ standard governs the length of flexi fire sprinkler piping?"
SOURCES = [("https://vertexaisearch.cloud.google.com/redirect/AUZ", "standards.govt.nz")]

RECORDS_ANSWER = "The barrier was set at 900 mm [1]."
DRAFT_TEXT = "NZS 4541 covers it; 1.5 m or 1.8 m, I am not sure."
VERIFIED_TEXT = "NZS 4541 cl 5.2 -- 1.8 m per NFPA 13."


def reply(text="", results=(), error=None, searched=None, timed_out=False):
    return client.Reply(
        text=text,
        search_results=[client.SearchResult(u, t) for u, t in results],
        error=error, timed_out=timed_out,
        searched=bool(results) if searched is None else searched)


def classified(kind, records_answer):
    return reply(text=json.dumps({"kind": kind, "records_answer": records_answer}))


class Vendor:
    """`client.call`, scripted per caller tag. An unscripted tag is recorded and
    answered with an error, so a call that should not happen is visible both as
    a tag in `callers` and as a degraded answer."""

    def __init__(self, **by_caller):
        self.queues = {k: list(v) for k, v in by_caller.items()}
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        q = self.queues.get(kw.get("caller"))
        if not q:
            return reply(error="unscripted call: %s" % kw.get("caller"))
        return q.pop(0)

    @property
    def callers(self):
        return Counter(c["caller"] for c in self.calls)

    def of(self, caller):
        return [c for c in self.calls if c["caller"] == caller]

    @property
    def web_calls(self):
        return [c for c in self.calls if c.get("web")]


def wire(monkeypatch, vendor, chunks=(CHUNK,), answer=(RECORDS_ANSWER, None),
         pinned=None, flag=True, responses=None):
    if flag:
        monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    body = responses or [{"chunks": list(chunks), **({"pinned_topic": pinned} if pinned else {})}]
    lam = FakeLambdaClient(body)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: lam)
    monkeypatch.setattr(web_answer.client, "call", vendor)
    synth = {"n": 0, "prompts": []}

    def fake_llm(prompt, max_tokens=4096, force_json=False, **kw):
        synth["n"] += 1
        synth["prompts"].append(prompt)
        return answer

    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    return synth


def ask(question=SPRINKLER_Q, **body):
    body.setdefault("caller_sub", "sub-1")
    body.setdefault("tz", "Pacific/Auckland")
    body.setdefault("now", NOW)
    return laa._rag_answer(dict(body, question=question))


def general_script(verify=None, draft=None):
    return {"web_draft": [draft or reply(text=DRAFT_TEXT)],
            "web_verify": [verify or reply(text=VERIFIED_TEXT, results=SOURCES)]}


# ----------------------------------------------------------------- the routes

def test_project_question_is_answered_from_the_records_alone(monkeypatch):
    vendor = Vendor(web_classify=[classified("project", True)])
    synth = wire(monkeypatch, vendor)
    out = ask("what did we agree about the barrier height")
    assert out["answer"] == RECORDS_ANSWER and out["grounded"] is True
    assert "from_web" not in out and "web" not in out
    assert vendor.callers == Counter({"web_classify": 1}), "no draft, no verify"
    assert synth["n"] == 1


def test_general_question_with_no_useful_records_is_answered_by_the_general_flow(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    out = ask()
    assert out["answer"] == VERIFIED_TEXT
    assert out["grounded"] is False and out["from_web"] is True
    assert out["citations"] == [], "no records text, so no [n] for a card to match"
    web = out["web"]
    assert (web["status"], web["kind"], web["conflicts"]) == ("verified", "general", [])
    assert web["sources"][0]["domain"] == "standards.govt.nz"
    assert "_trace" not in web
    assert vendor.callers == Counter({"web_classify": 1, "web_draft": 1, "web_verify": 1})


def test_general_question_with_useful_records_is_composed_like_mixed(monkeypatch):
    """RULING (controller, 2026-10-06): general + records_answer true goes through
    compose, one merged answer with the records' [n] kept against the cards. The
    records answer is never the whole answer to a general question."""
    composed = {"answer": "Records discuss 1.5 m [1]. NZS 4541 says 1.8 m.",
                "conflicts": ["the records say 1.5 m; NZS 4541 says 1.8 m"]}
    vendor = Vendor(web_classify=[classified("general", True)],
                    web_compose=[reply(text=json.dumps(composed))], **general_script())
    synth = wire(monkeypatch, vendor)
    out = ask()
    assert out["answer"] == composed["answer"]
    assert out["grounded"] is True and out["from_web"] is True
    assert len(out["citations"]) == 1
    assert out["web"]["answer"] == VERIFIED_TEXT
    assert out["web"]["conflicts"] == composed["conflicts"]
    assert vendor.callers["web_compose"] == 1 and synth["n"] == 1


def test_mixed_question_is_composed_with_conflicts_and_the_records_cards(monkeypatch):
    composed = {"answer": "The barrier was 900 mm [1]. WARNING: F4/AS1 requires 1000 mm.",
                "conflicts": ["the records say 900 mm; F4/AS1 requires 1000 mm"]}
    vendor = Vendor(web_classify=[classified("mixed", True)],
                    web_compose=[reply(text=json.dumps(composed))],
                    **general_script(verify=reply(text="F4/AS1: 1000 mm [1].", results=SOURCES)))
    wire(monkeypatch, vendor)
    out = ask("is our 900 mm balcony barrier compliant with F4/AS1")
    assert out["answer"] == composed["answer"]
    assert out["grounded"] is True and out["from_web"] is True
    assert len(out["citations"]) == 1, "the records cards, matching the [1] kept in the text"
    assert out["web"]["conflicts"] == composed["conflicts"]
    assert out["web"]["answer"] == "F4/AS1: 1000 mm [1]."
    assert vendor.callers == Counter({"web_classify": 1, "web_draft": 1,
                                      "web_verify": 1, "web_compose": 1})


def test_mixed_compose_failure_keeps_the_records_answer_and_the_general_block(monkeypatch):
    vendor = Vendor(web_classify=[classified("mixed", True)],
                    web_compose=[reply(error="HTTP 502: aborted")], **general_script())
    wire(monkeypatch, vendor)
    out = ask("is our barrier compliant")
    assert out["answer"] == RECORDS_ANSWER
    assert out["web"]["answer"] == VERIFIED_TEXT and out["web"]["conflicts"] == []


def test_mixed_compose_that_invents_a_marker_is_discarded(monkeypatch):
    vendor = Vendor(web_classify=[classified("mixed", True)],
                    web_compose=[reply(text=json.dumps({"answer": "x [4]", "conflicts": []}))],
                    **general_script())
    wire(monkeypatch, vendor)
    assert ask("is our barrier compliant")["answer"] == RECORDS_ANSWER


@pytest.mark.parametrize("records_answer", [False])
def test_project_question_the_records_cannot_answer_falls_back_to_the_general_flow(
        monkeypatch, records_answer):
    """Worst case 'slower', never 'nothing'."""
    vendor = Vendor(web_classify=[classified("project", records_answer)], **general_script())
    wire(monkeypatch, vendor)
    out = ask("what was the barrier height on level 2")
    assert out["answer"] == VERIFIED_TEXT and out["from_web"] is True
    assert out["grounded"] is False and out["citations"] == []


def test_project_question_with_no_records_at_all_falls_back_and_spends_no_classify(monkeypatch):
    vendor = Vendor(**general_script())
    wire(monkeypatch, vendor, chunks=())
    out = ask("what was the barrier height on level 2")
    assert out["answer"] == VERIFIED_TEXT and out["from_web"] is True
    assert vendor.callers == Counter({"web_draft": 1, "web_verify": 1})


def test_an_unreadable_classification_runs_both_sides_and_composes(monkeypatch):
    vendor = Vendor(web_classify=[reply(text="Sure! Here is what I think.")],
                    web_compose=[reply(text=json.dumps(
                        {"answer": "merged [1]", "conflicts": []}))],
                    **general_script())
    wire(monkeypatch, vendor)
    out = ask()
    assert out["answer"] == "merged [1]" and out["grounded"] is True
    assert vendor.callers["web_draft"] == 1 and vendor.callers["web_compose"] == 1


def test_a_far_chunk_set_skips_classify_and_runs_the_general_flow(monkeypatch):
    far = dict(CHUNK, distance=0.9, topic_title="Unrelated")
    vendor = Vendor(**general_script())
    wire(monkeypatch, vendor, chunks=(far,))
    out = ask()
    assert out["answer"] == VERIFIED_TEXT and out["citations"] == []
    assert vendor.callers["web_classify"] == 0


# ------------------------------------------- records synthesis only when usable

def test_no_records_synthesis_for_a_general_question_the_records_cannot_help(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    synth = wire(monkeypatch, vendor)
    ask()
    assert synth["n"] == 0


def test_no_records_synthesis_for_a_project_question_the_records_cannot_answer(monkeypatch):
    vendor = Vendor(web_classify=[classified("project", False)], **general_script())
    synth = wire(monkeypatch, vendor)
    ask("what was the barrier height on level 2")
    assert synth["n"] == 0


def test_no_records_synthesis_when_the_distance_gate_skips_classify(monkeypatch):
    vendor = Vendor(**general_script())
    synth = wire(monkeypatch, vendor, chunks=(dict(CHUNK, distance=0.9, topic_title="Unrelated"),))
    ask()
    assert synth["n"] == 0


def test_the_synthesis_runs_after_classify_for_project_and_mixed_and_useful_general(monkeypatch):
    for kind, ra, general_runs in (("project", True, False), ("mixed", True, True),
                                   ("general", True, True)):
        vendor = Vendor(web_classify=[classified(kind, ra)], web_compose=[reply(error="x")],
                        **general_script())
        synth = wire(monkeypatch, vendor)
        ask()
        assert synth["n"] == 1, kind
        assert (vendor.callers["web_draft"] == 1) is general_runs, kind


def test_a_classify_that_raises_still_pays_for_the_synthesis_and_runs_both(monkeypatch):
    vendor = Vendor(web_compose=[reply(error="x")], **general_script())
    synth = wire(monkeypatch, vendor)
    monkeypatch.setattr(web_answer, "classify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = ask()
    assert synth["n"] == 1 and vendor.callers["web_draft"] == 1
    assert out["answer"] == RECORDS_ANSWER


# ----------------------------------------------------- what the reader is told

def test_verify_failing_twice_gives_the_draft_verbatim_labelled_unverified(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)],
                    web_draft=[reply(text=DRAFT_TEXT)],
                    web_verify=[reply(error="HTTP 502: aborted")] * 2)
    wire(monkeypatch, vendor)
    out = ask()
    assert out["answer"] == DRAFT_TEXT
    assert out["web"]["status"] == "unverified" and out["web"]["sources"] == []
    assert out["from_web"] is True
    assert vendor.callers["web_verify"] <= 2 and vendor.callers["web_draft"] == 1


def test_a_question_over_the_cap_says_too_long_and_never_searches(monkeypatch):
    long_q = "Which NZ standard governs flexi sprinkler piping? " + "x" * 260
    assert len(long_q) > 300
    vendor = Vendor(web_classify=[classified("general", False)],
                    web_draft=[reply(text=DRAFT_TEXT)])
    wire(monkeypatch, vendor)
    out = ask(long_q)
    assert out["web"]["status"] == "too_long" and out["answer"] == DRAFT_TEXT
    assert vendor.web_calls == []


def test_which_in_the_corpus_does_not_refuse_the_question(monkeypatch):
    """The 2026-10-06 defect through the real route: `which` is in the records,
    the question starts with a search verb and `Which`."""
    chunk = dict(CHUNK, chunk_text="Ben asked which scaffold tag was which.")
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor, chunks=(chunk,))
    out = ask("search online Which NZ standard governs the length of flexi fire "
              "sprinkler piping")
    assert out["web"]["status"] == "verified" and len(vendor.web_calls) == 1


def test_a_commercial_term_does_not_refuse_the_question(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    out = ask("what is the going rate for a variation on scaffold hire in NZ")
    assert out["web"]["status"] == "verified" and out["web"]["refused"] is None


def test_the_verify_prompt_carries_the_question_and_the_draft_never_the_records(monkeypatch):
    chunk = dict(CHUNK, chunk_text="Neil signed off the Ellesmere scaffold variation.")
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor, chunks=(chunk,))
    ask()
    verify = vendor.of("web_verify")[0]
    assert SPRINKLER_Q in verify["prompt"] and DRAFT_TEXT in verify["prompt"]
    for call in vendor.of("web_draft") + [verify]:
        for private in ("Neil", "Ellesmere", "scaffold variation", "UC PK"):
            assert private not in call["prompt"]


def test_classify_judges_the_rewrite_and_everything_sent_out_uses_the_asker_s_words(monkeypatch):
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")
    monkeypatch.setattr(ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("what limit applies to flexi sprinkler hose", True))
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    ask("and what is the limit?", history=[{"question": "which standard?", "answer": "a"}])
    assert "flexi sprinkler hose" in vendor.of("web_classify")[0]["prompt"]
    for call in vendor.of("web_draft") + vendor.of("web_verify"):
        assert "and what is the limit?" in call["prompt"]
        assert "flexi sprinkler hose" not in call["prompt"], \
            "a rewrite derived from history must not leave the account"


# ------------------------------------------- the paths that never take the flow

def _no_web_calls(vendor, out):
    assert vendor.calls == [], "classify/draft/verify must not run: %s" % vendor.callers
    assert "from_web" not in out


def test_a_scoped_ask_never_takes_the_general_flow(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    out = ask(scoped=True, date="2026-09-03")
    _no_web_calls(vendor, out)
    assert out["answer"] == RECORDS_ANSWER


def test_a_scoped_ask_that_found_nothing_does_not_take_it_either(monkeypatch):
    vendor = Vendor(**general_script())
    wire(monkeypatch, vendor, chunks=())
    out = ask(scoped=True, date="2026-09-03")
    _no_web_calls(vendor, out)
    assert out["answer"] == "No relevant records found for this question."


def test_a_pinned_topic_never_takes_the_general_flow(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor, pinned=PINNED, responses=[{
        "chunks": [CHUNK], "pinned_topic": PINNED,
        "applied": {"topic_row_id": TOPIC_ID, "site_id": SITE_ID, "dropped": []}}])
    out = ask(topic_row_id=TOPIC_ID)
    _no_web_calls(vendor, out)


def test_a_pinned_topic_alone_is_enough_to_keep_the_flow_off(monkeypatch):
    """Independent of `scoped`: rag-search handed back a pinned topic and the
    request carries no scope fields. The pinned block is the reader's own record
    and classify would judge only `chunks`, never it."""
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor, pinned=PINNED)
    out = ask()
    _no_web_calls(vendor, out)


def test_voice_never_takes_the_general_flow(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    out = ask(mode="voice")
    _no_web_calls(vendor, out)


def test_voice_with_no_records_never_takes_it_either(monkeypatch):
    vendor = Vendor(**general_script())
    wire(monkeypatch, vendor, chunks=())
    out = ask(mode="voice")
    _no_web_calls(vendor, out)


def test_the_flag_off_is_todays_records_only_answer(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    synth = wire(monkeypatch, vendor, flag=False)
    out = ask()
    _no_web_calls(vendor, out)
    assert out["answer"] == RECORDS_ANSWER and synth["n"] == 1


def test_the_flag_off_with_no_records_is_the_plain_no_records_answer(monkeypatch):
    monkeypatch.delenv("ENABLE_WEB_ANSWER", raising=False)
    vendor = Vendor(**general_script())
    wire(monkeypatch, vendor, chunks=(), flag=False)
    out = ask()
    _no_web_calls(vendor, out)
    assert out["answer"] == "No relevant records found for this question."


# ------------------------------------------- neither side may lose the other

def test_a_crashed_general_flow_still_delivers_the_records_answer(monkeypatch):
    vendor = Vendor()
    wire(monkeypatch, vendor)
    monkeypatch.setattr(web_answer, "classify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = ask()
    assert out["answer"] == RECORDS_ANSWER and "error" not in out


def test_a_crashed_synthesis_still_delivers_the_general_answer(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", True)], **general_script())
    wire(monkeypatch, vendor)
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synth boom")))
    out = ask()
    assert out["answer"] == VERIFIED_TEXT and out["grounded"] is False
    assert out["citations"] == [], "no records text, so no cards"


def test_a_failed_draft_tells_the_reader_and_keeps_the_records_answer(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", False)],
                    web_draft=[reply(error="HTTP 500")])
    wire(monkeypatch, vendor)
    out = ask()
    assert out["answer"] == RECORDS_ANSWER
    assert out["web"]["answer"] is None and out["web"]["failed"] is True


# ----------------------------------------------------------------- ASK_ROUTE

def _route_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("ASK_ROUTE ")]


def test_one_ask_route_line_per_request_with_no_question_or_answer_text(monkeypatch, caplog):
    secret_q = "zebracrossing sprinkler standard"
    vendor = Vendor(web_classify=[classified("general", False)], **general_script())
    wire(monkeypatch, vendor)
    with caplog.at_level("INFO"):
        out = ask(secret_q)
    lines = _route_lines(caplog)
    assert len(lines) == 1
    record = json.loads(lines[0][len("ASK_ROUTE "):])
    assert record["kind"] == "general" and record["records_answer"] is False
    assert record["general_status"] == "verified" and record["retried"] is False
    assert {"classify", "draft", "verify"} <= set(record["ms"])
    assert "zebracrossing" not in lines[0]
    assert VERIFIED_TEXT not in lines[0] and out["answer"] not in lines[0]


def test_ask_route_records_a_retry(monkeypatch, caplog):
    vendor = Vendor(web_classify=[classified("general", False)],
                    web_draft=[reply(text=DRAFT_TEXT)],
                    web_verify=[reply(error="HTTP 502: aborted"),
                                reply(text=VERIFIED_TEXT, results=SOURCES)])
    wire(monkeypatch, vendor)
    with caplog.at_level("INFO"):
        ask()
    record = json.loads(_route_lines(caplog)[0][len("ASK_ROUTE "):])
    assert record["retried"] is True and record["general_status"] == "verified"


def test_ask_route_is_logged_once_even_for_a_request_that_never_classified(monkeypatch, caplog):
    vendor = Vendor()
    wire(monkeypatch, vendor, flag=False)
    with caplog.at_level("INFO"):
        ask(scoped=True, date="2026-09-03")
    lines = _route_lines(caplog)
    assert len(lines) == 1
    record = json.loads(lines[0][len("ASK_ROUTE "):])
    assert record["kind"] is None and record["general_status"] == "not_needed"


def test_the_response_never_leaks_the_trace(monkeypatch):
    vendor = Vendor(web_classify=[classified("general", True)], **general_script())
    wire(monkeypatch, vendor)
    out = ask()
    assert "_trace" not in json.dumps(out)
