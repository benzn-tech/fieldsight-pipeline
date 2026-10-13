# Voice agent on ElevenLabs — phase 2: identity, retrieval, restatement

2026-10-14 · builds on the owner's "Voice Q&A Agent on ElevenLabs: Integration Spec"
(2026-10-13) and the shell measured on TEST the same night (#1098, #1099, #1101).

## Where phase 1 left it

- `fieldsight-test-voice-llm` (Node, Function URL, response streaming, TEST only)
  answers ElevenLabs' Custom LLM calls with FIXED text. End to end through a real
  ElevenLabs agent (`agent_5701m4tg4bfye04tdqz951395j58`, James, `eleven_v4_turbo`):
  text sent → first audio **2.53–2.68 s** (n=3, NZ home network).
- **ElevenLabs waits 3–4 s for our first token.** `hold 3` played; `hold 4` was
  silent. Past the limit it retries the same turn **3 times ~4 s apart, then gives up
  with no audio and no error event to the client.** This is the governing constraint
  of everything below: the restatement is not a UX nicety, it is what keeps the
  turn alive while retrieval runs.
- ElevenLabs reserves `Authorization` for `custom_llm.api_key`; our endpoint takes the
  shared bearer in `X-Voice-Llm-Key`. It appends `/chat/completions` to the URL.

## Goal of this phase

A spoken question on TEST gets: a restatement within 600 ms of our receiving the
request, then an answer grounded in **the caller's own site records** (owner,
2026-10-13: records now, a standards library later once there is data), produced by
the **same** rewrite, ACL, retrieval and voice prompt the existing Ask uses.

Out of scope: the device client (phase 3), a standards corpus, the screen version of
the answer, prod.

## 1. Identity — a scoped voice-session token

**Problem.** `elevenlabs_extra_body` is set by the *client* at conversation start and
forwarded verbatim (ElevenLabs docs, read 2026-10-13). Anything identifying the user in
it can be forged; trusting a `user_id` there would let anyone read anyone's records.

**Rejected:** passing the Cognito idToken through `extra_body`. It works (the endpoint
can verify it against the pool's JWKS), but it hands ElevenLabs a credential valid for
**our whole API** for up to an hour. A third party logging request bodies would then
hold a working key to every org-api route.

**Chosen: an HMAC-signed token that is good for one thing only.**

- New org-api route `POST /api/org/voice/session` (Cognito-authenticated like every
  org-api route). It returns `{ token, expires_at }` where
  `token = base64url(payload) + "." + base64url(HMAC-SHA256(key, payload))` and
  `payload = { sub, exp, aud: "voice-llm", jti }`, `exp = now + 15 min`.
- The client passes it as `extra_body.voice_session`.
- `voice-llm` verifies signature, `aud`, and `exp` (constant-time compare) and uses
  `sub` as `caller_sub` — exactly the identity the existing Ask trusts from ApiFunction.
  A missing, bad or expired token → the spoken fallback line, never retrieval.
- The signing key is one secret (`VOICE_SESSION_SIGNING_KEY_TEST`, a GitHub secret) passed
  to **both** OrgApiFunction and VoiceLlmFunction, through the same guarded
  `--parameter-overrides` pattern as the bearer.
- Stateless on purpose: the deploy role cannot create a DynamoDB table (template.yaml
  records this), and `voice-llm` runs outside the VPC so it cannot reach Aurora. A token
  replayed within its 15 minutes reaches only that same user's own records, through the
  same ACL — the same exposure as the idToken it was minted from, but scoped to voice.

## 2. Retrieval — Python keeps every rule, Node only streams

**Seam.** `_rag_answer` builds the synthesis prompt exactly once, before it branches
(`prompt = build_rag_prompt(asked if rewritten else question, chunks, mode=…)`).
Everything the voice answer must obey — rewrite of "it/that" from history, the 0.55
distance gate, ACL via `caller_sub`, scope, the voice system context, the language rule
— has run by that line.

**New mode on AskAgentFunction**: `body.prompt_only = true` returns, right after that
line and before any synthesis or web call:

```json
{ "prompt": "...", "asked": "...", "rewritten": true,
  "chunk_ids": ["..."], "basis": {...}, "history_turns": 2 }
```

`voice-llm` invokes AskAgentFunction synchronously with
`{question, history, caller_sub, mode: "voice", prompt_only: true, tz}`, then streams
the answer from OpenRouter with that prompt (`stream: true`), forwarding each delta to
ElevenLabs as a `chat.completion.chunk`.

- `history` comes from ElevenLabs' own `messages` (all prior user/assistant turns),
  mapped to the existing `[{question, answer}]` shape. **The restatement and the
  fallback lines are stripped from assistant turns** before mapping — they are ours,
  not answers.
- The answer model is the one the voice Ask already uses (`ASK_VOICE_MODEL`
  resolution), read from the same env, not hard-coded in Node.
- **Why not stream from Python:** response streaming is native only on Node, and the
  deploy role may not read the Web Adapter layer (measured 2026-10-13). Why not port the
  retrieval to Node: it would fork the ACL, the rewrite and the prompt, and the repo has
  paid for every fork of that kind.

## 3. Restatement — fast, bounded, never a liability

Started **in parallel** with the AskAgent invoke, the moment the request arrives.

- Model: a fast non-reasoning model via OpenRouter (default `google/gemini-2.5-flash-lite`,
  env `VOICE_RESTATE_MODEL`), `max_tokens` 40, `temperature` 0.
- Prompt rules from the owner's spec: one sentence, ≤15 words, starts "Got it,", keeps
  standard/product/quantity words verbatim, never answers, no numbers or clause numbers.
- **Hard deadline 600 ms** from request arrival. Miss → the fixed line
  `"Got it, let me check that... "`. The server always appends `"... "` itself.
- **Leak guard:** a restatement containing a digit, or longer than 20 words, is
  discarded for the fixed line. Cheap, model-free, and it is the only guard that does
  not depend on the model obeying (repo memory: an instruction in a prompt is not
  behaviour).
- Skipped when the user turn starts with "No," / "I meant" (a correction): the fixed
  line is used instead so nothing restates a misunderstanding twice.

## 4. Deadlines — all measured against the 3–4 s cliff

| step | budget | on miss |
|---|---|---|
| first byte to ElevenLabs (restatement) | ≤ 600 ms | fixed line at 600 ms — the cliff is ~3 s away |
| AskAgent `prompt_only` invoke | ≤ 6 s | spoken fallback, stream ends |
| answer model first token | ≤ 4 s after prompt | spoken fallback |
| whole turn | ≤ 25 s | stream ends with what was said |

The 3–4 s cliff only governs the **first** token. Once the restatement is out,
ElevenLabs is speaking it and keeps the turn; later gaps are bounded by our own
deadlines above, which every turn log line records.

Fallback line (owner's spec): `"Sorry, I couldn't reach your site records. Please try again."`
— never a silent stream end.

## 5. Observability

One JSON line per turn (extends the shell's `voice_llm_turn`): `sub` hash (not the sub),
`restatement_source` (`model`|`fixed`|`skipped`), `restatement_ms`, `invoke_ms`,
`first_answer_token_ms`, `answer_chars`, `chunk_ids`, `asked`, `rewritten`,
`history_turns`, `outcome`. Never the token, never the bearer.

**Retry signal:** ElevenLabs' silent retry is invisible to the client. Two requests
with the same last user text and `messages` length within 10 s are logged
`retry_suspected: true` — the only place the 3–4 s cliff can be seen in production.

## 6. Rollout and tests

- TEST only, behind the existing `HasVoiceLlm` condition; prod untouched.
- `prompt_only` is additive: existing callers never send it; a guard test pins that the
  normal path's output is byte-identical with and without the new branch present.
- Node: unit tests on virtual time for the 600 ms deadline and the fallback; token
  verification (good, wrong sig, wrong aud, expired, missing) tested in both Python
  (mint) and Node (verify) against **one shared fixture** — the two halves of a contract
  are tested at the seam, not separately.
- Live on TEST: the phase-1 probe client, now with a minted token, against the owner's
  three phase-1 questions plus a two-turn "it" follow-up; record the per-step timings.

## 7. Open questions

- Is `gemini-2.5-flash-lite` fast enough to land a restatement inside 600 ms from
  ap-southeast-2 via OpenRouter? Measure before committing; the fixed line makes a
  miss harmless but frequent misses make the feature pointless.
- ElevenLabs' secret API returned 500 for this key; the bearer currently sits in agent
  `request_headers` (readable to workspace members). Retry the secret before phase 3.
