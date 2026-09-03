"""Customer-facing answers are English, and the prompt alone does not achieve it.

Both Ask system contexts have ended with "Answer in English" since long before
this module existed. Measured against the DEPLOYED model -- prod runs
`qwen3.6-flash`, a Chinese-first model -- a Chinese question still came back in
Chinese 1 time in 13. So what is tested here is not the presence of a sentence.
It is the two things that work when a sentence does not: where the sentence
sits, and a check on what actually came back.
"""
import pytest

import answer_language as al

laa = pytest.importorskip("lambda_ask_agent",
                          reason="requires psycopg/boto3 (installed in CI)")

CHUNKS = [{"chunk_text": "The slab was poured on the east side.",
           "site_name": "UC PK", "report_date": "2026-08-28",
           "topic_title": "Concrete"}]


@pytest.fixture(autouse=True)
def english(monkeypatch):
    monkeypatch.delenv("ASK_ANSWER_LANGUAGE", raising=False)


# ---- the switch -----------------------------------------------------------

def test_english_is_the_default_and_an_unset_env_is_not_a_crash():
    assert al.policy() == al.EN


def test_the_switch_is_one_env_var(monkeypatch):
    monkeypatch.setenv("ASK_ANSWER_LANGUAGE", "question")
    assert al.policy() == al.QUESTION


def test_a_typo_in_the_switch_falls_back_to_english(monkeypatch):
    """This reads an env var. A misspelled value in a deploy must not take Ask
    down, and must not silently turn the policy off either -- falling back to
    the product decision is the safe direction."""
    for junk in ("EN", "english", "Question", "", "true", "1"):
        monkeypatch.setenv("ASK_ANSWER_LANGUAGE", junk)
        assert al.policy() == al.EN, junk


# ---- where the rule sits --------------------------------------------------

def test_the_language_rule_is_the_last_thing_in_the_prompt():
    """The existing rule sits in a list at the top and leaked anyway. What is
    nearest the end is what the model is most likely to still be holding, and
    the thing directly above this used to be the user's question -- in the
    language we do not want back."""
    prompt = laa.build_rag_prompt("昨天都发生了什么", CHUNKS, today="2026-09-03")
    assert prompt.index("## Language") > prompt.index("## User Question")
    assert prompt.rstrip().endswith("asked in.")


def test_the_retry_prompt_names_what_went_wrong():
    """A second identical prompt is a second roll of the same dice."""
    first = laa.build_rag_prompt("昨天都发生了什么", CHUNKS, today="2026-09-03")
    again = laa.build_rag_prompt("昨天都发生了什么", CHUNKS, today="2026-09-03",
                                 insist_language=True)
    assert first != again
    assert "previous answer was not in English" in again


def test_following_the_question_asks_for_that_instead(monkeypatch):
    monkeypatch.setenv("ASK_ANSWER_LANGUAGE", "question")
    prompt = laa.build_rag_prompt("昨天都发生了什么", CHUNKS, today="2026-09-03")
    assert "same language the question was asked in" in prompt
    assert "Answer in English, whatever" not in prompt


# ---- the check on what came back ------------------------------------------

def test_a_chinese_answer_is_a_violation():
    assert al.violates("2026-08-28，UC PK 站点主要进行了应用工作流讨论和材料交付安排。")


def test_an_english_answer_is_not():
    assert not al.violates("On 2026-08-28, the slab was poured on the east side.")


def test_a_quoted_name_in_an_english_answer_is_not_a_violation():
    """A place name or a person's name can legitimately appear in an English
    answer, and prod transcripts carry both. A hit-based check would rewrite a
    correct answer; the check is a ratio for that reason."""
    assert not al.violates(
        "On 2026-08-28, Ben confirmed the delivery for the 长江 site was "
        "rescheduled to Monday, and the claim was held pending the inspection "
        "report from the structural engineer.")


def test_an_empty_answer_is_not_reported_as_a_language_leak():
    """An empty string is a different failure. Reporting it here sends the
    caller down the wrong path."""
    assert not al.violates("")
    assert not al.violates(None)


def test_nothing_violates_when_the_policy_is_to_follow_the_question(monkeypatch):
    monkeypatch.setenv("ASK_ANSWER_LANGUAGE", "question")
    assert not al.violates("2026-08-28，UC PK 站点主要进行了应用工作流讨论。")


# ---- the retry, driven through the answer path ----------------------------

def _wire(monkeypatch, answers):
    """Feed `answers` to successive call_llm invocations."""
    import llm_utils
    calls = {"n": 0, "prompts": []}

    def fake(prompt, **kw):
        calls["prompts"].append(prompt)
        i = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        return answers[i], None

    monkeypatch.setattr(llm_utils, "call_llm", fake)
    monkeypatch.setattr(laa, "RAG_SEARCH_FUNCTION", "rag-search-test")

    import io as _io
    import json as _json

    class C:
        def invoke(self, **kw):
            body = {"chunks": [dict(CHUNKS[0], source_s3_key="k", chunk_type="topic")],
                    "site_count": 1,
                    "basis": {"from": "2026-08-28", "to": "2026-08-28", "widened": False}}
            return {"Payload": _io.BytesIO(_json.dumps(body).encode())}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: C())
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda *a, **k: [[0.1] * 1024])
    return calls


ZH = "2026-08-28，UC PK 站点主要进行了应用工作流讨论和材料交付安排的协调。"
EN_ANS = "On 2026-08-28 the slab was poured on the east side."


def test_a_leaked_answer_is_retried_once_and_recovered(monkeypatch):
    calls = _wire(monkeypatch, [ZH, EN_ANS])
    out = laa._rag_answer({"question": "昨天都发生了什么", "caller_sub": "s",
                           "tz": "Pacific/Auckland"})
    assert out["answer"] == EN_ANS
    assert calls["n"] == 2
    assert "previous answer was not in English" in calls["prompts"][1]


def test_the_retry_happens_at_most_once(monkeypatch):
    """If the retry leaks too, the first answer is kept: both are equally wrong
    about language and equally right about the site, and a third call spends a
    waiting customer's time on the same dice."""
    calls = _wire(monkeypatch, [ZH, ZH])
    out = laa._rag_answer({"question": "昨天都发生了什么", "caller_sub": "s",
                           "tz": "Pacific/Auckland"})
    assert calls["n"] == 2
    assert out["answer"] == ZH


def test_an_english_answer_costs_no_second_call(monkeypatch):
    calls = _wire(monkeypatch, [EN_ANS])
    laa._rag_answer({"question": "what happened yesterday", "caller_sub": "s",
                     "tz": "Pacific/Auckland"})
    assert calls["n"] == 1


def test_the_response_declares_the_language_it_was_written_in(monkeypatch):
    """Computed from the policy, never sniffed from the text. The client renders
    its own line beside the answer and must not have to guess -- and guessing
    from the answer would make the client inherit the leak."""
    _wire(monkeypatch, [EN_ANS])
    out = laa._rag_answer({"question": "昨天都发生了什么", "caller_sub": "s",
                           "tz": "Pacific/Auckland"})
    assert out["answer_language"] == "en"

    monkeypatch.setenv("ASK_ANSWER_LANGUAGE", "question")
    _wire(monkeypatch, [ZH])
    out = laa._rag_answer({"question": "昨天都发生了什么", "caller_sub": "s",
                           "tz": "Pacific/Auckland"})
    assert out["answer_language"] == "question"
