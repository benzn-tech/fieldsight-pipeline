# The confirmation email should arrive in a minute, not sixteen

**Status:** design v2 — v1 was reviewed and had five blocking defects, all of
which changed the design rather than the wording. What v1 got wrong is recorded
at the bottom, because three of the five were cases of assuming an existing
mechanism did something it does not.
**Date:** 2026-08-08

## The measurement this starts from

A real prod session, timed end to end:

| time | event |
|---|---|
| 10:48 | recording starts |
| 10:54:13 | last chunk |
| — | **15 minutes of nothing** |
| 11:09:4x | idle inferred, `close_intent = idle` |
| **11:09:48** | SES sends, in seconds |

**SES is not slow.** Sixteen minutes, fifteen of them a timeout waiting to
discover something the device already knew.

## Why the fast path never fires

`session_close(intent="end")` already finalizes with `grace = 0`
(`lambda_org_api.py:915`). Pressing stop is *supposed* to produce an email in
about a minute.

The signal does not arrive. `CaptureManager.fireSessionClose`
(`CaptureManager.kt:253`) is fire-and-forget inside `runCatching`, and the
client's `post()` ends in `.getOrElse { false }` — a failure is not returned to
the caller, let alone surfaced.

| date | device-sent close | inferred after 15 min |
|---|---|---|
| 07-30 | 4 | 2 |
| 07-31 | 2 | 3 |
| 08-03 onward | **0** | 8 |
| 08-08 | **0** | 3 |

**Not one deliberate close has landed since 03 August.**

### That step change is not yet explained, and the fix depends on it

Roughly half to zero is not the shape of intermittent network failure. Two
candidates, and they matter because one of them would defeat this design too:

- `freshIdToken() ?: return@launch` (`CaptureManager.kt:257`) is a **silent**
  exit. If token refresh regressed, the new signal — which also needs a token —
  fails identically.
- 03 August is when org-api was returning 5XX for 88% of requests under the
  concurrency limit (BUG-43). That cause is fixed and would not recur.

**Task zero of the plan is to distinguish these**, by adding a log line on the
token-null path and reading it from one real recording. Building the durable
channel on top of a broken token refresh would move the failure, not remove it.
`complete` happens to survive it because it runs inside a retry loop, but that
is luck, not design.

## The fix: the end signal rides the upload

Chunk upload is the one channel with a persisted row, a retry loop, and survival
across app restarts and a site with no signal. Precedent: `groupId` was moved
onto the upload for exactly this reason after an offline join was silently lost.

But "durable" is weaker for `complete` than for the PUT: `complete` retries
until the worker's budget is exhausted (`UploadWorker.kt:157-162`), after which
the audio is in S3 and the marker is gone. Better than fire-and-forget by a wide
margin; not a guarantee. **The idle path therefore remains, unchanged, as the
backstop.**

### What the device sends

`POST /recordings/{id}/complete` gains two fields, together:

```
"sessionEnded": true,
"sessionChunkCount": 37        // how many chunks this session produced, total
```

The count is what makes the close decidable. It is known on the device at stop
time and nowhere else.

### What the backend does with it

**The marker ARMS the close. It does not perform it.** The chunk carrying the
marker can arrive before earlier chunks — uploads retry independently and an
offline queue drains in whatever order it drains. Closing on arrival would
finalize a meeting whose transcript is still coming, and a short email is worse
than a late one.

Arming writes two new columns on `meeting_session` (migration):

| column | why it cannot be an existing one |
|---|---|
| `end_marked_at` | `pending_close` is cleared by `touch_segment` — see below |
| `expected_chunks` | the completion test; nothing else knows the total |

Then, on **every** `complete` for an armed session, and again on every sweep
tick: count that session's `recordings` rows with `uploaded_at IS NOT NULL`. If
the count has reached `expected_chunks`, close it with `intent = "end"` —
`mark_pending_close` plus `end_group`, exactly as `session_close` does.

## The three things that make this correct rather than plausible

### 1. Arming cannot be `pending_close`

`touch_segment` (`meeting_session.py:206-223`) treats any chunk arriving while a
session is `pending_close` as a **resume**: it clears `close_intent`, clears
`closed_at`, flips back to `open`, and bumps `version`.

So if arming meant `mark_pending_close`, the offline-backlog case would be:
marker lands → armed → the first backlog chunk arrives → **the arming is erased
by the very chunks it is waiting for** → fall back to 15 minutes. The bug,
restored, by the fix.

`end_marked_at` is a column `touch_segment` does not touch. That is its entire
job.

### 2. The completion test cannot be "no outstanding uploads"

`recordings` rows are created lazily, at upload-url time (`insert_pending`,
`lambda_org_api.py:634`), not when the audio is recorded. With a worker
concurrency of 2 and an arbitrary queue order, the marker chunk can be among the
first processed — at which point **the rows for the remaining chunks do not
exist yet**, "nothing outstanding" is true, and the session closes on a fraction
of the meeting. Irreversibly: once it is `finalizing`, `touch_segment` no longer
resumes it.

Counting **up to a total the device told us** has no such hole. A row that does
not exist yet simply has not been counted.

### 3. `end_group` waits for the close

Marking the group ended at *arming* time would make
`_adopt_group_from_upload` refuse a late joiner's backlog
(`lambda_org_api.py:723-725`, "this recording stays solo"), and the lead's
marker arriving before a joiner's first upload is the normal case after an
offline stretch. The joiner would be dropped from the meeting it was in.
`end_group` therefore runs at close, with everything else.

## What happens when the count is never reached

A chunk whose file was deleted, or whose retry budget ran out, leaves a row
permanently un-uploaded (`markMissing`, `UploadWorker.kt:128`). The count never
reaches the total and this path never closes the session.

**The idle inference catches it, at today's 15 minutes.** v1 proposed lengthening
the idle timeout on the grounds that it would become a rare backstop; combined
with this case, that would make the stranded-chunk session *slower than it is
today*. The timeout stays where it is. The worst case of this design is exactly
today's behaviour, never worse — which is the property that makes it safe to
ship before the app half is proven.

Splitting `IDLE_CLOSE_SECONDS` from `SESSION_GAP_MINUTES` is still worth doing,
because one number should not answer both "how long a pause means a different
meeting" and "how long until we assume the device is gone" — but it ships
**defaulting to today's value**, and as a proper CFN Parameter passed by *both*
workflows (the unwired-toggle trap: a value set only on a live Lambda is erased
by the next reconcile, silently).

## Why the idle threshold must NOT become 30 seconds

The request was "30s is enough". For the **stop** path that is already true and
this design delivers it: a deliberate end uses grace 0.

Applying 30 seconds to the **idle** threshold is a different change with a bad
failure mode. Chunks are 30 seconds long, so "no chunk for 30 seconds" is a
normal condition *during* recording:

- one slow upload mid-meeting → session closed → confirmation email sent while
  the meeting is still running, the exact regression PR #282 was written to stop
- offline recording → **no chunks arrive at all** → the session closes seconds in
  and emails a near-empty summary, then two hours of audio arrives

## Failure behaviour

| case | behaviour |
|---|---|
| marker arrives before the session row exists (solo uploads do **not** `ensure_open` — that block only runs for a `groupId`, `lambda_org_api.py:714-742`) | the marker handler calls `ensure_open` first, so a single-chunk session — the shortest one, and the one where "closes on the same request" matters most — is not the one case that fails |
| two final `complete`s race, each seeing the other outstanding | neither closes; the **sweep tick re-runs the same check**, which is why the test is not only on the complete path |
| a chunk never uploads | never closes by this path; idle catches it at 15 minutes |
| the session is already `finalizing`/`sent` | idempotent no-op, as `session_close` already is |
| the close bookkeeping raises | swallowed and logged; **the upload must still succeed** — a `complete` that 500s strands an uploaded recording and the retry loop re-sends the whole file (BUG-43's family) |
| device sends the marker, then records more | a new session; the closed one is not reopened |
| a caller marks a session that is not theirs | rejected — the `recordings` row is company-scoped, and the session it names must belong to the same company |

## Cost

The completion count runs on `complete`, which is a hot path — every chunk of
every recording — and `_group_ended_for`'s docstring
(`lambda_org_api.py:778-786`) argues at length that this endpoint must stay at
one primary-key lookup.

Matching `recordings` by `s3_key LIKE '%_sid…%'` is a leading-wildcard scan and
is not acceptable here. The migration adds a `session_id` column to `recordings`,
populated at `insert_pending` from the filename contract, with an index. Only
armed sessions run the count at all; an unarmed session costs one boolean check
on a row already fetched.

## Scope

**In:** the two `complete` fields, the two `meeting_session` columns and the
`recordings.session_id` column plus index (one migration), the count-to-total
rule on both the complete path and the sweep, deferring `end_group` to close, the
separated idle parameter at its current default with full workflow wiring, the
app persisting the marker and the count on the capture row, and the token-null
log line that Task zero reads.

**Out:** changing chunk size or the sweep interval (a 1-minute tick is already
small beside a 30-second grace); making `/close` retry — it stays as a
best-effort fast path, since it costs nothing and sometimes wins the race; any
change to what the email contains, which is explicitly deferred.

**Not covered, and worth saying plainly:** the marker rides the *deliberate end*.
Stops that come from a lost camera or a failure path (`CaptureManager.kt:101`,
`:110`, `:303`) still send only the fire-and-forget idle close and still wait the
full idle timeout. Those are the sessions nobody is waiting on an email for, but
the claim "the email now arrives in a minute" is not true of them.

## Verification

The 15-minute path was proved by watching a real session change state in the
database. The new one is proved the same way, plus the cases unit tests cannot
reach:

1. record, stop, confirm `close_intent = 'end'` and `closed_at` within a minute
2. record with the device offline, restore signal, confirm the session closes
   **when the backlog drains** and not before — the case the whole arm/count
   split exists for
3. the `recordings`-count query run against a real database through the Data API
   in a rolled-back transaction, because `FakeConn` does not run SQL and the
   NULL/count semantics here are exactly the shape that has passed 1,598 green
   unit tests while being wrong

## What v1 got wrong

Recorded because the pattern is more useful than the individual errors: **three
of the five blocking defects were assumptions that an existing mechanism did
what it appeared to do.**

1. arming as `pending_close` — did not follow the state through `touch_segment`,
   which erases it
2. "no outstanding uploads" — did not check when `recordings` rows are created
3. `end_group` at marker time — did not check what `_adopt_group_from_upload`
   does to a late joiner once a group is ended
4. two racing `complete`s — "a later `complete` will ask again" assumed a later
   `complete` exists
5. lengthening the idle timeout — reasoned about the common case and made the
   stranded-chunk case worse than today

The reusable lesson: reusing a mechanism obliges you to trace the whole lifecycle
of the state through every path that writes it, not to satisfy yourself that the
entry point looks right.
