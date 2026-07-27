# ASR Provider Switch + Stop-Signal & Mis-Touch Continuity — TEST-push Design (2026-07-27)

**Status:** Design / for review. No code in this document. Scope = the concrete
near-term push to the **TEST** environment.

**Parent:** `2026-07-27-voice-timeliness-and-pipeline-enhancements-design.md`
(the full 5-thread design). This spec is the *buildable slice* of Thread 1 +
Thread 2's ASR swap, cut to exactly what the user greenlit on 2026-07-27.

**Related code/docs (reuse, don't duplicate):**
- `2026-07-21-alt-llm-asr-qwen-scribe-design.md` + `src/elevenlabs_utils.py` /
  `src/llm_utils.py` / `src/lambda_transcribe.py` (`ASR_PROVIDER` toggle) — the
  provider abstraction Soniox slots into. Lives on `origin/develop` (PR #116).
- `2026-07-23-session-continuity-design.md` — `session_base` assembly +
  `SESSION_GAP_MINUTES` (default 15). The mis-touch guard **extends** this.
- PR #143 — `session_id` on topics + `GET /api/org/sessions` (org-api). The
  close-session signal (§3) fits here.

---

## 1. Scope locked (user decisions, 2026-07-27)

| Decision | Locked value |
|---|---|
| Target env | **TEST only** (SAM `deploy.yml` / test stack). Not prod. |
| ASR engines | **Soniox + ElevenLabs**, runtime-switchable via a UI "button" (like the current ElevenLabs toggle). Transcribe stays as the untouched incumbent. |
| Qwen self-host ASR | **Dropped** — no GPU, API-only. Not in scope. |
| Doubao / DashScope ASR | **Deferred for sequencing, NOT compliance.** Per user 2026-07-27 the company imposes **no restriction**; the reason is simply to self-evaluate Soniox/ElevenLabs first. Doubao Seed-ASR (top-tier Chinese + code-switch, strong noise/accent) is a legitimate candidate to bring back in a later eval round — residency is *not* a hard blocker. |
| Text LLM | Qwen API key **now in GitHub secrets** → `LLM_PROVIDER=qwen` available; toggled separately, not the focus of this push. |
| Denoise | **OFF.** No enhancement stage built now; revisit during benchmarking. |
| Confirmation email | **Recorder only** (self-send). |
| Multi-vertical | **Company-level only** for v1 — vertical separated off the company; **no project override yet.** |

**Two build blocks in this push:**
- **Part A — ASR provider switch:** add Soniox; make the engine choice a runtime
  toggle so ElevenLabs-vs-Soniox benchmarking needs no redeploy.
- **Part B — Stop-signal + mis-touch/continuity guard:** the frontend emits an
  explicit "my segment ended" signal on stop, and a mis-touch (stop → immediate
  resume) must **not** wrap up the meeting or orphan later content from the prior
  context. This is the foundation the ≤2-min email (parent §3) later sits on.

---

## 2. Part A — ASR provider switch (Soniox + the ElevenLabs-style button)

### 2.1 Add Soniox (mirrors ElevenLabs exactly)

Per parent §4.6: new `src/soniox_utils.py` with `transcribe_segment(...)` +
`adapt_to_transcribe_json(...)`, one `ASR_PROVIDER == 'soniox'` branch in
`lambda_transcribe.py` (beside the elevenlabs branch, ~line 372). Async flow
(`POST /v1/files` → `POST /v1/transcriptions` model `stt-async-v5`,
`enable_speaker_diarization`, `language_hints:["en","zh"]`, `context.terms` →
poll → `GET …/transcript` → adapt tokens to the AWS `{results:{transcripts,items}}`
shape → `DELETE` cleanup). Env: `SONIOX_API_KEY`, `SONIOX_BASE_URL`,
`SONIOX_MODEL=stt-async-v5`, `SONIOX_LANGUAGE_HINTS=en,zh`. Downstream
(`transcript_utils`) untouched.

### 2.2 The "button" — from deploy-time param to runtime hot-swap

**Today:** `ASR_PROVIDER` is an env var / CFN param — switching engines needs a
redeploy. That's not a "button" and it's poor for A/B benchmarking.

**Change:** make provider selection a **hot-swappable S3 config**, exactly like
the existing `config/prompt_templates.json` pattern (CLAUDE.md: "Hot-swappable
from S3; Lambda falls back to inline defaults if missing").

- New `config/asr_provider.json` on the TEST bucket: `{"provider":"soniox"}`
  (values: `transcribe | elevenlabs | soniox`).
- `lambda_transcribe.py` reads it at invocation start; **env var `ASR_PROVIDER`
  is the fallback default** if the config object is missing. (Keep a short
  in-process cache with a few-seconds TTL so a hot 15-min sweep doesn't hammer
  S3 — same discipline as the `_cache.js` trap, [[fieldsight-ui-staleness-cache]].)
- **UI button (TEST only):** a small admin control writes `config/asr_provider.json`.
  Mirror whatever surface the ElevenLabs toggle uses; if none exists yet, a
  minimal segmented control (Transcribe / ElevenLabs / Soniox) on an admin/test
  panel is enough. Flipping it changes the next segment's engine — no redeploy.

### 2.3 Benchmark mode (optional, recommended for a real comparison)

A global toggle answers "which engine is live," but a clean ElevenLabs-vs-Soniox
comparison wants **the same audio through both**. Add an optional
`config/asr_provider.json` field `{"benchmark":["elevenlabs","soniox"]}`:
- When present, `lambda_transcribe` transcribes each segment with **every** listed
  engine and writes each to a provider-suffixed key
  (`transcripts/{user}/{date}/{base}.{provider}.json`); the **first** listed engine
  is the canonical one consumed downstream, the rest are for offline diff.
- Cost note: dual-run = 2× ASR spend on TEST audio only — acceptable for a bounded
  benchmark; turn it off after. Keeps your MER/PIER scoring on identical input.

**Decision to confirm (§7):** simple single-toggle button only, or also the
dual-run benchmark field? Recommend building both — the field is a few lines and
makes the comparison rigorous.

---

## 3. Part B — Stop signal + mis-touch / continuity guard

### 3.1 The requirement (user's words, restated)

1. The frontend/device, on **stop recording**, emits an explicit signal:
   "*this segment of mine has ended.*"
2. **Mis-touch protection:** if the user accidentally hits stop and recording
   **resumes immediately**, the meeting must **not** wrap up, and the continuation
   must **still join the prior context** — not be orphaned into a fresh session.

The core design move that satisfies both: **decouple "stop recording" from
"finalize + wrap-up + email."** Stopping the recorder is cheap and reversible;
finalizing is the expensive, one-way act — and it must be *deliberate or
grace-delayed*, never fired on a single tap.

### 3.2 Session state machine

Extend the `meeting_session` row (parent §3.1) with an explicit lifecycle:

```
                 segment arrives
   ┌───────────────────────────────────────────┐
   ▼                                            │
[open] ──stop signal──► [pending_close] ──resume within grace──┘
   │                          │
   │                          └── grace elapsed, no resume ──► [finalizing] ──► [sent]
   └── explicit "End & send" (deliberate) ─────────────────────► [finalizing]
```

- **open** — opened by the best-effort **record-start signal** (§3.7) or, if that
  never arrived, by the first segment landing in S3. Segments append to the same
  `session_base`.
- **pending_close** — stop signal received; a **grace timer** (default
  `STOP_GRACE_SECONDS = 30`, confirmed 2026-07-27, configurable) is running.
  **No email yet. Nothing wrapped up.** ("Grace" = the *mis-touch tolerance /
  forgiveness window* — a reversible buffer before the one-way finalize commits;
  call it 容错窗 in the UI copy if clearer.)
- **resume within grace** → back to **open**, same `session_base`, timer
  cancelled. The mis-touch is a no-op; later content joins prior context because
  the session was never closed.
- **finalizing** → Tier-2-lite + email (parent §3.2), reached only when the grace
  elapses with no resume, **or** the user makes a deliberate "End & send" action
  (§3.4).
- **sent** — email out; session closed.

### 3.3 Two layers of continuity protection (belt and suspenders)

**Layer 1 — grace window (primary mis-touch guard):** the `pending_close`→grace
mechanism above. A stop immediately followed by resume never leaves `open` in
practice, so nothing is orphaned and no premature email fires. Default **30 s**;
the email's latency floor becomes `grace + ~40 s finalize` ≈ 70 s — comfortably
inside the user's 1–2 min budget.

**Layer 2 — cross-chunk session assembly (MUST BE BUILT — not free today).**
⚠️ Correction (2026-07-28, after verifying `origin/develop`): the current model is
**"one source media file == one `session_base` == one extraction"**
(`session_scope.py` docstring; `gather_session_segments` groups strictly by
`session_base`). That binding does **not** join *separate* chunks — under 1-min
chunking every chunk is its own `session_base`/extraction, which is the
fragmentation problem, not a safety net. The gap-based assembly that *would* join
chunks within `SESSION_GAP_MINUTES` (and `SESSION_MAX_MINUTES`) lives **only in the
`2026-07-23-session-continuity-design.md` spec on `feat/session-continuity` — it is
NOT implemented on any branch.** So Layer 2 = the **new `session_id` assembly of
§8.3 decision 2**, which must be built (it can reuse `session_scope.SESSION_GAP_MINUTES`
= 15, which *is* real code on develop, but only for the #11 read-side export today).
Until it exists, Layer 1 (grace) is the *only* live mis-touch guard, and it already
covers the premature-email case; context-continuity across chunks depends on
building Layer 2.

**Interaction (once Layer 2 is built):** `STOP_GRACE_SECONDS` (30 s) ≪
`SESSION_GAP_MINUTES` (15 min) — deliberately. Grace guards the *email*; the
session-gap assembly (§8.3) guards the *context*.

### 3.4 Physical / frontend guards (what the user asked about)

Because the app device *is* where the stop happens, put a cheap guard there too —
belt-and-suspenders with the backend grace:

- **Distinguish Pause from End.** Make the primary control **Pause/Resume** (keeps
  the session `open`, no finalize). "**End meeting & send summary**" is a *separate,
  deliberate* action. A mis-tapped pause costs nothing.
- **Guard the End action** — a **long-press (≥1.5 s)** or a **one-tap confirm**
  ("End meeting and email the summary?"). Prevents a single accidental tap from
  finalizing. This is the "physical mechanism" the user asked for, in UX form.
- **Client-side debounce** — a stop event followed by a resume within a couple of
  seconds is coalesced client-side and never even sent as a close signal.
- **Voice-triggered Pause (optional, feasible).** Pause can be a **spoken command**
  even while the mic is busy recording — *provided the same app owns the mic stream
  and fans it to both the recorder and a lightweight on-device keyword spotter*
  (KWS: Picovoice/Porcupine or the platform speech API). Mic exclusivity only
  bites when *two different apps* contend; one app tapping its own buffer is fine.
  Caveats for a noisy site: pick a **long, uncommon wake phrase** (false-wake /
  missed-wake risk), and note the wake phrase itself lands in the recording
  (redact later if needed). Recommendation: build KWS **inside the recorder app**,
  not as a second app.

**Device classes:**
- **App / frontend recorder** — can send the explicit `close_session` signal and
  do the Pause/End UX above. Primary path.
- **RealPTT hardware** — may have no "close" API; it relies on the **idle-timeout**
  fallback (no segment for the gap window) → auto `pending_close` → grace →
  finalize. Same state machine, just no explicit signal.

### 3.5 The close-session signal (API)

Fits the org-api sessions surface (PR #143):

- `POST /api/org/sessions/{session_base}/close` — body `{intent: "pause"|"end"}`.
  - `intent:"end"` (deliberate End action) → `pending_close` with a **short** grace
    (or immediate finalize if you want zero delay on an explicit end — see §7).
  - a plain stop with no explicit end, or the idle-timeout, → `pending_close` with
    the full `STOP_GRACE_SECONDS`.
- Resume = the next segment upload for that `session_base` while `pending_close`
  → server flips it back to `open` and cancels the pending finalize.
- The grace timer: an EventBridge Scheduler one-shot (or a short-delay SQS
  message / Step Functions wait) scheduled on entering `pending_close`; on resume,
  the pending finalize is invalidated by a version/`status` check when it fires
  (fire → "is this session still pending_close and unresumed? if not, no-op").

### 3.6 Idempotency — never double-wrap, never double-email

- Finalize is **guarded by status + a version stamp**: it only proceeds if the
  session is still `pending_close`/`finalizing` for the version it was scheduled
  against. A resume bumps the version → the stale scheduled finalize no-ops.
- **One email per session.** If content genuinely resumes *after* an email was
  already sent (late resume beyond grace), do **not** orphan it: append to the
  same `session_base`, and send a single short **"updated summary"** follow-up
  rather than a second full email or a new session. (Confirmed 2026-07-27.)

### 3.7 Record-start signal (best-effort, confirmed 2026-07-27)

Symmetric with the close signal: pressing **record** (audio *or* video) fires a
best-effort `session_open` to the cloud, so the backend knows precisely when the
session began and can anchor timing/grace against real wall-clock rather than
inferring from the first upload.

- `POST /api/org/sessions/{session_base}/open` — `{started_at, kind:"audio"|"video"}`.
- **Best-effort by design.** Sites lose connectivity ("如果有信号"). If the open
  signal never arrives, the session still opens implicitly on **first segment
  arrival** (today's behaviour) — no regression. Same for the close signal: if it
  never arrives, the **idle-timeout** closes the session. Signals *sharpen* the
  timing; the S3-arrival path remains the floor.
- Value: a precise `opened_at`/`closed_at` pair makes the grace timer and the
  "was this a real gap or a mis-touch" judgement exact instead of inferred.

### 3.8 Email delivery (SES) + custom domain — answers "what sends the mail" & "CloudFront looks like phishing"

**Sending channel: AWS SES** (Simple Email Service, ap-southeast-2). Because the
recipient is the **recorder themselves** (an authenticated user whose email we
can verify), delivery is simple. Requirements:
- A **verified sender identity** (a domain identity, tied to the custom domain
  below — a from-address like `no-reply@app.fieldsightai.com`).
- New SES accounts start in **sandbox** (can only send to verified addresses).
  For self-send that can work by verifying recorders, but request **production
  access** to send freely.
- **SPF + DKIM + DMARC** on the sending domain — without these, mail gets flagged
  as spam/phishing. This is the *email-side* of the same "looks like a scam"
  problem the raw CloudFront URL has.

**Custom domain (fixes the phishing-looking `*.cloudfront.net`):**
- **Frontend:** Route 53 hosted zone + **ACM cert in us-east-1** (CloudFront
  requires us-east-1) + add the domain as a CloudFront *Alternate Domain Name*.
- **Blocker to resolve first — who controls `fieldsightai.com` DNS?** `www` hosts
  the company's main site, and the user notes the *company's* AWS is not under
  their control. Two paths:
  1. **Delegate a subdomain** (e.g. `app.fieldsightai.com` / `test.fieldsightai.com`)
     to the user's own Route 53 hosted zone — needs the company (DNS owner) to add
     NS records. Cleanest if they'll cooperate.
  2. **Register a fully-owned domain** (e.g. `fieldsightai.co.nz`, `fieldsight.app`)
     in the user's account — zero dependency on the company.
- The **same domain** serves both the app (CloudFront alias) and the email sender
  (SES identity), so one DNS decision unblocks both #6 and #7.
- This is a small infra sub-project, independent of the ASR/stop work; sequence it
  whenever the DNS ownership question is answered.

**"Can I just open a free `cloudfront.fieldsightai.com` directly on CloudFront?"**
Almost — but *not* purely inside CloudFront. The pieces (CloudFront alternate
domain name + ACM certificate) are **free** (only a Route 53 hosted zone is ~$0.50/mo).
But `cloudfront.fieldsightai.com` only *resolves* if a **DNS record is added in the
`fieldsightai.com` zone** pointing that subdomain at the distribution — and ACM
also needs a **one-time validation record** in that same zone. CloudFront can't
create those; only whoever controls `fieldsightai.com` DNS can. So:
- **If you can add DNS records to `fieldsightai.com`** (you control it, or the
  company adds two CNAMEs for you — one ACM-validation, one for the alias): free,
  done in ~30 min, and the URL stops looking like a scam.
- **If you can't get DNS access at all:** register your own domain (e.g.
  `fieldsightai.co.nz`, a few $/yr), put its zone in *your* Route 53 — zero
  dependency on anyone, same free CloudFront alias + ACM afterward.
There is no way to use *any* branded custom domain without a DNS record somewhere;
the raw `*.cloudfront.net` is the only thing that needs no DNS, and that's exactly
the scammy-looking URL you want to replace.

---

## 4. How it maps to the code

| Piece | Where | Change |
|---|---|---|
| Soniox client | new `src/soniox_utils.py` | mirror `elevenlabs_utils.py` (parent §4.6) |
| ASR dispatch | `src/lambda_transcribe.py` ~L372 | add `soniox` branch; read `config/asr_provider.json` (env fallback) |
| Provider button | UI admin/test panel + `config/asr_provider.json` (S3) | write the config; optional `benchmark[]` field |
| Session lifecycle | `meeting_session` row + org-api | add `status`, `version`, grace-timer; `POST …/sessions/{base}/open` + `/close` |
| Start/stop signals | frontend recorder + org-api | best-effort `session_open` / `close_session`; S3-arrival + idle-timeout fallback |
| Grace timer (30 s) | EventBridge Scheduler one-shot (or SQS delay) | schedule on `pending_close`; version-checked finalize |
| Voice-triggered Pause | recorder app (KWS) | optional; same-app mic fan-out to a keyword spotter |
| Continuity | existing `session_base` + `SESSION_GAP_MINUTES` | reused as Layer 2; no change |
| Finalize + email | new small lambda/step (parent §3.2) | Tier-2-lite + **SES** self-send; idempotent |
| Email deliverability + branding | SES domain identity + Route 53 + ACM (us-east-1) | custom domain for CloudFront + SPF/DKIM/DMARC; gated on DNS ownership (§3.8) |

TEST env only: SAM `deploy.yml`, test bucket, test org-api. Provider config +
Soniox keys as test-stack params/secrets. Qwen text key already in GitHub secrets.

---

## 5. Out of scope (explicit)

- Prod promotion of any of this (stays TEST until benchmarked).
- Qwen self-host ASR, Doubao, DashScope ASR.
- Denoise / voice-isolation stage.
- Participant emails (self-send only).
- Project-level vertical overrides.
- Multi-device fusion (parent Thread 3), checklist import (Thread 4) — separate.

---

## 6. Rollout on TEST

1. Land `soniox_utils.py` + dispatch branch + unit tests (mock Soniox HTTP), on a
   branch off `origin/develop` → TEST via `deploy.yml`.
2. Add the hot-swap `config/asr_provider.json` read + the UI toggle.
3. Benchmark: flip the button (or enable `benchmark:["elevenlabs","soniox"]`),
   run real site audio through both, score MER/PIER + noise/CS on identical input
   (reuse the ElevenLabs-vs-AWS dogfood harness already used this cycle).
4. Then build Part B (stop signal + grace state machine) — it's independent of the
   engine choice and is the foundation for the ≤2-min email.

---

## 7. Sub-decisions — RESOLVED 2026-07-27

1. **Grace length** `STOP_GRACE_SECONDS` — ✅ **30 s** (= the mis-touch tolerance /
   容错窗). Symmetric best-effort `session_open`/`close` signals added (§3.7).
2. **Explicit "End & send"** — ✅ **immediate finalize on a deliberate End**;
   full 30 s grace only on a plain/idle stop.
3. **Pause-vs-End UX** — ✅ **yes**, Pause primary + guarded End. Pause **may be
   voice-triggered** (KWS inside the recorder app, §3.4).
4. **Benchmark dual-run** — ✅ **build the `benchmark[]` field.** Soniox
   implementation itself handled by the user on a dedicated branch / separate
   session; this spec supplies the contract (parent §4.6).
5. **Late resume after email** — ✅ single **"updated summary"** follow-up.

**Still open (new, from 2026-07-27):**
- **DNS ownership of `fieldsightai.com`** (§3.8) — user: *TBD*, considering a
  `…​.fieldsightai.com` subdomain. Confirmed answer: a free `cloudfront.fieldsightai.com`
  is possible **only** if a DNS record can be added in that zone (control it, or the
  company adds two CNAMEs); otherwise register a fully-owned domain
  (`fieldsightai.co.nz` etc.). One decision unblocks both the app URL and SES.
  **Next action: find out who administers `fieldsightai.com` DNS and whether they'll
  add records / delegate a subdomain.**

---

## 8. Architecture impact — what the new paradigm changes in existing workflows

The user asked: *if I introduce this paradigm (1-min chunks + rolling + stop
lifecycle + provider switch), which existing workflows change?* Grounded in the
actual `origin/develop` pipeline. **The two things that ripple hardest are (a)
time-mapping under chunking and (b) session grouping** — both are current
BUG-09/11-class landmines that get a new layer.

### 8.1 The VAD → timestamp mapping file — picked up now (was under-specified)

**Finding:** the pipeline **already emits** `audio_segments/{user}/{date}/{basename}_vad_metadata.json`
(`lambda_vad.py` ~L772) with `segments_info[] = {s3_key, offset_start, offset_end,
duration, size_bytes}`. **But it is effectively write-only:** `lambda_keyframe.py`
(~L74) explicitly *skips* it, and time reconstruction
(`transcript_utils.extract_vad_metadata_from_filename` → used by
`lambda_report_generator`) reads offsets from the **filename** `off{X}_to{Y}`, not
from this file. So today, absolute time = `base_time(filename) + vad_offset(filename)
+ word.start(json)` — filename-only.

**Why chunking breaks filename-only timing.** Today VAD runs on the *whole
recording*, so `off{X}` is relative to the recording start (= the filename
timestamp) and the 3-term sum is correct. Under 1-min chunks, VAD runs *per chunk*,
so `off{X}` becomes relative to the **chunk**, and absolute time now needs a 4th
term — the chunk's offset from recording start:

```
absolute = recording_start + chunk_offset + vad_offset_within_chunk + word.start
```

Cramming `chunk_offset` into the filename too is brittle. **This is exactly where a
real, consumed mapping file belongs.** Two viable designs (pick in §8.3):

- **(T1) True-time chunk filenames** — each 1-min chunk is named with its *actual
  wall-clock start* (e.g. a chunk starting 12:19:34 → `..._12-19-34_...`). Then
  `base_time(filename)` already = chunk start, and the existing 3-term math in
  `transcript_utils` holds **with zero change**. Simplest — *if* the device clock is
  trustworthy (BUG-37: device `datetime.now()` is UTC; multi-device skew).
- **(T2) Authoritative manifest** — promote `_vad_metadata.json` from a byproduct to
  a **consumed** per-session manifest that records, for every segment,
  `{chunk_index, chunk_offset_s, vad_offset_s, absolute_start (server-cross-checked
  against the session_open signal / S3 receipt time)}`. `transcript_utils` reads
  *this*, not the filename, as the source of truth. Robust to clock skew and the
  foundation multi-device (parent Thread 3) will need anyway.

**Recommendation: do both** — T1 for the happy path (keeps `transcript_utils`
untouched), T2 as the authoritative cross-check the manifest *actually feeds*
(fixes the "written-but-ignored" gap; anchors on the best-effort `session_open`
timestamp §3.7 and the server receipt time, so a bad device clock can't silently
skew every timestamp). This is the concrete answer to "生成一个映射时间戳的文件".

### 8.2 Workflow-by-workflow impact

| Workflow / component | File(s) | Change | Magnitude |
|---|---|---|---|
| Device capture + upload | client + org-api `create_recording_upload_url` | one file/recording (client_uuid dedup) → **~1-min chunks each uploaded on close** + `session_open`/`close`; a **recording/session = many chunks**, not one row per chunk | **High** |
| VAD | `lambda_vad.py` | runs **per chunk**; `off` becomes chunk-relative; **promote `_vad_metadata.json` to a consumed timestamp manifest** (§8.1) | **High** (timing correctness) |
| Time reconstruction | `transcript_utils.normalize_transcript` (BUG-09/11) | **T1 → unchanged**; T2 → read manifest instead of filename | Low (T1) / Med (T2) |
| Session grouping | `lambda_extract_session.session_base_from_key` (`.split('_off')[0]`) | today groups VAD segments of **one source file** → one extraction. Chunking makes one meeting = **N chunks = N session_bases → N fragmented extractions/topics**. Must group by the **session** (SESSION_GAP / `session_id`, PR #143), not per-file basename | **High** |
| Transcribe | `lambda_transcribe.py` | +`soniox`, hot-swap `config/asr_provider.json`, per-chunk (already per-segment) | Medium |
| Rolling summary + email | **NEW** | `meeting_session` table, Tier-1 refine per chunk, Tier-2-lite finalize, SES self-send | Net-new |
| Stop lifecycle | **NEW** org-api + EventBridge + finalize | `open`/`close` signals, 30 s grace state machine, idempotent finalize | Net-new |
| Recordings row | `repositories/recordings.py`, org-api | one row per `client_uuid` upload → must represent a **session** spanning chunks (or link chunks to a session_id) | Medium |
| Keyframe | `lambda_keyframe.py` | currently skips `_vad_metadata.json`; if promoted (T2) it can **use** the manifest for topic→video mapping; per-chunk video | Low–Med |
| Report generator / meeting minutes | `lambda_report_generator.py`, `lambda_meeting_minutes.py` | mostly unchanged (read Aurora/transcripts); fast-email runs **parallel**, full report untouched; BUG-15 truncation + BUG-18 manifest still apply; consumes whatever §8.2-session-grouping produces | Low |
| Ingest / authority flip | `fieldsight-prod-ingest` | reads extractions → topics/findings; unchanged **iff** session grouping is fixed | Low |
| Ask-agent / RAG | `lambda_ask_agent.py` | unchanged now (streaming is a later thread) | None (now) |
| Frontend | `index.html` | Pause/End UX + record `open`/`close` signals + admin provider button; session picker (#11) already exists | Medium |
| Multi-vertical prompts/vocab | `prompt_templates.json`, `VOCABULARY_NAME` | company-level resolver (parent Thread 5) — separate track | Medium |

**Net:** 4 net-new components (rolling state, stop lifecycle, provider switch, fast
email), 2 high-risk *changes* to existing correctness-critical code (VAD timestamp
mapping §8.1, session grouping), and a handful of low/medium touch-ups. The report
generator, ingest, ask-agent and RAG stay essentially intact — the new paradigm is
**additive + a re-grouping**, not a rewrite.

### 8.3 Two pivotal decisions — RESOLVED 2026-07-28

1. **Chunk timestamping** — ✅ **both T1 + T2** (§8.1): true-time chunk filenames
   keep `transcript_utils` untouched, PLUS a *consumed* `_vad_metadata` timestamp
   manifest as the clock-skew-proof source of truth.
2. **Session grouping key** — ✅ **device-minted `session_id`** (§8.4), NOT the
   per-file basename. `extract_session` gathers by `session_id` instead of
   `.split('_off')[0]`. This is **new work** (the cross-chunk assembly is spec-only
   today, §3.3 correction), reusing `session_scope.SESSION_GAP_MINUTES` only as the
   inactivity constant.

### 8.4 Session identity & offline durability (device-minted `session_id` + store-and-forward)

Answers the user's 2026-07-28 questions: *does the session auto-bind an id? goes in
the DB? and how do start/end survive recording with no network + keep grace zones
ordered?*

**Device-minted `session_id` (the load-bearing choice).** On record-press the
**device** generates a UUID `session_id` (like the existing `recordings.client_uuid`)
and stamps it on **every chunk** of that session (in the key/metadata). Grouping is
then "all chunks sharing `session_id`" — deterministic and **offline-independent**:
even if *no* open/close signal ever reaches the server, chunks self-group. This
replaces timestamp/gap *inference* with a durable key that travels with the data.

**One DB row per session — `meeting_session`.** Keyed by `session_id`; holds
company/site/user, `opened_at`/`closed_at`, `status`, `rolling_summary`. Every
`topic` / `recording` / `extraction` carries `session_id` as an FK → future
tracking, audit, the #11 export picker, and multi-device meeting linking all key off
this one column. (Supersedes PR #143's older per-source-file `session_id` derivation.)

**Offline: signals become durable manifest records, not lossy live RPCs.**
- Device buffers chunks + a local **session manifest** (`session_id`, per-chunk
  monotonic `chunk_index`, `open`/`close`/`pause` markers + intent, device
  timestamps) in local storage; a **store-and-forward** queue uploads when
  connectivity returns. `open`/`close` are therefore *eventually-delivered records*,
  not fire-and-forget signals.
- **Ordering is arrival-independent:** chunks named `…{session_id}_{zero-padded
  chunk_index}…` so S3 lexical order = capture order; the server reconstructs by
  `(session_id, chunk_index)`, never by upload order.
- **Boundaries:** start = first chunk / `open` record; end = `close` record (carries
  pause/end intent); if `close` is missing (crash), the server infers close when the
  `session_id` sees no new chunk for `SESSION_GAP_MINUTES` **or** a new `session_id`
  begins.

**Grace zones stay ordered & unconfused under late/out-of-order arrival:**
- The grace/mis-touch test uses **device-relative deltas within one `session_id`**
  (`close@T` vs `resume chunk@T+Δ`, mis-touch iff `Δ ≤ STOP_GRACE_SECONDS`), **not**
  server receipt time — so it is immune to clock skew (BUG-37) and upload reordering.
- Each grace zone is **bound to a specific `session_id`** and anchored on that
  session's own `close` device-timestamp; sessions order by start time; `session_id`
  isolates them, so interleaved uploads of two sessions never cross grace zones.
- **Idempotency:** finalize is not scheduled until a session is *observed closed*; a
  late resume chunk bumps the session version → any stale scheduled finalize no-ops.

**Honest limitation:** the **≤2-min email requires connectivity at stop.** Recorded
offline, the confirmation email fires only after the device **syncs**. Session
*integrity* (grouping, ordering, no fragmentation, mis-touch protection) is fully
preserved offline; only the *timeliness* of the email degrades to "after sync."

---

With T1+T2, device-minted `session_id`, and the offline model set, the
"from-scratch, architecture-fitting" flow is pinned. Integrated changes land in:
`lambda_vad` (per-chunk VAD + promote `_vad_metadata` to consumed manifest),
`transcript_utils` (T2 manifest as time source, T1 keeps math), `lambda_extract_session`
(gather by `session_id`), `lambda_transcribe` (Soniox + hot-swap provider), plus the
new `meeting_session` table, session-lifecycle endpoints, grace timer, and the
SES finalize — all on a branch off `origin/develop`.
