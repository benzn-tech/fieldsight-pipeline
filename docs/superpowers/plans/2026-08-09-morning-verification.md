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

> **Resolved 2026-08-08:** the owner confirmed the allowance is fine, and a separate key for
> test landed the same night (#309/#310), so evaluation runs no longer draw on the prod
> budget. The arithmetic below is kept because the *failure mode* is the point — it is the
> one thing that looks like a backend fault and is not.

**This was the most likely way the morning could fail, and it has nothing to do with
anything that changed that night.**

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

### ✅ test now has its OWN ElevenLabs key (split and verified 2026-08-08 night)

They used to hold the **same** key — compared by hash, not assumed — so every test
recording spent prod's allowance, and the quota that decides whether this morning works
was a shared pool with no isolation between the environment you experiment in and the one
you demo from.

**That is fixed.** `deploy.yml` now reads `secrets.ELEVENLABS_API_KEY_TEST`
(PR #309); `deploy-prod.yml` is untouched, so prod kept its key and needed no
redeploy. Verified after the test deploy: the two functions hold **different** keys by
hash, and the new test key does real work — `/v1/speech-to-text` with `scribe_v2` returned
200 on a one-second probe.

**So recording on test no longer costs you anything this morning.** Two guards pin it (the
environments must not read the same secret; the test binding must not contain `||`, which
would silently restore the shared pool when the secret is missing).

The remaining exposure is the plaintext Lambda env, deliberately **not** fixed: only one
function holds the key, it is non-VPC, and `NoEcho` already masks the value at the
CloudFormation layer (`describe-stacks` returns `****`, confirmed). That is real but
modest. The shared quota was the one that had already nearly cost a demo, and it is the
one that got fixed.

Nothing done on the night of 08-08 consumed any of prod's allowance: the extraction replay
copied an existing transcript rather than re-transcribing, and both the extraction and the
output-ceiling probes ran on qwen.

If the balance is short, top up rather than falling back:
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

## 5b. Chinese is no longer being deleted, or ignored

Two defects of the same shape landed on prod later on 2026-08-08, **after the sections above
were written**. Both were live for months; neither logged anything; every existing test was
in English, so nothing caught them.

The shape: **normalising text to ASCII makes every non-Latin character vanish**, and once
two different things both become the empty string, they compare equal.

### 5b.1 The chunk seam was deleting Chinese speech (PR #314)

`chunk_stitch._norm` reduced a word to `[^0-9a-z]` characters, so every CJK character became
`""`. Two empty strings match, and the dedup scans longest-run-first — so **any two Chinese
runs matched at full window length** and up to `max_window` characters were dropped from the
head of every chunk.

Measured: tail `今天天气不错啊` against head `钢筋合格证还没到今天再催` deleted **7 of 11
characters**. On a real seam it turned `我自己补充到三分钟` into `己补充到三分钟`.

**To check:** in a mixed-language recording, take the extraction's evidence quotes and find
a Chinese sentence that spans a chunk boundary. It should be whole. Before the fix, the
first few characters after a seam were missing.

```bash
aws s3 cp s3://fieldsight-data-509194952652/extractions/Ben_UCPK2/{date}/{session_base}.json - \
  --region ap-southeast-2 | python -m json.tool | grep -A1 '"quote"'
```

A Chinese quote starting mid-word — `己补充…` rather than `我自己补充…` — means it regressed.

**Known limitation, deliberate:** a duplicate still survives when the engine transcribes the
same overlap differently (`…around the gully` against `Back around the galley.`). A fuzzy
match was written, measured, and removed — the engine glues a boundary word to the next
sentence (`galley。12`), so the fuzzy run took the batch number with it and turned *"Batch 12
bricks"* into *"that batch of bricks"*. **Losing a number is worse than carrying a
fragment.**

### 5b.2 A Chinese question could not match a single word (PR #319)

Search's hybrid ranking tokenised the question with `[^a-z0-9]+` and a 3-character floor, so
a Chinese question produced **zero terms**. No row could then be `lexical`, the
lexical-first ordering did nothing, and only rows within 0.55 cosine distance survived —
while an English question keeps a literal term match *regardless of distance* and ranks it
first.

**The same question asked in Chinese returned strictly less than in English, sometimes
nothing.**

**To check:** search for something in Chinese that you know appears in a topic title —
`钢筋合格证`, `防水`, `脚手架`. It should come back, and a title containing the words should
rank above a merely-similar one. Then search `B2` — the 3-character floor used to drop it,
**in English too**, and zone references are what people actually search for.

If Chinese search returns nothing where the English equivalent works, it regressed.

---

## 5c. Did the whole recording actually arrive?

Everything above checks that the pipeline treated the audio correctly. This checks something
different and more basic: **whether all of the audio is there at all.** Two ways it may not
be, and **neither produces any warning anywhere today.**

```bash
python scripts/missing_chunk_audit.py --bucket fieldsight-data-509194952652
```

Runs over every recording ever made, costs nothing, and needs no new permission.

### What it can tell you

The device saves audio in ~30-second pieces, numbered in order: 0, 1, 2, 3… all uploaded
separately.

**A missing number** means a piece was recorded and never reached the server. Piece 9 and
piece 11 are there, piece 10 is not — like a numbered page missing from a document. Those 30
seconds are simply gone: no audio, no transcript, nothing in the report, and **nothing that
says anything is missing**, because the only thing that would have said so is the piece
itself.

Measured across every recording to date: **6 pieces, ~3 minutes, 0.9% of all pieces.** One
62-minute meeting lost 2.5 minutes of itself this way — about 4%.

**A short piece in the middle** means the recorder stopped and restarted; the seconds between
were never captured. A short piece at the *end* is normal — that is just where you stopped —
and the script deliberately does not report those.

### Reading the output

```
Ben_UCPK  2026-08-07  sid39ad6c92  (129 chunks present)
   NEVER ARRIVED: 5 chunks (~2.5 min) [10, 28, 30, 37, 42]
   short mid-session: c0072 holds 5.0s — recorder restart
   (pauses, not loss: after c0243)
```

- **NEVER ARRIVED** — the serious one. That audio does not exist anywhere.
- **short mid-session** — a restart. Seconds lost, and a signal about the device.
- **pauses, not loss** — you pressed pause and came back. Listed so it is visibly *not*
  counted as loss.

### When to worry

Occasional missing pieces are the upload path giving up after its retries — the freeze/thaw
work exists for exactly this, and is written but not yet merged. **A device that starts
losing pieces regularly, or restarting several times per meeting, is a device to swap out.**

Measured so far, the two devices are not alike: `Ben_UCPK` restarted once in 129 pieces,
while `Sam_Yu` restarted **five times in one 260-piece meeting** and produced another
recording where **four of its five pieces were cut short**. That is a device-health signal,
and nothing surfaces it today except running this.

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

### Why the deploy history shows extra prod releases you did not ask for

Late on 2026-08-08 the prod deploy list gained two runs that changed no behaviour, and one
of them **failed**. Both are explained, both are fixed, and neither touched `src/`:

- A merge carrying only `docs/` and `scripts/` triggered a full prod deploy, because
  `scripts/**` was missing from `paths-ignore`. Nothing under `scripts/` is packaged — every
  function builds from `CodeUri: src/`, and the deployed zip was byte-identical. `scripts/**`
  is now ignored by both deploy workflows.
- That run then **failed at pytest collection**, because `deploy-prod.yml` re-runs the tests
  with its own dependency list which never received the `numpy` that `test.yml` got. The
  release job reported `skipped`, not `failed` — so the prod release path was blocked with
  no red flag anywhere. Fixed in PR #339/#340, and verified by watching the resulting main
  run reach `tests=success` and `deploy-prod=success`. A new parity test now fails if any
  two pytest-running workflows disagree on dependencies.

After those merges the four switches this document depends on were re-read from the live
functions: `VAD_THRESHOLD=0.15`, `NORMALISE_AUDIO=true`, `TRANSCRIBE_WHOLE_CHUNK=true`,
`ASR_PROVIDER=elevenlabs`, `FILTER_AUDIO_EVENT_TAGS=true`. Unchanged.

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
