# Morning verification — 2026-08-09

Everything that changed on prod on the night of 2026-08-08 acts **before anything
renders**. Normalisation, the VAD threshold, the tag filter and the announcement filter all
happen between the microphone and the report, so the dashboard looking normal is not
evidence that any of them worked — and, more importantly, not evidence that they did no
harm.

Each check below costs nothing and takes about a minute. Record one recording, then work
down the list.

Throughout, `{sid}` is the session id in the filename and `{date}` is the NZ date.
**Substitute them before running** — they are in braces, so bash leaves them
literal and the command SUCCEEDS and returns nothing rather than complaining. An
empty result from an unsubstituted placeholder looks exactly like an empty result
from the real failure the check exists to detect.

Every command in this document was executed against real prod data on the night of
08-08 — they run.

---

## ⚠️ Before you press record — the ElevenLabs balance

**This is the most likely way tomorrow fails, and it has nothing to do with anything that
changed tonight.**

August usage, read from `/v1/usage/character-stats`:

| day | credits |
|---|---|
| 8/1–8/5 | 0 |
| 8/6 | 173 |
| **8/7** | **7,797** ← the provider evaluation |
| 8/8 | 802 |
| **total** | **8,772** |

That 7,797 matches the recorded note that "fifteen five-minute runs exhausted a
10,000-credit allowance". **If the allowance is 10,000 per month, roughly 1,200 remain.**

The 8/8 figure covered about 25 minutes of audio — three prod sessions plus a night of
experiments — so roughly **32 credits per audio-minute**. At that rate a 40-minute meeting
is ~1,300 (already over) and a 78-minute one ~2,500.

**A quota failure looks exactly like a backend fault**: transcription simply stops. Prod
switched to ElevenLabs on 8/7, so the natural conclusion would be that last night's changes
broke it.

The API key lacks `user_read`, so the balance cannot be read from the API — **check the
ElevenLabs dashboard before recording.** Re-confirmed 2026-08-08 night: `/v1/user` and
`/v1/user/subscription` both return 401 `missing_permissions`, and
`/v1/usage/character-stats` comes back with an empty `usage` map, so the figures in the
table above cannot be refreshed programmatically either. The dashboard is the only source.

### 🔴 prod and test share ONE ElevenLabs key (verified 2026-08-08 night)

`fieldsight-prod-transcribe` and `fieldsight-test-transcribe` hold the **same** key —
compared by hash, not assumed. So **every test recording spends prod's allowance**, and the
quota that decides whether tomorrow morning works is a shared pool with no isolation
between the environment you experiment in and the one you demo from.

Two consequences worth acting on:

- **Before tomorrow, do not record on test to "check something quickly".** It comes out of
  the same budget, and the failure it causes on prod looks like a backend fault rather than
  a spent quota.
- **The durable fix is a separate test key, not moving the key into Secrets Manager.**
  Only one function holds it, it is non-VPC, and `NoEcho` already masks the value at the
  CloudFormation layer (`describe-stacks` returns `****`, confirmed). The plaintext Lambda
  env is a real but modest exposure; the shared quota is an availability risk that has
  already nearly cost a demo. Fix the one that bites first.

Nothing done on the night of 08-08 consumed any of it: the extraction replay copied an
existing transcript rather than re-transcribing, and both the extraction and the output-
ceiling probes ran on qwen. If it is short, top up rather than falling back:
`PROD_ASR_PROVIDER=transcribe` works, but reintroduces the fabrication the switch was made
to stop (AWS invented 10.7% of one meeting's words), which defeats the purpose of a session
meant to demonstrate quality.

---

## 0. The baseline to compare against

Captured from the last prod recording **before** the changes (2026-08-08 12:22 NZ, chunk
`c0009` of `sidb8bd73d313984c08bdf86360e48e0ba1`):

```json
{ "normalised": false, "loudness_dbfs_before": -33.3,
  "loudness_dbfs_after": -33.3, "vad_threshold": 0.2 }
```

---

## 1. Loudness normalisation — did it run, and did it lift?

```bash
aws s3 cp s3://fieldsight-data-509194952652/audio_segments/Ben_UCPK2/{date}/{chunk}_vad_metadata.json - \
  --region ap-southeast-2 | python -m json.tool | grep -E "normalised|loudness|vad_threshold"
```

| field | worked | did not |
|---|---|---|
| `normalised` | `true` | `false` → the variable did not reach the function |
| `loudness_dbfs_before` → `after` | roughly **−33 → −19** | before == after → the ffmpeg filter failed and fell back (it is best-effort by design, and the fallback is silent in the artifact but **logged**) |
| `vad_threshold` | `0.15` | `0.2` → the deploy did not carry the variable |

If `before == after`, the reason is in the log, not the artifact:

```
fields @message | filter @message like /Loudness/ | sort @timestamp desc
```
on `/aws/lambda/fieldsight-prod-vad`. `Loudness normalised: X → Y dBFS` is success;
`Loudness: X dBFS (not normalised)` means it was skipped or refused.

**Rollback:** `gh variable set PROD_NORMALISE_AUDIO --body false` then redeploy `main`.

---

## 2. VAD threshold 0.15 — is it keeping more, and is what it keeps real?

The point of 0.15 was two chunks in yesterday's session (`c0014`, `c0015`) that held real
speech and were dropped whole at 0.2, with no log line.

```bash
# how many chunks were dropped entirely
aws s3 ls s3://fieldsight-data-509194952652/audio_segments/Ben_UCPK2/{date}/ \
  --recursive --region ap-southeast-2 | grep _vad_metadata.json | wc -l
# versus how many produced a transcript
aws s3 ls s3://fieldsight-data-509194952652/transcripts/Ben_UCPK2/{date}/ \
  --recursive --region ap-southeast-2 | grep {sid} | wc -l
```

A sidecar exists for **every** chunk; a transcript exists only for chunks that held speech.
The gap is the drop count. Expect proportionally fewer drops than before.

**The thing to actually watch for** is the opposite failure: 0.15 admitting noise that the
engine then turns into sentences. Do not judge that by reading the transcript — fluent
invented text reads exactly like real text. Use the two checks that do not need ears:

- **T1** — send the same clip three times. Real speech comes back verbatim; invented text
  varies, or appears once and never again.
- **T2** — send the same clip to a second engine. Two models do not invent the same
  sentence; word-level overlap means it is real.

**Rollback:** `gh variable set PROD_VAD_THRESHOLD --body 0.2` then redeploy.

---

## 3. Audio-event tags — are they out of the extraction input?

```bash
aws s3 cp s3://fieldsight-data-509194952652/extractions/Ben_UCPK2/{date}/{session_base}.json - \
  --region ap-southeast-2 | grep -oE "\[[^]]{1,40}\]" | sort -u
```

**Nothing should come back.** Yesterday's session carried 18 distinct forms across 19 of 27
transcripts — `[background noise]`, `[laughs]`, `[鼠标点击]`, `[背景人声]` and so on.

The raw transcripts under `transcripts/` **will still contain them**, and that is correct:
the filter runs at assembly, so the transcript stays a faithful record of what the engine
returned while the extraction input is cleaned.

**Rollback:** `gh variable set PROD_FILTER_AUDIO_EVENT_TAGS --body false` then redeploy.
(This switch only started working with PR #296 — before that it was declared in the code
but passed by neither workflow, so setting it did nothing that survived a deploy.)

---

## 4. Device announcements — one meeting, one story

The filter moved into assembly, so extraction, the rolling summary, the confirmation email
and the multi-device merge now all read the same filtered stream. Before, only extraction
did.

Check the confirmation email and the report for the same session: neither should contain
"Recording started" or "Please stop recording" as something a person said. Yesterday's
`c0000` contained exactly that.

`announcement_stats` in the extraction artifact reports what was removed — the filter is
also the instrument that tells us what wording the recorders are actually using.

---

## 5. The session did not close early

This is the one that would be visible to you rather than to a log: a quiet stretch used to
stop touching the session, and prod infers an idle close, so **the confirmation email could
go out mid-meeting**.

The fix (sidecar-driven touch) has been on prod since PR #282 and the EventBridge rule is
`ENABLED`, but tonight's changes both alter how much silence flows through that path — a
looser threshold and normalisation change which chunks produce transcripts.

Check the confirmation email arrived **after** you stopped recording, not during.

---

## 6. What no check here can tell you

The two people furthest from a chest-mounted microphone are captured at about **5.3 of the
available 16 bits**. Normalisation redistributes loudness; it does not add information that
was never recorded. If attribution is still wrong for the people standing furthest away,
that is placement and device-side gain, not a backend setting — and no variable in this
document changes it.

---

## 7. What the night of 2026-08-08 added — and why it should change nothing you see

A second release went to prod that night (PR #305, merging #301–#304). **Both features in
it ship with their flags off, so nothing above changes and nothing new should be visible.**
This section exists so that if something *does* look different, you know where to look
instead of assuming the checks above lied.

| | prod | test |
|---|---|---|
| `ENABLE_GROUP_MERGE` (several devices, one record) | `false` | `true` |
| `EMIT_EVIDENCE` (extraction cites the transcript) | `false` | `true` |

Neither `PROD_*` repo variable exists, so both fall to the workflow defaults. Verified on
the deployed functions after the release, not read from the template.

**Three things did change on prod regardless of the flags**, because they are not behind
them:

1. **`topics.evidence` exists** (migration 0037) and `_TOPIC_COLS` now selects it. Every
   prod row is `NULL`, meaning *never measured* — which is deliberately distinct from
   "measured and cited nothing". Confirmed after the release by running the deployed
   column list against the prod database.
2. **`/live-items` responses now carry an extra `evidence` key** (null). The serializer has
   no allowlist, so it passes through. The dashboard is plain JS and ignores unknown keys.
3. **A failing group sweep can no longer roll back the finalize tick.** This one is worth
   stating plainly because it protects *your* morning: `sweep`, `reconcile` and the new
   group scan share one transaction, and the scan had no containment. A scan that raised
   would have rolled back `reconcile` — the step that moves a session to `sent` — so the
   symptom would have been **prod emails silently stopping**, looking nothing like a merge
   feature. It is now savepoint-isolated (PR #304).

### If the confirmation email does not arrive

Check §5 first — it is still the more likely cause. Then:

```bash
aws logs tail /aws/lambda/fieldsight-prod-finalize-sweep \
  --since 2h --filter-pattern "group sweep failed" --region ap-southeast-2
```

No output is the pass. (`aws logs tail --since` rather than
`filter-log-events --start-time` deliberately: the latter wants epoch
milliseconds, and a `<ms>` placeholder in a block you are copy-pasting is read by
bash as a redirection and fails before it ever reaches AWS.)

Silence there means the group path is not involved. It should be silent: the flag is off,
so the scan does not run at all.

### What you cannot test yet

**Multi-device merge.** The code is on prod but inert, and it should stay that way until
two real devices, one QR scan and one meeting have been through it — that is the only
evidence the feature delivers the coverage it claims. The synthetic run on test proves the
plumbing (claim → merged key → request artifact → correct routing), not the outcome.

Turning it on is one repo variable plus a redeploy, roughly ten minutes. It is not a thing
to do on the same morning you are testing recording quality, because if something is wrong
you will not know which change caused it.
