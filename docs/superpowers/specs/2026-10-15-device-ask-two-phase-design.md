# Device Ask: restate first, answer second, web when needed, interruptible

Date: 2026-10-15. Status: draft rev 1 (for Fable review).
Repos: fieldsight-pipeline (backend, this file), GrandTime (Android device; pointer commit).

## 1. Why

Owner test on the F2SP, 2026-10-14 23:50 NZ, TEST:

1. **0.7.19 gave no feedback at all between releasing the key and the answer** (~8 s). 0.7.20 adds a
   bundled line ("Got it. Checking your records now."), but the owner's decision for the voice
   agent was an LLM restatement of the question. It confirms the device heard the question
   correctly. They want the same on the device, with a protection mechanism behind it.
2. **"Searching online. Find out the maximum length of the fire sprinklers." answered "the reports
   do not contain..."**
   - Web answer is ON for TEST and prod, but `/api/ask/voice` excludes voice mode from it by
     design (`#827`, 2026-09-11): a push-to-talk worker cannot wait for a search, and the voice
     prompt forbids URLs.
   - With feedback during the wait, that reason no longer holds.
3. **A second long-press while an ask is in flight is ignored** (`AskCore.kt:87`). The worker is
   locked out until the answer finishes, which can be up to 60 s of request plus 120 s of
   playback. The owner wants a new long-press to interrupt the previous ask.

## 2. Constraints (measured, not assumed)

- **The device path is one synchronous request:**
  `POST /api/ask/voice` → ApiFunction (Timeout 28, invoke 26) → AskAgentFunction (Timeout 28,
  `ASK_DEADLINE_SECONDS` 24) → STT, RAG, TTS → JSON with base64 WAV.
  - API Gateway has a fixed 29 s integration timeout.
  - `test_the_ask_timeouts_descend` and `test_no_in_code_hard_stop_reaches_the_runtime_kill` pin
    the ordering.
- **Measured on TEST tonight:** STT 0.85–1.5 s (ElevenLabs), RAG 5.3–5.9 s, TTS 0.9–2.0 s, total
  7.8–8.2 s.
- **Web branch timings** (comments in `web_answer.py`): classify 2.4–3.3 s, draft 3–4 s,
  verify 8.8–11 s, compose ~3 s. `HARD_STOP_SECONDS` is 27.
  - Records + web + TTS cannot fit the 29 s ceiling synchronously.
- **The device client cannot stream:** no SSE, no voice-session token. Phase 3 of the voice agent
  is where tokens reach devices. The Node streaming Function URL is therefore not the device
  route.
- **Prod devices run 0.7.19/0.7.20 against `/api/ask/voice`.** That route must stay byte-for-byte
  unchanged.
- **`lambda_ask_agent.py` is being edited by the search session** (#1115 merged 2026-10-14).
  Changes there are limited to the lines named in §6.

## 3. Shape

Two phases on the existing Cognito-authorized API, plus a cancel. All new routes live on
ApiFunction (`lambda_fieldsight_api.py`).

```
device                      ApiFunction                AskAgent (sync)          AskVoiceWorker (async)
release key ──POST /ask/voice/start {audio,format,tz}──► invoke voice_start ──►  STT
  earcon (local, t≈0)                                                  write turn state "pending"
                                                                       Event-invoke ───────────► RAG (+web) → TTS
                                                                       restate (≤1.8 s) + TTS   write "searching_web" / "done"
            ◄── {turn_id, transcript, restatement audio|null} ◄────────────
  play restatement (or bundled filler if window missed)
  ──GET /ask/voice/result?turn=… (long-poll ≤20 s)──► read S3 state ◄──────────────────────────────┘
            ◄── {status: pending|searching_web|done|error|cancelled, …}
  status searching_web → play bundled "searching online" cue once
  done → play answer
new long-press during any of the above → cancel locally + POST /ask/voice/cancel {turn_id}
```

### 3.1 `POST /api/ask/voice/start`

- **Request body:** identical to `/api/ask/voice`, i.e. `audio` (base64), `format`, optional `tz`,
  optional `history`. Same 413 cap (`MAX_VOICE_AUDIO_B64`). Same 401 when the authorizer gives no
  `sub`.
- ApiFunction invokes AskAgentFunction RequestResponse with
  `{mode:"voice_start", audio, format, caller_sub, [tz], [history]}`.
- **AskAgent `voice_start`:**
  1. Decode and run STT (`_stt`, unchanged). An empty transcript or an STT failure returns the
     same `{error, transcript?}` bodies as `/ask/voice`. No turn is created.
  2. `turn_id = uuid4().hex`.
  3. Write the turn state `{status:"pending", created_at}` to the turn store (§4).
  4. Event-invoke AskVoiceWorker with
     `{turn_id, caller_sub, transcript, [tz], [history], web_intent}` (§5).
     - If the invoke fails, return `{error:"No answer", transcript}`. The device plays its error
       cue.
  5. Restate (§7) and TTS the restatement. Both are bounded by a combined `RESTATE_BUDGET_S`
     (2.5 s).
     - On any failure, guard miss or deadline: `restatement: null`.
  6. Return `{turn_id, transcript, restatement: {text, audioBase64, audioFormat:"wav"} | null}`.
- **Order matters:** step 4 comes before step 5, so the answer starts while the restatement is
  being made.

### 3.2 `GET /api/ask/voice/result?turn=<turn_id>`

- `turn_id` must match `^[0-9a-f]{32}$`, else 400.
- The S3 key is built from the authorizer's `sub` and `turn_id`, never from the client. An unknown
  turn or another user's turn is 404.
- **Long-poll:** read state every 300 ms until status is terminal (`done|error|cancelled`), or
  until it differs from the `since` query param (default `pending`), or until 20 s have passed.
  Then return the state.
- **State bodies:**
  - `pending`: `{status:"pending"}`
  - `searching_web`: `{status:"searching_web"}`
  - `done`: `{status:"done", transcript, answerText, basis, audioBase64, audioFormat:"wav", web:bool}`
  - `error`: `{status:"error", error}`
  - `cancelled`: `{status:"cancelled"}`
- A turn older than 90 s that is still non-terminal is reported as
  `{status:"error", error:"No answer"}`. This covers a worker that died without writing.

### 3.3 `POST /api/ask/voice/cancel {turn_id}`

- Writes the cancel marker (§4) and returns 204. The handler is idempotent. Unknown turns are 204
  too, so turn existence cannot be probed.
- **Best effort, and stated as such:**
  - The worker checks the marker before the web branch, before TTS, and before writing `done`.
  - A worker already past a check finishes and bills. Cancel saves cost; it does not kill.
- A cancelled turn writes **no** `voice_ask_log` row, because the user never heard an answer.

### 3.4 `/api/ask/voice` (old)

Unchanged, code and tests. This is the fallback for old APKs and for a new APK talking to a
backend without the new routes (§9).

## 4. Turn store

- **New bucket `AskTurnBucket`, created by the template.** Auto-named, public access blocked,
  SSE-S3, with a lifecycle rule that expires objects after 1 day.
  - Why not the data bucket: the data bucket is the lake, owned outside this template on prod, and
    a lifecycle PUT replaces its whole configuration.
  - Why not DynamoDB: answer audio is 300–650 KB of WAV and exceeds DynamoDB's 400 KB item limit.
- **Keys:**
  - `turns/<sha256(caller_sub)[:16]>/<turn_id>.json` holds the state.
  - `.../<turn_id>.cancel` is the cancel marker, an empty object.
- **Writers and readers:**
  - AskAgent: put (state).
  - Worker: get/put (state, marker).
  - ApiFunction: get (result) and put (cancel).
  - IAM: `S3CrudPolicy` on that bucket for the worker and ApiFunction, and `S3WritePolicy` for
    AskAgent.
- **Logs never contain transcript or answer text.** This applies to the state body, the logs and
  the error strings. Log lines carry `turn_id`, status, timings and `sub_hash`.

## 5. AskVoiceWorker

- **New function `AskVoiceWorkerFunction`.** Same `CodeUri: src/` and layer as AskAgent; handler
  `ask_voice_turn.worker_handler`.
  - Timeout 60, `ASK_VOICE_WORKER_DEADLINE_S` 50, MemorySize 256.
  - `MaximumRetryAttempts: 0` (EventInvokeConfig), because a retried turn would answer twice.
  - It is not behind API Gateway, so the 29 s ordering tests do not apply. It gets its own
    ordering test: deadline < Timeout, and every in-code hard stop < deadline.
  - Env: the same model, key, TTS and STT variables AskAgent has (copied from the same parameters
    and expressions), plus `ASK_TURN_BUCKET` and `VOICE_WEB_ENABLED`.
- **Steps:**
  1. **Cancel check.**
  2. **Build the RAG body:**
     `{question, caller_sub, mode:"voice", k:5, tz, [history], voice_web: VOICE_WEB_ENABLED, web_intent}`.
     - When `web_intent` is set, `question` has the intent phrase stripped (§8).
  3. **Run `_rag_answer`.**
     - When the web branch is about to run, it calls back `on_web_start()`. The worker uses that
       to write `searching_web` (cancel check first), **before** the web call begins, so the
       device cue is timely.
  4. **Cancel check, then `_tts(answer)`.**
  5. **Cancel check, then write `done`.** Fire `_invoke_voice_audit` exactly as `_voice_answer`
     does today.
  6. **On any exception or deadline:** write `{status:"error", error:"No answer"}`.
- **The `voice ask:` log line** keeps its shape and gains `turn=<id> web=<bool>`.
  `test_the_voice_path_says_where_the_seconds_go` keeps pinning it.

## 6. Edits inside `lambda_ask_agent.py` (blast radius)

1. **Handler dispatch:** add `voice_start` → `ask_voice_turn.start(body)`. All other logic lives in
   the new module `src/ask_voice_turn.py`.
2. **The two web gates that exclude voice:**
   - ~1322, the no-chunks general flow.
   - ~1427, the main classify/web branch.
   - Each changes from `body.get("mode") != "voice"` to
     `(body.get("mode") != "voice" or body.get("voice_web"))`.
   - The voice-model selection at ~1403 is not a web gate and stays untouched.
3. **`_rag_answer` reads two more optional body keys:**
   - `on_web_start`: a callable, never present on HTTP bodies; the worker passes it in-process.
   - `web_intent`: a bool. When true, it bypasses the distance-gate `_skip` and classify, and
     routes as `general` when no chunk passes `_DISTANCE_GATE`, else `mixed`.
4. **The voice rendering flag goes into `web_answer.general_answer` and `compose` as `voice=True`**
   (§8). `_rag_answer` passes `voice = body.get("mode") == "voice"`.

Nothing else in this file changes. The plan names the exact lines against `origin/develop` at
execution time.

## 7. Restatement (Python port of `voice_llm/restate.mjs`)

- **New `src/voice_restate.py`.**
  - Same system prompt, model (`VOICE_RESTATE_MODEL`, default `meta-llama/llama-3.1-8b-instruct`),
    `max_tokens` 40, temperature 0.
  - Same vendor key and base URL resolution as Ask's chat calls.
  - Same `isCorrection` and `modelRejected` rules.
  - Current question only, because the device sends no history today.
- **Guards** are the same model-free set: prefix `you're asking`, ≤25 words, no ellipsis or
  newline, every number and every capitalised word present in the question.
- **Shared fixture `tests/fixtures/restate_guard_cases.json`.** The cases are
  `{question, middle, ok, reason}`. Both `voice_llm/restate.test.mjs` and the new Python test load
  it. One rule, one fixture, two implementations that cannot drift silently.
- **Device assembly differs from the EL agent's,** because there is no 141-char floor on the
  device (that floor is an EL agent TTS hold-back):
  - `Got it, <middle>. Checking your site records now.`
  - With `web_intent`: `Got it, <middle>. Searching online now.`
- **Gated by `ASK_VOICE_RESTATE` (default `'false'`; TEST `'true'`).**
  - When off, `restatement` is always null.
  - The device then plays the bundled filler, which is today's behaviour.

## 8. Web answers in voice mode

- **Flag `VOICE_WEB_ENABLED`:** template parameter `VoiceWebEnabled`, default `'false'`. TEST
  flips it only after the measurement below. Prod: never passed in this work.
- **Explicit intent** is a model-free regex on the transcript, case-insensitive, anchored to
  phrases: `search (it )?online`, `search the (web|internet)`, `look (it )?up online`,
  `google (it|that)`, `on the internet`, `check online`.
  - The matched phrase and its trailing punctuation are stripped from the question used for
    retrieval and answering.
  - The unstripped transcript is what the device shows and logs.
- **Otherwise** the existing records-first routing (classify plus distance gate) now runs in voice
  mode too.
- **Voice rendering:**
  - `general_answer(..., voice=True)` and `compose(..., voice=True)` append a voice instruction to
    their prompts:
    - at most two short spoken sentences
    - name the standard or organisation, e.g. "according to NZS 4541"
    - no URLs, symbols or lists
    - when the status is `unverified`, say it could not be confirmed online
  - **A model-free guard follows the prompt.** Reject text containing `http`, `www.`, `](`,
    bullets, or more than 60 words.
    - On a reject with a records answer, speak the records answer.
    - On a reject without one, speak "I couldn't get a clear answer online for that."
- **Measure before flipping on TEST:** 20 web-intent and 10 records-miss questions through the
  worker.
  - Pass bar: 0 URL or markdown leaks after the guard; p90 worker time ≤ 35 s; no invented
    standard numbers (checked by hand against the cited source).

## 9. Device (GrandTime)

The device work runs in a separate repo and a separate PR. Version 0.7.21 (versionCode 47), dev
flavour first.

- **Client (`AskApiClient`):** adds `start(clip)`, `result(turnId, since)` and `cancel(turnId)`.
  - Every call goes through a cancellable OkHttp `Call` held by the ask job.
  - `start` returning 404 or 403 means the backend has no new routes. The client calls the old
    `ask(clip)` for that turn and remembers the outcome for the process lifetime.
- **Cues, in order after release:**
  1. **Immediate earcon** (`ask_thinking.wav`, already bundled), replacing the 0.5 s silence.
  2. **Restatement window `RESTATE_WINDOW_MS`** (initial 3000, a number to measure on TEST):
     - If start returns with a restatement inside the window, play it and never play the bundled
       filler.
     - If the window expires first, play `ask_searching.mp3` and discard the late restatement.
     - If start returns with `restatement:null`, play the bundled filler immediately.
  3. **On the first `searching_web` status**, play the new bundled `ask_searching_online.mp3`
     once, after any line already playing finishes.
     - Text: "Searching online now, this can take a little longer."
     - Same ElevenLabs voice (`QlZn2MPaf2jDn0461bnj`) and model `eleven_v4_turbo`, speed 1.20,
       generated once and committed.
  4. **On `done`**, play the answer after the current cue finishes. Never cut the restatement off
     mid-sentence.
- **Polling:** loop `result` until terminal, with an overall cap of 60 s from release.
  - On hitting the cap: error cue and a best-effort cancel.
- **Interrupt.** A long-press (the `HoldToTalkGate` DOWN after 1 s) or a discrete Ask key while
  `Thinking` or `Playing` performs the cancel sequence:
  1. Bump the ask generation.
  2. Cancel the ask job and its in-flight `Call`.
  3. Stop the filler, cue and answer playback.
  4. Fire-and-forget `cancel(turnId)` if a turn id is known.
  5. Release a borrowed mic exactly once, using the `fail()` order: `recorder.discard()` then
     `micHandover.end()`.
  6. Proceed exactly like `onPttDown` from Idle: listening cue, record.
- **Stale answers:** every continuation checks its generation before acting. An answer, cue or
  error from an older generation is dropped. This includes `onAnswer`, which today is guarded only
  by `state == Thinking`.
- **Tests:**
  - FSM tests for interrupt in Thinking and in Playing, and for stale-answer drop.
  - Window tests: restatement inside the window, late restatement, null restatement.
  - Fallback test: start 404 goes to old ask.
  - Read `AskCueBeforeMicTest` first, because it reads source text.

## 10. Release order and compatibility

1. **Backend PR to `develop`, then TEST deploy.**
   - Flags on TEST: `ASK_VOICE_RESTATE=true`, `VOICE_WEB_ENABLED=false`.
   - Verify `start`, `result` and `cancel` live with a TEST user's token.
2. **Measure web (§8),** then flip `VOICE_WEB_ENABLED=true` on TEST with a one-line PR.
3. **GrandTime 0.7.21 dev,** released to the test stage with `tools/release_apk.py --stage test`.
4. **Prod:** nothing in this work.
   - A prod APK built later is safe against the current prod backend, because of the 404
     fallback.
   - The backend reaches prod only on the owner's main release. `VoiceWebEnabled` and the
     restatement flag stay unset there, so prod behaviour is unchanged even after the release.
   - `AskTurnBucket` will be created on prod by that release. It is empty and harmless.

## 11. Fail paths

| Where | What the user hears |
|---|---|
| start: STT failed or empty | error cue (as today) |
| start: worker invoke failed | error cue |
| start: restate or TTS failed/late | bundled filler; answer unaffected |
| result: worker crashed or timed out | `error` → error cue |
| device cap of 60 s reached | error cue, best-effort cancel |
| web branch failed or guard rejected | records answer, or "couldn't get a clear answer online" |
| user interrupts | silence, then the listening cue for the new question |
| backend without new routes | old single-request path, bundled filler |

## 12. Out of scope

- Server-side history for device asks (the device sends none).
- Follow-up anchoring (a separate item).
- Streaming answer audio.
- Prod flags.
- The ElevenLabs voice agent (unchanged).
