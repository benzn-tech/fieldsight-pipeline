# Contact FieldSight: a message form instead of mailto

Date: 2026-11-08 · Repos: fieldsight-pipeline (backend), fieldsight-ui (web) · Status: reviewed (Fable, 2026-11-08). Its blocking points are folded in; the two owner questions were decided on the owner's behalf: attach the audio, and include the unedited transcript when it differs.

## Why

"Contact FieldSight" in the left nav opens the person's mail app (`mailto:sales@fieldsightai.com`). On most site and office machines that means Outlook starts, the person re-types who they are, and many never send. The owner asked for an "interest form" style: one click, say or type the problem, press Send, and the team receives it. The account already knows who the person is.

## What the owner decided (2026-11-08)

- No mail app. A small window in the page.
- The account's email and name are known and never typed.
- The person can type, or speak. Speaking is **one flow**: record → the system shows the words → the person may edit → Send sends **both the text and the original audio**. ("voice to text" and "voice note" are merged; the owner agreed.)
- The team receives it at `sales@fieldsightai.com`.

## Goals

1. From any page, open a window, type or record (up to 2 minutes), send in one click.
2. The team gets one email per message: the text, a transcript if there was audio, a link to the audio, and who/where it came from.
3. Every message is also kept as a record in S3, so nothing depends on the email arriving.
4. Works on TEST tonight; prod follows the normal release and the owner turns the flag on.

## Non-goals

- No inbox or reply UI for the team. Replies happen by email (Reply-To is the person).
- No ticket tracking, status, or history for the person.
- No attachments other than the one recording (no screenshots, files).
- No Aurora table. (See "Deliberate omissions".)
- Mobile app: not in scope.

## Anti-goals (would be a failure even if it "works")

- A message is lost because transcription failed. Transcription is a convenience; the audio and any typed text must still send.
- Identity trusted from the request body. Email and Cognito `sub` come from the authorizer claims only.
- Another line's TEST behaviour changes (for example by flipping the global `EmailSender`).
- A search term or token from the page URL leaks into the email (keep the existing rule: route path only, no query string).

## Facts this design rests on (verified 2026-11-08)

- org-api runs inside the VPC with no outbound internet (CLAUDE.md BUG-36). It cannot call SES or a speech-to-text provider, so this cannot be an org-api route.
- `FieldSightApi` already routes `/api/{proxy+}` (ANY, Cognito default authorizer) to the Ask proxy function (`src/template.yaml` ~:6337). A more specific resource `/api/contact/{proxy+}` takes precedence in API Gateway, so a new function can own it without touching the existing routes.
- On the live TEST site, `window.FS_ENV.baseUrl` and `orgBaseUrl` are both `https://wdsgobb7b0.execute-api.../prod/api`. `scripts/env.example.js` shows them as different gateways, so the client must use `orgBaseUrl` explicitly, which is the gateway that hosts `/api/org/*` (`FieldSightApi`).
- Chrome 156 `MediaRecorder.isTypeSupported`: `audio/webm;codecs=opus` true, `audio/mp4` true, `audio/ogg;codecs=opus` false.
- Stack parameters read via CloudFormation: TEST `EmailSender=ses`, `SenderEmail=sales@fieldsightai.com`; prod `EmailSender=ses`. Both `AskSttProvider=elevenlabs`. TEST therefore delivers real email.
- `src/email_sender.py` has no Reply-To and is gated by the global `EMAIL_SENDER` read at import (:25, :34, :65-81), so this feature does **not** use it. SES is in sandbox; sending from and to `sales@fieldsightai.com` (a verified identity) works. In-tree SES grants use `Resource: '*'` (template :2427-2434).
- `src/elevenlabs_utils.py:225` `stt_short(audio_bytes, filename)` uses `ELEVENLABS_ASK_API_KEY` (the Ask key, not the batch recording key, whose credit pool must not be spent here). The key reaches functions as a plain env var from the NoEcho parameter `ElevenLabsAskApiKey` (template :798, :2501). Its timeout `ELEVENLABS_ASK_TIMEOUT_SECONDS` (read at import, :221-222) applies **per attempt**, and it makes two attempts with a 1 s sleep (:266-289).
- Globals (template :1821-1830) set no `VpcConfig`; a function without one is outside the VPC.
- The frontend request helper (`scripts/api/_fetch.js:60-61, 169-214`) defaults to a 10 s timeout and retries 5xx **and its own timeouts** up to 3 times. `rawRequest` honours `opts.baseUrl` (:220). `orgRequest` prefixes `/org`, so it cannot be used for `/contact`. Responses must be `application/json` (including 400/404/429), or `isJsonResponse` folds them into `_notFound`.
- Lambda presigned URLs are signed with temporary STS credentials and die when those expire (hours), whatever `ExpiresIn` says. In-tree presigns use 900 s.

## Design

### Backend: one new function, `ContactFunction`

Outside the VPC, Python like the rest, Timeout 28 s (below API Gateway's 29 s). Event: `RestApiId: !Ref FieldSightApi`, `Path: /api/contact/{proxy+}`, `Method: ANY`, default Cognito authorizer.

Routes:

1. `POST /api/contact/transcribe`
   - Body: `{ "audio": "<base64>", "mime": "audio/webm;codecs=opus" | "audio/mp4" }`.
   - Returns `200 { "text": "..." }`, or `200 { "text": "", "error": "unavailable" }` when the provider fails or times out. Never 5xx for a provider problem: the client then lets the person send the audio with no text.
   - Does not store anything.
2. `POST /api/contact/messages`
   - Body: `{ "id": uuid (client-generated), "text": str, "audio": base64|null, "mime": str|null, "transcript": str|null, "context": { "name", "company", "project", "page" } }`.
   - **Idempotent on `id`:** if a record with that id already exists for this `sub`, return 201 with the same id and send no second email. A retried or double-clicked Send never produces two emails.
   - `text` is what the person typed or edited. `transcript` is the unedited machine transcript (so the team can see what was changed).
   - Stores the record and the audio, sends the email, returns `201 { "id": "<uuid>" }`.
   - If storage succeeds and the email fails, it still returns 201 and the record is marked `email_failed` (logged at ERROR). The person's message is not lost, and the S3 record is what the team can recover from.

Identity: `sub` and `email` from `requestContext.authorizer.claims`. `context.*` is what the browser reports. The email labels it as such. Reply-To is the claim email.

Validation:

| Field | Limit |
|---|---|
| `text` | 4000 characters |
| `transcript` | 8000 characters |
| decoded `audio` | 3 MB, mime in the two allowed values |
| message content | at least one of `text` or `audio` |

Over-limit gets a 400 with a plain message. Check the base64 string length **before** decoding. Strip CR/LF from every `context` value and cap each at 200 characters; HTML-escape everything that goes into the HTML body. The client records with `audioBitsPerSecond: 32000` (Chrome's opus default is about 128 kbps, which would make a 2-minute clip about 1.9 MB) and stops at 2:00, so a clip is about 0.5 MB.

Rate limit: 10 messages per `sub` per rolling hour, and 20 transcribe calls per `sub` per hour (each call spends the Ask STT credit pool). Over the limit, return `429 { retry_after_s }`. Counting uses the S3 prefix (below) for messages. For transcribe counts, the builder chooses a mechanism that does not need a new table, for example a small counter object per `sub` per hour under the same prefix. Accept that a concurrent burst can exceed the limit by a few.

Storage (DataBucketName), all under one new prefix:
- `contact_messages/{sub}/{yyyy-mm-ddTHHMMSSZ}_{id}.json` holds the record: id, at, sub, email, context, text, transcript, `audio_key`, mime, `env`, and `email_status` (`sent` | `email_failed`).
- `contact_messages/{sub}/{...}_{id}.{webm|m4a}` holds the audio.

IAM for the function: Get/Put on `contact_messages/*`, ListBucket on the bucket with `s3:prefix` `contact_messages/*` (otherwise a missing key reads as 403; see the org-api IAM trap), and `ses:SendRawEmail` (`Resource: '*'`, as elsewhere in the template). Env: `ELEVENLABS_ASK_API_KEY: !Ref ElevenLabsAskApiKey`, `ELEVENLABS_ASK_TIMEOUT_SECONDS: '12'` (two attempts plus the sleep stay under the 28 s budget), `STAGE: !Ref Stage`, `SENDER_EMAIL: !Ref SenderEmail`, `CONTACT_FIELDSIGHT`, and the bucket name. A transcribe call also gets an outer deadline of about 25 s.

Email (decided 2026-11-08 on the owner's behalf: the audio is attached, not linked, because a Lambda-signed link dies within hours):
- Sent with `boto3` `ses.send_raw_email` directly from the new module, as a MIME message: To `sales@fieldsightai.com`, From `SenderEmail`, Reply-To the claim email. It does not go through `email_sender.get_sender()`, so the stack's global `EmailSender` has no effect and no other line's TEST behaviour changes. The only recipient is our own inbox.
- Subject: `[TEST] ` on TEST + `FieldSight message from {name} ({company})`, with CR/LF stripped.
- Body (plain text, plus an escaped HTML part):
  - "Message:" and the text the person sent
  - no machine transcript (changed 2026-11-09 by the owner: next to the edited text it read as two overlapping versions). A details line says it is in the S3 record; the recording is attached.
  - email, sub, project, page, env, time in NZ
  - the S3 record key
- Attachment: the audio file (`message.webm` or `message.m4a`), when there is one.
- Logs carry id and sub only. They never carry text, transcript or audio.
- Accepted: the TEST authorizer also accepts prod-pool tokens (template :6266-6270), so a prod user could post a `[TEST]` message. That is harmless, because it reaches our inbox.

Flag: Parameter `ContactFieldSight` (`'true'|'false'`, default `'false'`) → env `CONTACT_FIELDSIGHT`, wired in `deploy.yml` (`vars.TEST_CONTACT_FIELDSIGHT || 'false'`) and `deploy-prod.yml` (`vars.PROD_CONTACT_FIELDSIGHT || 'false'`). It must pass `tests/unit/test_template_workflow_parameter_wiring.py`. Off → every `/api/contact/*` route returns 404 `{ "error": "not_found" }`. TEST repo variable set to `true` once deployed. Prod stays off until the owner sets it.

### Web: a Contact window

- `scripts/composites/contact-window.js` (new): a `ContactHost` mounted once by AppShell, the same pattern as `SettingsHost` (`scripts/pages/settings.js:621`), exposing `window.FS.contact.open()`. The window uses `ModalOverlay` (size md).
- `scripts/api/contact.js` (new): `transcribe(blob, mime)` and `send(payload)` on top of `rawRequest`/`request` with `baseUrl: FS.api.orgBaseUrl` (the gateway that hosts `/api/org/*`, i.e. `FieldSightApi`), `retry: false`, `timeoutMs: 28000`. An empty `orgBaseUrl` (mock mode or misconfiguration) means mailto fallback. **Do not edit `scripts/api/_fetch.js`**: another line is changing it.
- Company name comes from `/org/me` (`me.company_name`, as Settings does, `settings.js:143`), fetched lazily when the window first opens. The project comes from AppShell's `activeSite`.
- `left-nav.js`: `contactHelp` opens the window. `buildHelpMailto` stays (tests use it, and it is the fallback).

Window contents, top to bottom:
1. "From: {name} · {email}" (read-only), and "About: {project}" when one is selected.
2. A text area: "What can we help with?"
3. A record control. Press to start (the browser asks for the microphone the first time), a timer counts up to 2:00 and stops by itself, and there is a stop button. While recording, a simple live level meter shows from an `AnalyserNode` (the owner wants it lively). After stopping: a small player for the clip, "Transcribing…", then the words are **inserted into the text area** (appended if text is already there), where the person can edit them. "Remove recording" discards it.
4. Send (primary) and Cancel. Send is enabled when there is text or a recording.

Behaviour:
- Recording format: prefer `audio/webm;codecs=opus`, else `audio/mp4`, else hide the record control (typing still works).
- Microphone denied or absent: show one line, "Microphone not available. You can type instead.", and keep the window usable.
- Transcription failed or unavailable: show "Couldn't turn the recording into text. It will still be sent as a voice note." Send stays enabled.
- Send shows "Sending…" and disables itself. The client generates the message `id` once per draft, so a second press or a retry is idempotent.
- Send success: close, clear the draft, toast "Sent. We'll reply to {email}."
- Send 404 (flag off) or a network error: keep the window open with the text intact, and offer "Email us instead". That opens the existing mailto, with the typed text prefilled in the body.
- 429: show "You've sent several messages recently. Please try again in N minutes."
- Closing the window while text or a recording exists asks nothing, but the draft is kept until the page reloads (the same as Settings).
- Context sent: name, company, project (the selected site name), and page (the route path without the query string).

## Deliberate omissions (and why)

- **No Aurora table or migration.** S3 holds the record. This avoids a migration-number race with other lines (0097 landed today), a VPC function, and an owner step on prod. If the team later wants a queryable list, a backfill from S3 is straightforward.
- **No org-api change.** That avoids the VPC limits and the shared IAM block other lines are editing.
- **No presigned upload, no bucket CORS change.** Audio goes as base64 in the JSON body (as the Ask voice route already does). A 2-minute opus clip is about 0.5 MB, well under Lambda's 6 MB request limit.
- **No S3 trigger or scheduled worker.** Those are hand-wired or disabled on TEST. The request itself sends the email.

## Risks and what to measure first

1. **ElevenLabs scribe on a 60–120 s webm/opus and mp4 clip.** Before building the client: format accepted, latency, and whether `stt_short`'s 10 s timeout needs to be larger (the function has a 28 s budget; use about 20 s). If webm fails and mp4 works, the client prefers mp4. Record the numbers in the plan.
2. **Deploy role.** A new function and role need `github-actions-fieldsight-deploy` permissions. Similar out-of-VPC functions exist, so this is likely fine. Check with `aws iam simulate-principal-policy` **with `--resource-arns`** for the real `fieldsight-test-*` ARNs before merging.
3. **Retention.** `contact_messages/` has no lifecycle rule. That is acceptable for now; it is a follow-up.
4. **Shared files.** `src/template.yaml` (parameter block, new resource) and the two workflow files are edited by other lines too. Keep additions in their own blocks, and rebase right before merge.

## Testing

Backend unit tests:
- route 404 when the flag is off
- identity taken from claims and never from the body
- validation limits
- the base64 length is checked before decoding
- a CR/LF in `context` does not reach the Subject
- the same `id` sent twice produces one email
- rate limit
- transcribe failure returns `200 { text: "", error }`
- record written before the email
- `email_failed` path still returns 201
- `[TEST]` subject prefix
- IAM: the function's policy contains the ListBucket prefix condition (a template test in the style of `test_template_org_api_media_iam.py`)
- workflow wiring test

Web tests:
- the api calls pass `retry: false` and the long timeout
- one `id` per draft
- the mailto fallback carries the typed text
- the context has no query string
- format choice order
- Send enablement
- error-state copy

What can be verified tonight on TEST, and what cannot:
- **Can:** a typed message end to end from the signed-in tab (the S3 record exists, the email reaches sales@); `/transcribe` and `/messages` with a prerecorded clip sent by `fetch` from the signed-in tab; the 404 fallback; the window's states in mock mode.
- **Cannot:** Chrome automation cannot answer the microphone permission prompt, so pressing the record button with a real microphone is the owner's morning test.

## Rollout

1. Backend PR to `develop` with the flag (default false). CI deploys TEST. Set the repo variable `TEST_CONTACT_FIELDSIGHT=true` and re-run the TEST deploy.
2. Web PR to `dev` (TEST). Verify as above.
3. Prod: the backend goes out with the next release (the flag stays off). The web change goes out in a ui release PR the owner merges. Then the owner sets `PROD_CONTACT_FIELDSIGHT=true` and deploys. Until the flag is on, the window's Send shows "Email us instead", so the web change is safe to ship before the backend flag.

## Left to the builder

- Module layout inside the function, and the counter mechanism for transcribe rate limiting.
- HTML email styling (plain text alone is acceptable).
- Exact visual design of the window and level meter, within the existing tokens.
