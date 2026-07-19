# Site voice — real-time push-to-talk voice messages, scoped to a site

**Date:** 2026-07-18
**Status:** design finalized 2026-07-18 (physical key, deployment/rollout, and data-isolation decided — see "Decisions" below) — ready for writing-plans. Build spans pipeline (backend) + GrandTime (app).
**Goal:** A user at site X presses a physical talk key, records a short voice clip, and every member of site X who is online hears it within a few seconds. No Google Play Services on the device (MediaTek) → no FCM; the realtime channel must be self-hosted.

## Decisions (finalized 2026-07-18)
1. **Talk key = the SOS physical key, hold-to-talk.** The PTT key stays owned by "Ask agent". The SOS key currently maps only to stub actions (`SEND_SOS` / `TOGGLE_WARNING_LIGHT` — no real emergency behaviour exists), so it is free to repurpose at zero functional cost. Implemented by a new `SosKeySource` that mirrors the existing `PttKeySource`: subscribe to the raw `lolaage.sos.down` / `lolaage.sos.up` broadcasts and drive hold-to-talk directly, and exclude `lolaage.sos` from `F2spKeyEventSource.KEY_ACTION_PREFIXES` (the same "de-PTT-ify" treatment PTT already gets). Result: two symmetric, independent hold-to-talk keys — PTT→Ask agent, SOS→Site voice.
2. **Data isolation: Site voice is off-the-record.** Voice content never enters transcribe / daily-report / RAG. See "Data isolation invariant" — this is a hard requirement, not an incidental property.
3. **Rollout: backend-first, prod dark-launched behind a flag.** See "Deployment & rollout". The backend lands on `fieldsight-test` first (the app's WS client cannot be built/soak-tested without a live endpoint), reaches prod behind `PROD_ENABLE_SITE_VOICE=false`, and is enabled only once the app is device-accepted.
4. **Delivery persistence: keep a metadata-only `voice_messages` pointer row (no transcript) + 30-day S3 lifecycle.** Enables offline backfill + a small replay inbox; the DB row holds only `s3_key` + metadata, never content or transcribed text.

## Scope decision (the one that sets difficulty)
- **Chosen: "voice message" model** — record → upload clip to S3 → notify members over a realtime channel → each downloads + plays. Lands in ~2–5 s. This is the plan below.
- **Not chosen: "live PTT" model** — stream audio chunks as you speak, sub-second latency. Needs a jitter buffer, a live codec, and continuous streaming — materially harder (roughly doubles app effort) for a marginal UX gain. Only pursue if instant live talk becomes a hard requirement.

## Architecture (serverless, native to the existing AWS SAM stack)
Realtime channel = **API Gateway WebSocket API** (chosen over AWS IoT Core MQTT because it reuses the team's existing API-Gateway + Lambda + Cognito + SAM tooling; over polling because polling isn't real-time). Connection state lives in **Aurora** (not DynamoDB): the team already runs Aurora (prod+test share one cluster), the org-api Lambdas already connect to it in-VPC, and the scale (a few devices per site) makes Aurora's simple table ideal — one less service to add. The only thing DynamoDB gave "for free" (TTL auto-reap of stale connections) is replaced by reaping a connection when a fanout `@connections` POST returns `GoneException`, plus a low-frequency periodic sweep.

```
App (FGS holds a persistent WS)  ──$connect(idToken in handshake)──►  WS API ──(REQUEST Lambda authorizer verifies idToken via JWKS)──►  ws-connect Lambda ──► ws_connections (Aurora)
   SOS hold: record → S3 presigned PUT (dedicated voice/ prefix, NOT the recordings flow)
   → sendVoice {siteId, s3Key, durationS} ──► sendVoice Lambda (in-VPC):
        - authorize sender is a member of siteId
        - insert voice_messages row (history / offline backfill)
        - resolve recipients = ws_connections JOIN memberships on that siteId (all connected members, minus sender)
        - async-invoke voice-fanout Lambda with {connectionIds, payload}
   → voice-fanout Lambda (NON-VPC): POST payload to each connection via @connections mgmt API
        - GoneException connectionIds → async-invoke a tiny in-VPC reaper (or leave for the periodic sweep)
App receives {s3Key, sender, ts} over WS → GET presigned/download from S3 → play through speaker + cue tone
```

### Why the fanout is split VPC / non-VPC (BUG-36)
`sendVoice` must reach Aurora (membership check + insert + connection lookup) → it runs **in-VPC**. But the `@connections` broadcast is an `execute-api` HTTPS call to the API Gateway Management API, and the VPC has **no NAT and no `execute-api` interface endpoint**. An in-VPC Lambda calling `@connections` would **black-hole silently until timeout with zero log output** (BUG-36). Two options were considered:
- (a) Add an `execute-api` VPC interface endpoint / NAT — but an interface endpoint costs ~$7/mo/AZ × 3 ≈ **$20+/mo**, which blows the "~$1–3/mo" budget below.
- (b) **Split it, mirroring the existing SP-Ask pattern** (`AskAgentFunction` non-VPC + `VoiceAuditFunction` in-VPC): the in-VPC `sendVoice` only touches the DB and resolves the connection list, then async-invokes a **non-VPC `voice-fanout`** Lambda that performs the `@connections` POSTs. Runtime stays zero-outbound in-VPC.

**Chosen: (b).** It keeps the cost at ~$1–3/mo and reuses a pattern already proven in this repo.

### Backend (fieldsight-pipeline / SAM)
- **WebSocket API** — authored as raw `AWS::ApiGatewayV2::*` resources (`Api` with `ProtocolType: WEBSOCKET`, `Route`, `Integration`, `Authorizer`, `Deployment`, `Stage`). SAM has no first-class WebSocket type (unlike REST's `AWS::Serverless::Api`), and the template currently has no WebSocket resources at all — this is an all-new addition. Routes:
  - `$connect` — a **REQUEST-type Lambda authorizer** validates the Cognito idToken (JWKS signature + `exp`) and returns an IAM policy + `context {sub}`. **This is new code**: API Gateway WebSocket does **not** support the `COGNITO_USER_POOLS` authorizer that the REST API relies on, so the token cannot be validated by API Gateway itself the way `/api/org/*` routes are. The idToken rides in the handshake `Authorization` header (OkHttp can set handshake headers on native; query-string is the browser-only fallback and not needed here). `ws-connect` then upserts `connection_id → {user_id, company_id}` into `ws_connections`. (Site membership is looked up from `memberships` at fanout time, so the connection row stays minimal.)
  - `$disconnect` — `ws-disconnect` deletes the connection row.
  - `sendVoice` — body `{siteId, s3Key, durationS}`; verify the sender is a member of `siteId` (reuse the org-api `_allowed_site_ids` / `members_for_site` idiom); insert into `voice_messages`; resolve recipients = `ws_connections` JOIN `memberships` on that `siteId` (all connected members of the site, minus the sender); async-invoke `voice-fanout`.
- **`voice-fanout` Lambda (non-VPC)** — receives `{connectionIds, payload}`, POSTs to each via `execute-api:ManageConnections`; a `GoneException` for a connection → collect its id and async-invoke a tiny in-VPC reaper to delete the `ws_connections` row (this replaces DynamoDB TTL). Best-effort; the periodic sweep is the backstop.
- **`ws_connections` table** (Aurora): `connection_id (PK), user_id, company_id, connected_at`. In-VPC access, same pattern as the org-api Lambdas.
- **`voice_messages` table** (Aurora, beside `recordings` but deliberately separate): `id, company_id, site_id, sender_user_id, s3_key, duration_s, created_at`. **No transcript / content column** — pointer + metadata only.
- **`POST /api/org/voice/upload-url`** — a **dedicated** presigned-PUT endpoint added to `lambda_org_api` that writes clips under a new `voice/` S3 prefix and (on upload) records a `voice_messages` row. It does **NOT** reuse `create_recording_upload_url` / the `recordings` table — `recordings` feeds the extraction/ingest pipeline (`site_for_media` → `lambda_item_writer`), so routing voice through it would violate the data-isolation invariant. (This also sidesteps widening the `recordings.kind` CHECK constraint.)
- **`GET /api/org/sites/{siteId}/voice?since=<ts>`** — recent/missed messages for reconnect backfill; ACL by membership (mirror the existing sites ACL). Added to `lambda_org_api` (REST, in-VPC, existing Cognito authorizer — **no new auth work**).
- **Migration `0015`** — creates both `ws_connections` and `voice_messages` (one file is fine; migrations here are not one-table-per-file). Applied by `fieldsight-{test,prod}-migrate` after `sam deploy`. Because test and prod **share one Aurora cluster and one `schema_migrations` ledger**, the tables physically land at test-deploy time; the prod invoke is a no-op. Both tables are purely additive with no existing readers — safe.
- **IAM / VPC**: the in-VPC Lambdas (`ws-connect`, `ws-disconnect`, `sendVoice`, reaper) reuse the existing `VpcConfig` (`DbSubnetIds` + `${DbStackName}-LambdaSG`) and deploy-time-injected `PGPASSWORD` (BUG-36); they need `VPCAccessPolicy` + `lambda:InvokeFunction` for the async hop. The non-VPC `voice-fanout` needs `execute-api:ManageConnections` only. The upload endpoint needs S3 presign (already granted for the data bucket). **No DynamoDB. No new VPC endpoint / NAT.**
- **Periodic stale-connection sweep** (optional, low-frequency) — a small scheduled in-VPC Lambda; belt-and-braces beside GoneException reaping.

## Data isolation invariant (off-the-record)
Site voice content **must never** enter transcribe / daily-report / findings / RAG. This is guaranteed structurally, and written here so it is not accidentally broken later:
- **Dedicated `voice/` S3 prefix, non-overlapping with every S3 event trigger.** The transcribe→report→findings→RAG chain is driven by S3 events on `users/*/audio/*` and `users/*/video/*`. Clips live under `voice/{…}`, which matches **no** event filter → no VAD, no Transcribe job, no transcript, no report, no finding, no embedding. (Enforces BUG-13: output prefix must never overlap a trigger prefix — confirm `wire-s3-events.sh` never lists `voice/`.)
- **Dedicated upload endpoint + `voice_messages` table; never `recordings`.** `recordings` is read by the ingest / item-writer pipeline; the separate `voice/` endpoint + `voice_messages` table are read by **nothing** in the summary path (not report-generator, ingest, item-writer, rollup, or rag-search).
- **No transcription is ever triggered.** Clips are 16 kHz mono WAV (transcribe-ready) only because that is what the existing recorder produces — format does not imply processing; nothing invokes Transcribe on them.
- **Retention.** `voice/` clips get a 30-day S3 lifecycle expiry (adjustable 7/14/90). `voice_messages` rows are pruned in step. Content lives only in S3, short-term.
- **Future opt-in (explicitly out of scope now).** If transcription of Site voice is ever wanted, it would be a separate, opt-in Lambda over the `voice/` prefix — deliberately not built here (the requirement today is "not in RAG/DB summary/transcript").

## Cost (AWS, at your scale — a few sites, a handful of devices each)
Pay-per-use; **no idle/fixed cost** — the main reason API Gateway WebSocket beats a self-hosted MQTT broker or an always-on EC2 WebSocket server (those cost a fixed ~$15–30/mo even when idle). The VPC/non-VPC fanout split (above) avoids the ~$20+/mo an `execute-api` interface endpoint would have added.
- **API Gateway WebSocket**: $1.00 / million messages + $0.25 / million connection-minutes. ~10 devices online ~10 h/day ≈ 180k connection-min/mo (~$0.05) + a few tens of thousands of messages/mo (~$0.02). → **~$0.10/mo.**
- **Lambda** (authorizer / connect / disconnect / sendVoice / fanout / reaper / backfill): thousands of tiny invocations/day → **~$0–1/mo** (largely free tier).
- **Aurora**: already running (shared prod+test); the two small tables + light queries add negligible IO/ACU → **~$0 marginal.**
- **S3** (voice clips, ~150 KB–1 MB each, 30-day lifecycle): storage bounded at ~1–1.5 GB steady-state + small PUT/GET + egress to devices → **~$0.2–1/mo.**
- **Total marginal: ~$1–3/month** at your scale — effectively negligible; the Aurora cluster (already paid) dominates. Even at 100× the traffic it stays in the low tens of dollars/month.

### App (GrandTime)
- **New `SosKeySource`** — mirrors `PttKeySource`; raw `lolaage.sos.down`/`.up` → hold-to-talk. Exclude `lolaage.sos` from `F2spKeyEventSource.KEY_ACTION_PREFIXES`.
- **New `SiteVoiceManager`** (+ optional pure `SiteVoiceCore` FSM) — sibling to `AskManager` inside `CoreService`; orchestrates hold→record→upload→`sendVoice`, receive→download→play, and reconnect backfill. Mirrors `AskManager`/`AskCore`.
- **Persistent WebSocket client in `CoreService`** — the foreground service (kept alive by the battery-optimization exemption shipped 2026-07-18) holds this connection. Uses OkHttp's `WebSocket` (already a dependency — **no new Gradle dep**), authenticating with a fresh Cognito idToken (`freshIdToken`, including the 2026-07-17 expiry fix) on the handshake; exponential-backoff reconnect on drop; ping/keepalive; reconnect on network-available. Note: this is the **first on-device WebSocket in this repo** (SP-Ask's WS is server-side only) — real-device soak is required.
- **Send**: SOS hold → record a short clip (reuse `AskRecorder` → `AudioRecorder`, WAV 16 kHz mono) → `POST /api/org/voice/upload-url` presigned PUT → `sendVoice {siteId = AppState.selectedSite.value?.id, s3Key, durationS}`.
- **Receive**: on a WS message → download the S3 clip (presigned GET) → play through the speaker with a distinct cue tone (reuse `AskPlayer` + a new `AskSounds` "received" tone); queue into a small "Site voice" inbox for replay.
- **Mic arbitration** (mirror the existing Ask rules): refuse + "busy" cue while video is recording; Ask and Site voice are mutually exclusive (one talk at a time); a clip arriving while the user is talking is queued and played after; the sender never hears its own message (excluded at fanout).
- **Offline backfill**: on (re)connect, `GET …/voice?since=<lastSeenTs>` → play/queue what was missed while disconnected.
- **UI (v1)**: minimal — a "Site voice" status dot (WS connected/disconnected) + a receive cue tone + a short "recent" inbox list (last N clips, replayable — data is free from `voice_messages`/backfill). No long-term history UI.

## Deployment & rollout (backend-first, prod dark-launched behind a flag)
1. **Backend → test.** `pipeline` `feature/site-voice` → merge `develop` → `deploy.yml` auto `sam deploy --config-env test` + invoke `fieldsight-test-migrate` (creates `ws_connections` + `voice_messages` on the shared Aurora). Verify end-to-end with `wscat` + a script (connect/authorizer/sendVoice/fanout/backfill/reap) — **no app required**.
2. **App → test.** GrandTime `feature/site-voice`, **dev flavor** → points at the test stack. **Real-device soak** — backgrounded, screen-off, reconnect, battery. (The persistent-WS-on-OEM-ROM liveness is the #1 risk; the FGS + battery exemption are the mitigation, but this must be proven on-device.)
3. **Backend → prod (dark).** Merge `main` → `deploy-prod.yml` (GitHub `environment: production` reviewer gate) + invoke `fieldsight-prod-migrate` (no-op, shared ledger). Gate the WS API + routes behind a new repo variable **`PROD_ENABLE_SITE_VOICE=false`** (mirror `PROD_ENABLE_SCHEDULES` / `PROD_AUTHORITY_FLIP`). Purely additive, WS idle cost ~$0, zero crossover with existing paths.
4. **Enable + ship app.** Flip `PROD_ENABLE_SITE_VOICE=true`; ship GrandTime **prod flavor** (`main` + tag) to the field devices.

**Coordination note:** `docs/superpowers/plans/2026-07-18-phase3-graded-roles.md` (in-flight, dated later the same day) touches roles/permissions; `sendVoice`'s ACL depends on membership — confirm no collision before starting the backend plan.

## Difficulty / effort
- **Backend: Medium, ~1–1.5 weeks.** WebSocket API (raw ApiGatewayV2 resources) + **new WS Lambda authorizer** (the one genuinely new-in-repo piece) + `ws-connect`/`ws-disconnect`/`sendVoice` (in-VPC) + `voice-fanout` (non-VPC, the BUG-36 split) + tiny reaper/sweep + `voice_messages`/`ws_connections` migration + dedicated `voice/upload-url` + backfill endpoint + IAM. ~5–6 handlers (up from the earlier "3–4" estimate). Still the textbook serverless-WebSocket-chat pattern; the authorizer + the VPC split are the only non-boilerplate parts.
- **App: Medium, ~1–1.5 weeks.** `SosKeySource` + `SiteVoiceManager`/`SiteVoiceCore`, the persistent WS client (auth, reconnect/backoff, heartbeat, lifecycle inside the FGS — first WS on-device), record→upload→send, receive→download→play, mic arbitration, offline backfill, and the small inbox UI + cue tones.
- **Integration + multi-device on-device testing** (backgrounded, screen-off, reconnect, battery): ~few days.
- **Total: ~2.5–3 weeks** of focused cross-repo work. **Medium** overall — standard, well-trodden tech; no research risk.

### Main risks (all manageable)
- **Connection liveness on the OEM ROM** — a persistent WS must survive backgrounding/Doze. Mitigated by the FGS + battery-optimization exemption already shipped; still needs real-device soak testing (and this is the first WS on-device here).
- **WS Lambda authorizer correctness** — new JWKS-based idToken verification; must match the pool(s) the REST API trusts (`OrgUserPoolId`).
- **Reconnect + missed-message backfill correctness** — the `voice_messages` + `since` endpoint covers gaps.
- **Battery** — a persistent connection costs power; ping interval + reconnect strategy need tuning; bodycams are charged daily, so likely fine.
- **Fanout at scale** — `@connections` POST is per-connection; fine for a site's handful of members, not a concern at this scale.

## Out of scope (for the first cut)
- Live sub-second streaming PTT (the "live PTT" model above).
- Any transcription / report / RAG indexing of voice content (see Data isolation invariant) — off-the-record by design.
- Read receipts / typing / presence beyond "connected".
- Cross-site or company-wide broadcast (site-scoped only).
- Push wake when the app/FGS is fully killed (no FCM) — a killed device won't receive until the FGS is running again; the battery exemption + boot-autostart keep it running.
