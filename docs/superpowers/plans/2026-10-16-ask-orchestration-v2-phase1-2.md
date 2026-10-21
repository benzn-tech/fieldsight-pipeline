# Ask orchestration v2 — phases 1–2 implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:**
- Phase 1: move screen Ask onto the async turn store (start → worker → result), so it is no longer bound by the 29 s gateway.
- Phase 2: add a tool-calling LLM client for the phase 3 judge.

**Architecture:**
- AskAgent gets `mode: "turn_start"`. It stores the request in `ask_turns/<hash>/<turn>.json` with `channel: "screen"` and Event-invokes the existing voice worker.
- The worker branches on `channel`.
- ApiFunction gets `/api/ask/start|result|cancel`.
- The UI switches behind a config flag, with fallback to `/api/ask`.
- Phase 2 is a standalone module, `src/llm_tools_client.py`.

**Tech stack:** Python 3.11 Lambda, boto3, pytest, SAM; fieldsight-ui vanilla JS (branch `dev`, no CI).

**Spec:** `docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md` rev 2, §4 (phase 1) and §5 (phase 2). Phase 0 shipped as PR #1123.

## Rulings (controller, recorded before execution)

1. **Screen done shape.** The state stores the whole `_rag_answer` dict as `result`. The result route returns `{"status":"done","result":{…}}`.
   - Why: §4.3's field whitelist could silently drop a field the UI reads (`kind`, `metric`, `error`, `web_offer`, …). `/api/ask` passes the dict through as is today, so pass-through keeps parity.
   - Cost if wrong: one adapter line in the UI.
2. **Screen deadline.** Phase 1 uses the worker's existing env budgets (`ASK_DEADLINE_SECONDS=45`, `WEB_ANSWER_HARD_STOP=45`). There is no per-call `deadline_s` plumbing.
   - Why: §4.5's 40 s was chosen to fit under the UI's 55 s give-up. 45 s also fits (45 + up to 20 s of poll < the 90 s stale rule; the UI gives up at 60 s instead of 55).
   - This removes the M5 deadline refactor from phase 1.
   - Cost if wrong: a screen answer can take 5 s longer before the UI gives up.
3. **Screen progress states in phase 1.** Only `pending → searching_web → done|error`, using the existing `on_web_start` hook. `searching_records` and `writing` arrive with phase 3.
   - Why: smallest change; the voice set is unchanged by construction.
4. **The voice path is byte-for-byte unchanged.** A worker event without `channel`, or with `channel == "voice"`, takes today's code. This is pinned by the existing voice worker tests passing unmodified.

## Global constraints

- **Logging.** Never log question, answer, history, transcript or `caller_sub` text. Use `sub_hash` and counts only.
- **Identity** comes only from the authorizer `sub`, which ApiFunction passes as `caller_sub`.
  - The worker takes `caller_sub` from the Event only.
  - The stored `request` must not contain `caller_sub`: strip it before the put, and ignore it on read.
- **Old routes stay unchanged:** `/api/ask`, `/api/ask/voice*`, and `_DONE_FIELDS`.
- **Git hygiene.** Stage named files only, never `git add -A` or `.`. Never `git stash`. Development artefacts are in English.
- **Running tests.** Run one pytest process at a time: `python -m pytest -q -p no:cacheprovider <files>`.
- **Windows.** Use `MSYS_NO_PATHCONV=1` for the AWS CLI. Use Windows `C:/…` paths for Python.

## Review focus

1. A screen turn must never write `convo.json`/`offer.json`, never TTS, never audit.
2. A voice turn must take the old path exactly: same rag body, same done fields.
3. The stored request must never carry `caller_sub`, and the worker must not trust one if it does.
4. The result route must return the screen shape only for `channel == "screen"` states, and the voice shape otherwise.
5. The UI fallback must trigger only on `_notFound` from `/ask/start`, never on a result 404.

---

### Task 1: AskAgent `turn_start` and worker screen branch

**Files:**
- Modify: `src/ask_voice_turn.py`, `src/lambda_ask_agent.py` (`lambda_handler`, beside `voice_start`).
- Test: `tests/unit/test_ask_screen_turn.py` (new). Reuse the fake S3 / lambda fixtures from `tests/unit/test_ask_voice_turn.py`; read that file first.

**Produces:** `ask_voice_turn.start_screen(body) -> dict` and the worker `channel == "screen"` branch.

- [ ] **Tests first.**
  - `start_screen`, missing `caller_sub` ⇒ `{"error": "sign-in required"}` and no S3 write.
  - `start_screen`, empty question ⇒ `{"error": "Missing question"}`.
  - `start_screen`, ok:
    - one `put_state` with `status="pending"`, `channel="screen"`, `created_at`, and `request`. `request` equals the body minus `caller_sub` and `mode`.
    - one Event invoke of `ASK_VOICE_WORKER_FUNCTION` with exactly `{turn_id, caller_sub, channel:"screen"}`.
    - The return value is `{"turn_id": <32 hex>}`.
  - `start_screen`, invoke raises ⇒ state `error` "No answer", and the return is `{"error": "Ask temporarily unavailable"}`.
  - `sweep_old` is called once.
  - Worker with `channel="screen"`:
    - `_rag_answer` is called with `request` + `caller_sub` from the event. There is no `mode` key, no `voice_web`, no `web_intent`, no `offer_web`.
    - A `caller_sub` inside `request` is ignored.
    - It writes `{"status":"done","channel":"screen","result":<rag dict>,"created_at":…}`.
    - Citation markers are NOT stripped.
    - `laa._tts`, `laa._invoke_voice_audit` and `_remember` are never called (even with `ASK_VOICE_CONVO=true`).
  - Worker screen + `on_web_start` ⇒ a `searching_web` state with `channel="screen"`.
  - Worker screen + cancel marker before done ⇒ no done write.
  - Worker screen + `_rag_answer` raising ⇒ state `error`.
  - Worker screen + rag `{"error": "x"}` ⇒ state done with `result` carrying the error. This is parity with `/api/ask`, which returns 200 with the error dict.
  - The worker with no `channel` still passes every existing test in `tests/unit/test_ask_voice_turn.py` unmodified.
- [ ] Run, see red.
- [ ] **Implement.**
  - `start_screen` lives in `ask_voice_turn.py`.
  - `turn_id = uuid.uuid4().hex`.
  - It reuses `_get_s3`, `_bucket`, `sweep_old`, `put_state`, and `_get_lambda().invoke(FunctionName=os.environ["ASK_VOICE_WORKER_FUNCTION"], InvocationType="Event", Payload=…)`. Copy the env name and invoke form exactly from `start`.
  - In `lambda_handler`: `if body.get('mode') == 'turn_start': import ask_voice_turn; return ok(ask_voice_turn.start_screen(body))`.
  - In `worker_handler`, right after the pending check, add `if event.get("channel") == "screen": return _work_screen(...)`. The screen branch is a separate function so the voice path is untouched.
  - Log line: `screen ask: rag=%.2fs total=%.2fs turn=%s web=%s sub_hash=%s`.
- [ ] Run this file plus `tests/unit/test_ask_voice_turn.py`; pass.
- [ ] Commit: `ask turn: screen start and worker branch`.

### Task 2: ApiFunction `/api/ask/start|result|cancel`

**Files:**
- Modify: `src/lambda_fieldsight_api.py`.
- Test: `tests/unit/test_lambda_fieldsight_api_ask_screen_turn.py` (new). Mirror `tests/unit/test_lambda_fieldsight_api_ask_voice_turn.py`.

**Consumes:** Task 1 (`mode: "turn_start"`, the state shape).

- [ ] **Tests first.**
  - **`POST /api/ask/start` payload.** It builds exactly the payload `ask_question` builds today (same fields, history cleaning, scope fields, `scoped`, `tz`, `date`), plus `mode: "turn_start"`.
    - Extract the builder from `ask_question` into `_build_ask_payload(body, caller) -> (payload, err)`. `ask_question` uses it too, so its existing tests must still pass unchanged.
  - **`POST /api/ask/start` invoke.** It invokes AskAgent RequestResponse through a client whose `read_timeout` is 8 s: a new module-level `ask_start_client`, `Config(read_timeout=8, connect_timeout=2, retries={'max_attempts':0})`.
    - It passes the agent's `{turn_id}` or `{error}` through.
    - FunctionError ⇒ 500. ReadTimeout ⇒ 504.
    - No sub ⇒ 401.
  - **`GET /api/ask/result`.** Same long-poll as voice.
    - For a done state with `channel == "screen"` it returns `{"status":"done","result":state["result"]}`.
    - A done state without `channel` returns the voice body (`_DONE_FIELDS`), as today.
    - `since` accepts `pending|searching_web`.
    - Terminal turns are deleted as today.
  - **`POST /api/ask/cancel`.** Behaves the same as voice cancel.
  - **`KNOWN_ROUTES`** gains the three paths.
- [ ] Run, see red.
- [ ] **Implement.**
  - `_terminal_body(state)` gains the channel branch.
  - `/api/ask/result` and `/api/ask/cancel` call the existing `ask_voice_result` / `ask_voice_cancel` functions. Rename is not needed; add a docstring line.
- [ ] Run the new file, the voice-turn API file, and the existing `ask_question` tests (`-k "ask"` on `tests/unit/test_lambda_fieldsight_api*.py`); pass.
- [ ] Commit: `api: /api/ask/start, /result, /cancel for screen turns`.

### Task 3: template invariants

**Files:**
- Modify: `tests/unit/test_ask_voice_worker_template.py`.
- Modify: `src/template.yaml`, only if a gap is found.

- [ ] **Assert in tests:**
  - AskAgent has `s3:PutObject` on `ask_turns/*` and `lambda:InvokeFunction` on the worker.
  - ApiFunction has `s3:GetObject`, `s3:DeleteObject`, `s3:PutObject` (cancel marker) on `ask_turns/*`.
  - The worker has `s3:GetObject` and `s3:PutObject` on `ask_turns/*`, and has `ASK_DEADLINE_SECONDS` 45.
  - The worker `Timeout` is 60.
- [ ] If any assertion fails, fix `template.yaml` minimally and note it in the commit.
- [ ] Commit: `template: pin screen turn IAM`.

### Task 4: fieldsight-ui start + poll behind a flag

**Repo:** fieldsight-ui.
- Find the checkout under `C:/Users/camil/Dropbox` that contains `scripts/api/ask.js`.
- Create a fresh worktree from `origin/dev` on branch `feat/ask-async-turn`. Do not touch the existing checkout's working tree.

**Files:** `scripts/api/ask.js`, `scripts/composites/ask-chat.js`, and wherever `window.FS.config` flags are defined. Find how existing flags (e.g. `FS_TIMELINE_SOURCE`) reach the page and follow that pattern exactly.

**Flag:** `FS_ASK_ASYNC`.
- Default is off.
- **Dev:** add it as the Amplify dev-branch env value `true` in a separate step that the controller does with the AWS CLI. `update-branch --environment-variables` replaces the whole set; carry all existing `FS_*` values. Do not do this inside the task.

**Steps:**
- [ ] **`ask(body, {onProgress, signal})`.** When the flag is on:
  1. POST `/ask/start` with the same body.
     - If the response has `_notFound`, fall back to the old POST `/ask` once and return its value.
     - If it has `error`, return it as the old route would.
  2. Loop GET `/ask/result?turn=&since=` with `timeoutMs` 35000.
     - On a non-terminal status, call `onProgress(status)` and set `since=status`.
     - On `done`, return `res.result`, the same value the old `/ask` resolved to.
     - On `error` or `cancelled`, return `{error: …}` the way the old failure path does.
  3. Give up after 60 s total: POST `/ask/cancel` and return the existing failure.
  4. When the generation is stale (`isStale()`), or `signal` aborts, POST `/ask/cancel` and stop.

  When the flag is off, the code path is the existing one, byte for byte.
- [ ] **`ask-chat.js`.** Show the progress line while pending: "Searching your records…" for `pending` and "Checking the web…" for `searching_web`. Reuse the existing pending-indicator element and styles; add no new component.
- [ ] Check there is a test or lint setup (`package.json` scripts) and run it if one exists. There is no CI.
- [ ] Commit on `feat/ask-async-turn` and push. A PR to `dev` is opened by the controller after a Chrome check.

### Task 5: `llm_tools_client` (phase 2)

**Files:**
- Create: `src/llm_tools_client.py`.
- Test: `tests/unit/test_llm_tools_client.py`.

**Produces:**

```
chat(messages, tools, *, model=None, tool_choice="required", max_tokens=2048,
     deadline=None, caller="ask_judge") -> ChatTurn
```

`ChatTurn` is a dataclass with these fields:

| Field | Type |
|---|---|
| `tool_calls` | list of `{id, name, arguments: dict}` |
| `text` | str |
| `usage` | dict |
| `model` | str |
| `error` | str or None |
| `timed_out` | bool |
| `bad_args` | list of tool-call ids whose arguments were not JSON |

- [ ] **Tests first.** Mock the HTTP layer the same way `tests/unit/test_corroboration_client*.py` mocks it; read it first.
  - **Request body:** `messages`, `tools` (OpenAI shape), `tool_choice`, `max_tokens` + `llm_utils.REASONING_HEADROOM_TOKENS`, no `plugins`.
    - Model from `ASK_JUDGE_MODEL`, else `CORROBORATION_CHEAP_MODEL`.
  - **Parsing:**
    - `tool_calls` with JSON-string `arguments` are parsed into dicts.
    - Unparseable arguments land in `bad_args`, with `arguments={}`.
    - Text-only content gives `tool_calls == []` and `text` set.
  - **Fallbacks:**
    - HTTP 400 whose body mentions `tool_choice` makes the client retry once with `tool_choice="auto"`. A forced `{"type":"function",…}` value is sent as is.
    - The fallback chain from `ASK_JUDGE_FALLBACKS` (comma-separated) is tried in order on 5xx or timeout, within the deadline.
  - **Deadline:** with `deadline` passed and less than 1 s left, the client returns `timed_out=True` with no HTTP call.
  - **Logging:** the log line has caller, model, tool names, usage and ms. Assert the message text is absent with `caplog`.
- [ ] Run, see red.
- [ ] Implement. Reuse `corroboration_client`'s URL, key env and session helper; import them, never copy the key handling.
- [ ] Run; pass.
- [ ] Commit: `llm_tools_client: OpenRouter tool-calling chat for the Ask judge`.

### After all tasks (controller)

1. **Tests.** Run the full Ask subset: `python -m pytest -q -p no:cacheprovider tests/unit -k "ask or voice or web or template or llm"`.
2. **Backend.** Open a PR to develop, wait for CI green, merge, deploy to TEST, and confirm the run's `headSha` matches.
3. **Live TEST.** Invoke ApiFunction `/api/ask/start` and then `/result` with the test sub on "What did we do yesterday?" and one general question. That is about 4 model calls; state the count first.
4. **UI.** Set the Amplify dev env `FS_ASK_ASYNC=true`, carrying all existing `FS_*` values, and rebuild dev.
   - Check where dev's base URL points first. Per memory, dev may point at prod; if so, do not enable the flag there until the backend is on prod, and verify through a local served copy against TEST instead.
5. **Chrome.** Verify the Ask panel: progress line, answer, citations.
6. **Voice regression.** Run one live smoke through `smoke_turn.py` with an existing wav.
