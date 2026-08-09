# Chunks that were recorded and never arrived

**Date:** 2026-08-09
**Scope:** every chunk in the prod bucket — 672 objects, 24 sessions, 5.4 hours of audio.
**Why:** chasing a 3-second discontinuity turned up something larger, and the first two
attempts to size it were both wrong in instructive ways.

## The finding

Chunk indices are allocated in sequence by the device, so a hole in the sequence means a
chunk existed and never reached S3. Two sessions have holes:

| session | chunks present | missing | audio |
|---|---|---|---|
| `Ben_UCPK` 2026-08-07 `sid036cf2f1` | 4 | `c0001` | ~30 s |
| `Ben_UCPK` 2026-08-07 `sid39ad6c92` | 129 | `c0010 c0028 c0030 c0037 c0042` | **~2.5 min** |

The second is a 62-minute meeting, so **about 4% of it is absent from the record.**

**They left no trace anywhere.** Searching the whole bucket for those indices returns
nothing — no audio, no `audio_segments/` sidecar, no transcript. There is no artifact saying
a chunk is missing, because the only thing that would have created one is the chunk itself.

Six chunks across 24 sessions is **~0.9% of all chunks**, and every one is silent.

## Two wrong answers on the way here, both worth recording

**"64% of all recorded audio is lost."** Computing `next_chunk_start − (start + duration)`
and summing anything positive gives 12,463 seconds — 64% of everything ever recorded. It is
nonsense: the two largest entries are 5,758 s and 5,544 s, which are **someone pausing and
resuming an hour and a half later**. The session id and the index sequence both continue
across a pause, so a pause is indistinguishable from loss *by that metric*. Any measure of
"missing audio" has to separate a deliberate pause from a hole, and duration arithmetic
alone cannot.

**"1 in 336 chunks."** An earlier pass counted chunks shorter than 30 s and found ten, nine
of them legitimate (a session's last chunk, or a one-to-two-chunk session). That number
described a different and much rarer event — a recorder restart — and **missed whole chunks
being absent entirely**, because a chunk that never arrived has no size to be short.

The lesson both share: **the shape of the query decides what you are able to see.** Sizes
find truncation; timestamps find pauses; only the index sequence finds absence.

## What this is probably the same thing as

The upload freeze/thaw design exists because the client "retries for 7 days then silently
gives up", and the backlog work documents a path where rate-limiting is treated as permanent
failure and burns all eight retry attempts. **A chunk that exhausts its retries and is
dropped looks exactly like this**: an index allocated, no object, nothing logged server-side
because the server never heard about it.

That work (GrandTime PR #8, pipeline PR #274) is complete and unmerged, and ships inert.
This audit gives it a production baseline it did not have: **6 chunks, ~3 minutes, ~0.9% of
chunks, across every session ever recorded.**

## Detecting it is nearly free, and nothing does

Indices are contiguous by construction, so at session close the check is: are `0..max`
all present? Anything else is a hole, and its size in seconds is known.

Today nothing looks. The session closes, the extraction runs on whatever arrived, the
confirmation email goes out, and the report is missing whatever was said in those minutes —
with no field anywhere recording that the record is incomplete.

Two places it could live, both cheap:

- **at session close**, where `meeting_session` already knows the session ended and the
  finalize sweep already runs — report a count and the missing indices
- **in the extraction artifact**, alongside `transcript_stats`, so a reader can see the
  record is partial rather than assuming it is whole

The second matters more than the first. A number in a log tells whoever goes looking; a
field in the artifact tells the person reading the report that three minutes are missing.

⚠️ **Do not compute it from the transcript list.** A chunk VAD found silent is dropped before
transcription (`DROP_SILENT_CHUNKS`), so it has no transcript either — inferring absence from
missing transcripts would report every silent chunk as lost, which on this material is many
per session. The field would be noise and would be ignored, which is worse than not having
it. **Only the audio object proves arrival.**

`scripts/missing_chunk_audit.py` implements the correct method and can be run over history
without touching the pipeline.

### A second signal fell out of it

Running the audit over everything shows recorder restarts are not evenly spread:

| session | chunks | short mid-session |
|---|---|---|
| `Ben_UCPK` `sid39ad6c92` | 129 | 1 |
| `Sam_Yu` `sid622a0e7f` | 260 | **5** |
| `Sam_Yu` `sid14697d46` | 5 | **4 of 5** (20 s, 9 s, 11 s, 8 s) |

The last one is barely a recording at all — four of its five chunks were cut short. **One
device restarts far more than the other**, and nothing surfaces that either. It is a device
health signal sitting in data we already have.


## Reproducing

```bash
aws s3 ls s3://fieldsight-data-509194952652/users/ --recursive --region ap-southeast-2 \
  | grep "/audio/" | grep "_sid" | awk '{print $3, $4}' > chunks.txt
```

Group by `(folder, date, sid)`, take the index from `_c{NNNN}`, and report any `i` in
`min..max` with no object. Do **not** infer loss from timestamp arithmetic — that counts
pauses.
