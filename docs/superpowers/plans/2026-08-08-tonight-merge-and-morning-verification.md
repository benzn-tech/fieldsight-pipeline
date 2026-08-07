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

## What each stage will actually be set to

Checked against `gh variable list`, not against intent — none of the new variables exist,
so every one of them takes its workflow fallback:

| variable | exists? | TEST gets | PROD gets |
|---|---|---|---|
| `*_NORMALISE_AUDIO` | no | `true` (fallback) | **`false`** (fallback) |
| `*_DEVICE_ANNOUNCEMENT_PATTERNS` | no | `[]` → built-in defaults | `[]` → built-in defaults |
| `*_ASR_PROVIDER` | prod only (`elevenlabs`) | `elevenlabs` (fallback) | `elevenlabs` (variable) |
| `*_DROP_SILENT_CHUNKS` | no | `true` (fallback) | `true` (fallback) |
| `*_VAD_THRESHOLD` | prod only (`0.2`) | `0.2` (fallback) | `0.2` (variable) |

The row worth having checked is `ASR_PROVIDER`: `TEST_ASR_PROVIDER` does **not** exist,
while the test lambda is currently running ElevenLabs. Had `deploy.yml`'s fallback been
`transcribe`, tonight's merge would have silently reverted test to AWS Transcribe and every
morning comparison would have been against a different engine. It is `elevenlabs`, so it
does not — but that is the shape of regression a merge train produces, and it is invisible
in the diff.

`TRANSCRIPT_TEXT_LIMIT` is a template literal (`300000`) with no variable, so it is the
same on both stages.

## What is inert on prod after this

- P0 is code-complete on prod and does nothing: `NormaliseAudio` resolves to `false`
  there, and the template Parameter now also defaults to `false` so a manual `sam deploy`
  cannot turn it on by accident.
- Everything else is on in both stages. The three new artifact fields
  (`transcript_stats`, `device_announcements`, `generation`) are additive and **every
  consumer reads them with `.get()`** — checked in `lambda_item_writer`, `lambda_ingest`,
  `lambda_org_api`, `session_scope`. No consumer validates the artifact's key set.

- **But one existing field changes VALUE, not just company: `speaker_count`.** It is taken
  after the announcement filter, so a session of one person plus a device now reports 1
  where it used to report 2 — on prod as well, since that filter is unconditional. That is
  the intended fix, and it means item-writer's `speaker_count == 1` gate engages more
  often, resolving a self-referential responsible party to a real name in sessions where
  it previously declined to. Expect *more* named attributions, and check a couple are
  right.

- **Expect an email nobody has seen before.** With #282 a recording containing no speech
  now opens a session from the VAD sidecar and, on inferred close, sends a confirmation
  whose body is "No summary was generated for this recording." Previously such a session
  never opened and never emailed. Leaving a device running by accident will now produce
  mail. Not a bug — but it will look like one.

- **Known gap, deliberately not fixed tonight:** the announcement filter lives in
  `extract_session`, while `lambda_rolling_summary` and `lambda_session_finalize` call
  `assemble_deduped_turns` directly and so still see "Recording started" as speech. It can
  therefore appear in the Tier-1 rolling summary and in confirmation-email content.
  Fixing it properly means moving the filter into `assemble_deduped_turns`, which changes
  a function with three callers and eight test stubs — not something to do on the night
  before a hand-test, on a merge train already verified green.

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
"empty result"                  ← transcriber found nothing in audio VAD judged speech
"unreadable transcript segment" ← a genuinely malformed transcript; should be zero
```

Grep the **full** second phrase, not just `unreadable`: that word also appears in
"existing extraction is unreadable" on the throttle-read path, which is a problem with the
*published extraction*, not with a transcript, and counting the two together would make
the number meaningless.

With `DROP_SILENT_CHUNKS` on, silent chunks are never transcribed, so **every "empty
result" is a too-quiet event**. This is the before/after number for P0 and it costs no ASR
credits — which matters, the last evaluation exhausted a 10,000-credit allowance.

**3. Device announcements.** In the extraction artifact:

```
device_announcements: {removed: N, texts: [...]}
```

`texts` is the point: the app's prompt audio (`res/raw/recording_started.mp3` and
siblings) was staged 2026-08-07 and is not wired to any Kotlin yet, so this is how the
real wording becomes known. **If a phrase shows up that the patterns miss, set the repo
variable `TEST_DEVICE_ANNOUNCEMENT_PATTERNS`** (a JSON list of regexes) and redeploy — no
code change.

Set the **repo variable**, not the lambda's environment directly: a value written straight
onto the live function is erased by the next CloudFormation reconcile. An override
*replaces* the defaults rather than adding to them, so include the existing patterns as
well as the new phrase.

Also confirm no *person* was eaten: a turn about recording ("recording started late so
the first bit is missing") must still be in `topics`.

**4. Long sessions reach the model.** `transcript_stats.truncated` should be `false` for
anything under ~4.5 hours. If it is ever `true`, `lines_omitted` says how much, and the
prompt told the model — which is the part that used to be missing.

**5. The final pass covers the whole session.** For a session where transcripts land
during the final call, the published extraction's `source_transcripts` should reach the
**last** chunk, and `generation` should be `1` rather than `0`. Exactly one rerun round is
expected; more than one is worth reading the log for.

**The obvious regression test — replaying prod session `sid61be49d5...` on TEST — is not a
morning checkbox, and should not be treated as one.** Those 151 transcripts live in the
prod bucket. Copying them into `fieldsight-data-test-509194952652` fires a live extract
pass, session-activity and rolling summary on *every* PUT, and item-writer would try to
resolve a prod user folder against the test database — an identity-bridge miss at best,
misattribution against the seed data at worst. The coverage assertion itself would work,
but moving customer-derived data into test is a deliberate decision with its own
procedure, not a step to run before coffee.

Use a fresh recording instead. To exercise the race on purpose, record a session and keep
recording while the finalize sweep runs, so transcripts land during the final pass — that
is the condition, and it needs no prod data.

## One watch item, if a long session misbehaves

The final pass's measured ~170 s thinking call was against the **old 60,000-character**
cut. At 300,000 the input is five times larger, so on a genuinely long session that call
could approach `LLM_HTTP_TIMEOUT=540` inside `Timeout=600`. A timeout there raises, the S3
event retries with the same input, and it can keep retrying for up to six hours while
holding a concurrency slot — the shape of BUG-43, from a different direction.

No evidence it will happen; two-hour sessions render to 128k, well under the cap. But if a
multi-hour session's final pass misbehaves, look here first, and the lever is
`TRANSCRIPT_TEXT_LIMIT` in the template.

## What is NOT verified by any of the above

**Whether compression lifts site noise into new hallucinations.** This is the one open
question gating `PROD_NORMALISE_AUDIO`, and it needs a before/after ASR comparison on the
same clip. Use **30-second** clips — repeated runs on the 5-minute one are what exhausted
the credit allowance, and a 30-second clip exposes the same instability at a tenth of the
cost.
