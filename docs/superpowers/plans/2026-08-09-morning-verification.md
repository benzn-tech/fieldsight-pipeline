# Morning verification — 2026-08-09

Everything that changed on prod on the night of 2026-08-08 acts **before anything
renders**. Normalisation, the VAD threshold, the tag filter and the announcement filter all
happen between the microphone and the report, so the dashboard looking normal is not
evidence that any of them worked — and, more importantly, not evidence that they did no
harm.

Each check below costs nothing and takes about a minute. Record one recording, then work
down the list.

Throughout, `{sid}` is the session id in the filename and `{date}` is the NZ date.

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
