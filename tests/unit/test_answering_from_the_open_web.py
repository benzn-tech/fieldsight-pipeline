"""The smallest path from "the records do not cover this" to an answer.

Five review rounds produced a design and no working path. This is the design's
first route built end to end, and these tests hold the two things that make it
safe rather than merely working: what leaves the account, and what it says when
it fails.

The measurements the shape rests on, all against the deployed vendor:

    retrieval  1.36s   gate 3.23s   search 11.40s   compose 5.31s

Answering first and looking up second is 33.2s against a 29s gateway. Asking
first turns the gate into a router and both branches fit -- 21.30s when it looks
something up, 16.49s when it does not.
"""
import pytest

llm = pytest.importorskip("llm_utils")
client = pytest.importorskip("corroboration_client")
web = pytest.importorskip("web_answer")


CHUNKS = [
    {"site_name": "SB1108", "report_date": "2026-09-02", "topic_title": "Morning",
     "chunk_text": "Ben covered the hot works permit. Neil signed off the scaffold "
                   "variation at Ellesmere for $12,400."},
]


class FakeCall:
    """Records every call so a test can assert on WHAT LEFT, not only on what
    came back. The search step is the one that reaches a search engine, so its
    prompt is the thing worth reading."""

    def __init__(self, *replies):
        self.queue = list(replies)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append(dict(kw, prompt=prompt))
        if not self.queue:
            raise AssertionError("a step called the model more times than expected")
        return self.queue.pop(0)

    @property
    def search_prompt(self):
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


def verdict(answered, *subjects):
    import json
    return reply(text=json.dumps({"answered": answered,
                                  "subjects": list(subjects)}))


NZS = {"entity": "NZS 3604", "kind": "standard"}
SOURCES = [("https://vertexaisearch.cloud.google.com/redirect/AUZ", "standards.govt.nz")]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "true")


def run(monkeypatch, *replies, question="what is the NZ standard for timber framing"):
    fake = FakeCall(*replies)
    monkeypatch.setattr(web.client, "call", fake)
    return web.answer(question, CHUNKS), fake


# ------------------------------------------------------- what leaves the account

def test_the_search_prompt_carries_names_and_nothing_else(monkeypatch):
    """The one call that reaches a search engine. The excerpt above contains a
    person, a site and a dollar figure; none of them may appear."""
    _, fake = run(monkeypatch, verdict(False, NZS), reply(text="...", results=SOURCES),
                  reply(text="NZS 3604 covers timber framing [standards.govt.nz]."))
    sent = fake.search_prompt
    assert "NZS 3604" in sent
    for private in ("Neil", "Ellesmere", "12,400", "hot works", "SB1108"):
        assert private not in sent, "conversation text reached the search step: " + private


def test_subjects_carry_no_claim(monkeypatch):
    """`screen()` inspects the entity string and forwards `claim` verbatim into
    the search-enabled call. Safe only while claims come from an ANSWER. These
    come from a QUESTION, so there must be nothing to forward."""
    seen = {}
    real_screen = web.gate.screen

    def spy(entities, **kw):
        seen["entities"] = entities
        return real_screen(entities, **kw)

    monkeypatch.setattr(web.gate, "screen", spy)
    run(monkeypatch, verdict(False, NZS), reply(text="...", results=SOURCES),
        reply(text="an answer"))
    assert all(e["claim"] is None for e in seen["entities"])


def test_a_subject_the_gate_refuses_never_reaches_the_search(monkeypatch):
    """A person's name mislabelled `company` is the case the gate exists for."""
    out, fake = run(monkeypatch,
                    verdict(False, {"entity": "Neil Blunden", "kind": "company"}))
    assert fake.search_prompt is None, "a refused subject was searched anyway"
    assert out["searched"] is False
    assert out["dropped"], "a refusal must be reported, not silently dropped"


# ------------------------------------------------------------- when it fires

def test_records_that_answer_the_question_are_left_alone(monkeypatch):
    """The common case, and the one that must cost nothing beyond the gate."""
    out, fake = run(monkeypatch, verdict(True))
    assert out is None, "it looked something up for a question the records answered"
    assert len(fake.calls) == 1


def test_a_question_naming_nothing_external_is_not_looked_up(monkeypatch):
    """"What did we decide about the slab" names nothing outside the account.
    Inventing a subject here would send the customer's own affairs to a search
    engine."""
    out, fake = run(monkeypatch, verdict(False))
    assert fake.search_prompt is None
    assert out["subjects"] == [] and out["searched"] is False


def test_the_flag_is_read_at_call_time(monkeypatch):
    """A flag captured at import survives a warm container after the stack has
    been redeployed with it off."""
    monkeypatch.setenv("ENABLE_WEB_ANSWER", "false")
    assert web.answer("q", CHUNKS) is None


# --------------------------------------------------- what it says when it fails

def test_prose_about_a_search_that_never_ran_produces_no_answer(monkeypatch):
    """Measured on a second vendor: 200 OK, zero results, and a paragraph
    asserting findings. A model's account of its own tool use is not evidence."""
    out, _ = run(monkeypatch, verdict(False, NZS),
                 reply(text="I searched and the standard is confirmed.", searched=False))
    assert out["answer"] is None and out["searched"] is False
    assert out["timed_out"] is False and out["failed"] is False


def test_a_gate_that_did_not_return_a_verdict_does_not_search(monkeypatch):
    """Fail closed. An unreadable verdict is not permission to search."""
    out, fake = run(monkeypatch, reply(text="Sure! Here's what I think."))
    assert out is None
    assert fake.search_prompt is None


def test_a_search_that_timed_out_says_so_and_a_broken_one_does_not(monkeypatch):
    late, _ = run(monkeypatch, verdict(False, NZS),
                  reply(error="read timeout", timed_out=True))
    assert late["timed_out"] is True and late["failed"] is False

    broke, _ = run(monkeypatch, verdict(False, NZS), reply(error="HTTP 502"))
    assert broke["failed"] is True and broke["timed_out"] is False


def test_a_compose_failure_keeps_the_sources_it_already_had(monkeypatch):
    """The search succeeded and cost real time. Throwing its sources away
    because the last step broke reports less than we know."""
    out, _ = run(monkeypatch, verdict(False, NZS), reply(text="...", results=SOURCES),
                 reply(error="HTTP 500"))
    assert out["answer"] is None and out["failed"] is True
    assert out["sources"], "the sources were discarded"


# ------------------------------------------------------------- the happy path

def test_an_answer_carries_its_sources_by_publisher(monkeypatch):
    """The vendor returns Google grounding redirects, so parsing the URL would
    attribute every source to `vertexaisearch.cloud.google.com`."""
    out, _ = run(monkeypatch, verdict(False, NZS), reply(text="findings", results=SOURCES),
                 reply(text="NZS 3604 is the NZ timber-framing standard."))
    assert out["answer"].startswith("NZS 3604")
    assert out["searched"] is True and out["failed"] is False
    assert out["sources"][0]["domain"] == "standards.govt.nz"
    assert out["sources"][0]["url"].startswith("https://vertexaisearch.")


def test_the_budgets_fit_under_the_stop_and_the_gateway():
    """Sized from measurement; the sum is what makes the router shape work."""
    total = web.GATE_BUDGET + web.SEARCH_BUDGET + web.COMPOSE_BUDGET
    assert total + client.MIN_USEFUL_TIMEOUT <= web.HARD_STOP_SECONDS
    assert web.HARD_STOP_SECONDS < 29, "the gateway ceiling is not ours to raise"
