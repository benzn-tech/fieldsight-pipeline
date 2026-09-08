"""How long the reader waits is set by the prompt, not by the token cap.

Measured on prod 2026-09-08, four consecutive Ask calls of the same question:

    completion 899 / 927 / 1077 / 1224 tokens   at ~103 tok/s
    lambda Duration 10.2 / 11.5 / 11.8 / 13.3 s
    the model call inside it 9.0 / 10.3 / 8.9 / 11.9 s

Subtracting, retrieval is ~1.2s and 88% of the wait is the model typing prose.
`MAX_ANSWER_TOKENS` is 8000 and the model writes ~1000, so the cap never binds:
it is not what sets the length. The screen system context had no length rule at
all -- the VOICE one has had "one or two short spoken sentences" since it was
written, and the screen one was simply never given the equivalent.

What these tests can and cannot do: they pin that the rule is present, that it
is expressed as a number, and that the voice prompt is untouched. They CANNOT
show the model obeys it -- a rule being in the prompt is not the model following
it, which this repo has measured the hard way more than once. That check is a
measurement against the deployed TEST function, recorded in the PR.
"""
import os
import re

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

agent = pytest.importorskip("lambda_ask_agent", reason="requires boto3")


def _rules(context):
    return [line for line in context.splitlines() if line.startswith("- ")]


def test_the_screen_prompt_states_a_length_ceiling_as_a_number():
    """An adjective ("keep it short") leaves the model to pick; a number does
    not. The voice prompt has always carried one and the screen prompt did not,
    which is the whole difference between a two-sentence answer and a thousand
    tokens."""
    rules = " ".join(_rules(agent.RAG_SYSTEM_CONTEXT))
    assert re.search(r"\b\d+\s+words\b", rules), (
        "the screen prompt must bound the answer with a number of words")


def test_the_answer_comes_before_the_supporting_detail():
    """Established twice already on this prompt: the reader is on a site and
    reads the first line. A summary that arrives after three sentences of
    preamble is a summary they never reach."""
    rules = " ".join(_rules(agent.RAG_SYSTEM_CONTEXT)).lower()
    assert "first sentence" in rules
    assert "no preamble" in rules


def test_the_brevity_rule_hands_the_model_no_phrase_to_open_with():
    """Twice on this exact prompt a phrase written into it came back as the
    model's first words -- the widened-basis heading, and `"Bullet 1:"` copied
    out of a schema example. A rule that contains a quotable label invites the
    same failure, so the rule must read as instruction, not as specimen text.
    """
    for line in _rules(agent.RAG_SYSTEM_CONTEXT):
        assert '"' not in line, "a quoted specimen in a rule is a phrase to copy: " + line
        assert not re.search(r"^- (Summary|Answer|Overview)\s*:", line), line


def test_the_voice_prompt_is_left_alone():
    """Voice already had its own, tighter, rule and is spoken aloud: markdown
    bullets and a 150-word budget are both wrong there. Changing the screen
    prompt must not leak into it."""
    voice = agent.RAG_SYSTEM_CONTEXT_VOICE
    assert "one or two short spoken sentences" in voice
    assert "150 words" not in voice
    assert "bullets" not in voice.lower()


def test_both_prompts_still_end_on_the_language_rule():
    """The English policy is deliberately last in each context, and the
    per-call tail rule is appended after the question. Inserting rules above it
    must not have displaced it."""
    for context in (agent.RAG_SYSTEM_CONTEXT, agent.RAG_SYSTEM_CONTEXT_VOICE):
        assert _rules(context)[-1].startswith("- Answer in English")


def test_the_rule_reaches_the_prompt_the_model_is_actually_sent():
    """The constant existing is not the constant being used. `build_rag_prompt`
    chooses between the two contexts on `mode`, and only the screen branch
    should carry this."""
    chunks = [{"site_name": "SB1108", "report_date": "2026-09-02",
               "topic_title": "T", "chunk_text": "x"}]
    screen = agent.build_rag_prompt("what did I do today", chunks)
    voice = agent.build_rag_prompt("what did I do today", chunks, mode="voice")
    assert "150 words" in screen
    assert "150 words" not in voice
