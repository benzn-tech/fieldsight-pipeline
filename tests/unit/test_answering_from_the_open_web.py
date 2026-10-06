"""The general-knowledge flow: classify, draft -> verify, compose.

Spec: docs/superpowers/specs/2026-10-06-ask-knowledge-then-verify-design.md

Rewritten 2026-10-06. The old file tested the verdict + web-only lookup this
replaces. What it protected is kept where it still applies (the question goes
out whole and the excerpts do not; the rewrite never leaves the account; prose
about a search that never ran is not evidence) and the rest is replaced by the
properties the owner asked for: a failed search is never "nothing", and never
an invitation for the model to fill the gap.

The vendor client is stubbed: no test here makes a network call.
"""
import json

import pytest

client = pytest.importorskip("corroboration_client")
web = pytest.importorskip("web_answer")


CHUNKS = [
    {"site_name": "SB1108 Ellesmere College", "report_date": "2026-09-02",
     "topic_title": "Morning",
     "chunk_text": "Neil signed off the scaffold variation. Ben covered the permit."},
]

SOURCES = [("https://vertexaisearch.cloud.google.com/redirect/AUZ", "standards.govt.nz")]
Q = "Which NZ standard governs the length of flexi fire sprinkler piping?"


def reply(text="", results=(), error=None, searched=None, timed_out=False):
    return client.Reply(
        text=text,
        search_results=[client.SearchResult(u, t) for u, t in results],
        error=error, timed_out=timed_out,
        searched=bool(results) if searched is None else searched)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Script:
    """Stands in for `client.call`. Each step is (reply, seconds it took); the
    shared clock advances by that. A call beyond the script raises, which is how
    "no further model call" is asserted: it cannot happen silently."""

    def __init__(self, clock, *steps):
        self.clock = clock
        self.queue = list(steps)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        if not self.queue:
            raise AssertionError("a model call beyond the script: " + str(kw.get("caller")))
        r, seconds = self.queue.pop(0)
        self.clock.t += seconds
        return r

    @property
    def callers(self):
        return [c["caller"] for c in self.calls]

    @property
    def web_calls(self):
        return [c for c in self.calls if c.get("web")]


def run_general(monkeypatch, *steps, question=Q, budget=22.0):
    clock = Clock()
    script = Script(clock, *steps)
    monkeypatch.setattr(web.client, "call", script)
    out = web.general_answer(question, budget, clock=clock)
    return out, script


DRAFT = reply(text="NZS 4541 covers it; 1.5 m or 1.8 m, I am not sure.")
VERIFIED = reply(text="NZS 4541 cl 5.2 -- 1.8 m per NFPA 13.", results=SOURCES)


# ------------------------------------------------------------------ classify

def test_classify_with_no_records_is_general_and_asks_no_model(monkeypatch):
    script = Script(Clock())
    monkeypatch.setattr(web.client, "call", script)
    assert web.classify(Q, [], 8) == {"kind": "general", "records_answer": False,
                                      "source": "no_records"}
    assert script.calls == []


@pytest.mark.parametrize("kind", ["project", "general", "mixed"])
def test_classify_reads_each_kind(monkeypatch, kind):
    script = Script(Clock(), (reply(text=json.dumps(
        {"kind": kind, "records_answer": True})), 1))
    monkeypatch.setattr(web.client, "call", script)
    out = web.classify(Q, CHUNKS, 8)
    assert out == {"kind": kind, "records_answer": True, "source": "model"}
    assert script.calls[0]["caller"] == "web_classify"
    assert not script.calls[0].get("web"), "a classification must never search"


def test_classify_reads_a_fenced_reply(monkeypatch):
    monkeypatch.setattr(web.client, "call", Script(Clock(), (reply(
        text='```json\n{"kind": "project", "records_answer": false}\n```'), 1)))
    assert web.classify(Q, CHUNKS, 8)["records_answer"] is False


@pytest.mark.parametrize("bad", [
    reply(text="Sure! Here's what I think."),
    reply(text=json.dumps({"kind": "banana", "records_answer": True})),
    reply(text=json.dumps({"kind": "general", "records_answer": "yes"})),
    reply(text=json.dumps({"kind": "general"})),
    reply(error="HTTP 502: The operation was aborted"),
])
def test_an_unreadable_classification_runs_both_sides_not_neither(monkeypatch, bad):
    """Ruling: fail-safe is `mixed` + records useful. Both flows run and compose
    attributes each fact -- slower, never nothing, and never a project question
    answered from the model's general knowledge alone."""
    monkeypatch.setattr(web.client, "call", Script(Clock(), (bad, 1)))
    assert web.classify(Q, CHUNKS, 8) == {"kind": "mixed", "records_answer": True,
                                          "source": "fallback"}


def test_classify_judges_the_question_it_is_given(monkeypatch):
    """Conversation memory retrieves with the standalone rewrite, so the
    classifier has to judge the excerpts against THAT (measured on TEST
    2026-09-18: judged against the pronoun version it said no, and a web answer
    replaced a records answer that said Friday)."""
    script = Script(Clock(), (reply(text='{"kind":"project","records_answer":true}'), 1))
    monkeypatch.setattr(web.client, "call", script)
    web.classify("When must the Unit 11 backfill finish?", CHUNKS, 8)
    assert "Unit 11 backfill" in script.calls[0]["prompt"]


# ------------------------------------------------------------ general answer

def test_draft_then_verify_and_the_verify_carries_the_draft_and_no_records(monkeypatch):
    out, script = run_general(monkeypatch, (DRAFT, 3), (VERIFIED, 9))
    assert script.callers == ["web_draft", "web_verify"]
    draft_call, verify_call = script.calls
    assert not draft_call.get("web"), "the draft is from the model's own knowledge"
    assert verify_call["web"] is True
    assert Q in verify_call["prompt"] and DRAFT.text in verify_call["prompt"]
    assert out["status"] == "verified" and out["searched"] is True
    assert out["answer"] == VERIFIED.text
    assert out["sources"][0]["domain"] == "standards.govt.nz", \
        "the vendor returns Google redirects; the publisher is in the title"


def test_the_question_goes_out_whole_and_the_excerpts_do_not(monkeypatch):
    """`general_answer` is never handed the records, so nothing of the
    customer's can travel -- pinned here against a future change that adds an
    `excerpts` argument."""
    import inspect
    assert list(inspect.signature(web.general_answer).parameters)[:2] == ["question", "budget"]
    assert "chunks" not in inspect.signature(web.general_answer).parameters
    _, script = run_general(monkeypatch, (DRAFT, 3), (VERIFIED, 9))
    for call in script.calls:
        for private in ("Neil", "Ellesmere", "scaffold variation"):
            assert private not in call["prompt"]


def test_verify_fails_then_the_retry_succeeds_when_time_allows(monkeypatch):
    out, script = run_general(
        monkeypatch, (DRAFT, 3), (reply(error="HTTP 502: The operation was aborted"), 4),
        (VERIFIED, 9), budget=22.0)
    assert script.callers == ["web_draft", "web_verify", "web_verify"]
    assert out["status"] == "verified" and out["answer"] == VERIFIED.text
    assert out["_trace"]["retried"] is True


def test_there_is_no_retry_when_less_than_ten_seconds_remain(monkeypatch):
    """draft 3 s + a verify that dies at 11 s leaves 22 - 14 = 8 s: another
    ~9 s attempt cannot finish, so none is started."""
    out, script = run_general(
        monkeypatch, (DRAFT, 3), (reply(error="The operation was aborted"), 11),
        budget=22.0)
    assert script.callers == ["web_draft", "web_verify"]
    assert out["status"] == "unverified"
    assert out["answer"] == DRAFT.text
    assert out["_trace"]["retried"] is False


def test_the_retry_boundary_is_ten_seconds_exactly(monkeypatch):
    out, script = run_general(
        monkeypatch, (DRAFT, 3), (reply(error="boom"), 9), (VERIFIED, 9), budget=22.0)
    assert script.callers.count("web_verify") == 2     # 22 - 12 = 10 left: retry
    out, script = run_general(
        monkeypatch, (DRAFT, 3), (reply(error="boom"), 9.5), budget=22.0)
    assert script.callers.count("web_verify") == 1     # 9.5 left: no retry


def test_verify_failing_twice_returns_the_draft_verbatim_and_asks_nothing_more(monkeypatch):
    """Measured 2026-10-06: a model asked to fill the gap after a failed search
    wrote the most confident wrong number. The script holds exactly three calls;
    a fourth raises."""
    out, script = run_general(
        monkeypatch, (DRAFT, 2), (reply(error="HTTP 502: aborted"), 3),
        (reply(error="HTTP 502: aborted"), 3), budget=40.0)
    assert script.callers == ["web_draft", "web_verify", "web_verify"]
    assert out["answer"] == DRAFT.text, "verbatim, not rewritten"
    assert out["status"] == "unverified"
    assert out["failed"] is True and out["searched"] is False
    assert out["sources"] == []
    assert script.queue == []


def test_a_200_with_no_search_results_is_not_a_verification(monkeypatch):
    """Prose describing a search is not evidence one happened (measured on a
    second vendor: 200 OK, no results, a paragraph asserting findings)."""
    out, _ = run_general(
        monkeypatch, (DRAFT, 3),
        (reply(text="I searched and it is confirmed.", searched=False), 5),
        (reply(text="I searched and it is confirmed.", searched=False), 5),
        budget=40.0)
    assert out["status"] == "unverified" and out["answer"] == DRAFT.text


def test_a_timed_out_verify_is_marked_timed_out(monkeypatch):
    out, _ = run_general(monkeypatch, (DRAFT, 3),
                         (reply(error="read timeout", timed_out=True), 14), budget=22.0)
    assert out["timed_out"] is True and out["status"] == "unverified"


def test_a_failed_draft_is_reported_and_nothing_is_verified(monkeypatch):
    out, script = run_general(monkeypatch, (reply(error="HTTP 500"), 1))
    assert out["answer"] is None and out["failed"] is True
    assert script.callers == ["web_draft"], "there is no draft to check"


def test_a_question_over_the_cap_is_not_sent_to_the_web(monkeypatch):
    out, script = run_general(monkeypatch, (DRAFT, 3), question="x" * 301)
    assert script.web_calls == []
    assert out["status"] == "too_long" and out["refused"]
    assert out["answer"] == DRAFT.text and out["searched"] is False
    assert out["failed"] is False, "a refusal is not a failure"


def test_which_in_the_question_is_not_refused(monkeypatch):
    """The 2026-10-06 defect, end to end through the flow: this exact wording
    was refused because `which` occurs in the transcripts."""
    out, script = run_general(
        monkeypatch, (DRAFT, 3), (VERIFIED, 9),
        question="search online Which NZ standard governs the length of flexi "
                 "fire sprinkler piping")
    assert len(script.web_calls) == 1 and out["status"] == "verified"


def test_a_commercial_question_is_not_refused(monkeypatch):
    out, script = run_general(monkeypatch, (DRAFT, 3), (VERIFIED, 9),
                              question="what did the variation cost")
    assert len(script.web_calls) == 1 and out["refused"] is None


def test_the_chain_never_runs_past_its_budget(monkeypatch):
    """A budget already spent: the draft gets no time, and no web call is made."""
    out, script = run_general(monkeypatch, (reply(error="no time left"), 0), budget=1.0)
    assert script.callers == ["web_draft"] and out["answer"] is None


def test_the_budgets_fit_under_the_stop_and_the_gateway():
    assert web.HARD_STOP_SECONDS < 29, "the gateway ceiling is not ours to raise"
    assert web.VERIFY_RETRY_MIN_LEFT >= client.MIN_USEFUL_TIMEOUT
    # The slowest honest path: classify + draft + verify + compose, with typical
    # (not ceiling) timings from the spec's table.
    assert 1.3 + 2.5 + 3 + 9 + 3 < web.HARD_STOP_SECONDS


def test_the_flag_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")
    assert web.enabled() is True
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "false")
    assert web.enabled() is False


# ------------------------------------------------------------------- compose

RECORDS = "The barrier was set at 900 mm [1]. Neil signed it off [2]."
GENERAL = {"answer": "F4/AS1 requires 1000 mm [1].", "status": "verified"}


def compose_with(monkeypatch, text_or_reply, budget=8.0):
    r = text_or_reply if isinstance(text_or_reply, client.Reply) else reply(text=text_or_reply)
    script = Script(Clock(), (r, 3))
    monkeypatch.setattr(web.client, "call", script)
    return web.compose(RECORDS, GENERAL, "Is our barrier compliant?", budget), script


def test_compose_merges_and_reports_the_conflict(monkeypatch):
    out, script = compose_with(monkeypatch, json.dumps({
        "answer": "The barrier was 900 mm [1]. WARNING: F4/AS1 requires 1000 mm.",
        "conflicts": ["the records say 900 mm; F4/AS1 requires 1000 mm"]}))
    assert out["composed"] is True
    assert out["conflicts"] == ["the records say 900 mm; F4/AS1 requires 1000 mm"]
    assert "[1]" in out["answer"]
    call = script.calls[0]
    assert call["caller"] == "web_compose" and not call.get("web")


def test_compose_strips_the_general_blocks_own_markers_before_the_model_sees_them(monkeypatch):
    """The general text's [1] points at a WEB source; beside the records' [1] it
    would read as a record citation."""
    _, script = compose_with(monkeypatch, json.dumps({"answer": "ok", "conflicts": []}))
    prompt = script.calls[0]["prompt"]
    general_part = prompt.split("## GENERAL ANSWER")[1]
    assert "[1]" not in general_part and "F4/AS1 requires 1000 mm" in general_part
    assert "[1]" in prompt.split("## GENERAL ANSWER")[0], "the records' markers stay"


def test_compose_that_invents_a_citation_marker_is_discarded(monkeypatch):
    """The citations contract: card [i+1] <-> inline [n] in the RECORDS answer.
    A marker the records answer never had would point at the wrong card."""
    out, _ = compose_with(monkeypatch, json.dumps({
        "answer": "The barrier was 900 mm [1]. F4/AS1 says 1000 mm [3].",
        "conflicts": []}))
    assert out == {"answer": RECORDS, "conflicts": [], "composed": False}


@pytest.mark.parametrize("bad", [
    reply(error="HTTP 502"),
    reply(text="not json at all"),
    reply(text=json.dumps({"answer": "", "conflicts": []})),
    reply(text=json.dumps({"answer": "x", "conflicts": "none"})),
])
def test_a_failed_compose_falls_back_to_the_records_answer(monkeypatch, bad):
    out, _ = compose_with(monkeypatch, bad)
    assert out == {"answer": RECORDS, "conflicts": [], "composed": False}


@pytest.mark.parametrize("answer", [
    "x [1,3]", "x [1, 3]", "x [1-3]", "x [1][3]", "x [3]", "x [1; 3]", "x [1–3]",
])
def test_compose_discards_any_marker_form_naming_a_record_it_lacks(monkeypatch, answer):
    """RECORDS has only [1] and [2]; each form below names [3]. Review F7."""
    out, _ = compose_with(monkeypatch, json.dumps({"answer": answer, "conflicts": []}))
    assert out["composed"] is False and out["answer"] == RECORDS


@pytest.mark.parametrize("answer", ["ok [1]", "ok [1,2]", "ok [1-2]", "ok [1][2]", "ok [unverified]"])
def test_compose_accepts_markers_the_records_answer_has(monkeypatch, answer):
    out, _ = compose_with(monkeypatch, json.dumps({"answer": answer, "conflicts": []}))
    assert out["composed"] is True


def test_the_verify_prompt_asks_for_a_final_answer_not_a_review():
    """WIRING-LEVEL: this reads the prompt text, not model behaviour. The model's
    obedience was checked on TEST (2026-10-06 showed the review voice that this
    wording replaces); here we only pin that the instruction is present and the
    draft is still an input."""
    prompt = web.VERIFY_PROMPT
    assert "{draft}" in prompt and "{question}" in prompt
    assert "FINAL answer" in prompt and "addressed directly to the reader" in prompt
    assert "Never mention the draft, a colleague, or the checking process" in prompt
    for word in ("confirmed", "corrected", "verified", "review", "verification"):
        assert '"%s"' % word in prompt, "the forbidden word list lost " + word
    assert "colleague answered" not in prompt.lower()
    assert "[unverified]" in prompt


def test_the_draft_is_still_passed_to_verify(monkeypatch):
    _, script = run_general(monkeypatch, (DRAFT, 3), (VERIFIED, 9))
    verify = script.calls[1]
    assert DRAFT.text in verify["prompt"] and Q in verify["prompt"]
    assert verify["web"] is True


def test_the_draft_prompt_tells_the_model_it_cannot_know_the_project():
    assert "only the project's records could answer it" in web.DRAFT_PROMPT


def test_compose_makes_no_call_without_time(monkeypatch):
    out, script = compose_with(monkeypatch, "{}", budget=1.0)
    assert script.calls == [] and out["composed"] is False
