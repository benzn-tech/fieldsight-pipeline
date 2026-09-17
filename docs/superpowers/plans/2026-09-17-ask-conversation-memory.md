# Ask Conversation Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A follow-up question ("when is he finishing it?") retrieves the right excerpts, by rewriting it into a standalone question before embedding — with the conversation reaching the rewrite and never the answering prompt.

**Architecture:** One new pure module (`ask_rewrite`) sits between reading the request body and embedding inside `_rag_answer`. It turns `history + question` into one standalone question via the cheap corroboration lane, falling back to the user's own words on every failure. Retrieval, ACL and tombstones are unchanged because the rewritten question goes through the ordinary path; the answering prompt is unchanged because it never sees history. Two smaller changes ride along: a distance gate that skips the web-answer verdict call, and a timeout ladder that currently ascends.

**Tech Stack:** Python 3.11 Lambda, SAM/CloudFormation, pytest with hand-written doubles; Kotlin/Android (GrandTime); vanilla JS (fieldsight-ui).

**Spec:** `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md`

## Global Constraints

- **History key names are `question` and `answer`.** `_clean_voice_history` (`src/lambda_fieldsight_api.py:1391`) reads only those and `continue`s past anything else. `{q, a}` loses every turn silently.
- **Absent is not `[]`.** A body with no history sends **no key**. `lambda_fieldsight_api.py:1428` states the rule; collapsing them makes `history_turns` mean two things.
- **Caps are the deployed ones: `MAX_VOICE_HISTORY_TURNS = 6`, `MAX_VOICE_HISTORY_CHARS = 2000`** (`src/lambda_fieldsight_api.py:1362-1363`). This plan does not change them and does not introduce a second, smaller cap on any client.
- **Every field is added TWICE on the voice path.** `_voice_answer` builds `_rag_answer`'s body from scratch (`src/lambda_ask_agent.py:1667`) and hand-builds its return (`:1702`). The file says so at `:1662`: *"anything the screen path gains is absent here until someone adds it twice."*
- **The answering prompt never receives history.** `build_rag_prompt` gains no `history` parameter (spec §2).
- **The web-answer branch receives the ORIGINAL question, never `asked`** (spec §4.3).
- **`corroboration_client.call` does NOT add reasoning headroom.** It sends `max_tokens` raw (`src/corroboration_client.py:216`, default 1024). `llm_utils` adds `REASONING_HEADROOM_TOKENS` (`src/llm_utils.py:447`); this lane does not. Size `max_tokens` for thinking **plus** a question.
- **`timeout < MIN_USEFUL_TIMEOUT` (2.0 s) is refused by the client** (`src/corroboration_client.py:78`). Skip the rewrite rather than call with a sliver.
- **`_NO_LEX_MAX_DIST = 0.55` is not touched and not relaxed.** The new gate gets its own constant.
- **MSYS_NO_PATHCONV=1** on any AWS CLI call carrying a `/`-prefixed argument (BUG-42).
- **Never `git add -A`** on this repo's develop (memory: `fieldsight-windows-git-crlf`). Stage named paths.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/ask_rewrite.py` | **New.** Pure. `standalone_question(question, history, *, call, timeout)` → `(text, rewritten)`. Never raises, never returns non-question text. |
| `src/lambda_ask_agent.py` | `_rag_answer`: function-head restructure, rewrite call, conditional `time_range`, distance gate, `history_turns` log. `_voice_answer`: forward `history` in, carry `asked` out. |
| `src/lambda_fieldsight_api.py` | `ask_question`: forward `history` through the existing `_clean_voice_history`. |
| `src/web_answer.py` | `answer(..., *, skip_verdict=False)`. Chunks always passed so `question_admission` keeps its signals. |
| `src/template.yaml` | Three new parameters + env wiring on `AskAgentFunction`; the timeout ladder. |
| `.github/workflows/deploy.yml`, `deploy-prod.yml` | The three new parameter overrides. |
| `tests/unit/test_ask_rewrite.py` | **New.** The module's own rules, driven against a fake transport. |
| `tests/unit/test_lambda_ask_agent_rag.py` | The `/ask` route cases (8–12, 17–22). |
| `tests/unit/test_lambda_ask_agent_voice.py` | Voice carry-through (24). |
| `tests/unit/test_lambda_fieldsight_api_ask.py` | Proxy forwarding + malformed history (23). |

Two repos are separate tracks and cannot be committed from this worktree: `fieldsight-ui` (Task 11) and `GrandTime` (Tasks 0, 12).

---

### Task 0: Device sends `tz` — ships first, behind no flag

**Repo:** `GrandTime`. **Independent of every other task.** Without `tz`, `query_slots.resolve_today` returns `None` and every spoken "yesterday" searches all of time. The backend half is already deployed and already pinned by `tests/unit/test_ask_tz_is_forwarded.py`.

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/ask/AskApiClient.kt`
- Test: the module's existing ask client test

**Interfaces:**
- Produces: request body gains `"tz"`, an IANA zone id string.

- [ ] **Step 1: Write the failing test**

The class takes its HTTP transport by injection (`http: HttpFns = RealHttp()`); the zone must arrive the same way or it cannot be asserted.

```kotlin
@Test
fun `ask sends the device zone`() {
    val fake = FakeHttp(HttpResult(200, """{"transcript":"x","answerText":"y","audioBase64":"z","audioFormat":"wav"}"""))
    val client = AskApiClient("https://example/prod/api", fake, zoneId = { "Pacific/Auckland" })
    client.ask("token", "AAAA")
    assertEquals("Pacific/Auckland", JSONObject(fake.lastBody!!).getString("tz"))
}
```

- [ ] **Step 2: Run it and watch it fail**

Expected: compile error — `AskApiClient` has no `zoneId` parameter. That is the correct first failure.

- [ ] **Step 3: Minimal implementation**

```kotlin
class AskApiClient(
    private val baseUrl: String,
    private val http: HttpFns = RealHttp(),
    private val zoneId: () -> String = { java.time.ZoneId.systemDefault().id },
) {
    fun ask(idToken: String, audioBase64: String, format: String = "wav"): AskResult {
        val body = JSONObject()
            .put("audio", audioBase64)
            .put("format", format)
            .put("mode", "voice")
        // The caller's own zone, not a date: the backend resolves "yesterday"
        // against it (query_slots.resolve_today). Without this every spoken
        // relative date searches all of time. An unusable value costs nothing --
        // resolve_today returns None and the search is unfiltered, which is
        // exactly today's behaviour.
        runCatching { zoneId() }.getOrNull()?.takeIf { it.isNotBlank() }
            ?.let { body.put("tz", it) }
        ...
    }
```

- [ ] **Step 4: Update the class docstring**

It states the contract verbatim — *"request {audio, format, mode:\"voice\"}"*. Leaving it is how the next reader learns a false contract.

- [ ] **Step 5: Run the test, watch it pass; run the module's suite**

- [ ] **Step 6: Commit**

```bash
git commit -m "The device says which zone it is in, so 'yesterday' means a day"
```

**Verification on a real device (spec §8 step 8):** ask *"what happened yesterday?"* and confirm the answer is about yesterday, not about everything.

---

### Task 1: `ask_rewrite` — the module, with no caller

**Files:**
- Create: `src/ask_rewrite.py`
- Test: `tests/unit/test_ask_rewrite.py`

**Interfaces:**
- Produces: `standalone_question(question, history, *, call, timeout, clock=time.monotonic) -> tuple[str, bool]`
- Consumes: a `call(prompt, *, timeout, model, max_tokens, effort)` transport returning an object with `.ok`, `.text`, `.error` — the shape `corroboration_client.call` already returns.

- [ ] **Step 1: Write the failing tests** (spec §7.1–7.7)

```python
import ask_rewrite


class FakeReply:
    def __init__(self, text=None, ok=True, error=None):
        self.ok, self.text, self.error = ok, text, error


def _call(reply, counter):
    def call(prompt, **kw):
        counter.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply
    return call


HISTORY = [{"question": "what did James say about the ceiling grid?",
            "answer": "James said the grid on level 3 is behind."}]


def test_no_history_costs_no_model_call():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "what is the fire rating?", [], call=_call(FakeReply("x"), calls), timeout=4.0)
    assert (text, rewritten) == ("what is the fire rating?", False)
    assert calls == [], "the common case must not pay for a model call"


def test_a_follow_up_is_rewritten():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("when is James finishing the level 3 ceiling grid?"), calls),
        timeout=4.0)
    assert text == "when is James finishing the level 3 ceiling grid?"
    assert rewritten is True
    assert len(calls) == 1


def test_a_raising_transport_returns_the_original():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(RuntimeError("boom"), []), timeout=4.0)
    assert (text, rewritten) == ("when is he finishing it?", False)


def test_an_overlong_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("x" * 301), []), timeout=4.0)
    assert rewritten is False


def test_a_multiline_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("a?\nb?"), []), timeout=4.0)
    assert rewritten is False


def test_an_empty_reply_is_refused():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("   "), []), timeout=4.0)
    assert rewritten is False


def test_a_budget_below_the_client_floor_skips_the_call():
    calls = []
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply("x?"), calls), timeout=1.0)
    assert (text, rewritten) == ("when is he finishing it?", False)
    assert calls == [], "below MIN_USEFUL_TIMEOUT the client refuses anyway"


def test_a_not_ok_reply_returns_the_original():
    text, rewritten = ask_rewrite.standalone_question(
        "when is he finishing it?", HISTORY,
        call=_call(FakeReply(None, ok=False, error="429"), []), timeout=4.0)
    assert (text, rewritten) == ("when is he finishing it?", False)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_ask_rewrite.py -q`
Expected: `ModuleNotFoundError: No module named 'ask_rewrite'`.

- [ ] **Step 3: Minimal implementation**

```python
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
```

- [ ] **Step 4: Run the tests, watch them pass**

Run: `python -m pytest tests/unit/test_ask_rewrite.py -q` → 8 passed.

- [ ] **Step 5: Run the whole unit suite**

Run: `python -m pytest tests/unit -q`
Expected: the previous count + 8, zero failures. Nothing calls this module yet.

- [ ] **Step 6: Commit**

```bash
git add src/ask_rewrite.py tests/unit/test_ask_rewrite.py
git commit -m "A follow-up question can be rewritten to stand on its own"
```

---

### Task 2: The function head moves inside the `try`

**Files:**
- Modify: `src/lambda_ask_agent.py:1221-1235`
- Test: `tests/unit/test_lambda_ask_agent_rag.py`

**Interfaces:**
- Produces: `_rag_answer` computes `today`, `q_from/q_to`, `scope_req`, `plan` **inside** the `try`. No behaviour change.

This is its own task because it is a pure move with no new behaviour, and it must be reviewable on its own before a network call lands above those lines. `:1225` says the helpers live above the `try` because they never raise; `standalone_question` does make a call, so it cannot join them there without reintroducing the raw-500 path `:1243` records having already fixed once.

- [ ] **Step 1: Pin today's behaviour first**

```python
def test_a_malformed_scope_still_answers(wired):
    """Guards the move: these four helpers must keep producing the same result
    from inside the try as they did above it."""
    out = ask._rag_answer({"question": "what happened?", "caller_sub": SUB,
                           "tz": "Pacific/Auckland", "site_id": "not-a-uuid"})
    assert "error" not in out
    assert "invalid" in " ".join(out["applied_scope"]["dropped"])
```

- [ ] **Step 2: Run it, confirm it passes** — it describes today.

- [ ] **Step 3: Move the four calls inside the `try`, changing nothing else**

- [ ] **Step 4: Run `tests/unit/test_lambda_ask_agent_rag.py`, `test_ask_scoped.py`, `test_scoped_ask_contract.py`, `test_ask_time_anchor.py`** — all green, no assertion changed.

- [ ] **Step 5: Commit**

```bash
git commit -m "The ask route's head is computed inside the guarded region"
```

---

### Task 3: `_rag_answer` rewrites before it embeds

**Files:**
- Modify: `src/lambda_ask_agent.py` (`_rag_answer`)
- Test: `tests/unit/test_lambda_ask_agent_rag.py`

**Interfaces:**
- Consumes: `ask_rewrite.standalone_question` (Task 1).
- Produces: `asked` in the response; `history_turns` in the log; the embedded text is the rewritten one.

- [ ] **Step 1: Write the failing tests** (spec §7.8, 7.9, 7.10, 7.19)

**Read this before writing them — three seams are not where they look.**
`dashscope_utils`, `query_slots` and `metric_slots` are imported **inside**
`_rag_answer` (`lambda_ask_agent.py:1200`, `:1220`, `:1256`), so they are **not
attributes of `lambda_ask_agent`** and `monkeypatch.setattr(ask.query_slots, …)`
raises `AttributeError`. Patch the real module objects instead. And there is no
`_invoke_rag_search`: `_rag_answer` builds the payload inline and calls
`_get_lambda_client().invoke(FunctionName=RAG_SEARCH_FUNCTION, …)` — the
module-level `_get_lambda_client` is the seam.

```python
import io
import json

import dashscope_utils
import query_slots

import lambda_ask_agent as ask


class _FakeLambda:
    """Captures the rag-search payload and returns an empty result."""
    def __init__(self, sink):
        self.sink = sink

    def invoke(self, **kw):
        self.sink.append(json.loads(kw["Payload"]))
        body = json.dumps({"chunks": [], "basis": {}, "applied": {}}).encode()
        return {"Payload": io.BytesIO(body)}


def test_the_rewritten_text_is_what_gets_embedded(wired, monkeypatch):
    seen = []
    monkeypatch.setattr(dashscope_utils, "embed",
                        lambda xs: seen.append(xs) or [[0.0] * 1024])
    monkeypatch.setattr(ask.ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("when is James finishing the grid?", True))
    ask._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "q", "answer": "a"}]})
    assert seen == [["when is James finishing the grid?"]]


def test_the_answering_prompt_gets_the_original_and_no_history(wired, monkeypatch):
    """§2's whole claim. Assert the ABSENCE explicitly."""
    prompts = []
    monkeypatch.setattr(ask, "build_rag_prompt",
                        lambda q, c, **kw: prompts.append((q, kw)) or "PROMPT")
    monkeypatch.setattr(ask.ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten", True))
    ask._rag_answer({"question": "when is he finishing it?", "caller_sub": SUB,
                     "history": [{"question": "ceiling grid?", "answer": "level 3 is behind"}]})
    asked_with, kwargs = prompts[0]
    assert asked_with == "when is he finishing it?"
    assert "history" not in kwargs
    assert "level 3 is behind" not in str(kwargs)


def test_a_body_without_history_sends_the_payload_it_sends_today(wired, monkeypatch):
    payloads = []
    monkeypatch.setattr(ask, "_get_lambda_client", lambda: _FakeLambda(payloads))
    ask._rag_answer({"question": "what happened?", "caller_sub": SUB})
    before = dict(payloads[0])
    payloads.clear()
    ask._rag_answer({"question": "what happened?", "caller_sub": SUB, "history": []})
    assert payloads[0] == before, "absent and empty must retrieve identically"


def test_an_original_that_names_a_date_is_not_recomputed(wired, monkeypatch):
    ranges = []
    real = query_slots.time_range
    monkeypatch.setattr(query_slots, "time_range",
                        lambda q, t: ranges.append(q) or real(q, t))
    monkeypatch.setattr(ask.ask_rewrite, "standalone_question",
                        lambda q, h, **kw: ("rewritten with no date", True))
    ask._rag_answer({"question": "what happened yesterday?", "caller_sub": SUB,
                     "tz": "Pacific/Auckland",
                     "history": [{"question": "q", "answer": "a"}]})
    assert ranges == ["what happened yesterday?"], "the original resolved; do not re-ask"
```

- [ ] **Step 2: Run them, watch them fail**

Expected: `AttributeError: module 'lambda_ask_agent' has no attribute 'ask_rewrite'`.

- [ ] **Step 3: Minimal implementation**

```python
    # Inside the try, above time_range (Task 2 put them together).
    import ask_rewrite

    _h = body.get("history")
    # The SAME cleaner the voice proxy uses, imported from lambda_fieldsight_api
    # rather than reimplemented: one set of key names, one set of caps. The
    # agent re-cleans because it is invoked directly in tests and by the legacy
    # path, not only through the proxy.
    history = _clean_voice_history(_h)
    history_turns = len(history)

    # `_started` is captured at the top of _rag_answer, first statement:
    #     _started = time.monotonic()
    # ASK_DEADLINE_SECONDS is the whole-request budget (Task 9 wires it), set
    # below the ApiFunction timeout from Task 7 so this function gives up while
    # its caller is still listening.
    deadline_left = ASK_DEADLINE_SECONDS - (time.monotonic() - _started)
    asked, rewritten = ask_rewrite.standalone_question(
        question, history,
        call=corroboration_client.call,
        timeout=min(ASK_REWRITE_BUDGET, deadline_left))

    today = query_slots.resolve_today(body.get("tz"), now=_parse_now(body.get("now")))
    q_from, q_to = query_slots.time_range(question, today)
    if rewritten and q_from is None and q_to is None:
        # A rewrite may SUPPLY a missing anchor. It may never move one the user
        # gave, and it may never open the metric route: `metric_slots.detect`
        # below is still handed the ORIGINAL text, so a date word the model
        # invented would otherwise answer a window nobody named.
        q_from, q_to = query_slots.time_range(asked, today)
```

and at the embed:

```python
        query_vec = dashscope_utils.embed([asked])[0]
```

and in every `return` of this function: `"asked": asked if rewritten else None`.

- [ ] **Step 4: Run the tests, watch them pass. Run the whole suite.**

- [ ] **Step 5: Prove the tests guard it** — revert the `embed([asked])` line to `embed([question])`, watch test 1 go red, restore. Then revert the `build_rag_prompt` argument, watch test 2 go red, restore.

- [ ] **Step 6: Commit**

---

### Task 4: One cleaner, one set of caps, on both proxies

**Files:**
- Modify: `src/lambda_fieldsight_api.py` (`ask_question`, ~`:1256`)
- Modify: `src/lambda_ask_agent.py` (`_clean_history` helper)
- Test: `tests/unit/test_lambda_fieldsight_api_ask.py`

**Interfaces:**
- Produces: `/api/ask` forwards a cleaned `history`; the agent re-cleans defensively.

- [ ] **Step 1: Write the failing tests** (spec §7.23)

```python
def test_history_is_forwarded_cleaned():
    payload = api_ask({"question": "q", "history": [
        {"question": "a?", "answer": "b"}]})
    assert payload["history"] == [{"question": "a?", "answer": "b"}]


def test_the_wrong_key_names_are_dropped_per_turn_and_the_ask_still_answers(caplog):
    payload = api_ask({"question": "q", "history": [
        {"q": "a?", "a": "b"}, {"question": "real?", "answer": "yes"}]})
    assert payload["history"] == [{"question": "real?", "answer": "yes"}]
    assert "dropped 1 of 2" in caplog.text


def test_no_usable_history_sends_no_key():
    payload = api_ask({"question": "q", "history": []})
    assert "history" not in payload, "absent is not empty (spec 3.1)"
```

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement** — in `ask_question`, after the existing `if body.get('tz'):` block, mirroring the voice proxy exactly:

```python
    # Same cleaner as the voice route: one set of caps, one set of key names.
    # ABSENT, never an empty list -- see ask_voice, and the agent's
    # history_turns count, which would otherwise mean two things.
    raw_history = body.get('history')
    history = _clean_voice_history(raw_history)
    if history:
        payload['history'] = history
    if isinstance(raw_history, list) and len(raw_history) != len(history):
        logger.warning("ask: dropped %d of %d history turns",
                       len(raw_history) - len(history), len(raw_history))
```

- [ ] **Step 4: Run tests + suite. Commit.**

---

### Task 5: The voice path carries it, both ways

**Files:**
- Modify: `src/lambda_ask_agent.py:1667` (body in), `:1702` (return out)
- Test: `tests/unit/test_lambda_ask_agent_voice.py`

This is the "add it twice" task. The gateway already forwards `history` on the voice route; the agent drops it.

- [ ] **Step 1: Write the failing tests** (spec §7.24)

```python
def test_the_voice_body_carries_history_into_rag(monkeypatch):
    seen = {}
    monkeypatch.setattr(ask, "_rag_answer", lambda b: seen.update(b) or {"answer": "a"})
    ask._voice_answer({"audio": _WAV, "caller_sub": SUB,
                       "history": [{"question": "q", "answer": "a"}]})
    assert seen["history"] == [{"question": "q", "answer": "a"}]


def test_asked_survives_the_hand_built_voice_return(monkeypatch):
    monkeypatch.setattr(ask, "_rag_answer",
                        lambda b: {"answer": "a", "asked": "rewritten?"})
    out = ask._voice_answer({"audio": _WAV, "caller_sub": SUB})
    assert out["asked"] == "rewritten?", "this return is built, not passed through"
```

- [ ] **Step 2–5:** run → fail → add `"history": body.get("history")` to the built body and `"asked"` to the built return → pass → commit.

---

### Task 6: The distance gate keeps the guard's signals

**Files:**
- Modify: `src/web_answer.py` (`answer` signature)
- Modify: `src/lambda_ask_agent.py` (compute `nearest`, pass `skip_verdict`)
- Test: `tests/unit/test_answering_from_the_open_web.py`, `tests/unit/test_lambda_ask_agent_rag.py`

**Interfaces:**
- Produces: `web_answer.answer(question, chunks, *, skip_verdict=False)`.

- [ ] **Step 1: Write the failing tests** (spec §7.13–7.16, 7.21, 7.22)

```python
def test_skipping_the_verdict_still_screens_against_the_chunks(monkeypatch):
    """The whole point. answer(q, []) would skip the verdict AND blind
    question_admission, which derives two of three signals from chunks."""
    seen = {}
    monkeypatch.setattr(web_answer.question_admission, "screen",
                        lambda q, c: seen.update(chunks=c) or None)
    web_answer.answer("q", [{"chunk_text": "t", "site_name": "UC PK"}], skip_verdict=True)
    assert seen["chunks"], "chunks must still reach screen()"


def test_a_far_nearest_distance_skips_the_verdict(wired, monkeypatch):
    called = []
    monkeypatch.setattr(web_answer, "_verdict", lambda *a: called.append(1) or ({}, None))
    _rag_answer_with(chunks=[{"distance": 0.61}])
    assert called == []


def test_a_near_distance_still_asks_the_verdict(wired, monkeypatch): ...   # 0.40 -> called
def test_an_absent_distance_does_not_gate(wired, monkeypatch): ...         # no key -> called
def test_a_widened_basis_does_not_gate(wired, monkeypatch): ...            # basis.widened -> called
def test_a_lexical_chunk_beats_the_distance(wired, monkeypatch): ...       # 0.61 + lexical -> called
```

- [ ] **Step 2: Run, watch fail.**

- [ ] **Step 3: Implement.** In `web_answer.answer`, add the keyword and guard only the verdict block:

```python
def answer(question, chunks, *, skip_verdict=False, clock=time.monotonic):
    ...
    if chunks and not skip_verdict:
        verdict, err = _verdict(...)
        ...
```

In `_rag_answer`, after `_rerank_chunks`:

```python
# Seeded at 0.55 because that is the number measured for _aggregate_topics'
# per-GROUP filter -- which is a different comparison, and always with a lexical
# escape hatch. This is a per-CHUNK gate, so it gets its own constant and is
# unmeasured until it is measured: its worst case must stay "we paid for a
# verdict call we could have skipped", never a wrong answer.
_DISTANCE_GATE = float(os.environ.get("ASK_DISTANCE_GATE", "0.55"))

_dists = [c["distance"] for c in chunks if c.get("distance") is not None]

# `lexical` is NOT a field on a chunk. _aggregate_topics COMPUTES it at :859 for
# its own rows, and neither lambda_rag_search nor search_sql ever sets it -- so
# `c.get("lexical")` would be None for every chunk and this arm would be dead
# code wearing the shape of a guard.
#
# Match against the TITLE ONLY, never chunk_text. :845-849 says why, and getting
# this wrong fails in the opposite direction just as silently: "the retrieved
# chunks are semantically near the query so their text usually contains a term
# anyway (and common words like 'safety' appear everywhere), which would make
# the lexical flag true for nearly everything." A lexical arm that is always
# true is a gate that never fires.
#
# `derived_title` mirrors _aggregate_topics' own fallback chain so one
# definition of "the title" exists.
_terms = _lexical_terms(question)                       # :761
def _derived_title(c):
    md = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
    return (c.get("topic_title") or md.get("title")
            or (c.get("chunk_text") or "")[:60])
_lexical = any(
    any(t in _derived_title(c).lower() for t in _terms)
    for c in chunks
)

# Absent is not far: _aggregate_topics defaults a missing distance to 1.0, which
# is right for ranking and would silently route every chunk to the web here.
_skip = (bool(_dists) and not _lexical and not basis.get("widened")
         and min(_dists) > _DISTANCE_GATE)
web = web_answer.answer(question, chunks, skip_verdict=_skip)
```

- [ ] **Step 4: Run tests + suite. Revert `skip_verdict=_skip` to `False`, watch the gate tests go red, restore.**

- [ ] **Step 5: Commit.**

---

### Task 7: The timeout ladder descends

**Files:**
- Modify: `src/template.yaml:4360` (`ApiFunction` Timeout), `:1838` (`AskAgentFunction` Timeout), the `LLM_HTTP_TIMEOUT` env on `AskAgentFunction`
- Test: `tests/unit/test_the_limit_is_not_the_thing_that_stops.py` (extend)

**Not part of the feature and behind no flag** — a configuration defect. Measured 2026-09-17 on prod: APIGW 29 s < `ApiFunction` 30 s < `AskAgentFunction` 60 s < `LLM_HTTP_TIMEOUT` 45 s. Nothing enforces the budget the whole design is written against, so an overrun surfaces as a gateway 504 with the model call still running and still billing.

- [ ] **Step 1: Write the failing test**

```python
# Three helpers this task ADDS to the module. `_template()` and `_workflow()`
# already exist there; these do not, and both tests below are unrunnable until
# they do. `_env_text` returns the raw YAML of a function's Environment block
# (raw, because `!Ref X` is a YAML tag a plain loader will not resolve to a
# string); `_env_value` returns one LITERAL value from it; `_timeout` returns a
# function's Timeout property.
#
#   def _env_text(fn): ...          # the block as written, for `!Ref` matching
#   def _env_value(fn, key): ...    # one literal env value, as a string
#   def _timeout(fn): ...           # Resources[fn].Properties.Timeout


def test_the_ask_timeouts_descend():
    """Each layer must give up before the one waiting on it."""
    t = _template()
    api = t["Resources"]["ApiFunction"]["Properties"]["Timeout"]
    agent = t["Resources"]["AskAgentFunction"]["Properties"]["Timeout"]
    http = int(t["Resources"]["AskAgentFunction"]["Properties"]
                ["Environment"]["Variables"]["LLM_HTTP_TIMEOUT"])
    assert api < 29, "API Gateway gives up at 29s; the Lambda must go first"
    assert agent < api
    assert http < agent


def test_the_ask_deadlines_descend():
    """The OTHER ladder (Task 9). All three are LITERALS in the env blocks --
    deliberately, so one reader works for all of them: a !Ref would hide the
    number in a parameter Default and need a second way to read it. They live in
    two functions and two tasks, so nothing else notices when one is raised.

    A waiter's budget must EXCEED the budget of what it waits on, or it abandons
    a callee that is still working correctly."""
    invoke   = int(_env_value("ApiFunction", "ASK_INVOKE_TIMEOUT"))
    deadline = int(_env_value("AskAgentFunction", "ASK_DEADLINE_SECONDS"))
    one_call = int(_env_value("AskAgentFunction", "LLM_HTTP_TIMEOUT"))
    assert one_call < deadline, "one model call may not spend the whole request"
    assert deadline < invoke, "the agent must finish before its caller stops waiting"
    assert invoke < _timeout("ApiFunction"), "and the invoke before the runtime kills us"
```

- [ ] **Step 2: Run, watch it fail** with the real numbers (30, 60, 45).

- [ ] **Step 3: Set** `ApiFunction` 28, `AskAgentFunction` 26, `LLM_HTTP_TIMEOUT` **16**, each with a comment naming the layer above it.

16, not 24: `LLM_HTTP_TIMEOUT` is one model call inside a request whose whole
budget is `ASK_DEADLINE_SECONDS` (20, Task 9). Setting it equal to — or above —
that budget lets one call spend everything and leaves nothing for retrieval, the
rewrite or synthesis. See Task 9 for both ladders; this task owns only the hard
kills plus this one env value.

- [ ] **Step 4: Run suite. Commit.**

---

### Task 8: The screen path gets a timing line, and a failure gets a reason

**Two halves of spec §4.8, in one task because they are one log line.**

The second half fixes a gap that is currently invisible: `lambda_fieldsight_api.py:50`
builds `lambda_client = boto3.client('lambda')` with **no `Config`**, so
botocore's default read timeout outlives `ApiFunction`'s own 30 s. A hung Ask
Agent therefore kills `ApiFunction` from the runtime **before** its `except`
runs — `logger.error("Ask agent invocation failed: …")` never executes and the
only trace is a bare `Task timed out`. Add a
`botocore.config.Config(read_timeout=…, connect_timeout=…, retries={"max_attempts": 0})`
whose `read_timeout` sits below `ApiFunction`'s Timeout (Task 7), so the invoke
fails **inside our code**, where it can be named.

Measured 2026-09-17 on prod over 30 days: zero `Task timed out`, zero
`Ask agent invocation failed`, zero `FunctionError`, max duration 19.1 s. This
path has **never run in production**, so it cannot be validated by waiting —
the test must force it.

**Files (both halves):**
- Modify: `src/lambda_fieldsight_api.py:50` — the `boto3.client('lambda')` construction
- Modify: `src/lambda_fieldsight_api.py` — `ask_question`'s `except`
- Modify: `src/lambda_ask_agent.py` — `_rag_answer`'s timing line
- Test: `tests/unit/test_lambda_fieldsight_api_ask.py`, `tests/unit/test_lambda_ask_agent_rag.py`

- [ ] **Step 1: Write the failing test for the failure reason**

```python
def test_a_hung_agent_is_described_rather_than_killed(monkeypatch, caplog):
    """Today the runtime kills ApiFunction before this except runs, so the
    only trace is `Task timed out`. Force the botocore timeout instead."""
    import botocore.exceptions

    def _boom(**kw):
        raise botocore.exceptions.ReadTimeoutError(endpoint_url="lambda")
    monkeypatch.setattr(api.lambda_client, "invoke", _boom)

    resp = api.ask_question({"question": "q"}, {"sub": SUB})
    assert resp["statusCode"] == 504
    assert "ask agent read timeout" in caplog.text.lower()
```



**Files:**
- Modify: `src/lambda_ask_agent.py` (`_rag_answer`)
- Test: `tests/unit/test_lambda_ask_agent_rag.py`

The only per-call timing on `/ask` today is `llm_utils`'s `qwen done:` — which is why a 14-day prod window yields n=27 for a route with 5115 gateway invocations. The tail cannot be promised until it can be seen.

- [ ] **Step 2: Write the failing test for the timing line**

```python
def test_the_ask_logs_its_stages(wired, caplog):
    ask._rag_answer({"question": "q", "caller_sub": SUB})
    line = [r for r in caplog.records if "ask timing:" in r.message]
    assert line, "one structured line per answer, like the voice path's"
    for field in ("rewrite=", "retrieval=", "synthesis=", "total=", "history_turns="):
        assert field in line[0].getMessage()
```

- [ ] **Step 3: Run both, watch both fail**

Run: `python -m pytest tests/unit/test_lambda_fieldsight_api_ask.py::test_a_hung_agent_is_described_rather_than_killed tests/unit/test_lambda_ask_agent_rag.py::test_the_ask_logs_its_stages -q`

Expected, and they must fail for **different** reasons: the first because
`ask_question` returns 500 with no "read timeout" in the log, the second because
no `ask timing:` line exists at all.

- [ ] **Step 4: Give the invoke a deadline inside our own process**

```python
# src/lambda_fieldsight_api.py, replacing the bare boto3.client('lambda').
# BOTH imports are new: the module imports os and boto3 today (:35, :39) and
# does not mention botocore anywhere, so the except clause below needs its own.
import botocore.exceptions
from botocore.config import Config

# read_timeout BELOW ApiFunction's own Timeout (Task 7), so a hung Ask Agent
# fails HERE, in code that can name it, instead of the runtime killing this
# function first and leaving a bare `Task timed out` as the only trace.
# retries=0: a synchronous user-facing invoke must not silently double the wait.
_LAMBDA_INVOKE_TIMEOUT = int(os.environ.get("ASK_INVOKE_TIMEOUT", "24"))
lambda_client = boto3.client('lambda', config=Config(
    read_timeout=_LAMBDA_INVOKE_TIMEOUT,
    connect_timeout=5,
    retries={"max_attempts": 0},
))
```

and in `ask_question`'s `except`, name the class rather than the exception:

```python
    except botocore.exceptions.ReadTimeoutError:
        logger.error("ask agent read timeout after %ss", _LAMBDA_INVOKE_TIMEOUT)
        return error('Ask temporarily unavailable', 504)
    except Exception as e:
        logger.error(f"Ask agent invocation failed: {e}")
        return error('Ask temporarily unavailable', 502)
```

The response body is **not** what the reader sees — the frontend replaces every
failure with the one reassuring line (Task 11). The status code is for us.

- [ ] **Step 5: Add the timing line**

Mirror `lambda_ask_agent.py:1695`'s shape — one structured line, all stages,
emitted on every answer including the early returns.

- [ ] **Step 6: Run both, watch both pass. Run the whole suite.**

- [ ] **Step 7: Prove each guards its own half**

Revert the `Config(...)` alone → the read-timeout test goes red. Restore.
Revert the timing line alone → the timing test goes red. Restore. Two halves,
two independent reverts: a single revert that reddens both would mean one of
these tests is not testing what it claims.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_fieldsight_api.py src/lambda_ask_agent.py tests/unit/test_lambda_fieldsight_api_ask.py tests/unit/test_lambda_ask_agent_rag.py
git commit -m "A slow Ask is described in the log instead of vanishing"
```

---

### Task 9: The three flags, wired in three places each

**Files:**
- Modify: `src/template.yaml` — three parameters near `:301`, env on `AskAgentFunction` near `:1904`
- Modify: `src/template.yaml` — `ASK_DEADLINE_SECONDS` as a **literal** on `AskAgentFunction`, beside the existing `LLM_HTTP_TIMEOUT: '45'` (`:1846`); `ASK_INVOKE_TIMEOUT` as a **literal** on **`ApiFunction`**, whose `Environment.Variables` ends with `ASK_AGENT_FUNCTION` (`:4372`). The invoke timeout belongs to the function that *makes* the invoke, not the one that receives it.

**Why two of the five numbers are NOT parameters.** `LLM_HTTP_TIMEOUT` is
already a literal on this very function, and there is no `LlmHttpTimeout`
parameter. Making the two new ladder numbers `!Ref`s would put three sibling
timeouts in two different homes — two readable only from a parameter `Default`,
one only from the env block — and any test covering the ladder would need two
ways to read one thing. They are ladder constants, not product switches: the
cost is that changing one needs a template edit, which for a number that must
stay ordered against four others is the correct cost. Same trade the spec
records for `FINALIZE_EMAIL_WAIT_SEC`.
- Modify: `.github/workflows/deploy.yml:228`, `.github/workflows/deploy-prod.yml:262`
- Test: `tests/unit/test_template_workflow_parameter_wiring.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.parametrize("param", ["AskConversationMemory", "AskRewriteBudget",
                                   "AskDistanceGate"])
def test_the_flag_is_wired_in_all_three_places(param):
    """A flag wired in two of three reads as its default and nothing fails."""
    assert param in _template()["Parameters"]
    assert f"!Ref {param}" in _env_text("AskAgentFunction")
    for wf in ("deploy.yml", "deploy-prod.yml"):
        assert f"{param}=" in _workflow(wf)
```

- [ ] **Step 2: Run, watch it fail.**

- [ ] **Step 3: Implement**, mirroring the `EnableWebAnswer` block verbatim in form:

```yaml
  AskConversationMemory:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      Rewrite a follow-up question into one that stands on its own, using the
      previous turns. Off by default on every stack. The conversation reaches
      the rewrite only and never the answering prompt, so a recording deleted
      between turns cannot be quoted or cited -- see the design's 2.1.
```

```yaml
          ASK_CONVERSATION_MEMORY: !Ref AskConversationMemory
          ASK_REWRITE_BUDGET: !Ref AskRewriteBudget
          ASK_DISTANCE_GATE: !Ref AskDistanceGate
          # Literal, beside LLM_HTTP_TIMEOUT: '45' two lines away. A ladder
          # constant, not a switch -- see the note above.
          ASK_DEADLINE_SECONDS: '20'
```

and on **`ApiFunction`**, whose `Environment.Variables` currently ends with
`ASK_AGENT_FUNCTION`:

```yaml
          # How long this function waits for the Ask Agent. Below its own
          # Timeout (Task 7) so the invoke fails here, in code that can name it.
          ASK_INVOKE_TIMEOUT: '24'
```

All four, in both workflows — `deploy.yml` with `TEST_`, `deploy-prod.yml` with
`PROD_`:

```yaml
              "AskConversationMemory=${{ vars.TEST_ASK_CONVERSATION_MEMORY || 'false' }}" \
              "AskRewriteBudget=${{ vars.TEST_ASK_REWRITE_BUDGET || '4.0' }}" \
              "AskDistanceGate=${{ vars.TEST_ASK_DISTANCE_GATE || '0.55' }}" \
```

Three flags above, two literals beside them, and **two different ladders** the
numbers have to satisfy (Task 7). Conflating the ladders is how the first draft
of this section got the direction backwards.

**Hard kills — outside in.** Each layer must die before the one waiting on it,
so the outer layer is still alive to report what happened:

```
API Gateway 29  >  ApiFunction 28  >  AskAgentFunction 26
```

**Voluntary deadlines — also outside in, and all BELOW the kills.** A waiter's
budget must be LARGER than the budget of the thing it waits on, or the caller
abandons a callee that is still working correctly:

```
ASK_INVOKE_TIMEOUT 24   (ApiFunction's wait on the agent)
      >  ASK_DEADLINE_SECONDS 20   (the agent's whole-request budget)
            >  LLM_HTTP_TIMEOUT 16 (one model call)
```

`LLM_HTTP_TIMEOUT` must be strictly below `ASK_DEADLINE_SECONDS`, not equal to
it: a single model call permitted to consume the entire request budget leaves
nothing for retrieval, the rewrite or synthesis, and the request dies having
done one thing.

`ASK_DEADLINE_SECONDS` (20) sits below `AskAgentFunction`'s Timeout from Task 7
(26), which is itself below `ApiFunction`'s (28). A deadline above the timeout
that kills it is not a deadline — it is a number nothing ever reaches.

- [ ] **Step 4: Run suite. Commit.**

---

### Task 10: The gate on the rewrite

**Files:** `src/lambda_ask_agent.py`

- [ ] **Step 1: Test** — with `ASK_CONVERSATION_MEMORY` unset, a body carrying history produces a payload identical to the same body without it, and `standalone_question` is never called.
- [ ] **Step 2–5:** run → fail → read the flag at call time, never at import (`web_answer.enabled()` states why: a flag captured at import survives a warm container after the stack has been redeployed with it off) → pass → commit.

---

### Task 11: `fieldsight-ui`

**Repo:** `fieldsight-ui`. **No CI — an empty check list there is not evidence.** Local test results are the only evidence.

- [ ] Add `history` to `requestBodyFor()` (`scripts/composites/ask-chat.js:483`) as `{question, answer}` turns from the log it already keeps; **omit the key entirely when empty**.
- [ ] Do **not** add `tz` here — `scripts/api/ask.js:83` already injects the browser zone.
- [ ] Keep the clear-on-scope-change at `:696`. Add a test that pins it: carrying a conversation across a site switch would retrieve site B with site A's referents.
- [ ] Render `asked` when present and different from what was typed ("Searched for: …").
- [ ] Render `web.refused` when present — `question_admission` already returns the sentence; nothing shows it, so a guard reads as an outage.
- [ ] **Collapse the two failure messages into one reassuring line** (spec §4.8). `ask-chat.js:928` currently branches on `err.timeout` to say either *"The agent took too long to answer…"* or *"Could not reach the agent…"*. Both go. The replacement, for every failure class:

      FieldSight is busy at the moment. Your question has not been lost —
      please try again shortly. If it keeps happening, contact the
      FieldSight team.

- [ ] **Keep the branch, drop the display.** `err.timeout` (`_fetch.js:117`) and `err.status` (`:248`) still exist and still decide what is *reported*; they no longer decide what is *shown*. A test pins that a timeout and a 504 render identical text — otherwise the next person "helpfully" re-adds the distinction to the screen.
- [ ] Run the local suite; record the count in the PR.

---

### Task 12: `GrandTime` history (after Task 0 has run for a fortnight)

**Repo:** `GrandTime`. Deliberately last: §10.3 asks whether the device needs history at all, and Task 0's `history_turns` measurement answers it. **Do not start this task until that number exists.**

- [ ] Keep the last turns in memory only, cleared when the app is backgrounded.
- [ ] Send them as `{question, answer}`, capped by the server's numbers (6 / 2000), not a second device-side cap.
- [ ] Update the `AskApiClient` class docstring again.

---

## Verification on TEST

Spec §8, all ten steps. Three deserve emphasis here:

- **Step 6 (deletion)** is the single most important check: ask about a recording, delete it, ask a follow-up in the same chat. The answer must not restate the deleted content and must carry no citation to it.
- **Step 4** runs steps 2–3 **five times** and decides from the count. A prompt's behaviour is not established by one run.
- **Step 9** reads `ENABLE_WEB_ANSWER` back rather than setting it — it is already `'true'` on TEST — and checks that the verdict still runs on a question the records DO answer. A gate that never lets the verdict run and a gate that is always open produce the same log.

## Rollout

Order: **Task 0 (device `tz`, no flag)** → Tasks 1–10 (backend, inert) → Task 11 (web) → flag on for TEST → §8 → owner decides prod → Task 12.

Rollback is `ASK_CONVERSATION_MEMORY=false`, whose off-path is the code that runs today.
