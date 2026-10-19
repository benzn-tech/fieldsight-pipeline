# Check done: the device says what is still open

Status: design (2026-10-20). Fable review 1: rework of the function section
(VPC egress, ASR key/budget, authorization, session lookup, audio-only
recording); all findings applied below. Sub-project 3, step A of the
"project assets / template digitisation / inspection gaps" batch. Step B
(`2026-10-16-checklist-gaps-design.md`, on prod) lists the gaps after the
recording is processed; step A says them aloud while the inspector is still
standing at the pour.

## Why

Owner, 2026-10-15: do not just fill the template after the analysis -- tell the
user which items were not done. Step B tells him tomorrow, on the web. By then
the formwork is closed. Step A tells him at the moment he says the check is
finished, when walking back to the item costs a minute.

## The constraint that shapes everything

At the moment he says "pre-pour check done", the last part of the check is not
transcribed yet:

| Stage | Delay (current settings, both envs) |
|---|---|
| Open 30 s chunk (`SettingsStore.kt:28`) not uploaded yet | 0-30 s |
| Batch window (`BATCH_WINDOW_SEC` 120, seal deadline 150) | up to ~150 s |
| ElevenLabs ASR per batch | ~6-9 s |
| Live extraction throttle (`MIN_REEXTRACT_INTERVAL_S` 90) + LLM | 90 s + |

So a live `inspection_windows` row with `end_source='said'` appears roughly 2-5
minutes after the words (estimate from the settings, not measured). Any design
that waits for the normal pipeline answers after he has left.

**Decision:** the gap pass does not wait for the pipeline. It reads what is
already transcribed, transcribes the session's uploaded chunks that no stored
transcript covers yet itself (ephemeral: used for this answer and discarded;
the batch ledger and `transcripts/` are not touched), and says how far its
evidence reaches. The open chunk on the device is the one part it cannot see;
a device hint (below) closes that hole, and without it the answer says
"as of half a minute ago".

Rejected:
- **Bypass batching for the tail** (write real transcripts early). It edits
  `batch_ledger` / `batch_seal`, whose failure mode is audio that silently
  vanishes (`lambda_transcribe._maybe_batch`, `batch_seal.py:200-238`). Paying
  for up to ~4 minutes of duplicate ASR per "check done" is cheaper than that.
- **Backend detects "done" in the live extraction and pushes to the device.**
  2-5 minutes late; he has moved on.
- **ElevenLabs voice agent.** TEST-only, no device client, 3-4 s first-token
  limit. Revisit if the voice agent becomes the device's Ask channel.

Correction to step B's spec: the "3-4 s first-token limit" there is the
ElevenLabs agent's. The device's two-phase Ask has a 20 s result long-poll and
a 60 s device cap from key release, with the restatement covering the wait.

## How it works

1. During a recording he holds the Ask key and says "pre-pour check done"
   (any "<check> done / finished / complete", or "what's left on the <check>").
2. The two-phase Ask start (`ask_voice_turn.start`, Ask line) transcribes the
   clip, recognises the `check_done` intent with a regex (model-free, same shape
   as `voice_web.detect_intent`), restates ("Checking the pre-pour check..."),
   and carries the intent and the spoken check name to the worker in the event,
   as `web_intent` is carried today -- the worker does not re-detect.
3. The worker, before its agentic/legacy branch, invokes **CheckGapsFunction**
   synchronously and speaks the returned `say` line through its TTS.
4. He covers the items, then may ask again.

### Is the "done" phrase in the recording?

It depends on the recording type, and this decides whether items covered after
the prompt count in step B's report:

- **Video recording:** `AskCore` borrows the mic (`AskCore.kt:102`,
  `MicHandover`), so the phrase is not in the recording. The final check window
  runs to the next check or the recording stop, and later coverage counts.
- **Audio-only recording** (the usual case for a pre-pour check): no borrow.
  Whether a second `AudioRecord` even works on this ROM is unverified
  (`MicSilenceMonitor.kt:40-42` says both). If it works and capture keeps
  running, "pre-pour check done" lands in the transcript, `time_inspections`
  closes the window with `end_source='said'`, and coverage after the prompt
  falls outside step B's report.

**Decision:** the device line makes Ask during an audio-only recording behave
like video -- pause capture for the Ask clip (the clip is private to Ask by the
two-phase design anyway). That gives one rule: the Ask phrase is never in the
recording. Until the device ships that, step A still answers correctly; only
step B's later report may miss items covered after a spoken "done" in an
audio-only recording. Changing the final window rule is not in this spec.

## CheckGapsFunction (this line)

Two new Lambdas, mirroring the repo's AskAgent (no VPC) -> RagSearch (in VPC)
split, because the VPC has no egress (`template.yaml` ~2406, ~2704, ~4524):

- **CheckGapsFunction** -- no VPC. S3 reads, ElevenLabs ASR, LLM. Timeout 35 s.
- **CheckGapsDbFunction** -- in the VPC, Aurora only, Timeout 10 s. One call
  returns the DB facts below. No egress needed.

Both under `Condition: CheckDoneGaps` (param default `false`;
`TEST_CHECK_DONE_GAPS` / `PROD_CHECK_DONE_GAPS`). The condition also gates the
Ask worker's invoke policy on CheckGapsFunction (W1001 pattern used for
RagSearch). Invoked only by Lambda invoke; no API route.

New code files: `src/check_done.py` (logic, pure where possible),
`src/lambda_check_gaps.py`, `src/lambda_check_gaps_db.py`. Shared files edited:
`template.yaml` (two functions, the param, the worker's invoke policy), the two
deploy workflows (param), `tests/unit/test_template_timeout_invariants.py`
(`SYNC_CALLEES` gets `AskVoiceWorkerFunction -> CheckGapsFunction` and
`CheckGapsFunction -> CheckGapsDbFunction`). No edit to `ask_voice_turn.py`,
`lambda_org_api.py`, `lambda_transcribe.py`, `lambda_extract_session.py` or the
batch modules.

### Request (from the Ask worker)

```json
{"caller_sub": "...", "check_name": "pre-pour", "spoken": "pre-pour check done",
 "pressed_at": 1760000000}
```
`caller_sub` is trusted because the worker took it from the API authorizer;
nothing else in the request names a user or session. There is no `session`
field: the function only ever looks at the caller's own live session.

### DB hop (CheckGapsDbFunction)

Input `{caller_sub, check_name, pressed_at}`. Returns:
- `user_id`, `company_id`, `folder_name` -- `get_user_by_sub`
  (`repositories/users.py`).
- `session` -- the caller's `meeting_session` row (same `user_id`) with the
  largest `last_segment_at` within 5 minutes of `pressed_at` (server time,
  written by `session_activity.process_transcript_key` for every chunk, silent
  ones included). Its `opened_at` gives the NZ date via `nz_time`. None ->
  `no_recording`.
- `templates` -- `inspection_windows.checklist_templates(conn, company_id,
  user_id)` (current version bodies).
- `live_windows` -- `inspection_windows` rows filtered by `company_id AND
  user_folder AND session`, never by session alone.

### Steps (CheckGapsFunction)

1. **DB hop.** Error -> `status: "error"`.
2. **Which template.** Strip the done-words ("done", "finished", "complete",
   "what's left on", "check") from `check_name` -- they are not in
   `inspection_match.GENERIC` -- then `inspection_match.match({"kind": name,
   "name": name}, templates)`. A spoken name that matches nothing ->
   `no_template` (never the live window: he named a check, and another
   checklist's gaps read aloud as his are worse than "not found"). Only when
   nothing is left of the name after the done-words ("check done") -> the
   session's latest live window with a template. No template, or one without
   checklist sections -> `no_template`, with the two closest names in the `say`
   line. (Revised in review, 2026-10-20.)
3. **Transcript so far.** All S3 keys are built under
   `users/{folder_name}/...` from the DB hop; nothing from the request.
   - Stored: `gather_session_segments` + `assemble_session_turns` for the
     session.
   - Last covered chunk index: over the stored keys, the max of
     `fieldsight_batch_map.members[].chunk_index` for batch keys (`_bn`), else
     the filename's `_c{NNNN}`. By index, not by presence: silent chunks never
     get a transcript (`DROP_SILENT_CHUNKS`).
   - New: raw chunks `users/{folder}/audio/{date}/..._sid{session}_c{NNNN}.wav`
     with index > last covered, newest 8 at most (4 min). Older uncovered audio
     -> `partial`. Video recordings (mp4 under `users/*/video/*`) are not
     transcribed here in v1 -> `partial`.
   - Each new chunk: a new bounded STT call modelled on
     `elevenlabs_utils.stt_short` -- single attempt, ~12 s timeout, no
     diarisation, the Ask key `ELEVENLABS_ASK_API_KEY` (never the recording
     pipeline's key: its credits running out would stop recordings). Run in
     parallel, futures read with `result(timeout=)`; a failed chunk ->
     `partial`.
   - The 2 s chunk overlap is left in: a duplicated phrase is harmless to
     `evidence_found`.
   - `covered_until` = last included chunk's filename time + WAV duration
     (header), converted to epoch via `nz_time`.
4. **Window.** Start from the live window if present; else the gap prompt
   finds the start of this check in the last 45 minutes of transcript.
5. **Gap pass.** One `call_llm(..., enable_thinking=False, deadline=12)` with
   `checklist.shape_sentence()` + `checklist.items_block(items)`, the check
   name and the transcript text. Its rows go to `checklist.rebuild(section,
   items, transcript_text, report_date=<NZ today>)` with the same
   `transcript_text` the quotes came from (stored + new, concatenated), so the
   evidence rule and the gap definitions are step B's exactly.
6. **Say line.** Built in code, not by the model, English:
   - none open: "All 18 items on the pre-pour check are covered."
   - else: "3 items open on the pre-pour check: <text 1>; <text 2>; <text 3>.
     And 1 marked No: <text>." Up to 5 items read aloud, then "and N more, see
     the app". Item text cut to its first clause, max ~12 words.
   - partial: prefix "As of a minute ago, ..." when `covered_until` is more
     than 20 s before `pressed_at`, or audio was skipped.

Log line: status, template id, item counts, timings -- never transcript text
(the two-phase spec's rule, with its test).

### Response

```json
{"status": "ok", "check": {"name": "Pre-pour", "template_id": "..."},
 "not_covered": [{"n": 3, "text": "..."}],
 "failed": [{"n": 7, "text": "...", "comment": "..."}],
 "items": 18, "covered_until": 1759999970, "partial": false,
 "say": "3 items open on the pre-pour check: ..."}
```
`status`: `ok | no_recording | no_template | error`. Every status carries a
`say` line. The function never raises to the caller.

### Budget

From invoke: DB hop <1 s; STT in parallel ~1-3 s per 30 s chunk (`stt_short`
measured 0.9-1.4 s on 3-14 s clips; 30 s chunks to be measured), bounded at
12 s; LLM bounded at 12 s. Target <20 s, hard stop 35 s (function Timeout),
inside the worker's 60 s; boto read timeout > 35 s. Measure on TEST over 5
runs before claiming it.

Cost: up to 4 min of ASR + one LLM call per ask; a few cents at most. Up to 8
parallel STT posts on the Ask key per ask -- note against the key's
concurrency limit.

## Interfaces to other lines (they own these; this spec only states them)

- **Ask line (`ask_voice_turn`, worker):** `check_done` intent regex in
  `start`, carried in the event with `check_name`; the hook sits before the
  agentic/legacy branch; restate, invoke, speak `say`; a missing function
  (flag off) is treated as `error`. Behind its own flag.
- **Device line (GrandTime):**
  - Two-phase Ask (branch `feat/ask-two-phase`, 0.7.22) on the device.
  - **Blocker to verify on a real device:** Ask during an audio-only
    recording (second `AudioRecord`). Then pause capture for the Ask clip in
    audio-only as in video (see above).
  - Hint: on an Ask press during recording, close and upload the open chunk
    early so step 3 sees the last 0-30 s. Without it the answer is `partial`.

## Not in this spec

- Pushing gaps unasked when the live extraction sees "done".
- Merging the covered items back into one report (step B's "covering a gap
  later"), or changing the final window rule.
- Transcribing video chunks.
- Writing anything to `inspection_windows` or `checklist_reports`: both
  functions are read-only apart from their log line.

## Error handling

- DB hop fails -> `error`.
- Ask ASR key missing -> stored transcript only, `partial`.
- ASR of a chunk fails or times out -> skip it, `partial`.
- LLM fails or passes its deadline -> `error`, say "I couldn't check the list
  just now; it will be in today's report."
- Template has no checklist sections -> `no_template`.

## Tests

- Unit (`check_done.py`): done-word stripping; template choice (spoken match,
  live-window fallback, none, sectionless); last-covered index from batch maps
  and plain keys, silent gaps; chunk selection (index > covered, cap 8,
  partial, video skipped); every S3 key under the caller's folder (a request
  can't steer it); `covered_until` from WAV header; `say` wording (none / some /
  >5 / failed / partial / no_template / error); `rebuild` gets the model rows
  (fake LLM) and the same transcript text, gaps returned unchanged; log line
  has no transcript text.
- Unit (DB hop): session pick by user + `last_segment_at` window, another
  user's open session never returned; `live_windows` filter includes
  `user_folder`.
- Template: CheckGapsFunction has no `VpcConfig`; CheckGapsDbFunction has;
  both under the condition; `SYNC_CALLEES` timeout ordering.
- Integration (PG): `checklist_templates` and the session pick against real
  rows.
- TEST: a recorded check with three items skipped, invoked with a synthetic
  `pressed_at`; the answer names exactly those three; latency over 5 runs.
- IAM simulation with real ARNs: CheckGaps -- S3 Get on `users/*/audio/*` and the transcript keys
  `gather_session_segments` actually reads, ListBucket with a prefix
  condition, invoke CheckGapsDb; worker -- invoke CheckGaps only; negatives
  denied.
