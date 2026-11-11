# Contact FieldSight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the left-nav `mailto:` with an in-page window. The person types or records up to 2 minutes, the recording is transcribed into the editable text, and Send emails the text, transcript and audio to `sales@fieldsightai.com` and keeps an S3 record.

**Architecture:** One new out-of-VPC Lambda, `ContactFunction`, on `FieldSightApi` at `/api/contact/{proxy+}`, with two routes: `transcribe` (synchronous ElevenLabs Ask key) and `messages` (S3 record + SES raw email with the audio attached, idempotent on a client id). The web side is a `ContactHost` window in the `SettingsHost` pattern. The `mailto` fallback is used when the flag is off.

**Tech Stack:**
- Backend: Python 3.11, boto3, AWS SAM (`src/template.yaml`), pytest.
- Web: fieldsight-ui vanilla JS with `React.createElement`, MediaRecorder and AnalyserNode, `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-08-contact-fieldsight-design.md` (reviewed). Read it all before starting. The values below are copied from it.

## Global Constraints

- Flag: Parameter `ContactFieldSight` `'true'|'false'`, default `'false'`, env `CONTACT_FIELDSIGHT`. Workflow vars `TEST_CONTACT_FIELDSIGHT` / `PROD_CONTACT_FIELDSIGHT`, each `|| 'false'`. When off, every `/api/contact/*` returns `404 {"error":"not_found"}`.
- Identity comes only from `requestContext.authorizer.claims` (`sub`, `email`, `name`). Body `context.*` is "as reported by the browser".
- Limits:
  - `text` ≤ 4000 characters; `transcript` ≤ 8000; decoded audio ≤ 3 MB.
  - mime ∈ {`audio/webm;codecs=opus`, `audio/webm`, `audio/mp4`}.
  - Each `context` value has CR/LF stripped and is ≤ 200 characters.
  - At least one of text or audio is present.
  - Check the base64 length (≤ 4 · 3 MB / 3 + 4) **before** decoding.
- Rate limits per `sub` per rolling hour: 10 messages, 20 transcribes. Over the limit: `429 {"error":"rate_limited","retry_after_s":int}`.
- S3 under DataBucketName, prefix `contact_messages/{sub}/`:
  - record `{yyyy-mm-ddTHHMMSSZ}_{id}.json`;
  - audio `{same stem}.webm|.m4a`;
  - transcribe counter `_transcribe/{yyyy-mm-ddTHH}.json`.
- Email: `ses.send_raw_email`, To `sales@fieldsightai.com`, From `SENDER_EMAIL`, Reply-To the claim email.
  - Subject `"[TEST] " if STAGE != "prod"` + `FieldSight message from {name} ({company})`.
  - The audio is attached as `message.webm`/`message.m4a`.
  - It does not use `email_sender.get_sender()`.
- Logs carry `id` and `sub` only, never text, transcript or audio.
- Every response is JSON with the CORS headers of `lambda_fieldsight_api.ok/error` (`src/lambda_fieldsight_api.py:84-97`).
- Function: Timeout 28. Env: `ELEVENLABS_ASK_TIMEOUT_SECONDS: '12'`. Transcribe outer deadline 25 s. Provider failure → `200 {"text":"","error":"unavailable"}`.
- Web:
  - `scripts/api/_fetch.js` is not edited (another line owns it).
  - Calls use `baseUrl: FS.api.orgBaseUrl`, `retry: false`, `timeoutMs: 28000`.
  - `buildHelpMailto` stays exported and is unchanged.
- Python: `C:\Users\camil\fspy\Scripts\python.exe`. Never `git add -A`. Never `git stash`.

## Review Focus

1. **The same `id` sent twice** (double click, or a gateway retry after a slow SES call). Expected: one record, one email, both calls 201 with that id. Test in Task 2.
2. **A newline in `context.name`** (header injection). Expected: the Subject has no CR/LF. Test in Task 1.
3. **SES fails after the S3 write.** Expected: 201, the record says `email_status: "email_failed"`, and an ERROR log line carries only id and sub. Test in Task 2.
4. **Microphone denied, or MediaRecorder unsupported.** Expected: the record control shows "Microphone not available. You can type instead." and typing and sending still work. Test in Task 6.
5. **Flag off on prod while the new web code is live.** Expected: Send keeps the window open with the text and offers "Email us instead", whose mailto body contains the typed text. Test in Task 6.

---

## Backend (fieldsight-pipeline, branch `feat/contact-fieldsight` from `origin/develop`)

### Task 1: Pure contact module

**Files:**
- Create: `src/contact_messages.py`
- Test: `tests/unit/test_contact_messages.py`

**Interfaces (produces):**
- `MAX_TEXT=4000`, `MAX_TRANSCRIPT=8000`, `MAX_AUDIO_BYTES=3*1024*1024`, `ALLOWED_MIME={...}`, `MSG_PER_HOUR=10`, `TRANSCRIBE_PER_HOUR=20`.
- `validate_message(body: dict) -> tuple[dict | None, str | None]`
  - Returns `(clean, None)` or `(None, "plain message")`.
  - `clean` = `{id, text, transcript, audio_bytes|None, mime|None, ext|None, context}`.
  - `id` must be a uuid4 string.
  - `ext` is `webm` for webm mimes and `m4a` for mp4.
- `validate_audio(b64: str | None, mime: str | None) -> tuple[bytes | None, str | None]`, shared by transcribe.
- `clean_context(ctx) -> dict` with keys `name, company, project, page`. CR/LF are stripped, values capped at 200, and `page` is cut at `?`.
- `record_stem(sub, at_utc: datetime, id) -> str` returns `contact_messages/{sub}/{%Y-%m-%dT%H%M%SZ}_{id}`.
- `count_recent(keys: list[str], now_utc, window=timedelta(hours=1)) -> int` counts `.json` record keys (not under `_transcribe/`) whose timestamp falls in the window.
- `build_email(*, record: dict, audio: bytes | None, ext, sender, to, stage, now_nz_label) -> bytes` returns a MIME message (`email.mime`).
  - Body: the plain text first, then an escaped HTML alternative, then the attachment.
  - The transcript section appears only when the transcript is non-empty and `!= text.strip()`.

- [ ] **Step 1: Write failing tests.**
  - `test_validate_requires_text_or_audio`
  - `test_text_over_4000_rejected`
  - `test_bad_uuid_rejected`
  - `test_mime_not_allowed`
  - `test_b64_length_checked_before_decode`: patch `base64.b64decode` to raise, pass an over-long string, and expect the size error rather than the patched exception.
  - `test_context_strips_crlf_and_query`: name `"A\r\nBcc: x"` → `"A Bcc: x"`; page `/timeline?q=secret` → `/timeline`.
  - `test_count_recent_ignores_old_and_counter_keys`
  - `test_subject_test_prefix_and_no_newline`: stage `test` → starts with `[TEST] `; parse with `email.message_from_bytes`, and the Subject header has no `\n`.
  - `test_reply_to_is_claim_email`
  - `test_transcript_section_only_when_edited`
  - `test_audio_attached_with_ext`
  - `test_html_part_escapes`: text `<script>` appears as `&lt;script&gt;`.
- [ ] **Step 2: Run them.** `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_contact_messages.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** `src/contact_messages.py`. Standard library only.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(contact): pure validation, keys and email builder`.

### Task 2: Handler `lambda_contact.py`

**Files:**
- Create: `src/lambda_contact.py`
- Test: `tests/unit/test_lambda_contact.py`

**Interfaces:**
- Consumes Task 1.
- Produces `lambda_handler(event, context)`.
- Dependencies are injected for tests through module-level factories `_s3()`, `_ses()`, `_stt(audio_bytes, filename) -> str` (wraps `elevenlabs_utils.stt_short`), and `_now()`.
- Routes, after stripping `/api/contact`:
  - `POST /transcribe`
  - `POST /messages`
  - `OPTIONS` anything → 200 CORS
  - anything else → 404
- Flag: read `os.environ.get("CONTACT_FIELDSIGHT") == "true"` on every call. Off → 404 for everything except OPTIONS.
- Missing `sub` or `email` claim → 401.
- `/transcribe`:
  1. Validate the audio.
  2. Rate-check by reading and updating the counter object `contact_messages/{sub}/_transcribe/{hour}.json` (`{"n":int}`). A NoSuchKey/404 means 0. Any other ClientError propagates.
  3. Call `_stt` in a `concurrent.futures` thread with a 25 s deadline.
  4. On exception or timeout → `200 {"text":"","error":"unavailable"}` and a WARNING with sub only.
- `/messages`:
  1. Validate.
  2. Idempotency: list `contact_messages/{sub}/` and look for a key ending `_{id}.json`. If one exists → `201 {"id"}`, no email.
  3. Rate: `count_recent(...)` ≥ 10 → 429, with `retry_after_s` = seconds until the oldest one in the window ages out (minimum 60).
  4. Put the audio, if any.
  5. Put the record with `email_status: "pending"`.
  6. Send the email.
  7. Re-put the record with `sent` or `email_failed`.
  8. Return 201.

- [ ] **Step 1: Write failing tests** with a fake S3 (a dict) and a fake SES:
  - `test_flag_off_404`
  - `test_options_ok_when_off`
  - `test_identity_from_claims_not_body`: a body `email` is ignored and Reply-To uses the claim.
  - `test_transcribe_ok`
  - `test_transcribe_provider_error_is_200_unavailable`
  - `test_transcribe_timeout_is_200_unavailable`: patch the deadline to 0.1 s and the fake stt to sleep 1 s.
  - `test_transcribe_rate_limit_21st_is_429`
  - `test_message_writes_record_then_email`
  - `test_same_id_twice_one_email`
  - `test_ses_failure_still_201_and_marked`, which also asserts the ERROR log contains neither the text nor the transcript (caplog).
  - `test_message_rate_limit_11th_is_429`
  - `test_record_and_audio_keys_under_sub_prefix`
  - `test_responses_are_json_with_cors`, covering 400, 404 and 429.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Copy `ok/error` from `lambda_fieldsight_api.py:84-97`.
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(contact): ContactFunction handler (transcribe, messages)`.

### Task 3: Template, IAM, workflows

**Files:**
- Modify: `src/template.yaml`
  - Parameter `ContactFieldSight` next to `ProjectDocuments` (~:1550).
  - New resource `ContactFunction` in its own commented block after the Ask proxy function (~:6343).
- Modify: `.github/workflows/deploy.yml` (in the `--parameter-overrides` block next to `ProjectDocuments`, ~:398) and `.github/workflows/deploy-prod.yml` (~:422).
- Test: `tests/unit/test_template_contact.py`. The existing `tests/unit/test_template_workflow_parameter_wiring.py` must pass.

**Resource requirements:**
- `Handler: lambda_contact.lambda_handler`, `Timeout: 28`, `MemorySize: 256`, no `VpcConfig`.
- Env:
  - `CONTACT_FIELDSIGHT: !Ref ContactFieldSight`
  - `S3_BUCKET: !Ref DataBucketName`
  - `SENDER_EMAIL: !Ref SenderEmail`
  - `STAGE: !Ref Stage`
  - `ELEVENLABS_ASK_API_KEY: !Ref ElevenLabsAskApiKey`
  - `ELEVENLABS_ASK_TIMEOUT_SECONDS: '12'`
- Policies:
  - Get/Put on `arn:aws:s3:::${DataBucketName}/contact_messages/*`
  - ListBucket on the bucket with `StringLike s3:prefix ['contact_messages/*']`
  - `ses:SendRawEmail` on `'*'`
- Event: `Type: Api`, `RestApiId: !Ref FieldSightApi`, `Path: /api/contact/{proxy+}`, `Method: ANY`. No Auth override, so the default Cognito authorizer applies. Check how OPTIONS preflight is handled for `/api/org/{proxy+}` (Cors settings on `FieldSightApi`) and match it, so the browser preflight is not rejected by the authorizer.
- Check the stage name's parameter (`Stage` or similar) and the prod value, so that `STAGE != "prod"` is the correct TEST test. Adjust the Task 2 check to the real values.

- [ ] **Step 1: Write failing tests** in `test_template_contact.py`, parsing the template with the repo's existing CFN-aware yaml loader (look in other `test_template_*` files):
  - `test_function_outside_vpc`
  - `test_listbucket_prefix_condition`
  - `test_s3_grants_scoped_to_contact_prefix`
  - `test_event_path_and_api`
  - `test_flag_param_default_false`
  - `test_ask_key_and_timeout_env`
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** the template and workflow lines.
- [ ] **Step 4: Run.** The new tests, the wiring test and the full unit suite `-m pytest tests/unit -q -x` must pass. Run `sam validate --lint` if available locally; otherwise rely on CI's cfn-lint.
- [ ] **Step 5: Deploy-role check.** Run `AWS_PROFILE=fieldsight-deployer aws iam simulate-principal-policy --policy-source-arn arn:aws:iam::509194952652:role/github-actions-fieldsight-deploy --action-names lambda:CreateFunction iam:CreateRole iam:PutRolePolicy iam:PassRole apigateway:POST --resource-arns <real fieldsight-test-ContactFunction-* function and role ARNs>`. Expected: allowed. If denied, stop and report; do not merge.
- [ ] **Step 6: Commit** `feat(contact): ContactFunction resource, IAM, flag wiring`, then open a PR to `develop` that includes the spec and this plan.

### Task 4: TEST deploy and backend verification

- [ ] Merge the PR when CI is green (the base must be `develop`).
- [ ] Wait for the TEST deploy run to succeed.
- [ ] Set `gh variable set TEST_CONTACT_FIELDSIGHT --body true -R benzn-tech/fieldsight-pipeline` and re-run the TEST deploy workflow (`workflow_dispatch`, or re-run the latest).
- [ ] Verify from the signed-in TEST tab (Chrome) with `fetch` to `orgBaseUrl + '/contact/...'`, using the app's auth header via `FS.api.request` with `{baseUrl: FS.api.orgBaseUrl, method:'POST', retry:false, timeoutMs:28000}`.
- [ ] Make a speech clip in the tab:
  1. Fetch a real TEST WAV (from `FS.api.audio.getAudioSegments`).
  2. Play it into `AudioContext → MediaStreamDestination → MediaRecorder` (webm/opus, 32 kbps) for about 90 s.
  3. Repeat with `audio/mp4`.
  4. Call `/transcribe` with both. Record the latency and text length for each.
  5. If webm fails and mp4 works, the client must prefer mp4 (one-line change in Task 6).
- [ ] Send `/messages`:
  1. One with text only.
  2. One with text, transcript and audio.
  3. The same id again.
  4. Confirm two S3 records (`aws s3 ls s3://fieldsight-data-test-509194952652/contact_messages/ --recursive` with the deployer profile) and that the email arrived (ask the owner, or check the record's `email_status: sent`).

## Web (fieldsight-ui, branch `feat/contact-window` from `origin/dev`)

### Task 5: `scripts/api/contact.js`

**Files:**
- Create: `scripts/api/contact.js`
- Test: `tests/contact-api.test.js`
- Modify: `app-shell-preview.html` (script tag after the other api modules)

**Interfaces (produces):** `window.FS.api.contact`:
- `available() -> boolean`: `!!FS.api.orgBaseUrl && !FS_ENV.useMocks`.
- `transcribe(blob: Blob, mime: string) -> Promise<{text: string, error?: string}>`.
- `send({id, text, transcript, blob, mime, context}) -> Promise<{id}>`.
  - Throws an Error with `.status` on 4xx/5xx.
  - Resolves `{_notFound: true}` on 404, as the helper already does.
- `newId() -> string`: `crypto.randomUUID()`.
- `blobToBase64(blob) -> Promise<string>`.
- Calls: `FS.api.request('/contact/transcribe' | '/contact/messages', {method:'POST', body, baseUrl: FS.api.orgBaseUrl, retry:false, timeoutMs:28000})`. Check the real option names in `_fetch.js`, but do not edit it. The body is built from whitelisted keys only.

- [ ] **Step 1: Write failing tests:**
  - `send builds body from whitelisted keys only`
  - `send passes retry:false and timeoutMs 28000 and orgBaseUrl`
  - `transcribe passes the same options`
  - `available is false with empty orgBaseUrl`
- [ ] **Step 2: Run** `node --test tests/contact-api.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(contact): api client`.

### Task 6: The window, recorder and nav wiring

**Files:**
- Create: `scripts/composites/contact-window.js`
- Create: `tests/contact-window.test.js`
- Modify:
  - `scripts/left-nav.js:401-411` (`contactHelp` → `FS.contact.open()` when present, else the old mailto)
  - `scripts/app-shell.js` (mount `window.FieldSight.ContactHost` next to SettingsHost at ~:1558, passing `user` and the active site's name)
  - `styles/composites.css`
  - `app-shell-preview.html`

**Interfaces:**
- Pure helpers exported for tests (CommonJS like `left-nav.js:612`):
  - `pickMime(isTypeSupported) -> string | null`: order `audio/webm;codecs=opus`, `audio/mp4`, else null.
  - `canSend({text, blob, sending}) -> boolean`.
  - `appendTranscript(text, transcript) -> string`: blank-line separator; an empty text returns the transcript.
  - `fallbackMailto(user, route, text) -> string`: the existing mailto with the typed text prefilled at the top of the body.
  - `contextFor(user, me, siteName, route) -> {name, company, project, page}`: page is the route without the query string.
  - `errorCopy(kind, extra) -> string`, with the exact strings:
    - `mic`: "Microphone not available. You can type instead."
    - `stt`: "Couldn't turn the recording into text. It will still be sent as a voice note."
    - `rate`: "You've sent several messages recently. Please try again in N minutes." (N = ceil(retry_after_s/60))
    - `fallback`: "We couldn't send this here. Email us instead."
- `ContactHost({user, siteName, route})` registers `window.FS.contact = {open, close, isOpen}`. It mounts lazily, like SettingsHost, and keeps the draft (text, blob, transcript, id) until reload.
- Window:
  - From line: name · email.
  - About line: the project.
  - Textarea: "What can we help with?", max 4000 characters.
  - Record button. MediaRecorder with `audioBitsPerSecond: 32000`, a 1 s timeslice, and an automatic stop at 120 s. A count-up timer shows m:ss / 2:00.
  - Live level meter: an AnalyserNode drawn on a small canvas with rAF while recording. Respect `prefers-reduced-motion` with a static bar.
  - After stopping: an `<audio controls>` clip, "Transcribing…", then `appendTranscript`, plus "Remove recording".
  - Send / Cancel.
  - On success, toast "Sent. We'll reply to {email}." Close and clear the draft (new id).
  - On 404, `_notFound`, a network error or `!available()`, show `errorCopy('fallback')` with an "Email us instead" button that opens `fallbackMailto`.
  - On 429, show the rate copy.
  - The microphone stream's tracks are stopped on stop, remove and close.

- [ ] **Step 1: Write failing tests:**
  - `pickMime prefers webm opus then mp4 then null`
  - `canSend needs text or recording and not sending`
  - `appendTranscript separators`
  - `fallbackMailto contains typed text and no query string`
  - `contextFor strips query`
  - `errorCopy rate minutes rounding`: 61 s → 2 minutes.
  - `contactHelp calls FS.contact.open when present`
  - `existing buildHelpMailto tests still pass`
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full suite: `TZ=Pacific/Auckland node --test "tests/**/*.test.js"`, then `git checkout -- tests/fixtures/editor-to-prompt.body.json`. Expected: 0 failures.
- [ ] **Step 5: Mock check.** Serve the worktree on localhost and open `app-shell-preview.html`. Open the window, check its states at 1208×474 (nothing clipped), check that Send in mock mode shows the fallback, and check that the record button exists. Take a screenshot.
- [ ] **Step 6: Commit** `feat(contact): in-page Contact FieldSight window with voice note`, then open a PR to `dev`.

### Task 7: TEST verification and handover

- [ ] Merge the web PR to `dev` once its base is checked, and wait for Amplify (poll for a marker string unique to the new build).
- [ ] In the signed-in TEST tab, click Contact FieldSight and type a message marked `[automation test, please ignore]`. Press Send and expect the success toast. Confirm the S3 record exists and its `email_status` is `sent`.
- [ ] Record-button path: Chrome automation cannot grant the microphone, so this is the owner's morning test. Write the exact steps for it.
- [ ] Prod handover for the owner, numbered:
  1. The backend reaches prod in the next pipeline release, with the flag off.
  2. The ui release PR to `main` is merged by the owner.
  3. The owner sets `PROD_CONTACT_FIELDSIGHT=true` and runs the prod deploy.
