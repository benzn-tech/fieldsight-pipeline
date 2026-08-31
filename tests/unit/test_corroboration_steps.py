"""The four steps, and the two things they refuse to do.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §5.2, §5.3

Every model call is stubbed. What is under test is not whether haiku classifies
well -- no unit test can show that -- but whether this module reports only what
it was actually told, and stays quiet when it was told nothing usable. Those are
the failures that turn a corroboration card into a claim we cannot support.
"""
import json

import pytest

steps = pytest.importorskip("corroboration")
client = pytest.importorskip("corroboration_client")


class FakeCall:
    """Stands in for `corroboration_client.call`, one queued Reply per step."""

    def __init__(self, *replies):
        self.queue = list(replies)
        self.calls = []

    def __call__(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if not self.queue:
            raise AssertionError("a step called the model more times than expected")
        return self.queue.pop(0)


def reply(text="", results=(), error=None, search_error=None):
    return client.Reply(text=text,
                        search_results=[client.SearchResult(u, t) for u, t in results],
                        error=error, search_error=search_error, searched=bool(results))


def extraction(*items):
    return reply(text=json.dumps(list(items)))


def verdicts(*items):
    return reply(text=json.dumps(list(items)))


NAYLOR = {"entity": "Naylor Love Construction", "kind": "company",
          "claim": "was the main contractor on the job"}
SOURCES = [("https://naylorlove.co.nz/about", "About Naylor Love")]


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "true")


def _run(monkeypatch, *replies, question="who built it", answer="Naylor Love built it"):
    fake = FakeCall(*replies)
    monkeypatch.setattr(steps.client, "call", fake)
    return steps.corroborate(question, answer), fake


# ------------------------------------------------------------------ the happy path

def test_a_corroborated_claim_becomes_a_card_with_its_sources(monkeypatch):
    out, _ = _run(monkeypatch,
                  extraction(NAYLOR),
                  reply(text="Naylor Love is a large NZ contractor.", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "corroborated", "summary": "Public sources agree."}))
    assert len(out["corroborations"]) == 1
    card = out["corroborations"][0]
    assert card["state"] == "corroborated"
    assert card["claim"] == "was the main contractor on the job"
    assert card["sources"][0]["url"] == "https://naylorlove.co.nz/about"
    assert card["retrieved_at"].endswith("Z")
    assert out["timed_out"] is False and out["truncated"] is False


def test_conflicts_survives_the_pipeline(monkeypatch):
    """The state the feature exists for, and the easiest one to lose to an
    implementation that renders every finding as a summary."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "conflicts", "summary": "Sources name another firm."}))
    assert out["corroborations"][0]["state"] == "conflicts"


def test_no_checkable_claim_carries_no_sources(monkeypatch):
    """A card that cites sources invites the reader to hear it as verification.
    This state is a judgement about our own answer, and it has nothing to cite."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "no_checkable_claim", "summary": "Only named."}))
    card = out["corroborations"][0]
    assert card["state"] == "no_checkable_claim"
    assert card["sources"] == []


# ------------------------------------------------ it never reports a state it lacks

@pytest.mark.parametrize("bad", ["verified", "TRUE", "", None, "corroborated!", 1])
def test_a_state_outside_the_enum_drops_the_card(monkeypatch, bad):
    """Every available repair invents a finding: `corroborated` invents
    agreement, `not_found` invents an empty search, `no_checkable_claim` invents
    a judgement about the answer. So the entity is dropped and counted."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts({"entity": "Naylor Love Construction", "state": bad}))
    assert out["corroborations"] == []
    assert any(d["reason"] == "no usable state" for d in out["dropped"])


def test_an_entity_the_reconcile_step_ignored_is_dropped_not_guessed(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  verdicts())
    assert out["corroborations"] == []
    assert out["dropped"] == [{"entity": "Naylor Love Construction",
                               "reason": "no usable state"}]


def test_reconcile_returning_prose_instead_of_json_costs_the_cards(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES),
                  reply(text="Sure! Here is what I found: Naylor Love looks legitimate."))
    assert out["corroborations"] == [] and out["timed_out"] is True


def test_a_fenced_json_reply_is_still_read(monkeypatch):
    """Models add code fences. Treating that as unparseable would throw away a
    perfectly good verdict and report a timeout that did not happen."""
    fenced = reply(text='```json\n[{"entity": "Naylor Love Construction", '
                        '"state": "not_found", "summary": "nothing"}]\n```')
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="...", results=SOURCES), fenced)
    assert out["corroborations"][0]["state"] == "not_found"


# ------------------------------------------------- it never salvages a failed search

def test_a_failed_search_yields_no_cards_and_says_so(monkeypatch):
    """Step 3 is one call covering every entity, so there is no per-entity
    progress to keep. A shorter list would read as "we checked and found
    nothing", which is a claim about the world rather than about us."""
    out, _ = _run(monkeypatch, extraction(NAYLOR), reply(error="timeout"))
    assert out["corroborations"] == []
    assert out["timed_out"] is True


def test_a_failed_extraction_is_a_timeout_not_an_empty_answer(monkeypatch):
    out, _ = _run(monkeypatch, extraction(NAYLOR).__class__(error="timeout"))
    assert out["timed_out"] is True


def test_an_answer_naming_nothing_external_is_not_a_timeout(monkeypatch):
    """An empty extraction is a correct result for most Ask answers. Reporting
    it as a timeout would make the honest case look like a fault."""
    out, _ = _run(monkeypatch, extraction())
    assert out == {"corroborations": [], "dropped": [], "truncated": False,
                   "timed_out": False}


def test_a_search_tool_error_still_reaches_reconcile(monkeypatch):
    """The request succeeded and the model still wrote something; the tool error
    is logged, and the verdict it produces is `not_found` on its own terms
    rather than being upgraded here."""
    out, _ = _run(monkeypatch, extraction(NAYLOR),
                  reply(text="I could not search.", search_error="max_uses_exceeded"),
                  verdicts({"entity": "Naylor Love Construction",
                            "state": "not_found", "summary": "No sources reached."}))
    assert out["corroborations"][0]["state"] == "not_found"


# --------------------------------------------------------------- truncated vs timed_out

def test_truncated_is_the_cap_biting_and_not_a_deadline(monkeypatch):
    four = [dict(NAYLOR, entity=n) for n in
            ["Naylor Love Ltd", "Fletcher Building", "Hawkins Ltd", "Dominion Constructors"]]
    vs = [{"entity": e["entity"], "state": "not_found", "summary": "x"} for e in four]
    out, _ = _run(monkeypatch, extraction(*four),
                  reply(text="...", results=SOURCES), verdicts(*vs))
    assert out["truncated"] is True
    assert out["timed_out"] is False
    assert len(out["corroborations"]) == 3


def test_the_gate_refusing_everything_is_not_a_timeout(monkeypatch):
    """A privacy refusal and a missed deadline are different facts, and merging
    them would report the gate doing its job as an outage."""
    person = {"entity": "John Smith", "kind": "company", "claim": "was on site"}
    out, fake = _run(monkeypatch, extraction(person))
    assert out["timed_out"] is False
    assert out["dropped"][0]["reason"] == "shaped like a person's name"
    assert len(fake.calls) == 1, "it searched anyway"


def test_a_refused_entity_never_reaches_the_search_prompt(monkeypatch):
    """The gate's whole purpose, asserted against the string that actually
    leaves: the search step's prompt."""
    out, fake = _run(monkeypatch,
                     extraction(NAYLOR, {"entity": "Naylor Love $40k variation",
                                         "kind": "company", "claim": "was disputed"}),
                     reply(text="...", results=SOURCES),
                     verdicts({"entity": "Naylor Love Construction",
                               "state": "not_found", "summary": "x"}))
    search_prompt = fake.calls[1]["prompt"]
    assert "40k" not in search_prompt and "variation" not in search_prompt
    assert "Naylor Love Construction" in search_prompt


# ------------------------------------------------------------------------- the flag

def test_the_flag_is_read_at_call_time_not_import_time(monkeypatch):
    """A flag captured at import is a flag whose value is whatever the container
    started with. This repository has shipped switches that looked wired and only
    ever returned their default."""
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "false")
    assert steps.enabled() is False
    monkeypatch.setenv("ENABLE_EXTERNAL_CORROBORATION", "true")
    assert steps.enabled() is True


def test_the_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv("ENABLE_EXTERNAL_CORROBORATION", raising=False)
    assert steps.enabled() is False


# ---------------------------------------------------------- the budget is respected

def test_no_step_is_given_more_than_its_slice(monkeypatch):
    out, fake = _run(monkeypatch, extraction(NAYLOR),
                     reply(text="...", results=SOURCES),
                     verdicts({"entity": "Naylor Love Construction",
                               "state": "not_found", "summary": "x"}))
    assert fake.calls[0]["timeout"] <= steps.EXTRACT_BUDGET
    assert fake.calls[1]["timeout"] <= steps.SEARCH_BUDGET
    assert fake.calls[2]["timeout"] <= steps.RECONCILE_BUDGET
    assert sum(c["timeout"] for c in fake.calls) <= steps.HARD_STOP_SECONDS


def test_a_slow_first_step_leaves_the_later_steps_less_not_more(monkeypatch):
    """The budget is a share of one deadline, not three independent ones. A step
    that reads its own constant would let a slow predecessor push the total past
    the proxy's 30 s and turn a shaped body into a gateway error."""
    ticks = iter([0.0, 20.0, 20.0, 20.0, 20.0, 20.0])
    fake = FakeCall(extraction(NAYLOR), reply(text="...", results=SOURCES),
                    verdicts({"entity": "Naylor Love Construction",
                              "state": "not_found", "summary": "x"}))
    monkeypatch.setattr(steps.client, "call", fake)
    steps.corroborate("q", "a", clock=lambda: next(ticks))
    assert fake.calls[1]["timeout"] <= 4.0, "the search step ignored the elapsed time"


def test_the_cheap_steps_never_send_effort(monkeypatch):
    """`output_config.effort` is a 400 on haiku. The client drops it, and these
    two call sites are why that guard exists."""
    _, fake = _run(monkeypatch, extraction(NAYLOR),
                   reply(text="...", results=SOURCES),
                   verdicts({"entity": "Naylor Love Construction",
                             "state": "not_found", "summary": "x"}))
    assert fake.calls[0]["effort"] is None
    assert fake.calls[2]["effort"] is None


def test_only_the_search_step_gets_the_web_search_tool(monkeypatch):
    _, fake = _run(monkeypatch, extraction(NAYLOR),
                   reply(text="...", results=SOURCES),
                   verdicts({"entity": "Naylor Love Construction",
                             "state": "not_found", "summary": "x"}))
    assert fake.calls[0].get("tools") is None
    assert fake.calls[1]["tools"] == [client.WEB_SEARCH_TOOL]
    assert fake.calls[2].get("tools") is None


# --------------------------------------------------------------- it never raises

@pytest.mark.parametrize("bad", [None, "", 0])
def test_a_missing_question_or_answer_is_an_empty_body(monkeypatch, bad):
    monkeypatch.setattr(steps.client, "call", FakeCall())
    assert steps.corroborate(bad, "answer")["corroborations"] == []
    assert steps.corroborate("question", bad)["corroborations"] == []


def test_extraction_returning_junk_does_not_raise(monkeypatch):
    for junk in ['{"not": "a list"}', "not json at all", ""]:
        out, _ = _run(monkeypatch, reply(text=junk))
        assert out["corroborations"] == []
