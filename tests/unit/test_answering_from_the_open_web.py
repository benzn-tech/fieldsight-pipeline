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
