# Multi-Device Session Merge — Design

Date: 2026-08-04 · Status: DESIGN (approved in brainstorm, not yet implemented)
Repos: `fieldsight-pipeline` (backend) + `GrandTime` (mobile)

## The problem

One recorder cannot capture a whole meeting. A body-worn mic picks up the people
near it and loses the ones across the room. The business answer is more devices:
the site keeps spare units and hands them to the client inspector or the city
council inspector for the duration of a walk-through, so the inspector gets value
from the recording too and the site gets coverage it could not get from one mic.

That only works if the system can tell that several recordings are **the same
meeting**, and can merge what each device heard into one record.

**Non-goal:** identifying *who* is speaking. Persistent voice identity ("this
voice is John from City Council") is a separate concern with its own provider
questions and its own biometric-privacy constraints. It is deliberately split
into a second spec and is not required by anything here.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Guest devices sign in with **accounts in the site's own company** (e.g. `site.guest@customer.com`) | Keeps every merge inside one tenant. The cross-company invariant is never bent. |
| 2 | Group identity = **the lead device's `session_id`** | Already exists, already device-minted, so grouping survives offline. |
| 3 | Joining is by **QR scan** | The only strong signal that works with no network and no pairing. |
| 4 | Acoustic fingerprinting is **deferred to v2** | It is a probabilistic backstop for "someone forgot to scan"; it doubles v1's surface for a benefit we cannot yet size. |
| 5 | Merged content is stored **once**, presented per-person | N stored copies would fork under the existing content-correction flow and multiply RAG hits. |
| 6 | The first device to stop **emails immediately**; an `updated` email follows the merge | Preserves the ≤2-minute confirmation promise, which waiting for stragglers would break. |
| 7 | Transcripts are merged **by the extraction LLM**, not by an alignment algorithm | No shared clock exists to align on. Extraction already calls an LLM. |
| 8 | Group close is driven by the **existing finalize sweep**, with a timeout | A device that never stops must not hold the group forever. |

## Architecture

### Grouping

`meeting_session` gains one nullable column:

```sql
ALTER TABLE meeting_session ADD COLUMN group_id text
  REFERENCES meeting_session(session_id);
CREATE INDEX idx_meeting_session_group ON meeting_session (group_id)
  WHERE group_id IS NOT NULL;
```

`NULL` means a solo recording — which every existing row is, so the migration
carries no backfill and no behaviour change.

The lead device is its own group:

```
lead:      session_id = X,  group_id = X
joiner:    session_id = Y,  group_id = X
joiner:    session_id = Z,  group_id = X
```

Using the lead's `session_id` as the group key means **no identifier has to be
allocated**. The device already mints it locally when recording starts, so a
group can form with no connectivity at all — which is the point of choosing QR
over Bluetooth or GPS. A design that required the group to phone home for an ID
would throw that away.

### Joining

The lead shows a QR containing `fs1:<session_id>` (the prefix is a namespace
guard, so scanning an unrelated code fails cleanly rather than producing a
nonsense group). The joiner records normally; the only difference is that its
`group_id` rides along on `POST /api/org/sessions/{id}/open`, which already
exists and is already store-and-forward for offline devices.

**The chunk key format is not changed.** `..._sid{32hex}_c{NNNN}` is parsed by
`chunk_stitch.parse_chunk_key`, `session_scope`, VAD and transcribe; threading a
group through it would touch the whole pipeline for information only the merge
step needs.

### Merge

Each device's audio runs the existing pipeline untouched — VAD, transcribe, its
own transcripts. Nothing about single-device recording changes.

Merging happens once at extraction. When a session belongs to a group and the
group has closed, extraction assembles **every member's turns** and gives them to
the LLM as labelled parallel sources: *"these are N recordings of one meeting,
each incomplete; produce one record."*

Why not align the transcripts programmatically first: there is no common clock.
`assemble_deduped_turns` orders turns on "the single session clock" and
`_dedup_turn_boundaries` matches on time overlap — both assume one device. Across
devices the clocks are independently wrong; BUG-37 is a shipped instance of
exactly that (a device's wall clock was 12 hours out). Alignment would therefore
have to be content-based, which is what the LLM does natively, in a call the
pipeline was going to make anyway.

**Sizing.** ~25K characters of transcript per 30-minute recording; the meeting
prompt truncates at 120K (BUG-15). Three to four devices fit. Expected usage is
1–3. Beyond four, the merge degrades explicitly: merge the first N, and state in
the report that the rest were not included. That degradation point is where a
future content-alignment pre-pass (LCS over `chunk_stitch`'s word-level matcher)
would slot in; it is not built now.

### Storage and visibility

The merge writes **one** set of topics, owned by the lead's session. Group
members see them because the timeline read unions "my own topics" with "topics of
groups I was in" — one indexed lookup on `group_id`.

Storing a copy per member was considered because the user's phrasing was "10
people, 10 identical reports". It was rejected: a correction applied to one copy
would leave the others stale (the content-correction and re-index flow is already
live), and RAG would hold N vectors of the same sentence, so Ask would surface
the same content repeatedly. "Each person gets a report" is satisfied at the
delivery layer, which is where the user's requirement actually lives.

### Lifecycle

```
device A stops  → finalize → email A now ("N devices still recording")
device B stops  → finalize → email B now
                     ↓
        all members terminal, or group timeout
                     ↓
             group re-extraction → one merged set of topics
                     ↓
          "updated" email to every member, identical content
```

The trigger is the existing finalize sweep, not a device. The group is due when
every member has reached a terminal state, or when the group timeout expires —
reusing the same idle judgement `INFER_IDLE_CLOSE` already applies per session,
so a group cannot outlive the sessions in it. An inspector who forgets to press
stop, or who walks off and syncs hours later, must not stall everyone else's
report.

The `updated` email reuses the resend path finalize already has for late resumes.

## Leaving a group

The original draft had no exit path at all, which pointed straight at the one
failure this design exists to prevent. Without one:

```
Mon 10:00  inspector scans, joins the site manager's meeting  → device holds group X
Mon 11:00  meeting ends, inspector walks off with the device
Tue 09:00  inspector records at a different site
           → device still holds group X → Tuesday's audio merges into Monday's meeting
```

That is an over-merge: two unrelated meetings in one report, delivered to both
sets of people, and it reads perfectly fluently so nobody notices. Guest devices
make it worse — they go to visiting inspectors who have the least reason to
remember to "leave" anything.

### Two distinct actions, not one

| Action | Effect on me | Effect on the others |
|---|---|---|
| **Meeting ended** | leave the group | **all member devices stop recording** |
| **I'm leaving, meeting continues** | leave the group | **none** |

Both keep what was already recorded in the meeting; they differ only in whether
the rest of the group is told to stop. An early-departing inspector must have
the second one, or using the first would stop everybody else's recording.

Whether a guest may end the whole meeting is a **product** decision, and the
answer is yes: otherwise a meeting whose lead leaves first can never be closed
by the people still there.

### The prompt happens where the person is

The user is wearing the device on their chest, in a noisy site, possibly gloved.
They are **not looking at the screen**. So:

1. Recording stops.
2. After **20 seconds**, on **that device only** (whoever stopped — not all of
   them, which would be chaos), a bundled audio cue plays: recording has
   stopped, please confirm whether the meeting is over.
3. The screen shows two large targets and one quiet escape:

```
        会议是否已结束？

  ┌────────────────────────┐
  │      会议已结束          │   → tells every member device to stop
  └────────────────────────┘

  ┌────────────────────────┐
  │   我先走，会议继续        │   → only this device leaves
  └────────────────────────┘

          还没结束               ← small; does nothing, recording resumes later
```

Touch targets ≥56dp so a gloved hand can hit them. No countdown, no progress
bar — in daylight they are noise.

A **PTT shortcut maps to "meeting ended"**, the highest-frequency action, so the
most common case never requires looking at the screen at all. The rarer "I'm
leaving" needs the screen, which is the right trade.

The cue is **bundled audio, not TTS**. The copy is fixed, and the moment it
matters most is offline — a cloud TTS call that fails without network is worse
than useless. `AskSounds` already does exactly this for the SP-Ask cues
(`res/raw/*.wav`, explicitly "NOT downloaded"); this reuses that pattern.

### Telling the other devices

No new channel. Chunks upload roughly every 30 seconds, so the **upload response
carries the signal back**: when a group has been ended, the next upload from each
member returns that fact, and the device plays the cue and stops. Latency is one
chunk cycle, which is nothing against "the meeting is over".

Offline members simply never hear it and keep recording — handled by the
server-side time-span guard below rather than by pretending delivery is
guaranteed.

### After leaving, ask before resuming

Both exits end with the same question: **resume recording?** Ending a meeting is
not the same as finishing work — the person may be walking to the next task, or
may be done for the day. There is no safe default, so it is asked, and a
resumption starts a **fresh solo session with no `group_id`** so post-meeting
audio can never land in the meeting.

### Timeouts are a backstop, not the mechanism

The user's explicit answer is the primary path. Two timeouts exist only for when
nobody answers — device in a bag, person already driving away:

- **Device side:** the pending group clears after `SESSION_GAP_MINUTES` (15 min),
  *not* `STOP_GRACE_SECONDS` (30 s). Thirty seconds is the mis-touch window; a
  battery swap or a walk to the next building would blow through it and force a
  re-scan for no reason.
- **Server side, and this is the one that actually holds:** joining is refused
  when the lead session ended long ago, and a group whose members' server-side
  `opened_at` values span beyond the window is **not merged at all**.

The server guard cannot be skipped, because the device guard depends on the
device being correct — and devices crash, get reinstalled, and carry clocks that
have been observed 12 hours out (BUG-37). Only the server's own timestamps make
"yesterday never merges into today" unconditional.

## Failure behaviour

The bias throughout is **under-merge, never over-merge**. A missed merge degrades
to today's behaviour (separate reports). A wrong merge mixes two meetings into one
report and sends it to both sets of people — data contamination plus disclosure,
and hard to notice after the fact.

| Case | Behaviour |
|---|---|
| Group spans two companies | **Reject.** Cannot happen through the UI, but the server must not rely on that. |
| Scanned session unknown or already closed | Refuse the join; the device records solo. Never interrupt recording. |
| Members disagree on `declared_site` | Take the lead's, record the disagreement. Usually one person skipped site selection, not an attack. |
| Only one member produced content | Merge is a no-op; solo report as today. |
| Device scans its own code | Idempotent; it is the lead. |
| A member's transcript is missing or corrupt | Merge the rest; state in the report that one device's record was not included. |
| Group extraction fails | Per-device reports already sent stay valid. No rollback. |
| More than ~4 devices | Merge the first N, state the omission. Do not silently truncate. |
| Nobody answers the end-of-meeting prompt | Device clears the group after 15 min; the server refuses to merge across the time-span window regardless. |
| A member is offline when the group ends | It never receives the stop signal and keeps recording. The time-span guard keeps the stray audio out of the merge. |
| Device carries a stale group into the next day | **Server refuses.** Join is rejected against a long-ended lead, and merge is rejected on span. This must not depend on the device clock (BUG-37: observed 12 h out). |
| Guest ends a meeting the site manager is still in | Allowed by design — otherwise a meeting whose lead leaves first can never be closed. Every member is told, and each is asked whether to resume. |

## Testing

- **Pure/unit:** group membership resolution; the company-mismatch rejection; due-group detection including the timeout; parallel-source prompt assembly and its size guard; degradation past the device cap.
- **Contract:** `group_id` survives the offline store-and-forward path on `/open`.
- **Exit paths (the ones whose failure is silent):**
  - after the group is left, the **next** recording carries no `group_id` and produces its own separate minutes — this is the anti-over-merge guarantee and gets its own test;
  - "I'm leaving" leaves *only* the caller — every other member keeps recording;
  - "meeting ended" reaches the others through the upload response;
  - the **server** refuses a join against a long-ended lead, and refuses to merge a group whose members span beyond the window, **using server timestamps only** — asserted with a device clock deliberately set wrong, since a correct-clock test would pass either way and prove nothing.
- **Live:** two devices, one meeting, one scan — assert one merged topic set, and an `updated` email to both accounts. Then the same with the second device never stopping, asserting the timeout still closes the group. Then a third run where device B records again the next day, asserting its minutes are separate.

Prompt-level merge quality cannot be unit-tested meaningfully; it needs a real
two-device recording, which is also the only way to confirm the coverage claim
that motivates the feature.

## Out of scope

- Persistent speaker identity (separate spec)
- Acoustic fingerprint fallback (v2)
- Content-alignment pre-pass for >4 devices (deferred; the degradation point is defined)
- Cross-company sharing of any kind
