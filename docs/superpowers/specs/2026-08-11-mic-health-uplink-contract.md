# Microphone health: the backend half

**Status:** contract v2. **v1 was written for a parallel build that had already
shipped**, and invented a second field shape beside it. Rewritten to adopt what
the device actually sends. What v1 got wrong is at the bottom.
**Date:** 2026-08-11

## The device half is done

GrandTime PR #20 (`7c56d48`, on `origin/main`) ships microphone-health
measurement and uplinks it on the six-hourly status report. Verified in
`net/DeviceStatusClient.kt` — **flat top-level keys of the POST body**, not a
nested object:

| field | type | meaning |
|---|---|---|
| `silentSecondsS` | int, seconds | total zero-sample time this device has ever recorded |
| `longestSilentRunS` | int, seconds | the longest **continuous** zero run, spanning chunks and pause/resume |
| `silentRunsWithMicBorrowed` | int | how many of those runs happened while another feature held the mic |
| `lowestSessionPeak` | int or null | lowest per-session peak amplitude across sessions that recorded something; null until one has |

**Nothing about this is negotiable from the backend side.** It is in the field.
The backend's job is to receive it without destroying it.

## The one property the backend must not break

The device's counters are **cumulative and never reset**, and its own
documentation says why in terms of *this* endpoint:

> `POST /api/org/device/status` writes vitals as a last-write-wins `UPDATE` on
> `devices` — a gauge. If these were per-session values, the next clean report
> would erase the fault from the ledger before anyone looked, and the whole
> uplink would be worse than useless: it would read as proof of health.

So the counters are monotone by construction. **The server write must preserve
that**, not undo it:

```sql
mic_silent_seconds_s       = greatest(coalesce(devices.mic_silent_seconds_s, 0), %s)
mic_longest_silent_run_s   = greatest(coalesce(devices.mic_longest_silent_run_s, 0), %s)
mic_silent_runs_borrowed   = greatest(coalesce(devices.mic_silent_runs_borrowed, 0), %s)
mic_lowest_session_peak    = least(coalesce(devices.mic_lowest_session_peak, %s), %s)
```

A plain `SET x = %s` is correct **only** while the device's counters are
monotone and never reset. That is true today and it is a property of a build
that reaches devices independently of this code. `greatest`/`least` costs
nothing and removes the dependency: a reinstalled app, a factory reset, or a
future build that decides to reset cannot erase a recorded fault.

`lowestSessionPeak` is a **minimum**, not a maximum — it is the one field where
"worse" means smaller.

## Absent fields must not erase stored evidence

An older build sends the four backlog keys and none of the mic keys. The write
must therefore be conditional, not one unconditional UPDATE:

- when a report carries **no** mic fields, the mic columns and `mic_reported_at`
  are left exactly as they were
- when it carries them, they are folded in with `greatest`/`least` and
  `mic_reported_at = now()`

Sharing `backlog_reported_at` would make an old build's silence look like a
fresh "no problem".

> This is also worth fixing for the existing fields while in there: today
> `record()` writes `backlog_oldest_age_s = _int_or_none(...)` unconditionally,
> so a report that omits the key **nulls the stored value**. Same class of bug,
> already shipped, one line from the same edit.

## The columns

```sql
alter table devices add column if not exists mic_silent_seconds_s     bigint;
alter table devices add column if not exists mic_longest_silent_run_s bigint;
alter table devices add column if not exists mic_silent_runs_borrowed integer;
alter table devices add column if not exists mic_lowest_session_peak  integer;
alter table devices add column if not exists mic_reported_at          timestamptz;
```

`bigint` for the two second-counters, matching `backlog_oldest_age_s` — they are
cumulative over a device's whole life and `integer` is a ceiling nobody should
have to think about again.

## What reads it

**Today: nothing — and that is also true of the backlog columns this is modelled
on.** `backlog_oldest_age_s` has a writer, a migration, and no reader anywhere
in the repo. Adding five more write-only columns and calling the feature done
would repeat that.

**The first reader is `lambda_device_ledger`'s query**, which already assembles
the per-device report (asset tag, uuid, version, last seen, site) and pushes it
to Notion. Adding the mic columns there puts the numbers in front of a person on
the cadence that report already runs at.

Without that, a fault is recorded in a column nobody opens, which is the same
outcome as not recording it — and this project has been here before: alerting
that has never actually fired, twice.

## Detection latency, stated

The uplink is a **six-hourly** periodic worker with a network constraint. Four
reports a day. A microphone that fails at 09:00 may not appear in the ledger
until the afternoon.

**The timely artifact is the device's local log line, not this.** The uplink is
the durable record — what it is for is answering *"has this device ever
delivered silence"* when someone asks, not paging anyone at the moment it
happens.

## Why loudness is not the health signal

Correcting v1, which had this half wrong in a way a reader would have caught:

Pure digital silence reports **−120 dBFS**, not −41.8 — `lambda_vad`'s
`mean_dbfs` handles the all-zero case explicitly. So loudness *can* flag a
wholly silent chunk.

What it cannot flag is the **onset**: the first failing chunk was 97.6% zeros
with a 0.72-second live tail, and that tail pulls mean RMS up to −41.8 dBFS —
inside the range of a genuinely quiet room, and on this hardware (median −36
dBFS) not even unusual. A boolean "was this chunk silent" is false for it too.

Which is exactly why the device measures the **longest continuous run** rather
than a ratio or a mean: a run survives a live tail, a live head, and any mixture
of the two.

## Verification

- **Unit:** a report carrying the four mic fields writes them and stamps
  `mic_reported_at`; a report carrying none leaves the mic columns and
  `mic_reported_at` untouched; a **smaller** `longestSilentRunS` than the stored
  one does not lower it; a **larger** `lowestSessionPeak` does not raise it; a
  `null` `lowestSessionPeak` does not overwrite a stored value; garbage values
  land as NULL rather than 0; and — the module's standing rule — `record()`
  never raises whatever it is handed.
- **Against a real database:** the `greatest`/`least` folding. `FakeConn` does
  not execute SQL, so a `greatest` that silently does the wrong thing on NULL
  passes the entire unit suite. Run it on the test cluster inside a transaction
  and roll back.
- **End to end, and only this counts:** deny the microphone permission on a real
  device, record for a minute, wait for a status report (or trigger one), and
  confirm the row carries a large `mic_longest_silent_run_s`. The device half is
  shipped but that path has never been exercised against a backend that stores
  it.

## What v1 got wrong

v1 proposed five new fields — `micZeroRunMaxMs`, `micZeroRatioMax`,
`micChunksInspected`, `micCaptureRejected`, `micSource` — none of which the
device sends, in milliseconds where the device sends seconds, aggregated "since
the last report" where the device is deliberately cumulative, and per-chunk
where the device deliberately spans chunks because the onset run crosses
boundaries.

Built as written, the backend would have added six columns nothing fills while
dropping the four fields arriving today — **the exact failure the contract was
written to prevent**.

The cause was a grep on the wrong branch. I checked `feat/device-identity-phase2`,
found no uplink, and reported "the app computes these locally and uplinks none of
them" — while the work had merged to `main` through a different branch. I even
noted at the time that the branch might be wrong, and did not follow it up.

That is the sixth instance of one failure mode in a single day's work, and the
others were all in specs that name it: **"we already have a mechanism for that",
and its twin "there is no mechanism for that", are hypotheses.** Six times the
mechanism existed and did something other than its name implied, or existed
somewhere I had not looked.
