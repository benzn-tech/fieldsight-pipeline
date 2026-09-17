"""The one cleaner for a client-carried conversation history.

Moved out of `lambda_fieldsight_api.py` (2026-09-17, ask-conversation-memory
Task 3) so `lambda_ask_agent.py` can re-clean the same shape without importing
a sibling Lambda's handler module. Importing `lambda_fieldsight_api` from
`lambda_ask_agent` would run that module's own boto3 client construction and
env reads at import time -- a test-import-order trap this repo has already
paid for once (see `test-import-order-is-not-a-contract` in memory). This
module is pure: no boto3, no psycopg, no network, same posture as
`query_slots.py` and `ask_rewrite.py`.

PURE means every caller gets identical behaviour for identical input --
`lambda_fieldsight_api.ask_question` / `ask_voice` clean the history once on
the way in, and `lambda_ask_agent._rag_answer` cleans it again because that
function is invoked directly in tests and by the legacy S3 path, not only
through the gateway proxy.
"""
from __future__ import annotations

# Conversation continuity, step 2: the gateway carries the previous turns and
# the agent counts them. Nothing retrieves with them yet -- the device half and
# the retrieval half land separately, and an inert forward can be observed in
# production logs before either commits to a shape.
#
# Six turns because a follow-up refers to the last question, not to the start of
# a shift, and 2000 chars because a spoken answer is two or three sentences --
# the cap is for the pathological client, not the ordinary one. Their product is
# the number that matters: 6 x 2000 x 2 fields = 24K, against a 6MB synchronous
# invoke ceiling this body already fills with 1.5M chars of base64 audio.
MAX_VOICE_HISTORY_TURNS = 6
MAX_VOICE_HISTORY_CHARS = 2000


def _clean_voice_history(raw):
    """The forwardable turns in `raw`, most recent kept, or [] if there are none.

    FAILS SOFT on purpose. A device that ships a serialisation bug must lose its
    memory, not its voice: a 400 here would take hands-free Ask offline across a
    whole app build to protect a feature that is not wired up yet. Bad turns are
    dropped individually so one corrupted entry cannot erase a conversation that
    is otherwise intact, and the caller logs how many went missing.

    Only `question` and `answer` survive. This field ends up inside an LLM
    prompt, and forwarding whatever else the device keeps locally -- ids,
    timestamps, a `caller_sub` -- is how unreviewed client data gets there.
    Identity in particular comes from the authorizer, never from the body.

    The tail is kept, not the head: a follow-up refers to the last question, so
    dropping recent turns would answer against the conversation from ten minutes
    ago while looking like it worked.
    """
    if not isinstance(raw, list):
        return []
    kept = []
    for turn in raw[-MAX_VOICE_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        q, a = turn.get('question'), turn.get('answer')
        if not isinstance(q, str) or not isinstance(a, str):
            continue
        q, a = q.strip(), a.strip()
        if not q or not a:
            continue
        kept.append({'question': q[:MAX_VOICE_HISTORY_CHARS],
                     'answer': a[:MAX_VOICE_HISTORY_CHARS]})
    return kept
