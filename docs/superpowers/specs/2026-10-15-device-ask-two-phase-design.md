# Device Ask: restate first, answer second, web when needed, interruptible

Date: 2026-10-15. Status: rev 2. Fable reviewed rev 1 and returned APPROVE-WITH-CHANGES; every
finding is folded in below, and §13 records each ruling.

Repos: fieldsight-pipeline (backend; this file) and GrandTime (Android device; pointer commit).

## 1. Why

The owner tested Ask on the F2SP against TEST on 2026-10-14 at 23:50 NZ.

1. **0.7.19 gave no feedback between releasing the key and the answer, about 8 s.**
   0.7.20 adds a bundled line: "Got it. Checking your records now." The owner's decision is an
   LLM restatement of the question, which also confirms the device heard the question correctly,
   with a protection mechanism behind it.
2. **"Searching online. Find out the maximum length of the fire sprinklers." got the reply "the
   reports do not contain...".**
   - Web answer is ON for TEST and prod.
   - `/api/ask/voice` excludes voice mode from web answer by design (#827, 2026-09-11). The
     reasons were that a push-to-talk worker cannot wait for a search, and that the voice prompt
     forbids URLs.
   - Once the device gives feedback during the wait, that reason no longer holds.
3. **A second long-press while an ask is in flight is ignored** (`AskCore.kt:87`).
   - The user is locked out for up to 60 s of request plus 120 s of playback.
   - The owner wants a new long-press to interrupt the previous ask.

## 2. Constraints

All of these are measured, not assumed.

- **The device path is one synchronous request.**
  - Flow: `POST /api/ask/voice` → ApiFunction (Timeout 28, invoke 26) → AskAgentFunction
    (Timeout 28, `ASK_DEADLINE_SECONDS` 24) → STT, RAG, TTS → JSON with base64 WAV.
  - API Gateway has a fixed 29 s integration timeout.
  - The ordering is pinned by `test_the_ask_timeouts_descend`,
    `test_no_in_code_hard_stop_reaches_the_runtime_kill` and
    `test_template_timeout_invariants.py`.
- **Timings measured on TEST tonight:**

  | Step | Time |
  |---|---|
  | STT (ElevenLabs) | 0.85–1.5 s |
  | RAG | 5.3–5.9 s |
  | TTS | 0.9–2.0 s |
  | Total | 7.8–8.2 s |

- **The web chain** (from comments in `web_answer.py`) runs classify 2.4–3.3 s, draft 3–4 s,
  verify 8.8–11 s and compose ~3 s.
  - `HARD_STOP_SECONDS` is 27.
  - Records plus web plus TTS therefore cannot fit synchronously under 29 s.
- **The device client cannot stream.** It has no SSE support and no voice-session token. The Node
  streaming Function URL is not the device route; tokens reach devices in phase 3 of the voice
  agent.
- **Prod devices run 0.7.19 or 0.7.20 against `/api/ask/voice`.** That route stays byte-for-byte
  unchanged.
- **Another session (search) is editing `lambda_ask_agent.py`.** Edits there are limited to the
  list in §6.
- **The deploy role cannot delete or encrypt buckets.**
  - Checked with `simulate-principal-policy` on `github-actions-fieldsight-deploy`:
    `s3:DeleteBucket` and `s3:PutEncryptionConfiguration` are implicitly denied.
  - A template-created bucket would turn any later rollback into UPDATE_ROLLBACK_FAILED, so the
    template creates no bucket (§4).

## 3. Shape

Two phases run on the existing Cognito-authorized API, plus a cancel route. All three new routes
live on ApiFunction (`lambda_fieldsight_api.py`) and are added to `KNOWN_ROUTES`.

```
device                      ApiFunction                AskAgent (sync)          AskVoiceWorker (async)
release key ──POST /ask/voice/start {audio,format,tz}──► invoke voice_start ──►  STT
  earcon (local, t≈0)                                                  put state "pending"
                                                                       Event-invoke ───────────► RAG (+web) → TTS
                                                                       restate + TTS (≤3.0 s)   put "searching_web" / "done"
            ◄── {turn_id, transcript, web_intent, restatement|null} ◄──
  play restatement (or the bundled filler if the window was missed)
  ──GET /ask/voice/result?turn=…&since=… (long-poll ≤20 s)──► read state ◄─────────────────────────┘
  searching_web (and not web_intent) → bundled "searching online" cue once
  done → play the answer after the current cue
new long-press during any of this → cancel locally + POST /ask/voice/cancel {turn_id}
```

### 3.1 `POST /api/ask/voice/start`

- **Request body.** Identical to `/api/ask/voice`: `audio` (base64), `format`, optional `tz`,
  optional `history`. It has the same 413 cap (`MAX_VOICE_AUDIO_B64`), and returns the same 401
  when the authorizer supplies no `sub`.
- **ApiFunction** invokes AskAgentFunction RequestResponse with
  `{mode:"voice_start", audio, format, caller_sub, [tz], [history]}`.
- **AskAgent `voice_start`** is implemented in the new `src/ask_voice_turn.py`:
  1. **Decode and run STT** with `_stt`, unchanged. On an empty transcript or an STT failure it
     returns the same `{error, transcript?}` bodies `/ask/voice` returns, and no turn is created.
  2. **Mint the turn id:** `turn_id = secrets.token_hex(16)`. `web_intent` = the §8 regex applied
     to the transcript.
  3. **Housekeeping.** Delete this caller's turn objects older than 1 day (§4).
  4. **Put the turn state** `{status:"pending", created_at}`.
  5. **Event-invoke AskVoiceWorker** with
     `{turn_id, caller_sub, transcript, web_intent, [tz], [history]}`.
     - If the invoke fails, write state `error` and return `{error:"No answer", transcript}`.
  6. **Restate (§7) and TTS the restatement** within `RESTATE_BUDGET_S` = 3.0. The restate
     timeout is 1.2 s and the TTS timeout 1.8 s, each enforced by a `ThreadPoolExecutor` future
     `result(timeout=)`. Any failure, guard miss or timeout gives `restatement: null`.
  7. **Return**
     `{turn_id, transcript, web_intent, restatement: {text, audioBase64, audioFormat:"wav"} | null}`.
- **Order.** Step 5 comes before step 6, so the answer is already being worked on while the
  restatement is made.
- **Log line.** `voice start: stt= restate= restate_tts= start_total= restated=<bool>
  reason=<guard reason|null> web_intent=<bool> turn=<id> sub_hash=<…>`. It carries no question or
  answer text.

### 3.2 `GET /api/ask/voice/result?turn=<turn_id>&since=<status>`

- **Parameters.**
  - ApiFunction gains `queryStringParameters` parsing for this route only.
  - `turn` must match `^[0-9a-f]{32}$`, otherwise the route returns 400.
  - `since` must be one of `pending` or `searching_web`, and defaults to `pending`.
- **Key and ownership.** The key is built from the authorizer's `sub` and `turn`, never from the
  client. An unknown turn or another user's turn returns 404.
- **Long-poll.** The handler reads every 400 ms. It returns as soon as either:
  - the status is terminal (`done`, `error` or `cancelled`), or
  - the status differs from `since`.

  It returns after 20 s at the latest. That is below ApiFunction's 28 s Timeout and API Gateway's
  29 s ceiling. Cost: one ApiFunction slot for up to 20 s and up to 50 S3 GETs per poll. That is
  acceptable at 1000 concurrency, and a client that gives up still leaves its poll running to the
  20 s limit.
- **Precedence.** An existing `.cancel` marker wins over any state, so a late `done` after a
  cancel is reported as `cancelled`.
- **Bodies.**
  - `{status:"pending"}`
  - `{status:"searching_web"}`
  - `{status:"done", transcript, answerText, basis, audioBase64, audioFormat:"wav", web:bool}`
  - `{status:"error", error}`
  - `{status:"cancelled"}`
- **Stale turns.** A non-terminal turn older than 90 s is reported as
  `{status:"error", error:"No answer"}`. This covers a worker that died before writing.
- **Cleanup after a terminal read.** After returning a terminal status, the handler deletes the
  state object and any marker.

### 3.3 `POST /api/ask/voice/cancel {turn_id}`

- **Validation.** `turn_id` uses the same regex as `result`; anything else returns 400.
- **Effect.** Writes the cancel marker and returns 204.
  - The route is idempotent.
  - An unknown turn also returns 204, so turn existence cannot be probed.
- **Best effort, and documented as such.**
  - The worker checks the marker before the web branch, before TTS and before writing `done`.
  - A worker already past its last check finishes and bills. Cancel saves cost; it does not kill.
- **Audit.** A cancelled turn writes no `voice_ask_log` row, because the user never heard an
  answer.

### 3.4 `/api/ask/voice` (old)

The old route keeps its code and its tests unchanged. It is the fallback for old APKs, and for a
new APK whose backend lacks the new routes (§9).

## 4. Turn store

The store is a prefix in the existing data bucket (`DataBucketName`), not a new bucket and not
DynamoDB.

**Why this store:**
- A new bucket cannot be rolled back by the deploy role (§2).
- DynamoDB is ruled out twice over: the deploy role has no `dynamodb:CreateTable`, and 300–650 KB
  of answer audio exceeds the 400 KB item limit.
- A lifecycle rule on the lake would replace its whole lifecycle configuration, so no lifecycle
  rule is used.

**Keys:**
- State: `ask_turns/<sha256(caller_sub)[:16]>/<turn_id>.json`
- Cancel marker: `.../<turn_id>.cancel`, which is empty.

**No event triggers.** `ask_turns/` overlaps none of the bucket's event-notification prefixes.
These were read for both the TEST and prod buckets on 2026-10-15; the list starts with `users/`,
`audio_segments/`, `reports/`, `transcripts/` and `extraction_requests/`. A unit test pins the
prefix so that a later notification on `ask_` would be noticed.

**Cleanup without a lifecycle rule:**
- `result` deletes a turn after serving its terminal state.
- `voice_start` deletes this caller's objects under their prefix that are older than 1 day. That
  is one `ListObjectsV2` call per start, on a prefix holding only that user's few turns.
- Abandoned turns therefore live at most until the user's next ask after a day.

**IAM.** Inline statements, scoped to the prefix:

| Principal | Actions |
|---|---|
| AskAgent | `s3:PutObject`, `s3:DeleteObject` on `arn:aws:s3:::${DataBucketName}/ask_turns/*`; `s3:ListBucket` with condition `s3:prefix` = `ask_turns/*` |
| Worker | `s3:GetObject`, `s3:PutObject` on `ask_turns/*` |
| ApiFunction | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `ask_turns/*` |

- ApiFunction's comment "the only grant" is updated to match.
- The plan includes a gated step that runs `simulate-principal-policy` against the deployed roles
  with real ARNs.

**Text handling.**
- The state object carries the transcript and answer text, because the device needs them. That
  is storage, not logging.
- Log lines never carry that text; they carry only `turn_id`, status, timings and `sub_hash`.
- A test asserts that the new log lines contain neither text.

## 5. AskVoiceWorker

**Definition.** `AskVoiceWorkerFunction`:
- `CodeUri: src/`, `DashScopeLayer`, handler `ask_voice_turn.worker_handler`, MemorySize 256,
  Timeout 60.
- `EventInvokeConfig: MaximumRetryAttempts: 0`.

**Environment.** It uses the same expressions AskAgent uses for:
- `QWEN_*`, `LLM_*`
- `ASK_VOICE_MODEL`, `ASK_DISTANCE_GATE`, `RAG_SEARCH_FUNCTION`
- `CORROBORATION_API_KEY`, `CORROBORATION_CHEAP_MODEL`
- `ENABLE_WEB_ANSWER`, `ENABLE_RERANK`, `RERANK_CANDIDATE_K`
- every DashScope, ElevenLabs and TTS/STT variable
- `VOICE_AUDIT_FUNCTION`

It adds `ASK_TURN_BUCKET` (= `DataBucketName`) and `VOICE_WEB_ENABLED`.

**Budgets.** The worker has its own budget values, so the longer Timeout is actually usable:

| Variable | Value |
|---|---|
| `ASK_DEADLINE_SECONDS` | 45 |
| `WEB_ANSWER_HARD_STOP` | 45 |
| `LLM_HTTP_TIMEOUT` | 40 |

- TTS keeps its fixed 10 s.
- A new ordering test reads these values from the worker's own env block. It asserts that every
  in-code hard stop plus the 10 s TTS stays below Timeout 60.

**Policies:**
- `LambdaInvokePolicy` on RagSearchFunction and on VoiceAuditFunction.
- The S3 statements in §4.

**Other wiring:**
- AskAgent gains `LambdaInvokePolicy` on the worker and the env var `ASK_VOICE_WORKER_FUNCTION`.
- `("AskVoiceWorkerFunction","RagSearchFunction")` is added to `SYNC_CALLEES` in
  `test_template_timeout_invariants.py`. RagSearch's 23 s is below 60.
- RagSearchFunction is `Condition: HasDb`. The worker references it the same way AskAgent does,
  and the same W1001 note applies.

**Steps:**
0. **Duplicate guard.** Read the state and exit unless it is `pending`. Lambda async invocation
   is at-least-once even with zero retries.
1. **Cancel check.**
2. **Build the RAG body.**
   `{question, caller_sub, mode:"voice", k:5, tz, [history], voice_web: VOICE_WEB_ENABLED, web_intent}`.
   When `web_intent` is set, `question` has the intent phrase stripped (§8).
3. **Run `_rag_answer(body, on_web_start=cb)`.** The callback runs a cancel check and then puts
   `searching_web` before the web call starts.
4. **Cancel check, then `_tts(answer)`.**
5. **Cancel check, then put `done`.** Then call `_invoke_voice_audit(caller_sub, transcript,
   answer)` exactly as `_voice_answer` does.
6. **Failure handling.** On any exception or deadline, put
   `{status:"error", error:"No answer"}`.

**Log line.** `voice ask:` keeps its fields and adds `turn=<id> web=<bool>`.
`test_the_voice_path_says_where_the_seconds_go` keeps pinning `_voice_answer`'s line, and a
sibling test pins the worker's line with the same no-text assertion.

## 6. Edits inside `lambda_ask_agent.py` (blast radius)

Line numbers are against `origin/develop` 49135d6a. The plan re-reads them at execution time.

1. **Handler dispatch.** Route `mode == "voice_start"` to `ask_voice_turn.start(body)`. This match
   must come **before** the `body.get('audio')` branch (~2113), or the old voice path swallows it.
2. **Signature.** `_rag_answer(body)` becomes `_rag_answer(body, on_web_start=None)`. The callable
   is a keyword argument, never a body key, because an HTTP body cannot carry one.
3. **The two web gates** (~1322 no-chunks general flow, ~1427 main classify/web branch).
   - `body.get("mode") != "voice"` becomes
     `(body.get("mode") != "voice" or body.get("voice_web"))`.
   - `not scoped` becomes `(not scoped or body.get("web_intent"))`. An explicit request to search
     online overrides the "yesterday"/tz scoping, which otherwise silently blocks it.
   - The voice-model selection at ~1403 is not a web gate and is left untouched.
4. **`web_intent` routing.**
   - In the main branch, it bypasses the distance-gate `_skip` and classify. The route is
     `general` when no chunk passes `_DISTANCE_GATE`, else `mixed`.
   - In the no-chunks branch (~1330), it skips `_classify_for_route` and routes `general`.
5. **`on_web_start()`** is called immediately before `_general_run` at ~1332, and immediately
   before the pool submit at ~1527. It is not called at classify.
6. **`voice` threading.**
   - `voice = body.get("mode") == "voice"` is passed through `_general_run` (~739-747, both
     calls) to `web_answer.general_answer(..., voice=voice)`, and to
     `web_answer.compose(..., voice=voice)` at ~1596.
   - `PROJECT_FALLBACK_LEAD` (~1627) is screen text and is skipped when `voice` is true.

Nothing else in the file changes.

## 7. Restatement

`src/voice_restate.py` is a Python port of `voice_llm/restate.mjs`.

**The call.**
- **One raw HTTPS POST** to `${QWEN_BASE_URL}/chat/completions` with Ask's chat key. It does
  **not** go through `llm_utils.call_llm`, whose fallback ladder alone exceeds the budget.
- **Same parameters as the Node version:** the same system prompt, model `VOICE_RESTATE_MODEL`
  (default `meta-llama/llama-3.1-8b-instruct`), `max_tokens` 40 and temperature 0.
- **Same rules:** `isCorrection` and `modelRejected`.
- **Input:** the current question only.

**Guards.** The guards are the same model-free set:
- the prefix is `you're asking`
- at most 25 words
- no ellipsis or newline
- every number in the restatement appears in the question
- every capitalised word appears in the question

The capitalisation check uses `str[0].isupper()`, Python's equivalent of `\p{Lu}`.

**Shared fixture.** `tests/fixtures/restate_guard_cases.json` holds `{question, middle, ok, reason}`
cases and includes at least one non-ASCII capitalised case.
- `voice_llm/restate.test.mjs` and the new Python test both load it.
- One rule, one fixture, two implementations: drift between them turns a test red.

**Device assembly.** The device has no 141-char floor; that floor is the EL agent's TTS hold-back.
- Default: `Got it, <middle>. Checking your site records now.`
- With `web_intent`: `Got it, <middle>. Searching online now.`

**Flag.** Template Parameter `AskVoiceRestate`, default `'false'`.
- TEST passes `vars.TEST_ASK_VOICE_RESTATE || 'false'`, and the variable is set to `true`.
- Prod is listed in `UNWIRED_BY_DESIGN` with the reason "device restatement not released to
  prod".
- When the flag is off, `restatement` is always null and the device plays the bundled filler,
  which is today's behaviour.

## 8. Web answers in voice mode

**Flag.** Template Parameter `VoiceWebEnabled`, default `'false'`.
- TEST passes `vars.TEST_VOICE_WEB_ENABLED || 'false'`. Flipping it means setting the variable and
  re-running the TEST deploy.
- Prod is in `UNWIRED_BY_DESIGN` with the reason "voice web not released to prod".

**Explicit intent.** A model-free regex on the transcript, case-insensitive, matching only these
phrases:
- `search (it )?online`
- `search the (web|internet)`
- `look (it |that )?up online`
- `google (it|that)`
- `on the internet`
- `check online`

The matched phrase and its trailing punctuation are stripped from the question used for retrieval
and answering. The device shows and logs the unstripped transcript.

**Otherwise,** the existing records-first routing (classify plus the distance gate) now runs in
voice mode too.

**Voice rendering.** `general_answer(question, budget, *, kind, clock, voice=False)` and
`compose(records_answer_text, general_block, question, budget, voice=False)` append a voice
instruction to their prompts when `voice` is true. The instruction asks for:
- at most two short spoken sentences
- the standard or organisation named, e.g. "according to NZS 4541"
- no URLs, symbols or lists
- when the status is `unverified`, a statement that it could not be confirmed online

**Model-free guard after the prompt.** Reject text containing `http`, `www.`, `](`, a line
starting with a bullet, or more than 60 words. On reject:
- with a records answer: speak that.
- without one: speak "I couldn't get a clear answer online for that."

**Measure before flipping on TEST.** Run 20 web-intent and 10 records-miss questions through the
worker. The pass bar:
- 0 URL or markdown leaks after the guard
- p90 worker time ≤ 35 s
- no invented standard numbers, checked by hand against the cited source

## 9. Device (GrandTime)

This is a separate repo and a separate PR. Version 0.7.21 (versionCode 47), dev flavour first.

### Client (`AskApiClient`)

- Add `start(clip)`, `result(turnId, since)` and `cancel(turnId)`.
- Every call runs through an OkHttp `Call` that the ask job holds, so it can be cancelled.
- A `start` response of 404 or 403 (with no JSON `error` body) means the backend has no new
  routes.
  - The client uses the old `ask(clip)` for that turn.
  - It remembers the outcome for 10 minutes, not for the whole process, so a deploy-window 404 is
    not pinned.

### Cues after release, in order

1. **Earcon.** Play `ask_thinking.wav` (already bundled) immediately, replacing the 0.5 s of
   silence.
2. **Restatement window.** `RESTATE_WINDOW_MS` starts at 4500. It is to be measured on TEST from
   `start_total`, and the plan has a tuning step.
   - Start returns inside the window with a restatement: play it, and never play the bundled
     filler.
   - The window expires first: play `ask_searching.mp3` and discard the late restatement.
   - Start returns with `restatement:null`: play the bundled filler immediately.
3. **Searching-online cue.** On the first `searching_web` status, and only when start's
   `web_intent` was false (the restatement already said it), play the new bundled
   `ask_searching_online.mp3` once, after any line already playing.
   - Text: "Searching online now, this can take a little longer."
   - Voice `QlZn2MPaf2jDn0461bnj`, `eleven_v4_turbo`, speed 1.20. Generate once and commit.
4. **Answer.** On `done`, play the answer after the current cue finishes. Never cut the
   restatement off mid-sentence.

### Polling

Loop `result` with `since` set to the last status seen, until a terminal status arrives. The
overall cap is 60 s from release; when it is reached, play the error cue and send a best-effort
cancel.

### Interrupt

An interrupt is a `HoldToTalkGate` DOWN (after the 1 s hold), or a discrete Ask key, arriving
while the state is `Thinking` or `Playing`. It runs:

1. Bump the ask generation.
2. Cancel the ask job and its in-flight `Call`.
3. Stop the filler, the cues and answer playback.
4. Fire-and-forget `cancel(turnId)` when a turn id is known.
5. **Keep a borrowed mic borrowed.** AskCore carries `borrowedMic` into the new Listening state
   and skips the Release/Acquire pair. On this single-capture ROM, SegmentRecorder reopens
   asynchronously, so an `end()` followed immediately by `begin()` can leave the new
   `StartRecording` facing a held mic. That is the same trap `fail()` documents at
   `AskManager.kt:199-209`.
6. Continue exactly as `onPttDown` from Idle: listening cue, then record.

### Stale answers

Every continuation checks its generation before it acts, and anything from an older generation is
dropped: answers, cues and errors. This includes `onAnswer`, which today is guarded only by
`state == Thinking`.

### Tests

- FSM: interrupt in Thinking, interrupt in Playing, interrupt with a borrowed mic (no
  Release/Acquire), and dropping a stale answer.
- Window: restatement inside the window, a late restatement, a null restatement, and `web_intent`
  suppressing the online cue.
- Fallback: a 404 from `start` falls back to the old ask, and the fallback expires after its TTL.
- Read `AskCueBeforeMicTest` before refactoring; it reads source text.

## 10. Release order and compatibility

1. **Backend.** PR to `develop`, then the TEST deploy.
   - Set TEST variables: `TEST_ASK_VOICE_RESTATE=true`, `TEST_VOICE_WEB_ENABLED=false`.
   - Verify `start`, `result` and `cancel` live with a TEST user's token.
   - Run the IAM simulate step.
2. **Web measurement (§8).** Measure, then set `TEST_VOICE_WEB_ENABLED=true` and re-run the
   deploy.
3. **Device.** Release GrandTime 0.7.21 dev with `tools/release_apk.py --stage test`.
4. **Prod: nothing in this work.**
   - The 404 fallback makes a later prod APK safe against the current prod backend.
   - The backend reaches prod only with the owner's main release. That release creates no
     buckets or tables, and it adds:
     - the worker function
     - IAM scoped to the `ask_turns/` prefix
     - both flags left unset
   - Prod device behaviour therefore stays unchanged.

## 11. Fail paths

| Where | What the user hears |
|---|---|
| start: STT failed or empty | error cue (as today) |
| start: worker invoke failed | error cue |
| start: restate or TTS failed, late or guarded out | bundled filler; answer unaffected |
| result: worker crashed, timed out or stale | `error` → error cue |
| device cap 60 s | error cue, best-effort cancel |
| web branch failed or guard rejected | records answer, or "couldn't get a clear answer online" |
| user interrupts | silence, then the listening cue for the new question |
| backend without the new routes | old single-request path with the bundled filler |

## 12. Out of scope

- Server-side history for device asks.
- Follow-up anchoring (a separate item).
- Streaming answer audio.
- Prod flags.
- The ElevenLabs voice agent.

## 13. Review rulings (Fable, rev 1 → rev 2)

| # | Finding | Ruling |
|---|---|---|
| 1 | A new bucket can't be rolled back by the deploy role; `BucketEncryption` denied | Use the data-bucket prefix `ask_turns/` with delete-after-read and per-user day-old sweep. No owner IAM change needed tonight. |
| 2 | Worker IAM/env gaps | Full list in §5 |
| 3 | Unwired boolean toggles fail CI | Both flags are TEST repo variables and prod is `UNWIRED_BY_DESIGN` |
| 4 | Worker inherits 21/24/27 s budgets | Worker overrides 45/45/40, with its own ordering test |
| 5 | §6 edit list incomplete; `scoped` blocks intent; dispatch order | §6 items 1, 3–6 |
| 6 | Callable via body | Keyword argument |
| 7 | Restate via call_llm too slow; TTS has no timeout | Raw POST; futures with timeouts |
| 8 | A 3 s window misses half | 4500 ms, measured via `start_total` |
| 9 | Double "searching online" on explicit intent | `web_intent` returned; cue suppressed |
| 10 | At-least-once async; late done after cancel | Step 0 guard; cancel marker wins |
| 11 | Mic end/begin race on interrupt | Keep `borrowedMic` |
| 12 | Nits | Folded into §3.2, §3.3, §4, §7 and §9 |
