# Voice agent on ElevenLabs — phase 2: identity, retrieval, restatement

2026-10-14 · revision 2 · builds on the owner's "Voice Q&A Agent on ElevenLabs:
Integration Spec" (2026-10-13) and the shell measured on TEST (#1098 #1099 #1101
#1105 #1106 #1107). Revision 1 was reviewed (Fable, "Revise first": three blockers);
every finding is answered below or in §8, and every behavioural claim about
ElevenLabs is now a measurement, not an assumption.

## 0. What ElevenLabs actually does (measured on TEST, real agent, 2026-10-14)

All through a real ElevenLabs agent (James, `eleven_v4_turbo`) calling
`fieldsight-test-voice-llm`, timed from the client sending a text turn.

| # | finding | evidence |
|---|---|---|
| E1 | **No content for 3–4 s kills the turn.** `hold 3` spoke; `hold 4` was silent. Headers alone do not count — the shell sends headers before the hold. | hold 2/3/4/5/10/20/30 |
| E2 | **A gap mid-stream kills it too.** Restatement at 0 ms, answer 8 s or 15 s later: the restatement was spoken three times, the answer never. ElevenLabs retries the whole turn ~4.6 s apart, 3 times, then gives up with no audio and no client error. | delay 8, delay 15 |
| E3 | **Only CONTENT keeps a turn alive.** An SSE comment every second (`ka`): still 3 retries. A one-space `delta.content` every second (`sp`): one call, answer spoken after 8 s. | delay 8 ka / sp |
| E4 | **Short text is not spoken until more arrives.** A 27-char restatement, turn kept alive, was not voiced until the answer arrived (first audio ≈ answer time + 1 s). A 141-char restatement was voiced at **1.0–1.1 s** (n=2). `optimize_streaming_latency: 4`, a period ending, a newline, and padding with 130 spaces all changed nothing — the threshold counts real text. | delay 6 sp / l / n / p / w |
| E5 | **`elevenlabs_extra_body` is refused unless the agent allows it.** The conversation fails with "Custom LLM extra body override is not allowed for this AI agent" until `platform_settings.overrides.custom_llm_extra_body = true`; with it, our endpoint receives the keys. | v2 vs v3 agent |
| E6 | `Authorization` is reserved for `custom_llm.api_key`; ElevenLabs appends `/chat/completions` to the URL. | #1101 |
| E7 | Normal turn, restatement then answer 1.5 s later: first audio **2.53–2.68 s** (n=3). | shell, no probe |

Restatement model latency from this machine via OpenRouter, 8 calls each, first
token: `gemini-2.5-flash-lite` median 2.38 s (0/8 under 600 ms);
`gemini-3.8-flash` 1.93 s and emitted only "Got it" (reasoning ate `max_tokens`);
`llama-3.1-8b-instruct` 0.83 s (3/8 under 600 ms) but once invented content ("by a
certain deadline"). NZ home network, a pessimistic proxy — re-measure from Lambda.

## 1. Goal and scope

A spoken question on TEST gets a restatement, then an answer grounded in **the
caller's own site records** (owner 2026-10-13: records now, a standards library once
there is data), produced by the **same** rewrite, scope, retrieval and voice prompt
the existing Ask uses. Out of scope: the device client (phase 3), standards corpus,
the screen version of the answer, prod.

**The owner's "restatement within 1 s" cannot be met by a model-written restatement**
(E4 + model latency). Decision, taken on the owner's standing instruction to choose the
long-term option: restate when we can within a deadline, fall back to a fixed opener
that is heard in ~1 s when we cannot. §3 has the mechanics; the owner can reverse it.

## 2. Identity — a scoped voice-session token

`elevenlabs_extra_body` is set by the client and forwarded verbatim, so anything in it
can be forged. Passing the Cognito idToken would hand ElevenLabs a credential for our
whole API for an hour. Instead:

- New org-api route `POST /api/org/voice/session` (Cognito-authorised, behind the same
  provisioned/archived caller guard as every org-api route). Returns
  `{ token, expires_at }`, `expires_at = now + 5 min` (review #4: shortened from 15).
- `token = P + "." + S` where `P = base64url_nopad(utf8(json))`, json =
  `{"sub": ..., "aud": "voice-llm", "exp": <int epoch seconds>}`, and
  `S = base64url_nopad(HMAC-SHA256(key, ascii(P)))`. **Both sides sign and verify the
  received `P` string as bytes, never a re-serialised object; no padding on either side**
  (Python strips `=`; Node `base64url` emits none). No `jti`: there is no store to check
  it against (review #1).
- Verifier (`voice-llm`): split on the single `.`, recompute `S` over the received `P`,
  `crypto.timingSafeEqual`, then decode `P`, require `aud == "voice-llm"`,
  `exp > now - 30 s` skew allowance. Any failure → the spoken fallback, never retrieval.
- **Both ends fail closed on an empty key**: the mint route returns 503, the verifier
  refuses everything (review #2), mirroring how the shell refuses all callers when its
  bearer is empty. The bearer compare moves to `timingSafeEqual` in the same change.
- Key: GitHub secret `VOICE_SESSION_SIGNING_KEY_TEST` → template parameter
  `VoiceSessionSigningKey` (`NoEcho: true`, `Default: ''`) → env of OrgApiFunction and
  VoiceLlmFunction, through the guarded `--parameter-overrides` pattern. TEST only.
- The ElevenLabs agent must set `platform_settings.overrides.custom_llm_extra_body: true`
  (E5). The client sends `conversation_initiation_client_data.custom_llm_extra_body =
  {voice_session, tz}`. `tz` is honoured as-is: spoofing it only shifts which day the
  caller calls "today", inside an ACL they already passed.
- **Replay and archival** (review #4): a token is readable to anyone who can read the
  agent's traffic for 5 min and reaches only that user's records through the normal ACL.
  Whether `rag-search` / `get_user_by_sub` exclude archived users is **checked in the
  plan, not assumed**; if they do not, the mint route's guard is the only gate and the
  5-minute window is the exposure.

## 3. Restatement and keep-alive — shaped by E1–E4

From the moment a request is authorised, a **keep-alive loop** sends a one-space
`delta.content` every 1 s until the answer's first token (E3). This holds the turn open
through any retrieval time; it is what makes long retrieval survivable at all.

The first real text is the restatement, built to be **voiced immediately** (E4):

- Started in parallel with retrieval. Model `VOICE_RESTATE_MODEL` (default
  `meta-llama/llama-3.1-8b-instruct`, lowest latency measured; re-measured from Lambda in
  the plan), `max_tokens` 40, `temperature` 0, **no reasoning field** — and a guard that
  a reasoning model is rejected at startup (the `gemini-3.8-flash` "Got it" failure).
- The model writes only the middle: "you're asking about …" (≤18 words, owner's rules:
  keep standard/product/quantity words verbatim, never answer, no numbers).
- Server assembles `"Got it, " + middle + ", let me check your site records and pull up what was said... "`.
  The fixed tail guarantees ≥ ~120 characters for a typical question so E4 voices it at once.
- **Leak guard** (model-free, review-endorsed): any digit, more than 25 words, or text
  that does not start in the asked-about form → discarded for the fallback opener.
- **Deadline 1.8 s** from request arrival. Miss, leak or correction ("No," / "I meant")
  → fixed opener, ≥120 characters:
  `"Got it, let me check your site records for that. I'm pulling up what was said and recorded on site, this will only take a moment... "`
  (voiced at ~1.0 s per E4).

Expected heard time: fixed opener ≈1.0 s; model restatement ≈2–3 s. Neither risks E1,
because keep-alive content starts at once.

## 4. Retrieval — Python keeps every rule, Node only streams

New mode on AskAgentFunction: `prompt_only: true`. It runs `_rag_answer` unchanged up to
the single prompt build (`src/lambda_ask_agent.py`, `prompt = build_rag_prompt(...)`) and
returns instead of synthesising. It returns **one of two shapes** (review #6):

- `{"kind": "prompt", "prompt", "asked", "rewritten", "chunk_ids", "basis", "history_turns", "llm": {"model", "reasoning", "max_tokens", "temperature"}}`
- `{"kind": "answer", "answer", ...}` — for the three paths that already return a finished
  answer before the prompt is built: the metric route, a rag-search error, and empty
  retrieval. Node speaks `answer` verbatim. "No records" is therefore said as Ask says it
  today, not as "couldn't reach your records".

`llm` is the **resolved** request the voice path would send (review #7): resolution of
`ASK_VOICE_MODEL` (`none` on TEST) through `llm_utils` → model, `reasoning` (e.g.
`{effort: low}`), `max_tokens` including `REASONING_HEADROOM_TOKENS`, `temperature`.
Node sends exactly that to OpenRouter with `stream: true`. **The luna/mimo fallback chain
is not available on this streamed path**; on a provider error Node speaks the fallback
line. Recorded as a known gap, not hidden.

Corrections to revision 1:

- The 0.55 distance gate does **not** apply in voice mode (it lives in the web-check
  branch, not reached by voice) — struck (review #8).
- The language-leak check + retry cannot run on a stream (review #9). The plan measures
  the leak rate on the resolved voice model over the phase-1 questions; above 1 in 20, a
  streamed-path guard is designed then, not now.
- Voice audit: `_invoke_voice_audit` is in `_voice_answer`, not `_rag_answer`; this path
  writes no audit row in phase 2. Phase 3 (device) decides whether to add one.

**Budget** (review #12): `prompt_only` accepts `deadline_s` (default 8 s) and shrinks the
rewrite budget to fit, so a slow rewrite cannot starve embed + rag-search. Node aborts
its invoke at 9 s and speaks the fallback; the Python run continues to its own Timeout
(billed, logged as `invoke_abandoned`). Keep-alive (§3) makes this budget about patience,
not about E1.

**IAM and wiring** (review #14–#15): VoiceLlmFunction gets `LambdaInvokePolicy` on
AskAgentFunction and env `ASK_AGENT_FUNCTION`, `OPENROUTER_API_KEY` (from the existing
`QwenChatApiKey` parameter), `OPENROUTER_BASE_URL`, `VOICE_RESTATE_MODEL`. Missing key →
the function refuses every request and logs why (fail loud, like the EL keys).
VoiceLlmFunction becomes the second principal AskAgent trusts for `caller_sub`; it
**verifies the token before any invoke**, and there is no code path that invokes without
a verified `sub`.

History (review #10): ElevenLabs `messages` → `[{question, answer}]`. Skip `system`
messages; drop a leading assistant message with no preceding user turn; strip from each
assistant turn everything **through the first `"... "`** (the restatement/opener prefix)
and all keep-alive whitespace; a turn whose answer is then empty is dropped by
`_clean_voice_history` together with its question, which is acceptable and stated.

## 5. Deadlines

| step | budget | on miss |
|---|---|---|
| first content to ElevenLabs | ≤ 50 ms (a keep-alive space) | — |
| restatement assembled | ≤ 1.8 s | fixed opener |
| `prompt_only` invoke | ≤ 9 s (Python told 8 s) | fallback line |
| answer model first token | ≤ 6 s after the prompt | fallback line |
| whole turn | ≤ 30 s | stream ends with what was said |

Fallback line: `"Sorry, I couldn't reach your site records just now. Please try again."`

## 6. Observability

One JSON line per turn: `sub_hash` (salted SHA-256 prefix, never the sub),
`restatement_source` (`model`|`fixed`|`correction`), `restatement_ms`, `invoke_ms`,
`first_answer_token_ms`, `answer_chars`, `chunk_ids`, `rewritten`, `history_turns`,
`outcome`, `retry_suspected`. **No question or answer text** (review #5): the Ask path
logs none, and the shell's `user_text` field is removed in the same change.

`retry_suspected`: same `messages` length and same last-user-text hash within 10 s —
the only place E1/E2 can be seen in production.

## 7. Tests and rollout

- TEST only, behind `HasVoiceLlm`. `prompt_only` is additive; a guard pins that a request
  without it takes the existing path unchanged.
- Token: one fixture minted by the real Python code, verified by the real Node code, both
  directions of tamper (payload byte, signature byte, `aud`, expired, empty key) — the two
  halves tested at the seam.
- Node on virtual time: keep-alive cadence, the 1.8 s restatement deadline, leak guard,
  fallback on invoke/model timeout, `answer`-kind passthrough.
- Live on TEST through the v3-style agent: the three phase-1 questions, a two-turn "it"
  follow-up, a forged token, an expired token, an empty-records question. Each with the
  per-step timings above.

## 8. Review findings → disposition

1 jti dropped · 2 fail-closed + NoEcho + timingSafeEqual · 3 transport proven (E5) ·
4 TTL 5 min, archival checked in plan · 5 no text logged · 6 two return shapes ·
7 resolved `llm` returned · 8 gate struck · 9 leak measured, audit deferred ·
10 history rules · 11 first-content wording (E1) · 12 `deadline_s` · 13 measured (E2/E3)
→ keep-alive · 14 IAM + verify-before-invoke · 15 env, fail loud · 16 abandonment logged.
