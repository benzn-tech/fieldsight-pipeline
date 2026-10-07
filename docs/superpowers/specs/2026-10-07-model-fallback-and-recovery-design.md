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
- Every model except the last gets exactly one attempt, with no same-model retry, because a down model stays down for minutes. The LAST model in the chain keeps the full existing retry ladder, so a deploy with no fallback behaves exactly as before.
- A caller-supplied deadline (Ask) is respected: no next model is tried if the time left cannot cover a call.
- Every switch logs `LLM_FALLBACK {"caller", "from", "to", "reason"}`. `LLM_USAGE` already names the served model.

**D4. A session whose extraction fails on every model is recorded and retried until it succeeds.**
- extract-session writes `extraction_pending/{sessionBase}.json` with:
  `{userFolder, date, sessionBase, request_key, attempts, first_failed_at, last_error, next_attempt_at, expedite: false}`.
- It deletes the marker on success.
- The scheduled extraction-backlog lambda also **re-drives**. For each marker whose `next_attempt_at` has passed, it re-puts the original request object (the existing S3 trigger). The backoff is 5, 15, 30 and 60 min, then every 60 min, **forever** until success.
- `ExtractionPending` is an EMF metric (count of markers older than 30 min, dimension Stage) with an alarm on the existing alert topic.

**D5. Email tells the truth.**
- "Nothing was captured for this recording." may be sent only when extraction **succeeded** and produced no items.
- If finalize gives up waiting and an `extraction_pending` marker exists (or the final extraction failed), the email says:
  > Your recording from {site} on {date} {time range} reached us safely. Our AI processing hit a temporary model error, so your notes aren't ready yet. Please contact FieldSight if you need them urgently — we'll email your notes as soon as processing completes.
- When a re-drive later succeeds for a session that got that email, the normal notes email is sent then, once.

**D6. The web says what is happening, and lets the user expedite.**
- org-api `GET /sessions/pending?date=YYYY-MM-DD` returns the caller's own pending sessions for the day, read from the S3 markers.
- The Timeline and Today pages show a banner per pending session:
  > Your recording ({time range}) was uploaded safely and won't be lost. Our AI model is temporarily unavailable — we're reconnecting automatically and will show your notes as soon as it recovers.
- The banner carries an **"Expedite"** button that calls `POST /sessions/{sid}/expedite`:
  - org-api writes `expedite_requests/{sid}.json` (S3 only; org-api has no outbound route beyond S3).
  - The S3 event invokes the backlog lambda, which re-drives at once and publishes to the alert topic: "Customer {name} ({company}) asked to expedite session {sid} ({date} {time})".
  - The button shows "We've been notified and are on it" and is disabled for that session for 10 minutes.

## Out of scope
Qwen as a fallback (0/4 measured). Changing the primary model.
