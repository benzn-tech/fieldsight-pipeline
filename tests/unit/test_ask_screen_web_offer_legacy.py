"""Ask screen web offer on the legacy `_rag_answer` path (spec 2026-10-16
section 3, section 7 "Legacy screen").

Every case is driven through the real routing code with the rag-search, the LLM
and web_answer's entry points faked (the rig from test_ask_voice_web_gates).
Each one is run with and without `offer_web`: without it the screen must behave
exactly as it does today, which is the prod-parity half.
"""
import pytest

import lambda_ask_agent as laa   # noqa: E402
import voice_web                 # noqa: E402
from tests.unit.test_ask_voice_web_gates import (  # noqa: E402
    CHUNK, FAR_CHUNK, WEB_OK, FakeLambdaClient, ask, wire)

ANCHORED = "What did we do yesterday?"
PHRASE_Q = "What does NZS 4541 require? Search online."
STRIPPED_Q = "What does NZS 4541 require?"


def _no_web(rig):
    return "general" not in rig.events and "on_web_start" not in rig.events


def _spy_general(mp):
    seen = []
    real = laa._general_run

    def spy(web_mod, question, cls, chunks, started, route, **kw):
        seen.append(question)
        return real(web_mod, question, cls, chunks, started, route, **kw)

    mp.setattr(laa, "_general_run", spy)
    return seen


# ---- the parity pair: an empty set on a general question ---------------------

def test_screen_empty_general_without_offer_web_still_goes_online(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb())
    assert out["from_web"] is True
    assert "web_offer" not in out
    assert rig.events.count("general") == 1
    assert rig.events.count("classify") == 1


def test_screen_empty_general_with_offer_web_offers_without_any_call(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), offer_web=True)
    assert out["answer"] == voice_web.SCREEN_OFFER_NO_RECORDS
    assert out["web_offer"] is True
    assert out["citations"] == []
    assert "from_web" not in out
    assert rig.events.count("general") == 0
    assert rig.events.count("classify") == 0
    assert rig.events == [] and rig.prompts == []


def test_screen_offer_text_asks_no_question():
    assert voice_web.SCREEN_OFFER_NO_RECORDS == "Your site records don't cover that."
    # Voice keeps its spoken question.
    assert voice_web.OFFER_NO_RECORDS.endswith("Want me to search online?")


# ---- typed phrase -----------------------------------------------------------

def test_typed_phrase_without_offer_web_is_not_honoured(monkeypatch):
    # Time-anchored, so the automatic path would not go online either: the
    # phrase is the only thing that could send it out, and it must not.
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), question=ANCHORED + " Search online.")
    assert _no_web(rig)
    assert "from_web" not in out and "web_offer" not in out
    assert out["answer"].startswith("No relevant records found")


def test_typed_phrase_with_offer_web_is_honoured(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), question=ANCHORED + " Search online.", offer_web=True)
    assert out["from_web"] is True
    assert "web_offer" not in out


# ---- scoped -----------------------------------------------------------------

@pytest.mark.parametrize("chunks", [(), (CHUNK,), (FAR_CHUNK,)])
@pytest.mark.parametrize("offer", [{}, {"offer_web": True}])
def test_scoped_with_web_intent_gets_no_web_and_no_offer(monkeypatch, chunks, offer):
    rig = wire(monkeypatch, chunks=chunks, kind="general", records_answer=False,
               web=WEB_OK)
    out = ask(rig.cb(), web_intent=True, scoped=True, date="2026-09-01", **offer)
    assert _no_web(rig)
    assert "from_web" not in out and "web_offer" not in out


def test_scoped_with_typed_phrase_and_offer_web_gets_no_web(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), question=PHRASE_Q, offer_web=True, scoped=True,
              date="2026-09-01")
    assert _no_web(rig)
    assert "from_web" not in out and "web_offer" not in out


# ---- time anchor ------------------------------------------------------------

@pytest.mark.parametrize("chunks", [(), (CHUNK,), (FAR_CHUNK,)])
@pytest.mark.parametrize("offer", [{}, {"offer_web": True}])
def test_time_anchored_without_intent_gets_no_offer(monkeypatch, chunks, offer):
    rig = wire(monkeypatch, chunks=chunks, kind="general", records_answer=False,
               web=WEB_OK)
    out = ask(rig.cb(), question=ANCHORED, **offer)
    assert _no_web(rig)
    assert "from_web" not in out and "web_offer" not in out


@pytest.mark.parametrize("chunks", [(), (CHUNK,)])
def test_time_anchored_with_offer_web_and_web_intent_goes_online(monkeypatch, chunks):
    rig = wire(monkeypatch, chunks=chunks, kind="general", records_answer=False,
               web=WEB_OK)
    out = ask(rig.cb(), question=ANCHORED, offer_web=True, web_intent=True)
    assert out["from_web"] is True
    assert "web_offer" not in out
    assert "general" in rig.events


def test_time_anchored_with_web_intent_but_no_offer_web_stays_on_the_records(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), question=ANCHORED, web_intent=True)
    assert _no_web(rig)
    assert "from_web" not in out


# ---- identity error ---------------------------------------------------------

@pytest.mark.parametrize("offer", [{}, {"offer_web": True}])
def test_identity_error_gets_no_offer_and_no_web(monkeypatch, offer):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeLambdaClient(
        [{"chunks": [], "error": "caller not provisioned"}]))
    out = ask(rig.cb(), **offer)
    assert _no_web(rig) and "classify" not in rig.events
    assert "from_web" not in out and "web_offer" not in out


# ---- distance-gated / not-useful set ----------------------------------------

def test_distance_gated_without_offer_web_still_goes_online(monkeypatch):
    rig = wire(monkeypatch, chunks=(FAR_CHUNK,), kind="general", web=WEB_OK)
    out = ask(rig.cb())
    assert out["from_web"] is True
    assert "web_offer" not in out


@pytest.mark.parametrize("chunks,records_answer", [((FAR_CHUNK,), True),
                                                   ((CHUNK,), False)])
def test_distance_gated_or_not_useful_with_offer_web_offers_without_synthesis(
        monkeypatch, chunks, records_answer):
    rig = wire(monkeypatch, chunks=chunks, kind="general",
               records_answer=records_answer, web=WEB_OK)
    out = ask(rig.cb(), offer_web=True)
    assert out["answer"] == voice_web.SCREEN_OFFER_NO_RECORDS
    assert out["web_offer"] is True
    assert out["citations"] == []
    assert _no_web(rig)
    assert rig.prompts == []          # no records synthesis


# ---- partial records --------------------------------------------------------

def test_partial_records_without_offer_web_composes_with_the_web(monkeypatch):
    rig = wire(monkeypatch, kind="mixed", records_answer=True, web=WEB_OK,
               answer="Grounded answer [1].")
    out = ask(rig.cb())
    assert out["from_web"] is True
    assert "web_offer" not in out


def test_partial_records_with_offer_web_flags_the_offer_and_appends_nothing(monkeypatch):
    rig = wire(monkeypatch, kind="mixed", records_answer=True, web=WEB_OK,
               answer="Grounded answer [1].")
    out = ask(rig.cb(), offer_web=True)
    assert out["answer"] == "Grounded answer [1]."
    assert out["web_offer"] is True
    assert "from_web" not in out
    assert _no_web(rig)
    assert len(rig.prompts) == 1      # the records synthesis only


def test_partial_records_on_voice_still_appends_the_spoken_offer(monkeypatch):
    rig = wire(monkeypatch, kind="mixed", records_answer=True, web=WEB_OK,
               answer="Grounded answer [1].")
    out = ask(rig.cb(), mode="voice", voice_web=True, offer_web=True)
    assert out["answer"] == "Grounded answer [1]. " + voice_web.OFFER_ALSO


# ---- the question sent to the web -------------------------------------------

def test_typed_phrase_with_offer_web_strips_it_in_the_empty_branch(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    seen = _spy_general(monkeypatch)
    out = ask(rig.cb(), question=PHRASE_Q, offer_web=True)
    assert seen == [STRIPPED_Q]
    assert out["from_web"] is True


def test_typed_phrase_with_offer_web_strips_it_in_the_classified_branch(monkeypatch):
    rig = wire(monkeypatch, kind="mixed", records_answer=True, web=WEB_OK)
    seen = _spy_general(monkeypatch)
    composed = []
    import web_answer

    def compose(records_text, general_block, question, budget, voice=False):
        composed.append(question)
        return {"answer": records_text, "conflicts": [], "composed": True}

    monkeypatch.setattr(web_answer, "compose", compose)
    out = ask(rig.cb(), question=PHRASE_Q, offer_web=True)
    assert seen == [STRIPPED_Q]
    assert composed == [STRIPPED_Q]
    assert out["from_web"] is True


def test_web_intent_with_offer_web_sends_the_question_unchanged(monkeypatch):
    # An accepted offer re-sends the question as first asked.
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    seen = _spy_general(monkeypatch)
    ask(rig.cb(), question=PHRASE_Q, offer_web=True, web_intent=True)
    assert seen == [PHRASE_Q]


def test_typed_phrase_without_offer_web_sends_the_question_unchanged(monkeypatch):
    # Legacy screen, unscoped general: today's automatic web, unstripped.
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    seen = _spy_general(monkeypatch)
    ask(rig.cb(), question=PHRASE_Q)
    assert seen == [PHRASE_Q]
