"""The path from "the records do not cover this" to an answer.

The first build broke the question into gate-screened entity names, searched
those, and composed an answer. It failed on the most ordinary question there is:
asked "Which New Zealand standard covers timber design?", subject extraction
returned NOTHING on three runs out of four, because the question contains no
standard number -- the number is the thing the asker does not know.

So the question goes out whole, and `question_admission` decides whether it may.
These tests hold the two things that make that safe rather than merely working:
what leaves the account, and what it says when it does not.
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


class FakeCall:
    def __init__(self, *replies):
        self.queue = list(replies)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        if not self.queue:
            raise AssertionError("a step called the model more times than expected")
        return self.queue.pop(0)

    @property
    def web_prompt(self):
        for c in self.calls:
            if c.get("web"):
                return c["prompt"]
        return None


def reply(text="", results=(), error=None, searched=None, timed_out=False):
    return client.Reply(
        text=text,
        search_results=[client.SearchResult(u, t) for u, t in results],
        error=error, timed_out=timed_out,
        searched=bool(results) if searched is None else searched)


def verdict(answered):
    return reply(text=json.dumps({"answered": answered}))


SOURCES = [("https://vertexaisearch.cloud.google.com/redirect/AUZ", "standards.govt.nz")]
PUBLIC_Q = "Which New Zealand standard covers timber design?"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")


def run(monkeypatch, *replies, question=PUBLIC_Q, chunks=CHUNKS):
    fake = FakeCall(*replies)
    monkeypatch.setattr(web.client, "call", fake)
    return web.answer(question, chunks), fake


# ------------------------------------------------- the case the first build missed

def test_a_question_with_no_records_at_all_reaches_the_web(monkeypatch):
    """Retrieval returning nothing IS the verdict, so no model is asked whether
    an empty set answered anything. The first build returned a fixed "no
    relevant records" string ABOVE this hook and never reached it -- which is
    exactly what the owner hit on the first question they tried."""
    out, fake = run(monkeypatch, reply(text="NZS 3604 covers it.", results=SOURCES),
                    chunks=[])
    assert out["answer"].startswith("NZS 3604")
    assert fake.web_prompt is not None
    assert len(fake.calls) == 1, "it asked for a verdict on an empty corpus"


# ------------------------------------------------------- what leaves the account

def test_the_question_goes_out_whole_and_the_excerpts_do_not(monkeypatch):
    """The provider composes its own queries from the question. The customer's
    excerpts are not part of that and must not travel with it."""
    _, fake = run(monkeypatch, verdict(False),
                  reply(text="answer", results=SOURCES))
    sent = fake.web_prompt
    assert "timber design" in sent
    for private in ("Neil", "Ben", "Ellesmere", "scaffold variation"):
        assert private not in sent, "excerpt text reached the web step: " + private


def test_a_question_naming_a_colleague_is_never_sent(monkeypatch):
    """`question_admission` refuses it, and the refusal is reported rather than
    swallowed: a guard whose refusals are invisible cannot be measured."""
    # No commercial term in this one on purpose: "variation" would refuse it by
    # a different rule, and a test that cannot tell which rule fired proves
    # neither of them.
    out, fake = run(monkeypatch, verdict(False),
                    question="what did Neil say about the scaffold")
    assert fake.web_prompt is None
    assert out["searched"] is False
    assert out["refused"] and "own records" in out["refused"]


def test_a_refused_question_is_not_reported_as_a_failure(monkeypatch):
    """"We may not ask" is not "we asked and it broke". Only one of them is
    worth a retry."""
    out, _ = run(monkeypatch, verdict(False), question="what did the variation cost")
    assert out["refused"] and out["failed"] is False and out["timed_out"] is False


# ------------------------------------------------------------- when it fires

def test_records_that_answer_the_question_are_left_alone(monkeypatch):
    out, fake = run(monkeypatch, verdict(True))
    assert out is None, "it looked something up for a question the records answered"
    assert fake.web_prompt is None


def test_an_unreadable_verdict_does_not_authorise_a_search(monkeypatch):
    """Fail closed."""
    out, fake = run(monkeypatch, reply(text="Sure! Here's what I think."))
    assert out is None and fake.web_prompt is None


def test_the_flag_is_read_at_call_time(monkeypatch):
    """A flag captured at import survives a warm container after the stack has
    been redeployed with it off."""
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "false")
    assert web.answer(PUBLIC_Q, CHUNKS) is None


def test_skipping_the_verdict_still_screens_against_the_chunks(monkeypatch):
    """The whole point of `skip_verdict`: it must skip ONLY the verdict call,
    never blind `question_admission`, which derives two of its three signals
    (site names, the account's own words) FROM `chunks`. Calling
    `answer(question, [])` to skip the verdict would also disarm those checks
    -- this keyword exists so nobody has to."""
    seen = {}
    monkeypatch.setattr(web.question_admission, "screen",
                        lambda q, c: seen.update(chunks=c) or None)
    web.answer(PUBLIC_Q, CHUNKS, skip_verdict=True)
    assert seen["chunks"] == CHUNKS, "chunks must still reach screen() unchanged"


def test_skipping_the_verdict_skips_only_the_verdict(monkeypatch):
    fake = FakeCall(reply(text="NZS 3604 covers it.", results=SOURCES))
    monkeypatch.setattr(web.client, "call", fake)
    out = web.answer(PUBLIC_Q, CHUNKS, skip_verdict=True)
    assert out["answer"].startswith("NZS 3604")
    assert len(fake.calls) == 1, "only the web lookup ran, not the verdict"
    assert fake.web_prompt is not None


# --------------------------------------------------- what it says when it fails

def test_prose_about_a_search_that_never_ran_produces_no_answer(monkeypatch):
    """Measured on a second vendor: 200 OK, zero results, a paragraph asserting
    findings. A model's account of its own tool use is not evidence."""
    out, _ = run(monkeypatch, verdict(False),
                 reply(text="I searched and it is confirmed.", searched=False))
    assert out["answer"] is None and out["searched"] is False
    assert out["timed_out"] is False and out["failed"] is False


def test_a_lookup_that_timed_out_says_so_and_a_broken_one_does_not(monkeypatch):
    late, _ = run(monkeypatch, verdict(False), reply(error="read timeout", timed_out=True))
    assert late["timed_out"] is True and late["failed"] is False

    broke, _ = run(monkeypatch, verdict(False), reply(error="HTTP 502"))
    assert broke["failed"] is True and broke["timed_out"] is False


# ------------------------------------------------------------- the happy path

def test_an_answer_carries_its_sources_by_publisher(monkeypatch):
    """The vendor returns Google grounding redirects, so parsing the URL would
    attribute every source to `vertexaisearch.cloud.google.com`."""
    out, _ = run(monkeypatch, verdict(False),
                 reply(text="NZS 3604 covers timber framing.", results=SOURCES))
    assert out["searched"] is True and out["failed"] is False
    assert out["sources"][0]["domain"] == "standards.govt.nz"
    assert out["sources"][0]["url"].startswith("https://vertexaisearch.")


def test_the_budgets_fit_under_the_stop_and_the_gateway():
    total = web.VERDICT_BUDGET + web.WEB_BUDGET
    assert total + client.MIN_USEFUL_TIMEOUT <= web.HARD_STOP_SECONDS
    assert web.HARD_STOP_SECONDS < 29, "the gateway ceiling is not ours to raise"


# ------------------------------------------- the verdict judges what was searched

FOLLOW_UP = "When does it have to be finished?"
STANDALONE = "When does the Unit 11 backfill have to be finished?"


def test_the_verdict_judges_the_rewritten_question(monkeypatch):
    """Conversation memory retrieves with the standalone rewrite, so the
    verdict has to judge the excerpts against THAT. Judged against the pronoun
    version the model says no -- measured on TEST 2026-09-18, two rewritten
    follow-ups routed to the web, one of them answering with New Zealand's
    two-year consent rule while the records said Friday."""
    fake = FakeCall(verdict(True))
    monkeypatch.setattr(web.client, "call", fake)

    out = web.answer(FOLLOW_UP, CHUNKS, verdict_question=STANDALONE)

    assert out is None, "the records answer it; no lookup"
    assert STANDALONE in fake.calls[0]["prompt"]
    assert FOLLOW_UP not in fake.calls[0]["prompt"]


def test_the_rewrite_never_reaches_the_web_or_the_screen(monkeypatch):
    """The rewrite is derived from conversation history, and a history is a
    copy taken before a deletion. It may inform the verdict, which stays
    inside the account; the lookup and the admission screen see only what the
    asker typed."""
    seen = {}
    monkeypatch.setattr(web.question_admission, "screen",
                        lambda q, c: seen.update(question=q) or None)
    fake = FakeCall(verdict(False), reply(text="Two years.", results=SOURCES))
    monkeypatch.setattr(web.client, "call", fake)

    web.answer(FOLLOW_UP, CHUNKS, verdict_question=STANDALONE)

    assert seen["question"] == FOLLOW_UP
    assert STANDALONE not in fake.web_prompt
    assert FOLLOW_UP in fake.web_prompt


def test_no_rewrite_means_the_verdict_sees_the_question_as_asked(monkeypatch):
    """Absent, empty and whitespace all mean "there was nothing to resolve" --
    every caller that predates conversation memory keeps its old behaviour."""
    for arg in (None, "", "   "):
        fake = FakeCall(verdict(True))
        monkeypatch.setattr(web.client, "call", fake)
        assert web.answer(PUBLIC_Q, CHUNKS, verdict_question=arg) is None
        assert PUBLIC_Q in fake.calls[0]["prompt"]
