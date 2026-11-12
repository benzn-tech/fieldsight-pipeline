"""Voice Ask speed-up, 2026-11-12: what the owner's three device turns that morning showed.

- A records miss on voice now searches the web at once under ASK_VOICE_AUTO_WEB
  (pre-check first, project documents searched in round 1 beside the records).
- "Yes, please. Checking online." is a yes and an online request.
- The rewrite and the pre-check run on the judge's routing under a wall-clock limit.
"""
import json
import time

import pytest

import ask_orchestrator as orch
import ask_rewrite
import llm_tools_client
import voice_web
import web_answer
from llm_tools_client import ChatTurn
from tests.unit.test_ask_orchestrator_judge import (  # noqa: F401 - autouse env fixture
    FakeTools, ScriptChat, _block, _rows, env, final, run, voice_body)

# The judge file's autouse fixture replaces this with a refusal; the wall-clock tests
# below need the real one.
_REAL_WITHIN = llm_tools_client.complete_json_within


# ------------------------------------------------------------------ policy

@pytest.mark.parametrize("flag,expected", [("true", "auto"), ("false", "offer"), (None, "offer")])
def test_voice_policy_follows_the_auto_web_flag(monkeypatch, flag, expected):
    if flag is None:
        monkeypatch.delenv("ASK_VOICE_AUTO_WEB", raising=False)
    else:
        monkeypatch.setenv("ASK_VOICE_AUTO_WEB", flag)
    assert orch.policy("voice", False, False, False) == expected


def test_auto_web_never_reaches_a_dated_or_scoped_voice_question(monkeypatch):
    # "What did we do yesterday?" stays off the web (#1120).
    monkeypatch.setenv("ASK_VOICE_AUTO_WEB", "true")
    assert orch.policy("voice", False, True, False) == "never"
    assert orch.policy("voice", True, False, False) == "never"


def test_auto_web_leaves_the_screen_alone(monkeypatch):
    monkeypatch.setenv("ASK_VOICE_AUTO_WEB", "true")
    assert orch.policy("screen", False, False, False, offer_web=True) == "offer"


# ------------------------------------------------------------------ round 1

def test_round1_searches_documents_for_every_question_when_asked():
    q = "do you know what size of the timber lintels would be according to 3604?"
    assert [c["name"] for c in orch.round1_calls(q, None, None, None, documents=True)] == [
        "search_records"]
    assert [c["name"] for c in orch.round1_calls(
        q, None, None, None, documents=True, always_documents=True)] == [
        "search_records", "search_project_documents"]
    # Without the documents tool offered, never.
    assert [c["name"] for c in orch.round1_calls(
        q, None, None, None, documents=False, always_documents=True)] == ["search_records"]


class _DocTools(FakeTools):
    TOOLS = FakeTools.TOOLS + [{"name": "search_project_documents",
                                "description": "d", "parameters": {"type": "object"}}]


@pytest.mark.parametrize("flag,channel,expected", [
    ("true", "voice", ["search_records", "search_project_documents"]),
    ("false", "voice", ["search_records"]),
    ("true", "screen", ["search_records"]),
])
def test_run_round1_wires_documents_to_voice_auto_only(monkeypatch, flag, channel, expected):
    from tests.unit.test_ask_orchestrator_round1 import _ctx, _req
    monkeypatch.setenv("ASK_VOICE_AUTO_WEB", flag)
    tools = _DocTools()
    orch.run_round1(_req(question="What size are timber lintels per 3604?",
                         asked="What size are timber lintels per 3604?", channel=channel),
                    _ctx(), tools)
    assert [c["name"] for c in tools.batches[0]] == expected


# ------------------------------------------------------------------ phrases

def test_the_owners_reply_is_a_yes_and_an_online_request():
    t = "Yes, please. Checking online."
    assert voice_web.reply_kind(t) == "yes"
    assert voice_web.detect_intent(t)[0] is True
    assert voice_web.is_bare_search_request(t) is True


@pytest.mark.parametrize("reply", ["Go for it", "Sure, do that", "Approved", "Yes, check it online",
                                   "Please do", "Checking online, please"])
def test_common_ways_of_agreeing(reply):
    assert voice_web.reply_kind(reply) == "yes"


@pytest.mark.parametrize("reply", ["Thank you", "Okay", "Who poured B2?"])
def test_filler_and_questions_are_not_a_yes(reply):
    assert voice_web.reply_kind(reply) is None


@pytest.mark.parametrize("text", ["The portal will go online next week",
                                  "Is the site going online soon?",
                                  "check online induction status for the plumber"])
def test_site_speech_about_online_things_is_not_a_web_request(text):
    assert voice_web.detect_intent(text)[0] is False


def test_go_online_and_strips_cleanly():
    assert voice_web.detect_intent("Can you go online and find NZS 3604 lintel sizes") == (
        True, "Can you find NZS 3604 lintel sizes")


# ------------------------------------------------------------------ wall clock

def test_complete_json_within_abandons_a_slow_call(monkeypatch):
    def slow(*a, **k):
        time.sleep(3)
        return ChatTurn(text='{"web": "no"}')
    monkeypatch.setattr(llm_tools_client, "complete_json", slow)
    t = time.monotonic()
    turn = _REAL_WITHIN("p", seconds=1.2, caller="x")
    assert time.monotonic() - t < 2.0
    assert turn.timed_out is True and turn.error


def test_complete_json_within_passes_the_limit_down(monkeypatch):
    seen = {}

    def fake(prompt, **kw):
        seen.update(kw)
        return ChatTurn(text="{}")
    monkeypatch.setattr(llm_tools_client, "complete_json", fake)
    _REAL_WITHIN("p", seconds=4.0, caller="rewrite", max_tokens=120)
    assert seen["attempt_timeout"] == 4.0 and seen["max_tokens"] == 120
    assert seen["caller"] == "rewrite"


def test_complete_json_within_refuses_no_time():
    assert _REAL_WITHIN("p", seconds=0.2, caller="x").timed_out


# ------------------------------------------------------------------ rewrite transport

def _within_returns(monkeypatch, turn, seen=None):
    def fake(prompt, **kw):
        if seen is not None:
            seen.append((prompt, kw))
        return turn
    monkeypatch.setattr(llm_tools_client, "complete_json_within", fake)


def test_judge_call_reads_the_question_field(monkeypatch):
    seen = []
    _within_returns(monkeypatch, ChatTurn(text='{"question": "What size are 3604 lintels?"}'), seen)
    r = ask_rewrite.judge_call("PROMPT", timeout=4.0, max_tokens=2048, effort="low",
                               caller="rewrite")
    assert r.ok and r.text == "What size are 3604 lintels?"
    prompt, kw = seen[0]
    assert prompt.startswith("PROMPT") and '"question"' in prompt
    assert kw == {"seconds": 4.0, "caller": "rewrite", "max_tokens": ask_rewrite.JUDGE_MAX_TOKENS}


@pytest.mark.parametrize("turn", [ChatTurn(error="wall-clock limit reached", timed_out=True),
                                  ChatTurn(text="not json"), ChatTurn(text='{"q": "x"}')])
def test_judge_call_failures_fall_back_to_the_callers_words(monkeypatch, turn):
    _within_returns(monkeypatch, turn)
    text, rewritten = ask_rewrite.standalone_question(
        "and the cover?", [{"question": "what about B2", "answer": "poured"}],
        call=ask_rewrite.judge_call, timeout=4.0)
    assert (text, rewritten) == ("and the cover?", False)


def test_both_rewrite_sites_use_the_judge_transport():
    import inspect
    import lambda_ask_agent
    for mod in (orch, lambda_ask_agent):
        src = inspect.getsource(mod)
        assert "call=ask_rewrite.judge_call" in src, mod.__name__
        assert "call=corroboration_client.call,\n                timeout=min(ASK_REWRITE" not in src


# ------------------------------------------------------------------ rewrite-derived intent

def _rewrite_to(monkeypatch, text):
    monkeypatch.setenv("ASK_CONVERSATION_MEMORY", "true")
    monkeypatch.setattr(ask_rewrite, "standalone_question", lambda q, h, **kw: (text, True))


def test_a_rewritten_online_request_searches_the_web_with_the_rewrite(monkeypatch, caplog):
    import logging
    _rewrite_to(monkeypatch, "Can you check online what size the timber lintels would be "
                             "according to 3604?")
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    hist = [{"question": "timber lintel size per 3604?", "answer": "Not in your records."}]
    with caplog.at_level(logging.INFO):
        run(voice_body("Yes, please. I want it online.", history=hist),
            ScriptChat(final("Use the 3604 tables.", basis="web")), channel="voice")
    assert seen == ["what size the timber lintels would be according to 3604?"] or \
        seen == ["Can you what size the timber lintels would be according to 3604?"] or \
        len(seen) == 1 and "lintels" in seen[0] and "online" not in seen[0].lower()
    route = json.loads([r.getMessage() for r in caplog.records
                        if r.getMessage().startswith("ASK_ROUTE ")][0][len("ASK_ROUTE "):])
    assert route.get("web_intent_from_rewrite") is True and route["policy"] == "required"


def test_history_alone_never_sends_a_voice_question_online(monkeypatch, caplog):
    # The caller never named the web: a rewrite that says "online" is not enough.
    import logging
    _rewrite_to(monkeypatch, "Can you check online what Jo poured on B3?")
    assert voice_web.detect_intent("Can you check online what Jo poured on B3?")[0]
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    hist = [{"question": "who poured B2", "answer": "Jo."}]
    with caplog.at_level(logging.INFO):
        run(voice_body("and B3?", history=hist), ScriptChat(final("Jo [R1].")), channel="voice")
    route = json.loads([r.getMessage() for r in caplog.records
                        if r.getMessage().startswith("ASK_ROUTE ")][0][len("ASK_ROUTE "):])
    assert seen == [] and not route.get("web_intent_from_rewrite")
    assert route["policy"] != "required"


def test_a_spoken_follow_up_that_asks_for_the_web_searches_the_rewrite(monkeypatch):
    # TEST 2026-11-12: "And for a 2.4-metre opening, check online" sent only the fragment.
    _rewrite_to(monkeypatch, "What size timber lintel does NZS 3604 give for a 2.4-metre "
                             "opening? Check online.")
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    hist = [{"question": "Lintel sizes per NZS 3604?", "answer": "Tables 8.9 to 8.13."}]
    run(voice_body("And for a 2.4-metre opening", web_intent=True, history=hist),
        ScriptChat(final("Use table 8.9.", basis="web")), channel="voice")
    assert seen == ["What size timber lintel does NZS 3604 give for a 2.4-metre opening?"]


def test_the_screen_still_sends_the_original_words(monkeypatch):
    _rewrite_to(monkeypatch, "What cover does the B2 slab need?")
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    from tests.unit.test_ask_orchestrator_judge import body
    run(body("and the cover? search online",
             history=[{"question": "what about B2", "answer": "poured"}]),
        ScriptChat(final("x [R1]")))
    assert seen == ["and the cover? search online"]


def test_sentences_keep_their_decimals_and_clause_numbers():
    # TEST 2026-11-12: the device spoke only "14 for uplift fixings." as the answer.
    import lambda_ask_agent as laa
    text = ("For a 2.4 m opening NZS 3604 Table 8.9 gives a 2/190x45 lintel. "
            "Use Table 8.14 for uplift fixings. Check the span against clause 8.6.1.3 too.")
    assert laa._SENTENCE.findall(text) == [
        "For a 2.4 m opening NZS 3604 Table 8.9 gives a 2/190x45 lintel.",
        "Use Table 8.14 for uplift fixings.",
        "Check the span against clause 8.6.1.3 too."]


def test_a_long_web_answer_is_cut_to_its_first_two_whole_sentences():
    import lambda_ask_agent as laa
    long_tail = " ".join(["Further detail follows here."] * 20)
    text = ("For a 2.4 m opening Table 8.9 gives a 2/190x45 lintel. "
            "Use Table 8.14 for uplift fixings. " + long_tail)
    assert laa._speakable_web_answer(text, None) == (
        "For a 2.4 m opening Table 8.9 gives a 2/190x45 lintel. "
        "Use Table 8.14 for uplift fixings.")


def test_two_long_sentences_fall_back_to_the_first(caplog):
    import lambda_ask_agent as laa
    first = "NZS 3604 sizes timber lintels from the span tables in section 8 for your opening."
    second = " ".join(["and the loaded dimension and roof weight also matter"] * 8) + "."
    assert laa._speakable_web_answer(first + " " + second, None) == first


def test_an_unspeakable_answer_logs_counts_not_text(caplog):
    import logging
    import lambda_ask_agent as laa
    secret = "secretword " * 70
    with caplog.at_level(logging.WARNING):
        assert laa._speakable_web_answer(secret, None) == voice_web.NO_CLEAR_ANSWER
    assert "not speakable: words=70" in caplog.text and "secretword" not in caplog.text


# ------------------------------------------------------------------ pre-check is the gate

def _precheck(monkeypatch, verdict):
    monkeypatch.setattr(llm_tools_client, "complete_json_within", lambda *a, **k:
                        ChatTurn(text=json.dumps({"web": verdict})))


def test_after_a_precheck_no_the_judge_cannot_take_a_voice_question_online(monkeypatch):
    from tests.unit.test_ask_orchestrator_judge import calls
    monkeypatch.setenv("ASK_VOICE_AUTO_WEB", "true")
    _precheck(monkeypatch, "no")
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    chat = ScriptChat(calls(("web_search", {"query": "who is the plumber"})),
                      final("No record names the plumber."))
    run(voice_body("Who is doing the plumbing on this job?"), chat, channel="voice")
    assert seen == []
    assert "web_search" not in [t["function"]["name"] for t in chat.calls[0]["tools"]]


def test_after_a_precheck_yes_the_web_runs(monkeypatch):
    monkeypatch.setenv("ASK_VOICE_AUTO_WEB", "true")
    _precheck(monkeypatch, "yes")
    seen = []
    monkeypatch.setattr(web_answer, "general_answer",
                        lambda q, b, **kw: seen.append(q) or _block())
    run(voice_body("What size are timber lintels under NZS 3604?"),
        ScriptChat(final("Use the section 8 tables.", basis="web")), channel="voice")
    assert seen == ["What size are timber lintels under NZS 3604?"]


def test_voice_drops_a_malformed_ref_the_screen_keeps_its_text():
    out_v = orch.to_client("No site records mention it [D: none].", [], None, "voice")
    assert out_v["answer"] == "No site records mention it."
    out_s = orch.to_client("No site records mention it [D: none].", [], None, "screen")
    assert "[D: none]" in out_s["answer"]


# ---- AskAgent's own rewrite reads the judge env (2026-11-13) ----------------

def _template_env(resource):
    import os
    yaml = pytest.importorskip("yaml")

    class _L(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.MappingNode):
            return {suffix: loader.construct_mapping(node, deep=True)}
        return {suffix: loader.construct_sequence(node, deep=True)}

    _L.add_multi_constructor("!", _tag)
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "src", "template.yaml")
    with open(path, encoding="utf-8") as f:
        tpl = yaml.load(f, Loader=_L)
    return tpl["Resources"][resource]["Properties"]["Environment"]["Variables"]


@pytest.mark.parametrize("name", ["ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS",
                                  "ASK_JUDGE_PROVIDER", "ASK_JUDGE_REASONING"])
def test_ask_agent_rewrite_gets_the_same_judge_env_as_the_worker(name):
    """lambda_ask_agent's rewrite calls ask_rewrite.judge_call. Without these the
    prompt_only voice path ran on llm_tools_client.DEFAULT_MODEL (gemini, thinking
    on) -- found by the 2026-11-13 old-vs-new comparison."""
    assert _template_env("AskAgentFunction")[name] == _template_env("AskVoiceWorkerFunction")[name]
