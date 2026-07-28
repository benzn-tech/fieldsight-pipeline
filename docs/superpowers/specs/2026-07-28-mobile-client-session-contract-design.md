# Mobile / Client Session Contract — Design (2026-07-28)

**Status:** Design / for the mobile-client team. This is the **device side** of the
voice-timeliness paradigm. It defines exactly what the recorder app must produce so
the backend can group, time-map, and finalize sessions correctly — including
**offline** recording.

**Backend counterpart:** `2026-07-27-asr-switch-stop-continuity-test-design.md`
(esp. §3 stop/mis-touch, §8.1 timestamp mapping, §8.4 session identity & offline).
**Parent:** `2026-07-27-voice-timeliness-and-pipeline-enhancements-design.md`.

**The one rule:** everything the backend needs to reconstruct a session — identity,
order, boundaries, timing — must be **stamped on the data by the device** and
**buffered locally until uploaded**, never dependent on live connectivity. Live
API signals are a *best-effort optimization*, not a requirement.

---

## 1. What the device is responsible for (summary)

| # | Responsibility | Why |
|---|---|---|
| 1 | Chop each recording into **~1-minute chunks** (audio *or* video) | Fast per-minute upload → backend processes on arrival (rolling Tier-1) |
| 2 | Mint a **`session_id` (UUID)** on record-press; stamp it on **every chunk** | Offline-proof grouping key; no server round-trip needed |
| 3 | Stamp each chunk with a **monotonic `chunk_index`** and its **true wall-clock start** | Ordering (arrival-independent) + T1 time-mapping |
| 4 | Maintain a **local session manifest** (open/close/pause markers + timestamps) | Durable session boundaries that survive offline |
| 5 | **Store-and-forward** upload queue (buffer offline, retry, in order) | Recording where there's no signal |
| 6 | **Pause vs End** UX with a guard on End; optional voice-triggered Pause | Mis-touch protection (backend §3.4) |
| 7 | Best-effort `session_open` / `session_close` API calls | Sharpen server-side timing when online; optional |

---

## 2. `session_id` — device-minted, one per press-record→stop

- On **record-press**, the app generates a **UUID** and uses it as `session_id` for
  the entire recording until the session ends (End, or an idle/crash boundary).
- Format on the wire: **32 lowercase hex chars, no hyphens** (e.g.
  `9f8c1e2a4b6d47f0a1b2c3d4e5f60718`). *No hyphens* so it never collides with the
  backend's `YYYY-MM-DD_HH-MM-SS` timestamp parser (BUG-01).
- A **Pause → Resume keeps the same `session_id`** (it's still one session). Only a
  deliberate **End**, or a new record-press after a real gap, starts a new
  `session_id`.
- The same `session_id` goes into: every chunk key (§4), the local manifest (§5),
  and the best-effort open/close calls (§6).

## 3. ~1-minute chunking

- Split the live recording into segments of **~60 s** (configurable; last segment
  of a session is typically shorter — that's expected and fine).
- Each chunk is a self-contained, uploadable media file (audio: wav/m4a; video:
  mp4). Close and enqueue a chunk as soon as its ~60 s elapse — do **not** wait for
  the whole recording to finish.
- Chunk boundaries are the device's; the backend re-segments further with VAD.

## 4. Chunk file key convention (the wire format the backend parses)

Upload each chunk under the existing raw-media prefix, with these tokens:

```
users/{display_name}/{audio|video}/{YYYY-MM-DD}/
    {device}_{YYYY-MM-DD}_{HH-MM-SS}_sid{session_id}_c{NNNN}.{ext}
```

- `{HH-MM-SS}` — **the true wall-clock start of THIS chunk** (not the recording
  start). This is **T1**: it lets the backend compute absolute time as
  `chunk_start + vad_offset + word_offset` with no code change to the existing
  time math. Keep the `YYYY-MM-DD_HH-MM-SS` shape exactly (BUG-01).
- `sid{session_id}` — the 32-hex session UUID (§2).
- `c{NNNN}` — **zero-padded monotonic chunk index** within the session, starting
  `c0000`. Zero-padding makes S3 lexical order = capture order regardless of upload
  order.
- Example:
  `users/Ben_UCPK/audio/2026-07-28/Benl1_2026-07-28_14-03-00_sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718_c0007.wav`

**Clock caveat (BUG-37):** the device clock may be wrong (UTC vs NZ, drift). Stamp
your **best true local wall-clock**, but the backend does **not** fully trust it —
it cross-checks against the `session_open` timestamp and the S3 receipt time (§8.4
backend). Within a session, **relative** deltas (chunk N vs N+1, close vs resume)
are what drive grace/mis-touch, so absolute skew is tolerated. Still: set the clock
as correctly as you can, and send the device's UTC offset in the manifest (§5).

## 5. Local session manifest (the durable session record)

Maintain, in local storage, a manifest per session; upload it (and keep updating
the uploaded copy) via the store-and-forward queue. Suggested key:

```
users/{display_name}/{audio|video}/{YYYY-MM-DD}/{device}_{YYYY-MM-DD}_{HH-MM-SS}_sid{session_id}_manifest.json
```

Schema:

```json
{
  "session_id": "9f8c1e2a4b6d47f0a1b2c3d4e5f60718",
  "device": "Benl1",
  "display_name": "Ben_UCPK",
  "kind": "audio",
  "tz_offset_minutes": 780,            // device UTC offset (NZDT = +780); for skew correction
  "app_version": "…",
  "events": [
    {"type": "open",  "at": "2026-07-28T14:03:00+13:00"},
    {"type": "pause", "at": "2026-07-28T14:07:12+13:00"},
    {"type": "resume","at": "2026-07-28T14:07:20+13:00"},
    {"type": "close", "at": "2026-07-28T14:19:41+13:00", "intent": "end"}   // intent: "end" | "idle"
  ],
  "chunks": [
    {"index": 0, "key": "…_c0000.wav", "start": "2026-07-28T14:03:00+13:00", "duration_s": 60.0},
    {"index": 1, "key": "…_c0001.wav", "start": "2026-07-28T14:04:00+13:00", "duration_s": 60.0}
    // …appended as chunks close; re-upload manifest as it grows
  ]
}
```

- **`events`** carries the durable open/close/pause markers — this is what makes
  boundaries survive offline. `close.intent`: `"end"` (deliberate End → backend may
  finalize immediately) vs `"idle"`/absent (plain stop → backend applies the 30 s
  grace).
- **`chunks`** lets the backend detect missing/late chunks (gaps in `index`) and
  order deterministically.
- The manifest is **eventually consistent**: append + re-upload as the session
  grows; the backend reads the latest version. If the app dies mid-session and never
  writes a `close`, the backend infers close by inactivity (§8.4 backend).

## 6. Best-effort live signals (optional optimization)

When online, additionally call:
- `POST /api/org/sessions/{session_id}/open`  — `{started_at, kind}`
- `POST /api/org/sessions/{session_id}/close` — `{ended_at, intent: "end"|"idle"}`

These let the backend start the rolling summary and the 30 s grace timer *promptly*.
**They are not required for correctness** — if they fail (no signal), the manifest +
chunks reconstruct everything after sync. Never block recording or the UI on them;
fire-and-forget with the store-and-forward queue as the durable fallback.

## 7. Store-and-forward upload

- Buffer chunks + manifest in local storage; upload via a **retrying queue**.
- Upload order does **not** matter to the backend (it re-orders by
  `(session_id, chunk_index)`), but prefer in-order to shorten time-to-first-summary.
- Survive app restarts: the queue is persisted; on relaunch, resume uploading
  pending chunks/manifests.
- De-dup: if a chunk is re-enqueued after a partial upload, the same key overwrites
  idempotently (S3 PUT).

## 8. Pause / End UX + mis-touch guard (backend §3.4)

- **Pause is the primary, low-cost control** — keeps the session `open`, same
  `session_id`, writes a `pause`/`resume` event pair. A mis-tapped Pause costs
  nothing.
- **End is a separate, deliberate action** — guard it with a **long-press (≥1.5 s)
  or a one-tap confirm** ("End meeting and email the summary?"). Writes
  `close {intent:"end"}`.
- **Client-side debounce:** a stop immediately followed by a resume within ~2 s is
  coalesced locally and produces **no** close event (never even reaches the backend).
- **Optional voice-triggered Pause:** feasible if the *same app* fans its mic buffer
  to a lightweight on-device keyword spotter (Picovoice/Porcupine or platform KWS)
  while recording. Pick a **long, uncommon wake phrase** (noisy sites → false/missed
  wakes); note the phrase lands in the recording (backend can redact). Build KWS
  **inside the recorder app**, not as a second app (mic exclusivity).

## 9. What the device does NOT do

- No transcription, VAD, summarization, or emailing — all backend.
- No absolute-time authority — it stamps best-effort local time; the backend owns
  the authoritative absolute timeline (cross-checks skew).
- No session-grouping logic beyond stamping `session_id` — the backend assembles.

## 10. Acceptance checklist (device ↔ backend contract)

- [ ] Every chunk key carries `sid{32-hex}` + `c{NNNN}` + true `HH-MM-SS` start.
- [ ] `chunk_index` is monotonic, zero-padded, starts at 0000, no gaps in normal flow.
- [ ] One `session_id` per press-record→End; Pause/Resume keeps it.
- [ ] Manifest uploaded and kept current, with `events[]` (open/pause/resume/close+intent)
      and `tz_offset_minutes`.
- [ ] Recording works fully offline; chunks + manifest upload on reconnect, any order.
- [ ] End is guarded (long-press/confirm); quick stop→resume is debounced to no-op.
- [ ] (If built) live open/close calls are fire-and-forget, never block recording.
