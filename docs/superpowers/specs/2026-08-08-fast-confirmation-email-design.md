# The confirmation email should arrive in a minute, not sixteen

**Status:** design, not yet built
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

**SES is not slow.** Sixteen minutes, of which fifteen are a timeout waiting to
discover something the device already knew.

## Why the fast path never fires

`session_close(intent="end")` already finalizes with `grace = 0`
(`lambda_org_api.py:915`). Pressing stop is *supposed* to produce an email in
about a minute — sweep tick plus send.

The signal does not arrive. `CaptureManager.fireSessionClose` is documented
"best-effort — fire-and-forget", wrapped in `runCatching` with no retry, and the
client's `post()` ends in `.getOrElse { false }`, so a failure is not even
returned to the caller, let alone surfaced.

One network blip costs fifteen minutes, silently. The data shows it is not a
blip:

| date | device-sent close | inferred after 15 min |
|---|---|---|
| 07-30 | 4 | 2 |
| 07-31 | 2 | 3 |
| 08-03 onward | **0** | 8 |
| 08-08 | **0** | 3 |

**Not one deliberate close has landed since 03 August.**

## The fix: the end signal rides the upload

Chunk upload is the one channel that is already durable — it has a persisted row,
a retry loop, and it survives the app being killed and the site having no signal.
The close call has none of that. So move the signal onto the channel that works.

This is not a new idea in this codebase. `groupId` was moved onto the upload for
exactly this reason: `/open` is fire-and-forget, and a group joined in a shed
with no signal was silently lost. The lesson was written down as *"adding a
column 'so it can be recovered later' requires code that reads it, or the reason
is just a comment"*. Same shape, same fix.

### Shape

- The device persists `sessionEnded` on the capture row when recording stops —
  on the row, not in memory, because the upload may run hours later.
- `POST /recordings/{id}/complete` accepts `sessionEnded: true`.
- The backend does what `session_close(intent="end")` does: `mark_pending_close`
  + `end_group`, idempotent, failures swallowed.

Solo uploads that send nothing keep a byte-identical request and response — the
same rule `groupEnded` already follows on this endpoint, and most of the traffic
here has no part in this feature.

## The subtlety that decides whether this works: arrival order

**The chunk carrying the end marker can arrive before earlier chunks.** Uploads
run concurrently, retry independently, and after an offline stretch the queue
drains in whatever order it drains. Closing the session the moment the marker
lands would finalize a meeting whose transcript is still arriving — and the
email would go out short, which is worse than late.

**Rule: the marker arms the close; it does not perform it.** On every `complete`
for a session that is armed, ask whether that session still has any recording row
with `uploaded_at IS NULL`. If it does, do nothing — a later `complete` will ask
again. The last upload to finish is the one that closes the session.

This falls out correctly in the cases that matter:

- **normal stop** — the marker is on the last chunk, nothing is outstanding, the
  session closes on that same request
- **offline backlog** (BUG-43's two-hour case) — the session closes when the
  backlog drains, which is the first moment the transcript is actually complete
- **a chunk that never uploads** — the session never closes by this path, and the
  idle inference below still catches it

## Why the idle timeout must NOT become 30 seconds

The request was "30s is enough, make it as fast as possible". For the **stop**
path that is already true and this design delivers it: `STOP_GRACE_SECONDS` is
30, and a deliberate end uses grace 0.

Applying 30 seconds to the **idle** threshold would be a different change with a
bad failure mode. Chunks are 30 seconds long, so "no chunk for 30 seconds" is a
normal condition *during* recording. It would mean:

- one slow upload mid-meeting → session closed → confirmation email sent while
  the meeting is still going, which is the exact regression the sidecar-touch fix
  (PR #282) was written to stop
- offline recording → **no chunks arrive at all** → the session closes seconds
  in and emails a near-empty summary, then two hours of audio arrives afterwards

Once the end signal is durable, the idle path stops being the normal route and
becomes what it should have been: the answer to "the device died and is never
coming back". That deserves a **longer** timeout, not a shorter one.

`IDLE_CLOSE_SECONDS = SESSION_GAP_MINUTES * 60` also ties two unrelated
questions to one number — "how long a pause means a different meeting" (15
minutes is right) and "how long until we assume the device is gone". This design
gives the second its own parameter so they can move independently, defaulting to
today's value so nothing changes on landing.

## Failure behaviour

| case | behaviour |
|---|---|
| `sessionEnded` on a session already `finalizing`/`sent` | idempotent no-op, as `session_close` already is |
| the close bookkeeping raises | swallowed and logged; **the upload must still succeed** — a `complete` that 500s strands an uploaded recording as un-uploaded and the retry loop re-sends the whole file (BUG-43's family) |
| both the old `/close` and the new marker arrive | same idempotent path, whichever is first wins |
| device sends the marker, then records more | a new session; the closed one is not reopened |

## Scope

**In:** the `complete` flag, the arm-then-close rule, the separated idle
parameter, the app persisting and sending the marker.

**Out:** changing chunk size or the sweep interval (a 1-minute tick is already
small next to the 30-second grace); making `/close` retry (it becomes
redundant, and keeping it as a best-effort fast path costs nothing); any change
to what the email contains — the user has explicitly deferred that.

## How it will be verified

The current 15-minute path was proved by watching a real session flip state.
The new one is proved the same way: record, stop, and confirm `close_intent`
is `end` with `closed_at` within a minute of the last chunk — and separately,
that a session whose uploads are still outstanding does **not** close early.
Nothing here is provable from unit tests alone; the arrival-order rule in
particular is a claim about a real queue.
