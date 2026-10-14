# Device Ask Two-Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the key is released, the device Ask speaks an LLM restatement of the question
(falling back to the bundled filler). It can then answer from the web, and a new long-press
interrupts it.

**Architecture:**
- `POST /ask/voice/start` runs STT, kicks an async worker Lambda, and returns a guarded
  restatement.
- The worker runs RAG, with web allowed for voice behind a flag, then TTS. It writes turn state
  under `ask_turns/` in the data bucket.
- The device long-polls `GET /ask/voice/result` and can `POST /ask/voice/cancel`.
- The old `/ask/voice` is untouched.

**Tech Stack:** Python 3.12 Lambdas (SAM), boto3, pytest; Node `node --test` (`voice_llm/`);
Kotlin/Android (GrandTime), JUnit.

**Spec:** `docs/superpowers/specs/2026-10-15-device-ask-two-phase-design.md` (rev 2). Read §3–§9
before any task. The spec is the authority; where this plan is silent, follow the spec.

## Global Constraints

- Development artefacts (code comments, commit messages, docs) are in English. Match the
  surrounding comment style, which explains *why*.
- Never log question, transcript or answer text. Log lines carry ids, statuses, timings and
  `sub_hash` only.
- Identity comes only from the authorizer (`caller['sub']`), never from a client body field.
- `/api/ask/voice` and `_voice_answer` behaviour stay byte-for-byte unchanged, and their existing
  tests stay green untouched.
- Edits to `src/lambda_ask_agent.py` are limited to spec §6. Another session edits this file, so
  keep the hunks small.
- Git:
  - never `git stash`, never `git add -A` or `git add .`; stage named files only.
  - one commit per task minimum.
  - end commit messages with the two attribution lines:
    `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and
    `Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M`.
- Backend worktree: `C:/Users/camil/Dropbox/fs-voicellm`, branch `feat/device-ask-two-phase`.
- Device worktree: `C:/Users/camil/Dropbox/gt-ask-2phase`, branch `feat/ask-two-phase` off
  `origin/main`.
- Python tests: `python -m pytest tests/unit/<file> -q` from the backend worktree. Run one pytest
  process at a time, because the machine is memory-constrained.
- Node tests: `node --test` in `voice_llm/`.
- Flags default off:
  - `AskVoiceRestate` and `VoiceWebEnabled` Parameters default `'false'`.
  - TEST reads `vars.TEST_ASK_VOICE_RESTATE` and `vars.TEST_VOICE_WEB_ENABLED`, each
    `|| 'false'`.
  - Prod is listed in `UNWIRED_BY_DESIGN`.

## Review Focus

1. **Interrupt during Thinking while a video is recording (borrowed mic).** The new question must
   record. Nothing may call `micHandover.end()`/`begin()` in between.
2. **A stale answer arriving after an interrupt.** It must never play, including when the new turn
   is also in Thinking.
3. **Explicit "search online" when the question also says "yesterday".** `scoped` must not block
   the web path when `web_intent` is set.
4. **Duplicate async delivery of the worker event.** The second delivery exits at step 0 without
   writing anything.
5. **A late restatement after the bundled filler has started.** It must be discarded, never
   played on top of the filler.

---

## Backend (fieldsight-pipeline)

### Task 1: `voice_restate.py` and the shared guard fixture

**Files:**
- Create: `src/voice_restate.py`
- Create: `tests/fixtures/restate_guard_cases.json`
- Create: `tests/unit/test_voice_restate.py`
- Modify: `voice_llm/restate.test.mjs` (add one test that loads the fixture)

**Interfaces produced:**
- `voice_restate.guard_middle(question: str, middle) -> dict`. Returns `{"ok": True, "middle": str}`
  or `{"ok": False, "reason": "guard_prefix"|"guard_words"|"guard_number"|"guard_name"}`. The
  semantics are identical to `guardMiddle` in `voice_llm/restate.mjs`, including trailing
  punctuation trim and the lower-cased first letter.
- `voice_restate.is_correction(question) -> bool` and `voice_restate.model_rejected(model) -> bool`,
  the same regexes as the Node versions.
- `voice_restate.assemble(middle: str, web_intent: bool) -> str`. Returns
  `"Got it, <middle>. Checking your site records now."` or
  `"Got it, <middle>. Searching online now."`.
- `voice_restate.restate(question: str, *, web_intent: bool, timeout_s: float = 1.2) -> dict`.
  Returns `{"text": str|None, "reason": str|None, "ms": int}`.
  - It makes one raw `urllib.request` POST to `${QWEN_BASE_URL}/chat/completions` with
    `Authorization: Bearer ${QWEN_API_KEY}` and the body
    `{model, messages:[system RESTATE_SYSTEM, user question], max_tokens:40, temperature:0}`.
  - The model is `VOICE_RESTATE_MODEL` (default `meta-llama/llama-3.1-8b-instruct`), read at call
    time.
  - It is gated by env `ASK_VOICE_RESTATE == "true"` (otherwise `{"text": None, "reason": "off"}`),
    and also gated by `is_correction` and `model_rejected`.
  - Every exception, including timeout, returns `text: None` with a reason; it never raises.
- `RESTATE_SYSTEM` is the same string as the Node `RESTATE_SYSTEM`, copied verbatim.

**Fixture:** at least 14 cases covering each guard reason and each pass path:
- a number present and a number absent
- a capitalised name present and absent
- a non-ASCII capital (`"you're asking about Ōtautahi"` when the question contains `Ōtautahi`
  → ok; when it doesn't → `guard_name`)
- `"…"`, a newline, 26 words, a missing prefix, a curly apostrophe `you’re asking`, the pronoun
  `I` allowed
- trailing `"."` stripped

- [ ] Write `tests/unit/test_voice_restate.py`. Cover:
  - parametrised fixture cases against `guard_middle`
  - `assemble` for both `web_intent` values
  - `restate` with `urllib.request.urlopen` monkeypatched for: success, timeout, non-200, guard
    miss, flag off, correction, rejected model
  - an assertion that the request body sent has exactly the keys above and the system prompt
    equal to `RESTATE_SYSTEM`
  - a test that `RESTATE_SYSTEM` equals the Node string, parsed out of `voice_llm/restate.mjs`
    with a regex over the `RESTATE_SYSTEM = [...]` array
- [ ] Add to `voice_llm/restate.test.mjs` a test that reads
  `../tests/fixtures/restate_guard_cases.json` and asserts `guardMiddle(q, m)` matches
  `{ok, reason}` for every case. When `ok`, it also asserts the returned middle equals the
  fixture's `expected_middle`, if present.
- [ ] Run both test files and see them fail, then implement, then see them pass.
- [ ] Commit.

### Task 2: voice rendering and intent in `web_answer.py`, plus `voice_web.py`

**Files:**
- Create: `src/voice_web.py`
- Modify: `src/web_answer.py` (only `general_answer` and `compose`, adding trailing `voice=False`
  kwargs)
- Create: `tests/unit/test_voice_web.py`

**Interfaces produced:**
- `voice_web.detect_intent(transcript: str) -> tuple[bool, str]`. Returns `(web_intent, stripped_question)`
  using exactly the spec §8 phrase list, case-insensitive, word-bounded.
  - The matched phrase plus adjacent punctuation and whitespace are removed, and leading
    connectives left dangling (`"and"`, `","`, `"."`) are trimmed.
  - If stripping leaves fewer than 2 words, it returns the original transcript as the question.
- `voice_web.VOICE_RULES: str` is the voice instruction appended to the prompts (spec §8 bullets).
- `voice_web.speakable(text: str|None) -> bool` is the model-free guard. It returns False for
  `None`/empty, `http`, `www.`, `](`, any line starting with `-`, `*` or `•` (after whitespace),
  or more than 60 words.
- `voice_web.NO_CLEAR_ANSWER = "I couldn't get a clear answer online for that."`
- `web_answer.general_answer(question, budget, *, kind="general", clock=None, voice=False)`:
  when `voice`, append `voice_web.VOICE_RULES` to the prompt(s) it builds. Nothing else changes.
- `web_answer.compose(records_answer_text, general_block, question, budget, voice=False)`: the
  same.

- [ ] Tests:
  - `detect_intent` on:
    - "Searching online. Find out the maximum length of the fire sprinklers." →
      `(True, "Find out the maximum length of the fire sprinklers.")`
    - "Can you look it up online what NZS 4541 says about..." → True
    - "google that" alone → `(True, original)`
    - "What did we search for yesterday" → False
    - "online induction booked?" → False
  - `speakable` on each reject reason and on a clean two-sentence answer.
  - `general_answer`/`compose` with `voice=True` send a prompt containing `VOICE_RULES`. With
    `voice=False` the prompt is byte-identical to before: capture it via the existing fakes in
    `tests/unit/ask_web_fakes.py` and compare with a run on the unmodified call.
- [ ] Run `tests/unit/test_answering_from_the_open_web.py`,
  `tests/unit/test_ask_knowledge_then_verify.py` and the new file. All are green.
- [ ] Commit.

### Task 3: the §6 edits in `lambda_ask_agent.py`

**Files:**
- Modify: `src/lambda_ask_agent.py`, only the spec §6 items
- Create: `tests/unit/test_ask_voice_web_gates.py`

**Interfaces:**
- **Consumes:** `voice_web.speakable`, `voice_web.NO_CLEAR_ANSWER`, and the `voice` kwarg (Task 2).
- **Produces:**
  - `_rag_answer(body, on_web_start=None)`
  - `lambda_handler` dispatches `body["mode"] == "voice_start"` to `ask_voice_turn.start(body)`
    before the `body.get('audio')` branch. The import is local (`import ask_voice_turn`) so the
    module loads only on that path; the module is created in Task 4.
  - For this task's tests, monkeypatch `sys.modules["ask_voice_turn"]` with a fake.

**Edit details** (spec §6), anchored by text and not by line number:
1. Both `body.get("mode") != "voice"` web gates (the no-chunks general flow, and the
   `if (body.get("mode") != "voice" and chunks and not scoped` branch) accept `body.get("voice_web")`.
   `not scoped` becomes `(not scoped or body.get("web_intent"))`.
2. `web_intent` bypasses `_skip` and classify, setting `cls = {"kind": "general" if no chunk
   passes the gate else "mixed", "records_answer": <bool>, "source": "intent"}`. Find how `cls`
   is consumed after `_classify_for_route` and produce the same shape. In the no-chunks branch it
   skips `_classify_for_route` and routes `general`.
3. `on_web_start()` is called, if not None and wrapped in `try/except Exception: logger.warning`,
   immediately before the `_general_run` call in the no-chunks branch and immediately before the
   pool submit of `_general_run` in the main branch.
4. `voice = body.get("mode") == "voice"` is threaded through `_general_run(...)` (new trailing
   kwarg `voice=False`) to `web_answer.general_answer(..., voice=voice)`, and through the
   `web_answer.compose(...)` call. `PROJECT_FALLBACK_LEAD` is not prepended when `voice` is true.
5. Voice-only speakable guard: when `voice` and the final answer came from the web branch
   (`from_web` or compose), and `not voice_web.speakable(answer)`, replace the answer with the
   records answer if one exists, else with `voice_web.NO_CLEAR_ANSWER`.

**Tests** (use `ask_web_fakes.py` patterns):
- `mode=voice` without `voice_web` → web never called. This pins today's behaviour.
- `mode=voice, voice_web=True`, records miss → web called, `on_web_start` called once, before
  the general run (record the call order).
- `web_intent=True` and `scoped=True` (tz plus "yesterday") → web still called.
- `web_intent=True` → classify not called.
- A voice web answer containing `https://x` → replaced.
- Screen mode (`mode` absent) prompt bytes are unchanged; reuse an existing screen-path test's
  captured prompt.
- `lambda_handler({"mode":"voice_start","audio":"..."})` reaches the fake `ask_voice_turn.start`,
  not `_voice_answer`.

- [ ] Run the new file plus `test_lambda_ask_agent_voice.py`, `test_answering_from_the_open_web.py`,
  `test_ask_knowledge_then_verify.py`, `test_ask_paths_fail_closed.py` and
  `test_ask_prompt_only.py`.
- [ ] Commit.

### Task 4: `ask_voice_turn.py` (store, start, worker)

**Files:**
- Create: `src/ask_voice_turn.py`
- Create: `tests/unit/test_ask_voice_turn.py`

**Interfaces produced:**
- `TURN_RE = re.compile(r"^[0-9a-f]{32}$")`
- `PREFIX = "ask_turns/"`
- `owner_prefix(caller_sub) -> str` returns `"ask_turns/" + sha256(sub).hexdigest()[:16] + "/"`.
- `state_key(caller_sub, turn_id)` and `cancel_key(caller_sub, turn_id)`.
- `put_state(s3, bucket, caller_sub, turn_id, state: dict)`, `get_state(...) -> dict|None`,
  `is_cancelled(...) -> bool`, `put_cancel(...)`, `delete_turn(...)`,
  `sweep_old(s3, bucket, caller_sub, now, max_age_s=86400)`.
  - The bucket comes from env `ASK_TURN_BUCKET`.
  - The S3 client is created lazily at module level, the same pattern as other modules.
- `start(body) -> dict`, called by `lambda_handler`. Spec §3.1 steps 1–7.
  - It uses `lambda_ask_agent._stt` and `_tts` via a local import inside the function, to avoid an
    import cycle.
  - It uses `voice_web.detect_intent` and `voice_restate.restate`.
  - It runs restate and then TTS of the assembled text under a `ThreadPoolExecutor(1)`:
    restate is bounded by its own `timeout_s=1.2`, and TTS by `future.result(timeout=1.8)`.
  - The returned `restatement.audioBase64` is the base64 of the WAV `_tts` returns. Read `_tts` and
    `_voice_answer` for the exact return shape.
  - The STT and error bodies mirror `_voice_answer`'s.
  - The worker Event invoke uses env `ASK_VOICE_WORKER_FUNCTION`, with the payload from spec §3.1
    step 5.
  - It logs the `voice start:` line (spec §3.1) and nothing else that carries content.
- `worker_handler(event, context) -> None`, spec §5 steps 0–6.
  - It calls `lambda_ask_agent._rag_answer(body, on_web_start=cb)`.
  - The `done` state is `{status:"done", transcript, answerText, basis, audioBase64,
    audioFormat:"wav", web: bool(route used web)}`. Read how `_voice_answer` derives
    `answerText`/`basis` from the `_rag_answer` result and mirror it exactly.
  - It calls `lambda_ask_agent._invoke_voice_audit(caller_sub, transcript, answer)` after `done`
    is put, only when not cancelled.
  - It logs `voice ask: stt=0.00s rag=… tts=… total=… … turn=<id> web=<bool>`, with the same
    field set as `_voice_answer`'s line plus the two new fields.

**Tests** (fake S3 as an in-memory dict honouring Get/Put/Delete/ListObjectsV2; fake Lambda
client; monkeypatch `_stt`, `_tts`, `_rag_answer`, `restate`):
- start:
  - happy path with a restatement
  - restate None → `restatement: None`
  - TTS slower than 1.8 s → `restatement: None` and the call returns in under 3.5 s
  - STT empty → error body and no state written
  - invoke failure → state `error` and an error body
  - worker invoked before restate (record the order)
  - `web_intent` returned
  - sweep deletes only this caller's objects that are more than a day old
- worker:
  - step 0: non-pending → no writes
  - cancelled before RAG → no `_rag_answer` call
  - `on_web_start` puts `searching_web`
  - cancelled before TTS → no TTS and no done
  - exception → `error` state
  - no audit row when cancelled
- logs: capture every record during start and worker. Assert that none contains the transcript
  or answer strings used in the test, and that the `voice start:` and worker `voice ask:` lines
  have the documented fields.
- the `ask_turns/` prefix does not start with any notification prefix listed in spec §4
  (hard-coded list).

- [ ] TDD as usual.
- [ ] Commit.

### Task 5: ApiFunction routes

**Files:**
- Modify: `src/lambda_fieldsight_api.py`
- Create: `tests/unit/test_lambda_fieldsight_api_ask_voice_turn.py`

**Interfaces:**
- `ask_voice_start(body, caller)`: the same validation as `ask_voice`, extracted into a shared
  helper **without** changing `ask_voice`'s behaviour or payload. It invokes the agent with
  `mode: "voice_start"` plus the same keys, and passes the agent JSON through as `ask_voice`
  does.
- `ask_voice_result(query, caller)`:
  - parses `turn`/`since`
  - long-polls `ask_voice_turn.get_state`/`is_cancelled` every 0.4 s for up to `RESULT_WAIT_S`
    (env, default 20), with an injectable clock and sleep for tests
  - applies cancel precedence, the 90 s stale rule and delete-after-terminal
  - returns 404 for a missing turn
- `ask_voice_cancel(body, caller)`: regex check → `put_cancel` → 204. Use the module's existing
  response helpers; if no 204 helper exists, add one beside `ok`/`error`.
- `lambda_handler`:
  - adds the three routes and adds them to `KNOWN_ROUTES`
  - parses `event.get("queryStringParameters") or {}` for the result route only
  - does not log query values, keeping the `LEGACY_CALL` contract

**Tests:**
- 401 without sub on all three routes
- the start payload keys are exactly `{mode, audio, format, caller_sub}` (plus tz/history when
  given)
- result: 400 on a bad turn; 404 on another user's turn (state put under a different sub); return
  on status change versus `since`; terminal deletes the state; cancel wins over done; stale →
  error; the wait is capped by the fake clock
- cancel: 204 on unknown, 400 on a bad id
- `test_lambda_fieldsight_api_ask_voice.py` is untouched and green

- [ ] TDD.
- [ ] Commit.

### Task 6: template, workflows, and wiring tests

**Files:**
- Modify: `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
  (prod: no new lines; verify only)
- Modify: `tests/unit/test_template_workflow_parameter_wiring.py` (`UNWIRED_BY_DESIGN` entries)
- Modify: `tests/unit/test_template_timeout_invariants.py` (`SYNC_CALLEES` entry)
- Create: `tests/unit/test_ask_voice_worker_template.py`

**Content** (spec §4, §5, §7, §8):
- **Parameters:** `AskVoiceRestate` and `VoiceWebEnabled`, String, `AllowedValues ['true','false']`,
  Default `'false'`, each with a Description.
- **`AskVoiceWorkerFunction`:**
  - `FunctionName: !Sub ["${P}-ask-voice-worker", ...]`, the same pattern as AskAgent
  - Handler `ask_voice_turn.worker_handler`, Timeout 60, MemorySize 256, `DashScopeLayer`
  - `EventInvokeConfig: {MaximumRetryAttempts: 0, MaximumEventAgeInSeconds: 120}`
  - Env: copy **every** variable from AskAgentFunction's block using the same expressions, then
    override `ASK_DEADLINE_SECONDS: '45'`, `WEB_ANSWER_HARD_STOP: '45'`, `LLM_HTTP_TIMEOUT: '40'`,
    and add `ASK_TURN_BUCKET: !Ref DataBucketName` and `VOICE_WEB_ENABLED: !Ref VoiceWebEnabled`.
  - Policies: `LambdaInvokePolicy` on RagSearchFunction and on VoiceAuditFunction, plus an inline
    `Statement` with `s3:GetObject`/`s3:PutObject` on `arn:aws:s3:::${DataBucketName}/ask_turns/*`.
  - Add a comment on why this function exists and why its budgets differ.
- **AskAgentFunction:**
  - env `ASK_VOICE_WORKER_FUNCTION: !Ref AskVoiceWorkerFunction`, `ASK_TURN_BUCKET`,
    `ASK_VOICE_RESTATE: !Ref AskVoiceRestate`, `VOICE_RESTATE_MODEL:
    'meta-llama/llama-3.1-8b-instruct'`
  - policies: `LambdaInvokePolicy` on the worker; inline S3 `PutObject`/`DeleteObject` on
    `ask_turns/*`, plus `s3:ListBucket` on the bucket ARN with
    `Condition: StringLike: s3:prefix: ask_turns/*`
- **ApiFunction:**
  - env `ASK_TURN_BUCKET`
  - inline S3 Get/Put/Delete on `ask_turns/*`
  - update the "only grant" comment
- **`deploy.yml`:** pass `"AskVoiceRestate=${{ vars.TEST_ASK_VOICE_RESTATE || 'false' }}"` and
  `"VoiceWebEnabled=${{ vars.TEST_VOICE_WEB_ENABLED || 'false' }}"` beside `EnableWebAnswer`.
- **`UNWIRED_BY_DESIGN`:** `"AskVoiceRestate": {"prod"}` and `"VoiceWebEnabled": {"prod"}`, with
  a comment.
- **`SYNC_CALLEES`:** add `("AskVoiceWorkerFunction", "RagSearchFunction")`.
- **New test file:**
  - The worker env contains every key AskAgent's env has, with the same values except the three
    overrides.
  - The worker's `ASK_DEADLINE_SECONDS` and `WEB_ANSWER_HARD_STOP`, plus 10 (TTS), are below its
    Timeout.
  - `MaximumRetryAttempts` is 0.
  - The S3 resources are prefix-scoped (no bare bucket `/*`).
  - Prod workflow passes neither new Parameter.
- **Run:** `python -m pytest tests/unit/test_template_*.py tests/unit/test_ask_voice_worker_template.py -q`.
  Then run cfn-lint at the version pinned in CI, if it is installed locally; otherwise rely on CI's
  `validate-template`.
- [ ] Commit.

### Task 7: full suite, PR, TEST deploy, live verification

- [ ] Full `python -m pytest tests/unit -q -p no:cacheprovider` (one process) and `node --test` in
  `voice_llm/`. Record the counts.
- [ ] Push and open a PR to `develop`. Wait for CI green, then merge. Merging to develop is ours.
  Confirm the TEST deploy run for the merge `headSha` succeeds.
- [ ] Set repo variable `TEST_ASK_VOICE_RESTATE=true` (with `gh variable set`) and re-run the TEST
  deploy.
- [ ] Run `aws iam simulate-principal-policy` for the three function roles against real ARNs:
  `ask_turns/x`, `transcripts/x` (must be denied), and the worker and RAG function ARNs.
- [ ] Live, by invoking AskAgent `voice_start` directly with a TEST user's sub and a recorded
  clip (generate one with ElevenLabs TTS of "When can Zone 1 be backfilled?" as WAV 16 kHz), then
  polling the S3 state:
  - restatement text guarded
  - `start_total` logged
  - `done` written
  - the voice audit row written
- [ ] Verify the result and cancel routes through API Gateway with a TEST Cognito idToken, if one
  can be minted for the test user without a password. Otherwise invoke ApiFunction with a
  synthetic authorizer event.

### Task 8: web-in-voice measurement and flip

- [ ] Write `tools/voice_web_eval.py`. It invokes the worker synchronously with 20 web-intent and
  10 records-miss questions (a list in the script, construction NZ topics) and prints, per turn:
  time, route, `speakable` result, and the answer text (to the console only, not to logs).
- [ ] Check the spec §8 pass bar. If it passes, set `TEST_VOICE_WEB_ENABLED=true` and re-run the
  TEST deploy. If not, record why and leave the flag off.

---

## Device (GrandTime, worktree `C:/Users/camil/Dropbox/gt-ask-2phase`)

Create the worktree with
`git -C C:/Users/camil/Dropbox/GrandTime worktree add C:/Users/camil/Dropbox/gt-ask-2phase -b feat/ask-two-phase origin/main`
after `git fetch`. Gradle unit tests: `./gradlew testDevDebugUnitTest` (check the task name in
the repo's README or the fieldsight-mobile skill).

### Task D1: `AskApiClient` start/result/cancel, cancellable calls, fallback TTL

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/ask/AskApiClient.kt`
- Modify: the `HttpFns`/`RealHttp` seam it uses
- Test: `AskApiClientTest.kt`

**Interfaces:**
- `sealed class StartResult`:
  - `Started(turnId: String, transcript: String, webIntent: Boolean, restatementWav: ByteArray?)`
  - `NoRoute`, `StageError(msg, transcript?)`, `AuthExpired`, `Error(msg)`
- `sealed class TurnStatus`:
  - `Pending`, `SearchingWeb`, `Cancelled`, `Failed(msg)`, `NotFound`
  - `Done(transcript, answerText, wav: ByteArray)`
- `suspend fun start(clip, tz): StartResult`
- `suspend fun result(turnId, since: String): TurnStatus`
- `fun cancel(turnId)` (fire-and-forget)
- Every call is cancellable: wrap the OkHttp `Call` in `suspendCancellableCoroutine` with
  `invokeOnCancellation { call.cancel() }`. This replaces the blocking `execute()` for these new
  calls; the old `ask()` stays as is.
- `NoRoute` covers a 404 or 403 whose body is not JSON with an `error` key. The client caches
  `noRouteUntil = now + 10 min`, and while that is active `start` returns `NoRoute` without a
  call.
- The result call uses a read timeout of 30 s (the server holds it for 20 s).

**Tests:** request shapes, every response parse, the NoRoute TTL with a fake clock, and that
cancellation cancels the Call.

### Task D2: FSM interrupt, generation and borrowed mic (`AskCore`)

**Files:** `ask/AskCore.kt`, `AskCoreTest.kt`

- `onPttDown` and `onDiscreteAsk` in `Thinking` or `Playing` return
  `[CancelTurn, StopAllAudio, <the Idle onPttDown commands without AcquireMic>]` and move to
  `Listening`, keeping `borrowedMic`.
- A new `generation: Int` is incremented on every interrupt and on every new ask. Commands that
  carry results (`onAnswer`, `onTurnStatus`, `onStartResult`) take a generation and are ignored
  when it is stale.
- Tests:
  - interrupt in Thinking
  - interrupt in Playing
  - interrupt with a borrowed mic: no Release/Acquire command emitted, and `borrowedMic` still
    true
  - a stale `onAnswer` is ignored while the new turn is in Thinking
  - an interrupt from Listening is not possible (unchanged)

### Task D3: `AskManager` orchestration and cues

**Files:**
- `ask/AskManager.kt`, `ask/AskSounds.kt`
- `res/raw/ask_searching_online.mp3` (new)
- tests

**Behaviour** (spec §9):
- **On release:** play the earcon (`ask_thinking.wav`) immediately, then launch the ask job (a
  tracked `Job`) that calls `start`.
- **Restatement window:** `RESTATE_WINDOW_MS = 4500`. The first of these wins and the others are
  disarmed:
  - start returns with WAV inside the window → play it
  - the window expires → `sounds.thinking()` (the bundled filler), and drop any late restatement
  - start returns with no restatement → filler now
- **Polling:** loop `result(turnId, since)` with an overall cap of 60 s from release.
  - On `SearchingWeb` the first time, when `!webIntent`, queue `ask_searching_online`.
  - On `Done`, queue the answer. Queued audio waits for the current cue to complete.
  - On `NoRoute`, run the old `api.ask` path (today's behaviour with the bundled filler at
    500 ms).
- **Cancel path:** `cancelTurn()` cancels the job, stops all players, and calls
  `api.cancel(turnId)` when it is known.
- **Generate the cue mp3** with ElevenLabs:
  - voice `QlZn2MPaf2jDn0461bnj`, model `eleven_v4_turbo`, `voice_settings.speed` 1.2,
    `output_format=mp3_44100_128`
  - text "Searching online now, this can take a little longer."
  - Use the TEST ElevenLabs key from the TEST ask-agent Lambda env, read into a shell variable and
    never printed.
- Keep `AskCueBeforeMicTest` green. Read it first.
- **Tests** (fake API, fake sounds and players, virtual time): each window branch, a late
  restatement dropped, the online cue suppressed with `webIntent`, the answer queued after the
  cue, the cap → error cue, and interrupt mid-poll cancels the job and calls `cancel`.

### Task D4: version, build, release to the test stage

- [ ] Bump to versionCode 47 / versionName "0.7.21" in `app/build.gradle.kts`.
- [ ] Run the unit tests.
- [ ] `assembleDevRelease`, signed per the fieldsight-mobile skill.
- [ ] `python tools/release_apk.py --stage test --apk <apk> --notes "Ask restates your question, can search online, long-press to interrupt" --dry-run`,
  then the real run.
- [ ] Push the branch and open a PR to GrandTime `main`. Do not merge without the owner, since
  `main` builds prod.
- [ ] Add a pointer commit to GrandTime docs referencing the spec path in fieldsight-pipeline.

### Task 9: end-to-end and final tests

- [ ] If a device is connected over adb, install 0.7.21 dev, ask a question, and read logcat for
  the cue order. Otherwise leave this for the owner's morning test, with exact steps.
- [ ] Re-run the full backend unit suite and the Node suite on the merged develop. Report the
  counts.
