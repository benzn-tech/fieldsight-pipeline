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

Six chunks across 25 sessions is **~0.89% of all chunks**, and every one is silent.

**This is a lower bound, not a census.** `holes()` scans only between the lowest and highest
index *present*, so a session whose first chunks were lost looks like a session that started
late — and one real session (`sidc30fb98d`) begins at `c0003` where every other begins at
`c0000`. Leading losses are invisible by construction. The parser also silently dropped that
entire session until 2026-08-09, because it read the timestamp from a fixed underscore slot
and `AUD_…` carries one token before the date where `ben_ucpk_…` carries two.

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

## What it actually is — and it is not what this document first said

> **Corrected 2026-08-09 after review.** The original conclusion was that these are uploads
> that exhausted their retries, and that the freeze/thaw work covers them. **Both halves were
> wrong**, and the data that disproves them was never consulted: the `recordings` table.

Every one of the six missing chunks **has a row marked as successfully uploaded**:

| chunk | in S3 | `size_bytes` | created → uploaded |
|---|---|---|---|
| `c0010` | **no** | 960044 | 02:22:33 → 02:23:28 — **55 s** |
| `c0011` | yes | 960044 | 02:24:16 → 02:24:18 — **2 s** |
| `c0028` | **no** | 960044 | 02:31:39 → … |

The rows claim a *complete* 30-second file. And `complete_recording` →
`recordings.mark_uploaded` sets `uploaded_at = now()` **with no check that the object
exists** — the client says "done", the server records "uploaded".

The delay is the tell: chunks that arrived show ~2 s from created to uploaded; the missing
ones show ~55 s. That is a PUT being attempted, failing, and `complete` being called anyway.

**And the object never existed.** The bucket has versioning **enabled**, and the missing keys
have **zero versions and zero delete markers** — so this is not an upload that was later
deleted.

So the shape is **complete-without-object**, not retry exhaustion. The difference decides
where the fix goes:

- **Freeze/thaw acts on uploads the client knows failed.** These were reported as
  *successes*, so it would never see them. (It is also **merged** — pipeline #274 and
  GrandTime #8 both landed 2026-08-07, before the meeting measured here. The earlier claim
  that it was "complete and unmerged" was simply wrong.)
- **The cheap detector is server-side**: verify the object on `complete`, or reconcile
  `uploaded_at` against S3. Both use data the server already has, and neither depends on the
  client noticing anything.

**6 chunks, ~3 minutes, ~0.89% of chunks** remains the measured figure — but see the census
caveat below.

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

### And the obvious place cannot see the data — measured, not assumed

`extract_session` looks like where this belongs: it already lists S3, already builds the
artifact, and already runs once per session at the final tier. **Its IAM cannot see the
audio.** Read from the live role rather than the template:

```json
"Action": "s3:ListBucket",
"Resource": "arn:aws:s3:::fieldsight-data-509194952652",
"Condition": { "StringLike": { "s3:prefix": ["transcripts/*", "extractions/*"] } }
```

`users/{folder}/audio/{date}/` is not in that list.

**But it is not the only proof of arrival, and the original version of this section was wrong
to say so.** `lambda_vad` writes the `_vad_metadata.json` sidecar for **every** chunk it
processes, including ones it drops as silent — deliberately, so that "the drop must be
auditable". Sidecars are 1:1 with arrived chunks on every session checked (129/129, 260/260,
5/5), and a chunk that never arrived has no sidecar either. So `audio_segments/*` — **derived
pipeline data, not raw customer audio** — answers the same question at a much smaller
privilege.

Two caveats if that route is taken: sidecars only prove arrival from the date the
drop-still-writes-a-sidecar behaviour deployed, and a VAD failure delays one, so **"no
sidecar yet" must read as unknown, not as missing.**

So building it there costs a widening of the extraction role to list raw customer audio.
That is a real privilege increase for a 0.9% event, and it carries the failure mode this
repo has already been bitten by three times: **add the code, miss the IAM, and the denial is
swallowed into "no chunks found" — which reads as "the record is complete."** A field that
silently always says everything is fine is worse than no field.

If it is built later, the check must treat a listing failure as **unknown**, never as
complete, and the IAM must be verified with `simulate-principal-policy` against the live
role — not read from the template.

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
