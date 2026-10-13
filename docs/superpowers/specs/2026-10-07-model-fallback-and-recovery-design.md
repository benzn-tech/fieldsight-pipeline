# Model fallback and recovery: no recording is ever lost to a model outage (design, 2026-10-07)

Status: owner-approved 2026-10-07 ("顺序就这么做。推送").

## Incident (prod, 2026-10-07)

`meta/muse-spark-1.3-contributor` returned 502/503 for hours. A one-minute test2 recording was affected at every stage:
- **Extraction:** it failed 6 times in two minutes, then nothing retried it.
- **Email:** finalize waited 300 s and then emailed "Nothing was captured for this recording".
- **Web:** showed nothing.
- **Monitoring:** the backlog lambda saw the gap, but it only logs.

Emergency mitigation (owner, the same day): eight prod functions were switched to `openai/gpt-6-luna-pro` by hand. The next SAM deploy reverts that.

## Measured (2026-10-07, 4 past prod sessions, scratchpad `fallback-eval.md`)

| model | settings | result |
|---|---|---|
| `openai/gpt-6-luna-pro` | today's reasoning effort | 4/4 parsed, close to muse, $0.014–0.020 per session |
| `xiaomi/mimo-v2.6-pro` | thinking ON, `reasoning.max_tokens` cap | 0/4. The cap is ignored (MiMo has no thinking budget), so reasoning ate `max_tokens`. |
| `xiaomi/mimo-v2.6-pro` | **`reasoning: {"enabled": false}`** | 2/2 parsed, close to muse, $0.002–0.012, 38–194 s |
| `qwen/qwen3.8-flash` | today's settings | 0/4: reasoning hit the length limit, plus 429s |

## Decisions

**D1. One fallback chain, in the shared client, for every caller.**
- The order is `QWEN_MODEL` (muse today) → `openai/gpt-6-luna-pro` → `xiaomi/mimo-v2.6-pro`.
- It is configured by `LLM_FALLBACK_MODELS`, a comma list with this default. Setting it empty disables fallback.
- It lives in `llm_utils._call_qwen` on the OpenRouter path, so extraction, Ask, rolling summary, finalize, minutes, reports and the matcher all get it.

**D2. Per-model request profile.** The reasoning parameter belongs to the model, not to the caller:
- muse: unchanged (effort low/high, never `enabled:false`, which returns 400).
- luna: unchanged (effort as today).
- mimo: `reasoning: {"enabled": false}` always, with `max_tokens` = the caller's answer budget (no headroom needed).

Unknown models keep today's behaviour.

**D3. What moves to the next model.**
- **Moves on:** HTTP 5xx, 429, OpenRouter's "Provider returned error" (whatever status it arrives with, including 404), a timeout/connection error, a 200 with empty content, and `finish_reason=length` with empty content.
- **Does not move on:** any other 4xx, which is our bug and must surface.
- How hard each model is tried (final-review ruling 3; applies whenever the chain budget `LLM_CHAIN_BUDGET_SECONDS` or a caller deadline bounds the call):
  - **Primary:** up to 2 attempts on 429/5xx/timeout (the existing short backoff), so one blip on a healthy muse does not spill into luna. Each attempt's timeout is `min(LLM_HTTP_TIMEOUT, 0.6 x budget)`; that 0.6 share is also the primary's whole-ladder deadline, so a hung primary is not retried into the time luna and mimo need.
  - **Luna:** one attempt, `min(LLM_HTTP_TIMEOUT, 0.6 x what is left)`.
  - **Mimo (the last):** the full existing ladder, bounded by what is left.
  - With no budget and no deadline the primary keeps today's full ladder and timeout; later models get one attempt each.
- A caller-supplied deadline (Ask) is respected: no next model is tried if the time left cannot cover a call.
- Every switch logs `LLM_FALLBACK {"caller", "from", "to", "reason"}`. `LLM_USAGE` already names the served model.

**D4. A session whose extraction fails on every model is recorded and retried until it succeeds.**
- extract-session writes `extraction_pending/{sessionBase}.json` with:
  `{userFolder, date, sessionBase, request_key, attempts, first_failed_at, last_error, next_attempt_at, expedite: false, failure_kind}`.
- `failure_kind` is `"model"` when the call failed on every model (an outage), and `"parse"` when a model ANSWERED but the JSON was unparseable, truncated or malformed (final-review ruling 1). Both write a marker and still raise (Errors metric, S3 retries unchanged).
- It deletes the marker only when a final pass WROTE an extraction (ruling 2 / finding 8); a pass that returns nothing never clears one.
- The scheduled extraction-backlog lambda also **re-drives**. For each marker whose `next_attempt_at` has passed, it re-puts the original request object (the existing S3 trigger), through the full chain. The backoff is 5, 15, 30 and 60 min, then every 60 min; **after 24 h of failures, every 6 h** (rulings 4/5).
  - `model` markers are re-driven **forever** until success.
  - `parse` markers are re-driven at most **6** times, then `gave_up_at` is set and the re-driver stops. The marker stays: it is still counted in `ExtractionPending` (the alarm stays on) and still returned by `GET /sessions/pending` (the banner stays). Expedite allows exactly one more re-drive.
  - Per tick: at most **20** re-drives, expedited first, then oldest failure first; every new `next_attempt_at` gets 0-120 s of jitter; the loop stops early when the Lambda has under 20 s left (`context.get_remaining_time_in_millis`).
  - The marker re-write is an S3 conditional write (`If-Match` on the ETag read), one re-read on a lost race, and a marker cleared in between is not resurrected. An SDK without `If-Match` falls back to a plain put (T3's fallback).
- **No speech (ruling 2):** when a FINAL pass finds zero usable transcript turns, extract-session writes `extraction_empty/{sessionBase}.json` (`reason: "no_usable_speech"`) and clears any marker. That is a success-empty answer. A missing API key on a final is NOT that: it raises (alarm), writes no marker and clears none.
- **`promised_only` markers** (the backstop promised notes and nothing failed) expire after 24 h: the backlog deletes them and logs an ERROR (`EXTRACTION_PROMISE_EXPIRED`); they count in `ExtractionPending` until deleted.
- `ExtractionPending` is an EMF metric (count of markers older than 30 min, dimension Stage) with an alarm on the existing alert topic.

**D5. Email tells the truth.**
- "Nothing was captured for this recording." may be sent only when extraction **succeeded** and produced no items, which includes a final pass that recorded `extraction_empty/` for the session (no speech). A rolling backstop with no marker, no rows and no empty record says "notes are on the way" instead.
- finalize's race re-check (`recovered_after_error` on the final extraction) reads and writes the extraction with `If-Match`, one re-read on a lost race.
- If finalize gives up waiting and an `extraction_pending` marker exists (or the final extraction failed), the email says:
  > Your recording from {site} on {date} {time range} reached us safely. Our AI processing hit a temporary model error, so your notes aren't ready yet. Please contact FieldSight if you need them urgently — we'll email your notes as soon as processing completes.
- When a re-drive later succeeds for a session that got that email, the normal notes email is sent then, once.

**D6. The web says what is happening, and lets the user expedite.**
- org-api `GET /sessions/pending?date=YYYY-MM-DD` returns the caller's own pending sessions for the day, read from the S3 markers.
- The Timeline and Today pages show a banner per pending session:
  > Your recording ({time range}) was uploaded safely and won't be lost. Our AI model is temporarily unavailable — we're reconnecting automatically and will show your notes as soon as it recovers.
- The banner carries an **"Expedite"** button that calls `POST /sessions/{sid}/expedite`:
  - org-api checks the caller owns the session (marker `userFolder` == caller's `folder_name`; anything else, a missing marker or a `promised_only` one is 404) and stamps the marker itself: `expedite: true`, `expedite_requested_at`, `expedite_by`, `expedite_by_name`, `expedite_by_company`, `next_attempt_at` = now (conditional on the ETag where the SDK supports `If-Match`, else last-writer-wins). A second request within 10 minutes is 429 with `retry_after_s`. No `expedite_requests/` object and no new S3 notification.
  - The backlog lambda's 5-minute `PendingRedrive` run notices `expedite_requested_at` without `expedite_notified_at`, publishes to the alert topic ("Customer {name} ({company}) asked to expedite their recording {sid} ({date} {time range}). Attempts so far: {n}. Last error: {error}."), stamps `expedite_notified_at`, and re-drives at once as for any due marker. No topic on the stack: no notice. A failed publish stamps nothing and is retried next run.
  - `GET /sessions/pending` returns `time_range` ("HH:MM–HH:MM" NZ) from the marker, which extract-session fills from the transcript key names (null when unknown), and `expedited_at`.
  - The button shows "We've been notified and are on it" and is disabled for that session for 10 minutes.

## Final-review notes (2026-10-08)

- **Ruling 6 (validation):** TEST schedules are off (`EnableSchedules=false`), so `PendingRedrive` does not run there and no EMF metric is emitted. Validation invokes the backlog manually: `aws lambda invoke --payload '{"task":"redrive"}'`.
- **Ruling 9 (left as is):** `GET /sessions/pending` lists the whole `extraction_pending/` prefix (cap 500 GETs) per call. Accepted for now; revisit with date-keyed markers if the prefix grows.
- Not changed: ExtractSession's async `MaximumRetryAttempts` (review finding 4's second half) stays at the default; the re-driver now also has the 6 h rung and the per-tick cap.
- Known narrow gap: if the rolling backstop promised "notes on the way" a moment before extract-session concluded there was no speech, that recorder gets no follow-up (the `promised_only` marker is cleared by the empty record).

## Out of scope
Qwen as a fallback (0/4 measured). Changing the primary model.
