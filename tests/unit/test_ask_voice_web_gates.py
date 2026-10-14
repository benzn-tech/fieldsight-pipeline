"""Ask Agent: voice mode reaching the web branch (device Ask two-phase spec
2026-10-15 section 6 and 8).

Driven through `_rag_answer` with a fake rag-search and web_answer's entry
points stubbed, so the real routing code is what is under test.
"""
import io
import json
import os
import sys
import types

import pytest

os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

import lambda_ask_agent as laa   # noqa: E402
import llm_utils                 # noqa: E402
import dashscope_utils           # noqa: E402
import web_answer                # noqa: E402
import voice_web                 # noqa: E402
from tests.unit.ask_web_fakes import block  # noqa: E402

NOW = "2026-08-30T09:00:00+00:00"
CHUNK = {"id": "c-1", "chunk_text": "Scaffold tagged.", "chunk_type": "topic",
         "topic_id": None, "source_s3_key": "reports/2026-09-03/Ben/daily_report.json",
         "metadata": {}, "topic_title": "Scaffold", "topic_summary": "",
         "report_date": "2026-09-03", "site_id": "5c0e8d7a-1111-4222-8333-444455556666",
         "site_name": "UC PK", "site_slug": "uc-pk", "distance": 0.1}
FAR_CHUNK = dict(CHUNK, distance=0.9, topic_title="Unrelated", id="c-2")
Q = "What does NZS 4541 require?"
WEB_OK = {"answer": "According to NZS 4541, it is fine."}


class FakeLambdaClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def invoke(self, FunctionName, InvocationType, Payload):
        body = self.responses.pop(0) if self.responses else {"chunks": []}
        return {"Payload": io.BytesIO(json.dumps(body).encode("utf-8"))}


class Rig:
    def __init__(self):
        self.events = []        # ordered: classify, general, on_web_start
        self.general_kw = []
        self.compose_kw = []
        self.prompts = []

    def cb(self):
        return lambda: self.events.append("on_web_start")


def wire(mp, chunks=(CHUNK,), web=None, kind="general", records_answer=True,
         answer="Grounded answer [1]."):
    mp.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    mp.setattr(laa, "_get_lambda_client",
               lambda: FakeLambdaClient([{"chunks": list(chunks)}]))
    rig = Rig()

    def fake_llm(prompt, max_tokens=4096, force_json=False, **kw):
        rig.prompts.append(prompt)
        return (answer, None)

    mp.setattr(llm_utils, "call_llm", fake_llm)
    mp.setenv("ENABLE_WEB_ANSWER", "true")

    def fake_classify(question, ch, budget):
        rig.events.append("classify")
        return {"kind": kind, "records_answer": records_answer and bool(ch),
                "source": "model"}

    def fake_general(question, budget, **kw):
        rig.events.append("general")
        rig.general_kw.append(kw)
        return block(**(web or {"failed": True, "status": "unverified"}))

    def fake_compose(records_text, general_block, question, budget, voice=False):
        rig.compose_kw.append({"voice": voice})
        return {"answer": records_text + " Web says so.", "conflicts": [], "composed": True}

    mp.setattr(web_answer, "classify", fake_classify)
    mp.setattr(web_answer, "general_answer", fake_general)
    mp.setattr(web_answer, "compose", fake_compose)
    return rig


def ask(on_web_start=None, **body):
    body.setdefault("caller_sub", "sub-1")
    body.setdefault("tz", "Pacific/Auckland")
    body.setdefault("now", NOW)
    body.setdefault("question", Q)
    return laa._rag_answer(body, on_web_start=on_web_start)


# ---- gates -----------------------------------------------------------------

def test_voice_without_voice_web_never_reaches_the_web(monkeypatch):
    rig = wire(monkeypatch, web={"answer": "From the web."})
    out = ask(rig.cb(), mode="voice")
    assert rig.events == []
    assert "from_web" not in out


def test_voice_without_voice_web_and_no_records_never_reaches_the_web(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web={"answer": "From the web."})
    out = ask(rig.cb(), mode="voice")
    assert rig.events == []
    assert "from_web" not in out


def test_voice_web_records_miss_calls_on_web_start_once_before_the_run(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), mode="voice", voice_web=True)
    assert out["from_web"] is True
    assert rig.events.count("on_web_start") == 1
    assert rig.events.index("on_web_start") < rig.events.index("general")
    assert rig.general_kw[0].get("voice") is True


def test_voice_web_main_branch_calls_on_web_start_once_before_the_run(monkeypatch):
    rig = wire(monkeypatch, kind="general", records_answer=False, web=WEB_OK)
    out = ask(rig.cb(), mode="voice", voice_web=True)
    assert out["from_web"] is True
    assert rig.events.count("on_web_start") == 1
    assert rig.events.index("on_web_start") < rig.events.index("general")


def test_on_web_start_is_not_called_when_the_web_is_not_reached(monkeypatch):
    rig = wire(monkeypatch, kind="project", web=None)
    ask(rig.cb(), mode="voice", voice_web=True)
    assert "on_web_start" not in rig.events


def test_a_raising_on_web_start_does_not_stop_the_answer(monkeypatch):
    wire(monkeypatch, chunks=(), web=WEB_OK)

    def boom():
        raise RuntimeError("device gone")

    out = ask(boom, mode="voice", voice_web=True)
    assert out["from_web"] is True


# ---- web_intent ------------------------------------------------------------

def test_web_intent_overrides_a_scoped_ask(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    out = ask(rig.cb(), mode="voice", voice_web=True, web_intent=True, scoped=True, date="2026-09-01")
    assert out["from_web"] is True
    assert "general" in rig.events


def test_scoped_without_web_intent_still_blocks_the_web(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web={"answer": "From the web."})
    out = ask(rig.cb(), mode="voice", voice_web=True, scoped=True, date="2026-09-01")
    assert "general" not in rig.events
    assert "from_web" not in out


def test_web_intent_does_not_call_classify_in_the_main_branch(monkeypatch):
    rig = wire(monkeypatch, web=WEB_OK)
    out = ask(rig.cb(), mode="voice", voice_web=True, web_intent=True)
    assert "classify" not in rig.events
    assert "general" in rig.events
    assert out["from_web"] is True


def test_web_intent_no_chunk_inside_the_gate_routes_general(monkeypatch):
    rig = wire(monkeypatch, chunks=(FAR_CHUNK,), web=WEB_OK)
    seen = {}
    real = laa._general_run

    def spy(web_mod, question, cls, chunks, started, route, **kw):
        seen["cls"] = cls
        return real(web_mod, question, cls, chunks, started, route, **kw)

    monkeypatch.setattr(laa, "_general_run", spy)
    ask(rig.cb(), mode="voice", voice_web=True, web_intent=True)
    assert seen["cls"] == {"kind": "general", "records_answer": False, "source": "intent"}
    assert "classify" not in rig.events


def test_web_intent_with_a_chunk_inside_the_gate_routes_mixed(monkeypatch):
    rig = wire(monkeypatch, chunks=(CHUNK,), web=WEB_OK)
    seen = {}
    real = laa._general_run

    def spy(web_mod, question, cls, chunks, started, route, **kw):
        seen["cls"] = cls
        return real(web_mod, question, cls, chunks, started, route, **kw)

    monkeypatch.setattr(laa, "_general_run", spy)
    ask(rig.cb(), mode="voice", voice_web=True, web_intent=True)
    assert seen["cls"] == {"kind": "mixed", "records_answer": True, "source": "intent"}
    assert rig.compose_kw == [{"voice": True}]


def test_web_intent_with_no_chunks_skips_classify(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web=WEB_OK)
    ask(rig.cb(), mode="voice", voice_web=True, web_intent=True)
    assert "classify" not in rig.events


# ---- speakable guard and citation strip ------------------------------------

def test_a_voice_web_answer_with_a_url_is_replaced(monkeypatch):
    wire(monkeypatch, chunks=(), web={"answer": "See https://x for the rule."})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == voice_web.NO_CLEAR_ANSWER


def test_a_composed_voice_answer_loses_a_trailing_url_sentence(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "ok"})
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "Scaffold tagged [1]. See https://x", "conflicts": [], "composed": True})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Scaffold tagged."


def test_a_composed_voice_answer_with_a_url_up_front_falls_back_to_the_records_answer(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "ok"})
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "See https://x. Scaffold tagged [1].", "conflicts": [], "composed": True})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Grounded answer."


def test_voice_web_answers_lose_citation_markers_before_the_guard(monkeypatch):
    wire(monkeypatch, chunks=(),
         web={"answer": "According to NZS 4541 it is fine [1]. Also fine [2, 3]."})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "According to NZS 4541 it is fine. Also fine."


def test_screen_mode_keeps_citation_markers_and_urls(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web={"answer": "Fine [1] see https://x"})
    out = ask()
    assert out["answer"] == "Fine [1] see https://x"
    assert rig.general_kw[0].get("voice") is False


def test_a_voice_records_only_answer_is_not_touched(monkeypatch):
    wire(monkeypatch, kind="project", web=None, answer="The slab is done [1] https://x")
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "The slab is done [1] https://x"


def test_project_fallback_lead_is_skipped_in_voice_only(monkeypatch):
    wire(monkeypatch, kind="project", records_answer=False, web=WEB_OK)
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == WEB_OK["answer"]
    wire(monkeypatch, kind="project", records_answer=False, web=WEB_OK)
    out2 = ask()
    assert out2["answer"].startswith(laa.PROJECT_FALLBACK_LEAD)


# ---- fix round 1 -----------------------------------------------------------

def test_web_intent_is_ignored_in_screen_mode_even_when_scoped(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web={"answer": "From the web."})
    out = ask(rig.cb(), web_intent=True, voice_web=True, scoped=True, date="2026-09-01")
    assert "general" not in rig.events
    assert "from_web" not in out


def test_web_intent_without_voice_web_is_ignored_in_voice(monkeypatch):
    rig = wire(monkeypatch, chunks=(), web={"answer": "From the web."})
    out = ask(rig.cb(), mode="voice", web_intent=True, scoped=True, date="2026-09-01")
    assert rig.events == []
    assert "from_web" not in out


def test_web_intent_without_voice_web_keeps_classify_in_the_main_branch(monkeypatch):
    rig = wire(monkeypatch, kind="project", web=None)
    ask(rig.cb(), web_intent=True)
    assert "classify" in rig.events


@pytest.mark.parametrize("raw", [
    "Fine [1-3].", "Fine [1–3].", "Fine [1, 2].", "Fine [1][2].", "Fine [1] [2]."])
def test_voice_strips_ranges_lists_and_adjacent_markers(monkeypatch, raw):
    wire(monkeypatch, chunks=(), web={"answer": raw})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Fine."


def test_records_fallback_is_stripped_before_it_is_spoken(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "ok"}, answer="Scaffold tagged [1][2].")
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "See https://x", "conflicts": [], "composed": True})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Scaffold tagged."


def test_an_unspeakable_records_fallback_becomes_no_clear_answer(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "ok"}, answer="Read https://y [1]")
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "See https://x", "conflicts": [], "composed": True})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == voice_web.NO_CLEAR_ANSWER


def test_voice_d5_message_goes_through_the_guard(monkeypatch):
    wire(monkeypatch, chunks=(), web=None)
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "No relevant records found for this question."
    monkeypatch.setattr(voice_web, "speakable", lambda t: False)
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == voice_web.NO_CLEAR_ANSWER


def test_screen_d5_message_is_unchanged(monkeypatch):
    wire(monkeypatch, chunks=(), web=None)
    monkeypatch.setattr(voice_web, "speakable", lambda t: False)
    out = ask()
    assert out["answer"] == "No relevant records found for this question."


# ---- screen path unchanged -------------------------------------------------

def test_screen_prompt_is_the_unchanged_screen_prompt(monkeypatch):
    rig = wire(monkeypatch, kind="project", web=None)
    ask(voice_web=True)
    assert len(rig.prompts) == 1
    assert laa.RAG_SYSTEM_CONTEXT_VOICE not in rig.prompts[0]
    assert laa.RAG_SYSTEM_CONTEXT in rig.prompts[0]


# ---- handler dispatch ------------------------------------------------------

def test_voice_start_reaches_ask_voice_turn_not_the_old_voice_path(monkeypatch):
    seen = {}
    fake = types.ModuleType("ask_voice_turn")

    def start(body):
        seen["body"] = body
        return {"turn_id": "t-1"}

    fake.start = start
    monkeypatch.setitem(sys.modules, "ask_voice_turn", fake)
    monkeypatch.setattr(laa, "_voice_answer",
                        lambda body: pytest.fail("old voice path must not run"))
    resp = laa.lambda_handler({"body": json.dumps(
        {"mode": "voice_start", "audio": "AAAA", "caller_sub": "sub-1"})}, None)
    assert json.loads(resp["body"]) == {"turn_id": "t-1"}
    assert seen["body"]["mode"] == "voice_start"


def test_a_long_voice_web_answer_is_trimmed_to_two_sentences(monkeypatch):
    long = "Use 4 m spacing per NZS 4541. Coverage is 12 square metres. " + "More detail here. " * 30
    wire(monkeypatch, chunks=(), web={"answer": long})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Use 4 m spacing per NZS 4541. Coverage is 12 square metres."


def test_a_failed_compose_in_voice_speaks_the_web_answer(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "Per NZS 3604, studs are at 600 centres."})
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "Grounded answer.", "conflicts": [], "composed": False})
    out = ask(mode="voice", voice_web=True)
    assert out["answer"] == "Per NZS 3604, studs are at 600 centres."


def test_a_failed_compose_on_screen_keeps_the_records_answer(monkeypatch):
    wire(monkeypatch, kind="mixed", web={"answer": "Per NZS 3604, studs are at 600 centres."})
    monkeypatch.setattr(web_answer, "compose", lambda *a, **k: {
        "answer": "Grounded answer.", "conflicts": [], "composed": False})
    out = ask()
    assert out["answer"] == "Grounded answer."
