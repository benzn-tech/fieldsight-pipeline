# Voice Agent Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TEST voice-agent endpoint answer spoken questions from the caller's own site records, with a verified identity, a keep-alive that survives ElevenLabs' content timeouts, and an opener that is voiced at once.

**Architecture:** Python keeps every Ask rule: AskAgentFunction gains a `prompt_only` mode that stops right after the one prompt build and returns the prompt plus the *resolved* LLM request (or a finished answer for the three early-return paths). The Node Function URL (`voice_llm/`) verifies an HMAC voice-session token minted by a new org-api route, streams a fixed ≥141-char opener, keeps the turn alive with one-space content deltas whenever 1 s passes without content, invokes `prompt_only`, then streams the answer from OpenRouter with exactly the resolved request.

**Tech Stack:** Python 3.11 Lambdas (pytest, FakeConn doubles), Node.js 22 Lambda with response streaming (`node:test`, no npm dependencies; AWS SDK v3 is provided by the runtime), SAM template, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-14-voice-agent-phase2-design.md` (revision 3, Fable: "Ready for plan"). Read §0 (measurements E1–E7) before any task.

## Global Constraints

- TEST only. Every new resource/env sits behind `Condition: HasVoiceLlm`; `deploy-prod.yml` passes no new parameter. A guard test already fails if it does (`tests/unit/test_voice_llm_shell.py`).
- Token: `P = base64url_nopad(utf8(json))`, json `{"sub","aud":"voice-llm","exp":<int epoch s>}`; `S = base64url_nopad(HMAC-SHA256(key, ascii(P)))`; token `P + "." + S`. Verify over the **received** `P` bytes, `timingSafeEqual`, `aud == "voice-llm"`, `exp > now - 30`. TTL 300 s. Empty key → refuse (Node) / 503 (mint). No `jti`.
- Opener (exact, ≥141 chars): `Got it, let me check your site records for that now. I'm pulling up what was said and recorded on site today, and this will only take a moment... `
- Fallback line (exact): `Sorry, I couldn't reach your site records just now. Please try again.`
- Keep-alive: one-space `delta.content` whenever no content has been written for 1000 ms, from authorisation until `[DONE]`. SSE comments do NOT count (E3).
- Logs carry no question or answer text. `sub_hash` = first 12 hex of HMAC-SHA256(signing key, sub).
- Node answer request: `messages` is exactly one `user` message carrying the whole prompt; model/reasoning/max_tokens/temperature exactly as `prompt_only` returns them.
- `OPENROUTER_BASE_URL` comes from the existing `QwenBaseUrl` parameter and the key from `QwenChatApiKey`; never new literals.
- Deadlines: `prompt_only` invoke abort 9000 ms (Python told `deadline_s: 8`); answer first token 6000 ms; whole turn 30000 ms.
- No npm dependencies in `voice_llm/` (package.json must keep `name` + `version`).
- Windows checkout: files may be CRLF; edit with exact single-line anchors; never `git add -A`; never `git stash`.

## Amendments after plan review (override the task text where they conflict)

- **A1 (Task 1):** the equality test alone is tautological once `_call_qwen` calls the builder. Step 0, BEFORE editing `llm_utils.py`: capture the body `_call_qwen` posts today for three inputs (OpenRouter base URL; force_json True/False; enable_thinking True/False/None, model None and a fixed id) and paste those dicts as **literals** into `test_llm_request_builder.py`. The refactor must leave them green. The diff of `_call_qwen` must be a pure move (`git diff --color-moved`) — move the existing lines, do not retype the plan's reconstruction of the dashscope branch.
- **A2 (Task 2):** history seam: add a test sending `history: [{"question":"q1","answer":"a1"}]` (Node's exact shape; `ask_history._clean_voice_history` reads `question`/`answer`) and assert `history_turns == 1`. Deadline: only the rewrite budget reads the base before the prompt is built (`_ask_budget_left` callers at ~1622/1677 are after it and never run under `prompt_only`). The rag-search invoke has no client timeout of its own; Node's 9 s abort is the bound and is logged as `invoke_abandoned` — do not add a boto Config in this task.
- **A3 (Task 8):** `@aws-sdk/client-lambda` exists in the Lambda runtime but not locally or in CI. Load it with `await import("@aws-sdk/client-lambda")` **inside** the default `invokeAsk` only — never a top-level import (it would break every `node --test` file at load). Wrap the whole turn in try/catch/finally: any throw → `turn.fail(FALLBACK)`. Coerce `String(result.answer ?? "").trim()` (the outer-except body may lack `answer`). The 30 s turn guard must also abort the OpenRouter fetch (pass an `AbortSignal` into `streamAnswer`; add `signal` to its options) so the Lambda does not run on after the stream closed.
- **A4 (Task 5/8):** add a Node test `OPENER.length >= 141` (the opener is 146 chars; 141 is the shortest measured voiced length, spec §0 E6). The spec's opener string gained the word "today" for this reason.
- **A5 (all):** `git fetch origin && git merge origin/develop` was done by the controller before Task 1. Implementers: never `git stash`, never `git add -A`, never push.

## Review Focus

1. **A forged, tampered, expired or missing token** must never reach retrieval — the turn speaks the fallback line. (Task 4 tests every tamper; Task 8 tests the handler never invokes on a bad token.)
2. **A question whose retrieval is empty or fails** must be spoken as Ask already words it, not as "couldn't reach" — and an empty `answer` string must become the fallback line. (Task 2 `kind: answer` tests; Task 8 passthrough tests.)
3. **A slow provider mid-answer** (no token for >1 s after the answer started) must not kill the turn — keep-alive keeps writing. (Task 5 virtual-time test with a stalled source.)
4. **History containing our own opener/keep-alive spaces** must not reach the rewrite as an "answer". (Task 6 strips through `...`+whitespace, drops keep-alive-only turns.)
5. **The normal (non-voice, non-prompt_only) Ask path** must be byte-for-byte unchanged. (Task 2 guard test; Task 1 pins `_call_qwen` payload equality.)

---

### Task 1: One request builder for the OpenRouter payload

**Files:**
- Modify: `src/llm_utils.py` (inside `_call_qwen`, the block that builds `payload` and calls `_openrouter_fields`)
- Test: `tests/unit/test_llm_request_builder.py`

**Interfaces:**
- Produces: `llm_utils.build_qwen_payload(prompt, max_tokens, force_json, enable_thinking=None, model=None) -> dict` — the exact dict `_call_qwen` posts (before any model-chain substitution of `payload["model"]`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_llm_request_builder.py
"""The voice endpoint streams with the request Python would have sent.
That only holds if there is ONE builder; two copies drift (the repo has paid
for a forked request shape before -- vendor-swap fossils)."""
import importlib

import pytest


@pytest.fixture
def lu(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "k")
    monkeypatch.setenv("QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    import llm_utils
    return importlib.reload(llm_utils)


def test_builder_is_what_call_qwen_posts(lu, monkeypatch):
    sent = {}

    def fake_post(url, body, headers, deadline=None, clock=None, **kw):
        sent["body"] = body
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(lu, "_post_with_retry", fake_post)
    monkeypatch.setattr(lu, "model_chain", lambda m: [m])
    lu._call_qwen("hello prompt", 512, False, enable_thinking=False, model="google/x")
    built = lu.build_qwen_payload("hello prompt", 512, False, enable_thinking=False, model="google/x")
    import json
    assert json.loads(sent["body"]) == built


def test_builder_sends_one_user_message(lu):
    p = lu.build_qwen_payload("the whole prompt", 256, False)
    assert p["messages"] == [{"role": "user", "content": "the whole prompt"}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --with-requirements requirements-test.txt python -m pytest tests/unit/test_llm_request_builder.py -q`
Expected: FAIL — `AttributeError: module 'llm_utils' has no attribute 'build_qwen_payload'`. If `_post_with_retry`'s real call signature or body encoding differs from the fake (read it first), adapt the fake to capture the body exactly as posted — the assertion must compare against what is really sent.

- [ ] **Step 3: Extract the builder**

Move the payload-building lines out of `_call_qwen` into:

```python
def build_qwen_payload(prompt, max_tokens, force_json, enable_thinking=None, model=None):
    """The exact request body `_call_qwen` posts, before any model-chain swap.

    One builder on purpose: the voice endpoint (Node) streams with the request
    the Ask path resolves here, so a second copy of this logic would let the
    two drift silently (spec 2026-10-14 phase 2, §4)."""
    thinking = QWEN_ENABLE_THINKING if enable_thinking is None else bool(enable_thinking)
    payload = {"model": model or qwen_model_for(thinking),
               "messages": [{"role": "user", "content": prompt}]}
    if LLM_TEMPERATURE is not None:
        payload["temperature"] = LLM_TEMPERATURE
    if not _is_dashscope(QWEN_BASE_URL):
        _openrouter_fields(payload, max_tokens, force_json, enable_thinking, thinking)
    elif thinking:
        payload["enable_thinking"] = True
    else:
        payload["enable_thinking"] = False
        if force_json:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["max_tokens"] = max_tokens
    return payload
```

Keep the existing explanatory comments that sat on those lines (move them with the code). In `_call_qwen` replace the moved block with `thinking = QWEN_ENABLE_THINKING if enable_thinking is None else bool(enable_thinking)` (still needed below) and `payload = build_qwen_payload(prompt, max_tokens, force_json, enable_thinking, model)`.

- [ ] **Step 4: Run the new tests and the whole llm suite**

Run: `uv run --with-requirements requirements-test.txt python -m pytest tests/unit/test_llm_request_builder.py tests/unit -q -k "llm or qwen or openrouter"`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit** — `git add src/llm_utils.py tests/unit/test_llm_request_builder.py`; message "One request builder for the OpenRouter payload".

---

### Task 2: `prompt_only` mode on AskAgentFunction

**Files:**
- Modify: `src/lambda_ask_agent.py` (`_rag_answer`: the `deadline_left = ASK_DEADLINE_SECONDS - …` line ~1062; right after `prompt = build_rag_prompt(...)` ~1330; `lambda_handler` RAG dispatch ~2061)
- Test: `tests/unit/test_ask_prompt_only.py`

**Interfaces:**
- Consumes: `llm_utils.build_qwen_payload` (Task 1).
- Produces (invoke response `body`, JSON): either
  `{"kind":"prompt","prompt":str,"asked":str,"rewritten":bool,"chunk_ids":[str],"basis":obj|null,"history_turns":int,"llm":{"model":str,"reasoning":obj|null,"max_tokens":int|null,"temperature":num|null}}`
  or `{"kind":"answer","answer":str, ...whatever the early return carried}`.

- [ ] **Step 1: Write the failing tests** (mirror the fixtures/monkeypatching in `tests/unit/test_lambda_ask_agent_rag.py`; read it first and reuse its helpers for the rag-search fake, embed fake and `caller_sub`)

```python
# tests/unit/test_ask_prompt_only.py  -- skeleton; reuse the rag test helpers
def test_prompt_only_returns_prompt_and_resolved_llm(...):
    # rag-search fake returns two chunks; call_llm must NOT be called
    # monkeypatch llm_utils.call_llm to raise if called
    res = laa.lambda_handler({"question": "when is the backfill due",
                              "caller_sub": "sub-1", "mode": "voice",
                              "prompt_only": True}, None)
    body = json.loads(res["body"])
    assert body["kind"] == "prompt"
    assert "backfill" in body["prompt"]
    assert body["chunk_ids"] == ["c1", "c2"]
    assert body["llm"]["model"]  # resolved, never "none"
    assert body["llm"] == {k: llm_utils.build_qwen_payload(
        body["prompt"], laa.MAX_ANSWER_TOKENS, False,
        enable_thinking=<same as voice path>, model=<same>).get(k)
        for k in ("model", "reasoning", "max_tokens", "temperature")}

def test_empty_retrieval_is_an_answer_kind(...):   # rag-search returns []
    assert body["kind"] == "answer" and body["answer"]   # Ask's own no-records text

def test_rag_search_error_is_an_answer_kind(...):    # rag-search FunctionError
    assert body["kind"] == "answer"

def test_without_prompt_only_the_path_is_unchanged(...):
    # same inputs, no prompt_only: response has no "kind", call_llm called once
    assert "kind" not in body

def test_deadline_s_replaces_the_base(...):
    # ASK_DEADLINE_SECONDS=24, deadline_s=3, history present, rewrite fake
    # records its timeout: assert recorded timeout <= 3
```

Fill the `<same as voice path>` placeholders by reading lines ~1602-1632: `voice_model = ASK_VOICE_MODEL unless "", "none"`; with a voice model the call uses `enable_thinking=False, model=voice_model`, otherwise `enable_thinking=None, model=None`. The test must compute the expectation the same way.

- [ ] **Step 2: Run to verify they fail** — `… -m pytest tests/unit/test_ask_prompt_only.py -q` → FAIL (`kind` missing).

- [ ] **Step 3: Implement**

At the rewrite budget line:

```python
            _base = (float(body.get("deadline_s") or ASK_DEADLINE_SECONDS)
                     if body.get("prompt_only") else ASK_DEADLINE_SECONDS)
            deadline_left = _base - (time.monotonic() - _started)
```

Immediately after `prompt = build_rag_prompt(...)`:

```python
        if body.get("prompt_only"):
            # Voice agent (spec 2026-10-14 phase 2 §4): every rule above has run;
            # Node streams the synthesis with exactly the request this path
            # would have sent. Nothing below (web check, synthesis, language
            # retry) runs for this caller.
            _vm = os.environ.get("ASK_VOICE_MODEL", "").strip()
            _vm = None if _vm.lower() in ("", "none") else _vm
            _req = llm_utils.build_qwen_payload(
                prompt, MAX_ANSWER_TOKENS, False,
                enable_thinking=(False if _vm else None), model=_vm)
            return {
                "kind": "prompt", "prompt": prompt,
                "asked": asked, "rewritten": bool(rewritten),
                "chunk_ids": [str(c.get("id")) for c in chunks if c.get("id") is not None],
                "basis": basis, "history_turns": history_turns,
                "llm": {k: _req.get(k) for k in ("model", "reasoning", "max_tokens", "temperature")},
            }
```

(Use the local names actually in scope at that line — read them; `llm_utils` is imported lazily inside `_rag_answer`, so import it before this block if it is not yet bound there.)

In `lambda_handler`, replace `return ok(_rag_answer(body))` with:

```python
        result = _rag_answer(body)
        if body.get("prompt_only") and "kind" not in result:
            # The three early returns (metric route, rag-search error, empty
            # retrieval) and the outer except carry a finished answer, not a
            # prompt. Node speaks it verbatim; an empty one becomes the
            # fallback line on the Node side.
            result = {"kind": "answer", **result}
        return ok(result)
```

- [ ] **Step 4: Run** the new tests plus `tests/unit/test_lambda_ask_agent_rag.py` → PASS.
- [ ] **Step 5: Commit** "AskAgent prompt_only: the voice endpoint gets the prompt and the resolved request".

---

### Task 3: Mint voice-session tokens (Python) + shared fixture + org-api route

**Files:**
- Create: `src/voice_session.py`
- Modify: `src/lambda_org_api.py` (route in `dispatch`, after the caller guard and the `/app/latest` route)
- Create: `tests/unit/test_voice_session.py`, `tests/fixtures/voice_session_token.json`

**Interfaces:**
- Produces: `voice_session.mint(sub: str, key: str, now: int, ttl: int = 300) -> dict` → `{"token": str, "expires_at": int}`; raises `ValueError` on empty key. `voice_session.verify(token, key, now) -> str | None` (Python verifier, used only in tests and fixture generation). Route `POST /api/org/voice/session` → 200 `{"token","expires_at"}`, 503 when `VOICE_SESSION_SIGNING_KEY` is empty.
- Produces fixture: `tests/fixtures/voice_session_token.json` = `{"key": "fixture-key-not-secret", "now": 1800000000, "sub": "11111111-2222-3333-4444-555555555555", "token": "<minted>", "expires_at": 1800000300}`.

- [ ] **Step 1: Failing tests**

```python
# tests/unit/test_voice_session.py
import base64, hashlib, hmac, json, os
import pytest
import voice_session as vs

KEY = "fixture-key-not-secret"

def test_round_trip():
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    assert vs.verify(t, KEY, now=1001) == "sub-1"

def test_wire_format_is_unpadded_and_signs_the_received_payload():
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    p, s = t.split(".")
    assert "=" not in t
    want = base64.urlsafe_b64encode(hmac.new(KEY.encode(), p.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert s == want
    body = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert body == {"sub": "sub-1", "aud": "voice-llm", "exp": 1300}

@pytest.mark.parametrize("mutate", ["payload", "sig", "aud", "expired", "garbage"])
def test_tamper_is_refused(mutate):
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    p, s = t.split(".")
    now = 1001
    if mutate == "payload": p = p[:-1] + ("A" if p[-1] != "A" else "B")
    if mutate == "sig": s = s[:-1] + ("A" if s[-1] != "A" else "B")
    if mutate == "aud":
        p = base64.urlsafe_b64encode(json.dumps({"sub": "sub-1", "aud": "other", "exp": 1300}).encode()).rstrip(b"=").decode()
        s = base64.urlsafe_b64encode(hmac.new(KEY.encode(), p.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    if mutate == "expired": now = 1300 + 31
    tok = "nonsense" if mutate == "garbage" else p + "." + s
    assert vs.verify(tok, KEY, now=now) is None

def test_empty_key_cannot_mint():
    with pytest.raises(ValueError):
        vs.mint("sub-1", "", now=1)

def test_fixture_is_current():
    """The Node verifier (voice_llm/token.test.mjs) is tested against this file;
    regenerate it from mint() if the format ever changes."""
    f = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "voice_session_token.json")))
    assert vs.mint(f["sub"], f["key"], now=f["now"])["token"] == f["token"]
```

Plus an org-api handler test (mirror `tests/unit/test_org_api_sessions.py`'s `make_event` + monkeypatched `users.get_user_by_sub`): 200 with a token that `vs.verify` accepts for the caller's sub; 503 when the env key is empty; the archived-caller 403 already enforced by `dispatch` is asserted too.

- [ ] **Step 2: Run → FAIL** (module missing).

- [ ] **Step 3: Implement `src/voice_session.py`**

```python
"""Voice-session tokens for the ElevenLabs voice agent (spec 2026-10-14 phase 2 §2).

HMAC-signed, good for one audience ("voice-llm") and five minutes. Minted by
org-api for an authenticated caller; verified by the Node endpoint, which then
uses `sub` as caller_sub. The Cognito idToken is NOT used: ElevenLabs carries
this value in every request body, and an idToken there would be a credential
for the whole API."""
import base64
import hashlib
import hmac
import json

AUD = "voice-llm"
SKEW = 30


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _sig(key: str, p: str) -> str:
    return _b64(hmac.new(key.encode(), p.encode("ascii"), hashlib.sha256).digest())


def mint(sub, key, now, ttl=300):
    if not key:
        raise ValueError("voice session signing key is empty")
    exp = int(now) + int(ttl)
    p = _b64(json.dumps({"sub": sub, "aud": AUD, "exp": exp}, separators=(",", ":")).encode())
    return {"token": p + "." + _sig(key, p), "expires_at": exp}


def verify(token, key, now):
    if not key or not isinstance(token, str) or token.count(".") != 1:
        return None
    p, s = token.split(".")
    if not hmac.compare_digest(s, _sig(key, p)):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return None
    if body.get("aud") != AUD or not isinstance(body.get("exp"), int) or body["exp"] <= int(now) - SKEW:
        return None
    return body.get("sub") or None
```

Note `test_wire_format…` expects `exp` 1300 with ttl 300 — matches. Generate the fixture once with `python -c` calling `mint` with the fixture values and write the JSON file.

Route in `dispatch` (after `/app/latest`):

```python
    if route == "/voice/session" and method == "POST":
        key = os.environ.get("VOICE_SESSION_SIGNING_KEY", "")
        if not key:
            return error("voice sessions are not configured", 503)
        return ok(voice_session.mint(sub, key, now=int(time.time())))
```

(import `voice_session` with the other local imports; confirm `os`/`time` are imported.)

- [ ] **Step 4: Run → PASS**; also `tests/unit/test_org_api*.py`.
- [ ] **Step 5: Commit** "Voice-session tokens: mint, route, shared fixture".

---

### Task 4: Verify tokens in Node against the shared fixture

**Files:** Create `voice_llm/token.mjs`, `voice_llm/token.test.mjs`

**Interfaces:**
- Consumes: `tests/fixtures/voice_session_token.json` (Task 3).
- Produces: `verifyVoiceSession(token: string, key: string, nowSeconds: number) -> string | null` (the sub).

- [ ] **Step 1: Failing test**

```js
// voice_llm/token.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createHmac } from "node:crypto";
import { verifyVoiceSession } from "./token.mjs";

const f = JSON.parse(fs.readFileSync(new URL("../tests/fixtures/voice_session_token.json", import.meta.url)));
const b64 = (b) => Buffer.from(b).toString("base64url");

test("verifies the token Python minted", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, f.now + 1), f.sub);
});

test("every tamper is refused", () => {
  const [p, s] = f.token.split(".");
  const flip = (x) => x.slice(0, -1) + (x.at(-1) === "A" ? "B" : "A");
  const audP = b64(JSON.stringify({ sub: f.sub, aud: "other", exp: f.expires_at }));
  const audS = b64(createHmac("sha256", f.key).update(audP).digest());
  for (const [label, tok, now] of [
    ["payload", flip(p) + "." + s, f.now],
    ["signature", p + "." + flip(s), f.now],
    ["aud", audP + "." + audS, f.now],
    ["expired", f.token, f.expires_at + 31],
    ["garbage", "nonsense", f.now],
    ["two dots", f.token + ".x", f.now],
  ]) assert.equal(verifyVoiceSession(tok, f.key, now), null, label);
});

test("an empty key refuses everything", () => {
  assert.equal(verifyVoiceSession(f.token, "", f.now), null);
});

test("inside the 30 s skew allowance still verifies", () => {
  assert.equal(verifyVoiceSession(f.token, f.key, f.expires_at + 29), f.sub);
});
```

- [ ] **Step 2: Run** `cd voice_llm && node --test` → FAIL (module missing).

- [ ] **Step 3: Implement**

```js
// voice_llm/token.mjs -- verifier for org-api's voice-session tokens.
// Format and rules: spec 2026-10-14 phase 2 §2. Signed over the RECEIVED
// payload string; never re-serialise. Empty key refuses all.
import { createHmac, timingSafeEqual } from "node:crypto";

const AUD = "voice-llm";
const SKEW = 30;

export function verifyVoiceSession(token, key, nowSeconds) {
  if (!key || typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [p, s] = parts;
  const want = Buffer.from(createHmac("sha256", key).update(p, "ascii").digest("base64url"));
  const got = Buffer.from(s);
  if (got.length !== want.length || !timingSafeEqual(got, want)) return null;
  let body;
  try { body = JSON.parse(Buffer.from(p, "base64url").toString("utf8")); } catch { return null; }
  if (body?.aud !== AUD || !Number.isInteger(body?.exp) || body.exp <= nowSeconds - SKEW) return null;
  return typeof body.sub === "string" && body.sub ? body.sub : null;
}
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** "Node verifies voice-session tokens against Python's fixture".

---

### Task 5: The stream writer — opener, keep-alive, terminal states

**Files:** Create `voice_llm/stream.mjs`, `voice_llm/stream.test.mjs`

**Interfaces:**
- Produces: `createTurnStream(out, { id, created, idleMs = 1000, setTimer = setTimeout, clearTimer = clearTimeout })` → `{ text(s), finish(), fail(line) }`.
  - `text(s)` writes one `chat.completion.chunk` with `delta.content = s` (first chunk also `role: "assistant"`) and resets the idle timer.
  - While open, if `idleMs` passes with no write, writes a one-space content chunk and re-arms (E3).
  - `finish()` writes the `finish_reason: "stop"` chunk then `data: [DONE]\n\n`, stops the timer, ends `out`. Idempotent.
  - `fail(line)` = `text(line)` then `finish()`.

- [ ] **Step 1: Failing tests** (use `node:test`'s `mock.timers`)

```js
// voice_llm/stream.test.mjs
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { createTurnStream } from "./stream.mjs";

const sink = () => { const w = []; return { w, write: (s) => w.push(s), end: () => w.push("<END>") }; };
const contents = (w) => w.filter((s) => s.startsWith("data: {")).map((s) => JSON.parse(s.slice(6)).choices[0].delta.content ?? null);

test("keep-alive writes a space after each idle second, and stops at finish", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1 });
  t.text("opener ");
  mock.timers.tick(999); assert.deepEqual(contents(o.w), ["opener "]);
  mock.timers.tick(1);   assert.deepEqual(contents(o.w), ["opener ", " "]);
  mock.timers.tick(1000); assert.equal(contents(o.w).length, 3);
  t.finish();
  mock.timers.tick(5000);
  assert.equal(o.w.at(-1), "<END>");
  assert.equal(o.w.at(-2), "data: [DONE]\n\n");
  assert.equal(contents(o.w).filter((c) => c === " ").length, 2, "no keep-alive after finish");
  mock.timers.reset();
});

test("a stall mid-answer is bridged (Review Focus 3)", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1 });
  t.text("The answer starts");
  mock.timers.tick(3500); // provider stalls 3.5 s
  t.text(" and continues.");
  assert.ok(contents(o.w).filter((c) => c === " ").length >= 3);
  t.finish(); mock.timers.reset();
});

test("first chunk carries the assistant role; finish is idempotent", () => {
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1, setTimer: () => 0, clearTimer: () => {} });
  t.text("a"); t.text("b"); t.finish(); t.finish();
  const chunks = o.w.filter((s) => s.startsWith("data: {")).map((s) => JSON.parse(s.slice(6)));
  assert.equal(chunks[0].choices[0].delta.role, "assistant");
  assert.equal(chunks[1].choices[0].delta.role, undefined);
  assert.equal(o.w.filter((s) => s === "data: [DONE]\n\n").length, 1);
});

test("fail speaks the line then ends", () => {
  const o = sink(); const t = createTurnStream(o, { id: "x", created: 1, setTimer: () => 0, clearTimer: () => {} });
  t.fail("Sorry.");
  assert.deepEqual(contents(o.w).slice(0, 1), ["Sorry."]);
  assert.equal(o.w.at(-1), "<END>");
});
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```js
// voice_llm/stream.mjs -- one ElevenLabs turn's SSE stream.
// Keep-alive is CONTENT (a single space): measured 2026-10-14, SSE comments do
// not keep a turn alive and any content gap of ~4 s kills it (spec §0 E1-E3).
export function createTurnStream(out, { id, created, idleMs = 1000, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let first = true, done = false, timer = null;
  const chunk = (content, finish = null) => {
    const delta = {};
    if (first) { delta.role = "assistant"; first = false; }
    if (content) delta.content = content;
    return `data: ${JSON.stringify({ id, object: "chat.completion.chunk", created,
      model: "fieldsight-voice", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`;
  };
  const arm = () => {
    if (timer !== null) clearTimer(timer);
    timer = setTimer(() => { if (!done) { out.write(chunk(" ")); arm(); } }, idleMs);
  };
  const api = {
    text(s) { if (done || !s) return; out.write(chunk(s)); arm(); },
    finish() {
      if (done) return; done = true;
      if (timer !== null) clearTimer(timer);
      out.write(chunk("", "stop")); out.write("data: [DONE]\n\n"); out.end();
    },
    fail(line) { api.text(line); api.finish(); },
  };
  return api;
}
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** "Voice stream writer: content keep-alive through [DONE]".

---

### Task 6: ElevenLabs `messages` → Ask history

**Files:** Create `voice_llm/history.mjs`, `voice_llm/history.test.mjs`

**Interfaces:** Produces `toHistory(messages) -> { question: string, history: Array<{question, answer}> }` — `question` is the last `user` message's text; `history` the pairs before it.

- [ ] **Step 1: Failing tests**

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { toHistory } from "./history.mjs";

const OPENER = "Got it, let me check your site records for that now. I'm pulling up what was said and recorded on site today, and this will only take a moment... ";

test("pairs prior turns and strips our opener and keep-alive spaces", () => {
  const r = toHistory([
    { role: "system", content: "sys" },
    { role: "user", content: "when is the backfill due" },
    { role: "assistant", content: OPENER + "   Friday, before the digger goes back.  " },
    { role: "user", content: "who is doing it" },
  ]);
  assert.equal(r.question, "who is doing it");
  assert.deepEqual(r.history, [{ question: "when is the backfill due", answer: "Friday, before the digger goes back." }]);
});

test("tolerates whitespace normalised around the ellipsis", () => {
  const r = toHistory([
    { role: "user", content: "q1" },
    { role: "assistant", content: "Got it, let me check that...Friday." },
    { role: "user", content: "q2" },
  ]);
  assert.deepEqual(r.history, [{ question: "q1", answer: "Friday." }]);
});

test("an assistant turn that was only the opener is dropped with its question", () => {
  const r = toHistory([
    { role: "user", content: "q1" }, { role: "assistant", content: OPENER + "     " },
    { role: "user", content: "q2" },
  ]);
  assert.deepEqual(r.history, []);
});

test("a leading assistant greeting with no user turn is ignored", () => {
  const r = toHistory([{ role: "assistant", content: "Hello" }, { role: "user", content: "q" }]);
  assert.deepEqual(r, { question: "q", history: [] });
});

test("array content parts are joined", () => {
  const r = toHistory([{ role: "user", content: [{ type: "text", text: "part one" }, { type: "text", text: "two" }] }]);
  assert.equal(r.question, "part one two");
});
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```js
// voice_llm/history.mjs -- ElevenLabs messages -> the Ask history shape.
// Our opener/restatement is a PREFIX of each assistant turn ending in "...";
// keep-alive spaces pad it. Both are ours, not answers (spec §4, review #10).
const text = (c) => typeof c === "string" ? c
  : Array.isArray(c) ? c.map((p) => typeof p === "string" ? p : p?.text || "").join(" ") : "";
const stripOurs = (s) => {
  const i = s.indexOf("...");
  return (i >= 0 ? s.slice(i + 3) : s).replace(/\s+/g, " ").trim();
};

export function toHistory(messages) {
  const turns = (Array.isArray(messages) ? messages : []).filter((m) => m && (m.role === "user" || m.role === "assistant"));
  let last = -1;
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === "user") { last = i; break; }
  const question = last >= 0 ? text(turns[last].content).trim() : "";
  const history = [];
  let pending = null;
  for (let i = 0; i < last; i++) {
    const m = turns[i];
    if (m.role === "user") pending = text(m.content).trim();
    else if (pending !== null) {
      const answer = stripOurs(text(m.content));
      if (answer) history.push({ question: pending, answer });
      pending = null;
    }
  }
  return { question, history };
}
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** "Map ElevenLabs messages to Ask history, without our own words".

---

### Task 7: Stream the answer from OpenRouter

**Files:** Create `voice_llm/answer.mjs`, `voice_llm/answer.test.mjs`

**Interfaces:**
- Produces: `streamAnswer({ prompt, llm, baseUrl, apiKey, fetchImpl = fetch, firstTokenMs = 6000, onText }) -> Promise<{ ok: boolean, chars: number, firstTokenMs: number|null, error?: string }>`. Sends `POST {baseUrl}/chat/completions` with `{model: llm.model, messages:[{role:"user",content:prompt}], stream:true, ...(llm.reasoning?{reasoning:llm.reasoning}:{}), ...(llm.max_tokens?{max_tokens}:{}), ...(llm.temperature!=null?{temperature}:{})}`; calls `onText(delta)` for each non-empty `delta.content`; aborts with `ok:false, error:"first_token_timeout"` if no content within `firstTokenMs`.

- [ ] **Step 1: Failing tests** — a fake `fetchImpl` returning a `Response` whose body is a `ReadableStream` of SSE lines:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { streamAnswer } from "./answer.mjs";

const sse = (lines, delayMs = 0) => new Response(new ReadableStream({
  async start(c) { for (const l of lines) { if (delayMs) await new Promise((r) => setTimeout(r, delayMs)); c.enqueue(new TextEncoder().encode(l)); } c.close(); },
}), { status: 200, headers: { "content-type": "text/event-stream" } });
const d = (t) => `data: ${JSON.stringify({ choices: [{ delta: { content: t } }] })}\n\n`;

test("sends exactly one user message and the resolved request, forwards deltas", async () => {
  let sent; const got = [];
  const r = await streamAnswer({ prompt: "P", llm: { model: "m", reasoning: { effort: "low" }, max_tokens: 900, temperature: 0 },
    baseUrl: "https://x/api/v1", apiKey: "k",
    fetchImpl: async (url, init) => { sent = { url, body: JSON.parse(init.body), auth: init.headers.Authorization }; return sse([d("Fri"), d("day."), "data: [DONE]\n\n"]); },
    onText: (t) => got.push(t) });
  assert.equal(sent.url, "https://x/api/v1/chat/completions");
  assert.deepEqual(sent.body.messages, [{ role: "user", content: "P" }]);
  assert.equal(sent.body.model, "m"); assert.deepEqual(sent.body.reasoning, { effort: "low" });
  assert.equal(sent.body.stream, true); assert.equal(sent.auth, "Bearer k");
  assert.deepEqual(got, ["Fri", "day."]); assert.equal(r.ok, true); assert.equal(r.chars, 7);
});

test("no content inside the first-token budget is a timeout, not a hang", async () => {
  const r = await streamAnswer({ prompt: "P", llm: { model: "m" }, baseUrl: "u", apiKey: "k", firstTokenMs: 50,
    fetchImpl: async (u, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(new Error("aborted")))),
    onText: () => {} });
  assert.equal(r.ok, false); assert.equal(r.error, "first_token_timeout");
});

test("an HTTP error is reported, nothing spoken", async () => {
  const got = [];
  const r = await streamAnswer({ prompt: "P", llm: { model: "m" }, baseUrl: "u", apiKey: "k",
    fetchImpl: async () => new Response("nope", { status: 502 }), onText: (t) => got.push(t) });
  assert.equal(r.ok, false); assert.deepEqual(got, []);
});

test("a delta split across network chunks is reassembled", async () => {
  const got = [];
  const full = d("Hello world");
  await streamAnswer({ prompt: "P", llm: { model: "m" }, baseUrl: "u", apiKey: "k",
    fetchImpl: async () => sse([full.slice(0, 15), full.slice(15), "data: [DONE]\n\n"]), onText: (t) => got.push(t) });
  assert.deepEqual(got, ["Hello world"]);
});
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement**

```js
// voice_llm/answer.mjs -- stream the synthesis with the request Python resolved.
export async function streamAnswer({ prompt, llm, baseUrl, apiKey, fetchImpl = fetch, firstTokenMs = 6000, onText }) {
  const body = { model: llm.model, messages: [{ role: "user", content: prompt }], stream: true };
  if (llm.reasoning) body.reasoning = llm.reasoning;
  if (llm.max_tokens) body.max_tokens = llm.max_tokens;
  if (llm.temperature !== null && llm.temperature !== undefined) body.temperature = llm.temperature;
  const ac = new AbortController();
  const t0 = Date.now(); let first = null, chars = 0;
  const guard = setTimeout(() => { if (first === null) ac.abort(); }, firstTokenMs);
  try {
    const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
      method: "POST", signal: ac.signal,
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body) });
    if (!res.ok) return { ok: false, chars: 0, firstTokenMs: null, error: `http_${res.status}` };
    const dec = new TextDecoder(); let buf = "";
    for await (const part of res.body) {
      buf += dec.decode(part, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!block.startsWith("data: ") || block === "data: [DONE]") continue;
        let delta; try { delta = JSON.parse(block.slice(6)).choices?.[0]?.delta?.content; } catch { continue; }
        if (!delta) continue;
        if (first === null) first = Date.now() - t0;
        chars += delta.length; onText(delta);
      }
    }
    return { ok: chars > 0, chars, firstTokenMs: first, error: chars > 0 ? undefined : "empty" };
  } catch (e) {
    return { ok: false, chars, firstTokenMs: first, error: first === null && ac.signal.aborted ? "first_token_timeout" : String(e?.message || e) };
  } finally { clearTimeout(guard); }
}
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit** "Stream the voice answer with the resolved request".

---

### Task 8: Handler orchestration (replaces the shell's fixed answer)

**Files:** Modify `voice_llm/index.mjs`, `voice_llm/index.test.mjs`

**Interfaces:**
- Consumes: Tasks 4–7. Env: `VOICE_LLM_BEARER`, `VOICE_SESSION_SIGNING_KEY`, `ASK_AGENT_FUNCTION`, `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `VOICE_LLM_PROBES` (`true` keeps the shell's `hold/delay` probes reachable on TEST).
- Produces: `handle(event, responseStream, deps)` where `deps = { invokeAsk(payload) -> Promise<object>, fetchImpl, now }` defaulted to real implementations; exported for tests.

Turn flow (exact):
1. Route/bearer checks as today (bearer compare → `timingSafeEqual`). Missing `VOICE_SESSION_SIGNING_KEY`, `ASK_AGENT_FUNCTION` or `OPENROUTER_API_KEY` → 503 JSON, logged `voice_llm_misconfigured` with the missing names.
2. Open the SSE stream; `turn = createTurnStream(...)`; immediately `turn.text(OPENER)`.
3. `sub = verifyVoiceSession(body.elevenlabs_extra_body?.voice_session, key, now)`. Null → `turn.fail(FALLBACK)`, log `outcome:"bad_token"`, return. **No invoke happens before this line.**
4. `{question, history} = toHistory(body.messages)`; invoke Ask with `{question, history, caller_sub: sub, mode:"voice", prompt_only:true, deadline_s:8, tz: extra.tz}` under a 9000 ms abort. Timeout/error → `turn.fail(FALLBACK)`, `outcome:"invoke_failed"` (+`invoke_abandoned:true` on timeout).
5. Parse the invoke payload (`JSON.parse(payload.body)`). `kind:"answer"` → `turn.text(answer.trim() || FALLBACK)`; `turn.finish()`.
6. `kind:"prompt"` → `streamAnswer({..., onText: (t) => turn.text(t)})`; `!ok` → `turn.fail(FALLBACK)` when nothing was spoken yet, else `turn.finish()`.
7. Whole-turn guard 30000 ms → `turn.finish()`.
8. One log line `voice_llm_turn` with: `sub_hash`, `outcome`, `invoke_ms`, `first_answer_token_ms`, `answer_chars`, `chunk_ids`, `rewritten`, `history_turns`, `kind`, `retry_suspected`. **Remove `user_text`.** `retry_suspected`: module-level map `hash(lastUserText + messages.length) -> lastSeenMs`, true if seen within 10 s.

- [ ] **Step 1: Failing tests** (extend `index.test.mjs`; inject `deps`):
  - bad token → stream contains OPENER then FALLBACK, `invokeAsk` never called.
  - good token + `kind:"answer"` "No relevant records found for this question." → spoken verbatim after OPENER.
  - good token + `kind:"answer"` with `answer:""` → FALLBACK.
  - good token + `kind:"prompt"` + fake fetch deltas → OPENER then deltas then `[DONE]`; `invokeAsk` received `prompt_only:true`, `caller_sub` = fixture sub, `history` from `messages`.
  - invoke never resolves → FALLBACK within 9 s (mock timers).
  - log line contains no question text and has `sub_hash` of length 12.
  - missing `OPENROUTER_API_KEY` → 503.
  Mint the test token from `tests/fixtures/voice_session_token.json` (set `VOICE_SESSION_SIGNING_KEY` to its key and `deps.now = () => f.now`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the flow above. Real `invokeAsk`: `import { LambdaClient, InvokeCommand } from "@aws-sdk/client-lambda"` (provided by the Node 22 runtime — do not add it to package.json), `InvocationType: "RequestResponse"`, decode `Payload` bytes to JSON. Probes (`hold`/`delay`) stay available only when `VOICE_LLM_PROBES === "true"` and keep their current behaviour.
- [ ] **Step 4: Run** `node --test` → all PASS (existing shell tests updated where the fixed answer was asserted).
- [ ] **Step 5: Commit** "Voice endpoint answers from the caller's own records".

---

### Task 9: Template, IAM, secrets, workflow wiring

**Files:** Modify `src/template.yaml`, `.github/workflows/deploy.yml`, `tests/unit/test_voice_llm_shell.py`

- [ ] **Step 1: Failing wiring tests** (append to `test_voice_llm_shell.py`):
  - `VoiceSessionSigningKey` parameter exists with `NoEcho: true`, `Default: ''`.
  - VoiceLlmFunction env has `VOICE_SESSION_SIGNING_KEY: !Ref VoiceSessionSigningKey`, `ASK_AGENT_FUNCTION: !Ref AskAgentFunction`, `OPENROUTER_API_KEY: !Ref QwenChatApiKey`, `OPENROUTER_BASE_URL: !Ref QwenBaseUrl`, `VOICE_LLM_PROBES: 'true'`, and `Policies` with `LambdaInvokePolicy: {FunctionName: !Ref AskAgentFunction}`.
  - OrgApiFunction env has `VOICE_SESSION_SIGNING_KEY: !If [HasVoiceLlm, !Ref VoiceSessionSigningKey, '']`.
  - deploy.yml binds `VOICE_SESSION_SIGNING_KEY: ${{ secrets.VOICE_SESSION_SIGNING_KEY_TEST }}` and passes it through a guarded `VOICE_SESSION_PARAM` like `VOICE_LLM_PARAM`; deploy-prod.yml mentions neither (extend `test_prod_does_not_create_the_endpoint`).
  Read the real names of the Qwen key/base-URL parameters in `template.yaml` first and use them verbatim.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the template/workflow edits (single-line anchors; comments ABOVE the `sam deploy` command, never inside its continuation — `test_no_comment_sits_inside_a_line_continuation`).
- [ ] **Step 4: Run** the wiring suites + `cfn-lint==1.53.3 src/template.yaml` (CI's pin) → PASS, 0 findings.
- [ ] **Step 5: Commit** "Wire the voice endpoint to Ask, OpenRouter and the session key".
- [ ] **Step 6 (controller, not implementer): secret.** Generate a 32-byte key into the scratchpad file `voice-session-key-test.txt` and set `gh secret set VOICE_SESSION_SIGNING_KEY_TEST --repo benzn-tech/fieldsight-pipeline < file`. If the classifier blocks it, hand the owner that one command. Never print the value.

---

### Task 10: Live verification on TEST

Controller-run, after merge + deploy.

- [ ] Check archival: read `users.get_user_by_sub` and `lambda_rag_search.py` for `archived_at`; record in the PR whether an archived user's still-valid token reaches records (spec §2).
- [ ] Mint a token for a TEST user with records via the new route (synthetic authorizer event to `fieldsight-test-org-api`, body status checked, not just the invoke 200).
- [ ] Agent: a v3-style agent (extra-body override on) — probe client sends `custom_llm_extra_body: {voice_session, tz}`.
- [ ] Run: the three phase-1 questions; a two-turn "it" follow-up; a forged token; an expired token; an empty-records question. Record per turn: first audio, answer spoken (y/n), the endpoint's log fields. Log one real returned assistant `messages` turn (TEST only) to confirm the strip shape.
- [ ] Measure the opener's first-audio time (n ≥ 3) — the spec relies on ≥141 chars voicing at ~1 s.
- [ ] Write the results into the PR description and the spec's §0 as E8+.

---

### Task 11: Model restatement behind a flag (off)

**Files:** Create `voice_llm/restate.mjs`, `voice_llm/restate.test.mjs`; modify `voice_llm/index.mjs`

- Only when `VOICE_RESTATE_ENABLED === "true"` (template default `'false'`): instead of writing the OPENER immediately, keep-alive alone runs (first content = a space) while `restate(question)` races a 1800 ms deadline; result passes the leak guard (no digit, ≤25 words, starts "you're asking") and the assembled `"Got it, " + middle + ", let me check your site records and pull up what was said... "` is ≥141 chars → write it; otherwise write OPENER. Correction turns ("No," / "I meant") → OPENER.
- Tests on virtual time: deadline → OPENER; digit → OPENER; short → OPENER; good → assembled text; flag off → OPENER written synchronously before any await.
- Model `VOICE_RESTATE_MODEL` (default `meta-llama/llama-3.1-8b-instruct`); reject at startup any model id the operator marks reasoning (`VOICE_RESTATE_MODEL` containing `gemini-3`, `-thinking`, `r1`) — log and stay on OPENER.
- Leaves the flag off; turning it on is a separate, measured decision (spec §3).
- Commit "Model restatement, behind a flag that stays off".

---

## Self-review

- Spec coverage: §2 → T3/T4/T8/T9; §3 → T5 (keep-alive), T8 (opener), T11 (restatement); §4 → T1/T2/T6/T7/T8/T9; §5 → T7/T8 deadlines; §6 → T8 logging; §7 → T10. Archival check → T10.
- Type consistency: `verifyVoiceSession` (T4) used in T8; `createTurnStream` (T5) `{text, finish, fail}` used in T8/T11; `toHistory` (T6) returns `{question, history}`; `streamAnswer` (T7) `onText`; `prompt_only` shapes (T2) consumed in T8 step 5–6.
- No placeholders except the explicit "read the real name first" instructions in T1/T2/T9, which name exactly what to read.
