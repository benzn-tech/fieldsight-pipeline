# Tonight's merge order, and what to check in the morning

**Date:** 2026-08-08
**Purpose:** land six branches on `develop` so TEST holds a coherent build, and give the
morning a short list of things to look at that would actually catch a regression.

## Merge order

Verified, not assumed: all six merge onto `origin/develop` in this order with **zero
conflicts**, and the combined tree passes **2010 unit tests + cfn-lint 1.53.3**.

| # | PR | What | Note |
|---|---|---|---|
| 1 | **#282** | session touch survives a quiet meeting | another session's work |
| 2 | **#281** | P0 — loudness normalisation before ASR | prod ships **off** |
| 3 | **#283** | P1a — long sessions no longer truncated to 47% | |
| 4 | **#284** | P1b — device announcements filtered | stacked on #283 |
| 5 | **#285** | P1c — final pass re-checks coverage; empty≠unreadable | stacked on #284 |
| 6 | **#286** | docs — spec withdrawal + roadmap corrections | docs only |

#284 and #285 are **stacked** (each targets the branch below it). GitHub retargets them to
`develop` as their base merges. CI only runs on PRs targeting `develop`/`main`, so those
two get their first CI run at that moment — expect it, it is not a failure.

Nothing here is order-sensitive beyond the stacking; #282 and #281 are independent of the
rest and of each other.

## What is inert on prod after this

- `PROD_NORMALISE_AUDIO` → **false**. P0 is code-complete on prod and does nothing until
  the repo variable is set. `TEST_NORMALISE_AUDIO` → true.
- Everything else is on in both stages. All three are additive to the extraction artifact
  (`transcript_stats`, `device_announcements`, `generation`) and **every consumer reads
  them with `.get()`** — checked in `lambda_item_writer`, `lambda_ingest`,
  `lambda_org_api`, `session_scope`. No consumer validates the artifact's key set, so an
  older reader against a newer artifact is safe, and the reverse is too.

## Check this before anything else, and it is not from tonight's work

**Prod switched to ElevenLabs and has never run on it.** `ASR_PROVIDER=elevenlabs` on both
stages (PR #280, prod deployed 13:53 UTC 08-07 = 01:53 NZ), and
`fieldsight-prod-transcribe` has **zero log events** since — nobody has recorded on prod
since the switch. The first real customer transcription on the new provider will be
tomorrow's.

Test transcribed successfully on it at 12:54 UTC 08-07 (6 words, 4 items), so the key had
quota left after the evaluation that exhausted a 10,000-credit allowance. That is
reassuring, not proof.

So: **after the first prod recording, check `fieldsight-prod-transcribe` before judging
anything else.** A quota failure there would look exactly like "the new backend work broke
transcription", and it would not be that.

```
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-prod-transcribe \
  --start-time <ms> --region ap-southeast-2 --filter-pattern '"ElevenLabs"'
```

Expect `ElevenLabs transcript written: s3://...`. If instead there is a 401/429/quota
error, the fix is a repo variable — `PROD_ASR_PROVIDER` back to `transcribe` — not a code
change.

## Morning checks, in order of what they would catch

Record a short session on a device, then:

**1. Did normalisation run, and did it help?** In the TEST VAD sidecar
(`audio_segments/{user}/{date}/..._vad_metadata.json`):

```
normalised: true
loudness_dbfs_before / loudness_dbfs_after     ← expect roughly -40 → -20
```

If `normalised: false`, read the log line next to it — it says why, and a failure falls
back to the original audio rather than losing the chunk.

**2. The free too-quiet metric.** In `/aws/lambda/fieldsight-test-extract-session`, count:

```
"empty result"   ← transcriber found nothing in audio VAD judged to be speech
"unreadable"     ← a genuinely malformed transcript; should be zero
```

With `DROP_SILENT_CHUNKS` on, silent chunks are never transcribed, so **every "empty
result" is a too-quiet event**. This is the before/after number for P0 and it costs no ASR
credits — which matters, the last evaluation exhausted a 10,000-credit allowance.

**3. Device announcements.** In the extraction artifact:

```
device_announcements: {removed: N, texts: [...]}
```

`texts` is the point: the app's prompt audio (`res/raw/recording_started.mp3` and
siblings) was staged 2026-08-07 and is not wired to any Kotlin yet, so this is how the
real wording becomes known. **If a phrase shows up that the patterns miss, add it to
`DEVICE_ANNOUNCEMENT_PATTERNS`** — no code deploy needed.

Also confirm no *person* was eaten: a turn about recording ("recording started late so
the first bit is missing") must still be in `topics`.

**4. Long sessions reach the model.** `transcript_stats.truncated` should be `false` for
anything under ~4.5 hours. If it is ever `true`, `lines_omitted` says how much, and the
prompt told the model — which is the part that used to be missing.

**5. The final pass covers the whole session.** For a session where transcripts land
during the final call, the published extraction's `source_transcripts` should reach the
**last** chunk, and `generation` should be `1` rather than `0`. Exactly one rerun round is
expected; more than one is worth reading the log for.

The direct regression test for this is to re-run prod session
`sid61be49d563524f51b17c54c67733b08c` on TEST and confirm coverage reaches `c0150`
(it published 95 of 151 and stopped at `c0129`).

## What is NOT verified by any of the above

**Whether compression lifts site noise into new hallucinations.** This is the one open
question gating `PROD_NORMALISE_AUDIO`, and it needs a before/after ASR comparison on the
same clip. Use **30-second** clips — repeated runs on the 5-minute one are what exhausted
the credit allowance, and a 30-second clip exposes the same instability at a tenth of the
cost.
