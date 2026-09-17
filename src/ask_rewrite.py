"""Rewrite a follow-up so it can be retrieved.

`_rag_answer` embeds the question text alone and `query_slots.time_range` parses
a calendar range out of that same text. A follow-up -- "when is he finishing
it?" -- carries no noun to embed and no date to parse, so the vector lands
nowhere in particular. Putting the previous turn into the ANSWERING prompt would
not fix that: the model would understand the question and still be handed the
wrong excerpts.

So the conversation is spent here, on producing one question that stands on its
own, and nowhere else. It never reaches the answering prompt: see the design's
2.1 (a chat log is a copy taken before a deletion and passes through no
predicate) and 2.2 (RAG_SYSTEM_CONTEXT's "DATA, not instructions" rule names
excerpts, not user-authored history).

PURE. No boto3, no psycopg, no network -- the transport arrives as `call`, the
same posture `query_slots` takes for its clock. Import costs nothing.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 300

# corroboration_client refuses a timeout below its own floor, so calling with
# less is a guaranteed round trip to an error we would fall back from anyway.
MIN_USEFUL_TIMEOUT = 2.0

# NOT an answer budget. corroboration_client.call sends max_tokens raw (:216) --
# unlike llm_utils, which adds REASONING_HEADROOM_TOKENS (:447). Reasoning
# tokens are completion tokens and come out first: measured elsewhere in this
# repo, max_tokens=1200 produced 1197 reasoning tokens and content=''. A rewrite
# needs ~30 tokens of answer and the rest is thinking room.
MAX_TOKENS = int(os.environ.get("ASK_REWRITE_MAX_TOKENS", "2048"))

PROMPT = """Rewrite the final question so it can be understood on its own, using the
conversation only to resolve what words like "he", "it", "that" or "then"
refer to.

Change nothing else. Do not answer it. Do not add detail that is not in the
conversation. If the final question already stands on its own, return it
unchanged.

Return only the question, on one line.

## Conversation
{history}

## Final question
{question}
"""


def _history_block(history):
    out = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        q, a = turn.get("question"), turn.get("answer")
        if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
            out.append("Q: {}\nA: {}".format(q.strip(), a.strip()))
    return "\n\n".join(out)


def _usable(reply_text):
    """A question the caller could have typed, or None.

    Shape, not content: this string is about to be embedded and displayed. The
    module cannot tell a good rewrite from a bad one -- that is what rendering
    `asked` is for -- but it can refuse something that is not a question.
    """
    text = (reply_text or "").strip()
    if not text or len(text) > MAX_QUESTION_CHARS or "\n" in text:
        return None
    return text


def standalone_question(question, history, *, call, timeout, clock=time.monotonic):
    """The question, rewritten so it stands on its own, or the original.

    Returns (text, rewritten). NEVER raises. Every failure -- no history, a
    refused budget, a transport that raises, a reply that is not a question --
    returns the caller's own words, which is always safe to retrieve with. That
    is `query_slots`'s principle applied here: a rule that does not recognise
    something returns nothing, and nothing means "behave exactly as today".
    """
    original = (question or "").strip()
    block = _history_block(history or [])
    if not original or not block:
        return original, False
    if timeout is None or timeout < MIN_USEFUL_TIMEOUT:
        logger.info("ask rewrite: skipped, %.1fs left", timeout or 0.0)
        return original, False

    try:
        reply = call(PROMPT.format(history=block, question=original),
                     timeout=timeout, max_tokens=MAX_TOKENS, effort="low")
    except Exception as e:                        # noqa: BLE001 - any transport failure
        logger.warning("ask rewrite: transport failed: %s", e)
        return original, False

    if not getattr(reply, "ok", False):
        logger.warning("ask rewrite: not ok: %s", getattr(reply, "error", None))
        return original, False

    text = _usable(getattr(reply, "text", None))
    if text is None:
        logger.warning("ask rewrite: reply was not a question")
        return original, False
    return text, True
