# Speaker identity — design

**Date:** 2026-08-08
**Status:** Design, **phased**. Replaces `2026-08-08-whole-session-diarization-design.md`
and supersedes `2026-08-07-speaker-attribution-measurement-design.md` (withdrawn in its §0).
**Direction (user, 2026-08-08):** don't go deep; use what exists; copy the behaviour of
consumer recorders — name a person once, and they are labelled automatically after.
**Review:** an adversarial pass on the first draft found that draft's central claim false
(§1) and its "shallow" framing unsupported (§2). This version is phased so the cheap, safe
value ships before the expensive, regulated part, and so the measurement that decides
whether the expensive part is possible happens first.

## 0. Where the value is, and why it must be phased

The product need is "who said this". Four different things deliver parts of it at wildly
different cost, and the previous draft presented them as one unit:

| | delivers | needs |
|---|---|---|
| **A. Stop lying** | removes confidently wrong labels | nothing new |
| **B. The wearer** | separates the device owner from everyone else | identity already known from device assignment |
| **C. Within-session clusters** | consistent "Speaker A/B" across a whole session | embedding + clustering; **no storage, no consent** |
| **D. Cross-session names** | the Plaud behaviour — name once, auto after | a table, a UI, biometric consent, re-extraction plumbing |

**C is a strict prerequisite measurement for D**: if voices cannot be separated on this
audio, D cannot work no matter how it is built. So C is both a deliverable and the gate.

## 1. The draft's central claim was half wrong — correcting it

The draft said: *identity can come from the audio directly, so the provider's per-chunk
label resetting stops mattering.*

**The vectors would be ours. The segmentation would still be the provider's.**
`transcript_utils.speaker_turns_from_items` builds a turn from *consecutive word items
sharing a `speaker_label`* — the provider's label is the boundary. So:

- Embedding a turn inherits every merge the provider made.
- The people this work exists for are **precisely the ones the provider merges away** (§4),
  so for them the merge is not an edge case, it is the normal case. A turn containing two
  distant speakers yields one blended centroid, silently.
- The ground-truth set already shows three of eighteen turns holding two sources.

**Escaping that means embedding fixed sub-turn windows and re-attributing words to windows —
building a diarizer.** That is not shallow, and any phase that claims to fix merges must
budget for it. Phases A–C below are honest about which of them do and do not.

## 2. What "use what exists" actually buys, and what it does not

Verified against the repo, because the draft overstated this.

**Genuinely already there:**

| | evidence |
|---|---|
| Vector storage + cosine search | `pgvector` installed; `report_chunks.embedding vector(1024)` + `hnsw (… vector_cosine_ops)`. `vector(192)` (ECAPA-class) is well under pgvector's 2000-dim limit |
| Tenant isolation | `users.company_id`, company-scoped row filtering is the model throughout |
| ONNX-from-S3 in Lambda | `models/silero_vad.onnx` fetched from S3 by a function whose layer carries `onnxruntime` |
| Per-turn audio is recoverable | turns carry `source_filename` + in-file `start_sec`/`end_sec`; under `TRANSCRIBE_WHOLE_CHUNK=true` the `audio_segments/` WAV is the whole 30s chunk, so offsets are sample-exact against it |

**Not there, and each is real work:**

- **No clustering library.** The layer has `ffmpeg`, `onnxruntime`, `numpy`, `soundfile` —
  **no `scipy`, no `sklearn`**. Agglomerative clustering must be hand-rolled on numpy or the
  layer rebuilt.
- **Two different layers, easily conflated.** `template.yaml:355–359` warns explicitly:
  `sitesync-vad-layer:2` vs `fieldsight-vad-layer` (**cp312-only**). A new function must pin
  its runtime accordingly and handle the `HasVadLayer` condition.
- **Layers are built outside the stack** and passed in as ARN parameters. Adding a model is
  not a template edit alone.
- **S3 event triggers are configured manually, outside the stack**, and `audio_segments/*.wav`
  already notifies the transcribe function. **S3 rejects overlapping notification configs**,
  so an embed stage cannot hang off that event — it needs the existing request-artifact
  pattern (as `extraction_requests/` does) or an explicit fan-out.
- **Aurora access needs `PsycopgLayer` + `VpcConfig`.** Combined unzipped layer size against
  the 250 MB limit is unverified.
- **IAM, twice.** The new function's role *and* `github-actions-fieldsight-deploy` both need
  checking with `simulate-principal-policy` — the repo's standing trap, where a missing
  deploy-role permission fails stack creation and blocks the pipeline.
- **No ONNX export chosen.** SpeechBrain ECAPA→ONNX is not turnkey; WeSpeaker ships ONNX.

**No session-level artifact exists** for cluster results. Nothing today can express
"turn → cluster", "cluster → sample clip", "this cluster is unnamed". That has to be
designed (§6), and the draft's diagram had arrows with no boxes.

## 3. Evidence this design rests on — where it actually lives

The draft cited measurements that exist only inside the draft. Pointers, so the plan can
start:

- **Session with two known speakers and a script as ground truth:**
  `sidfb57faf959ed40d68ca8b02797605a20`, prod, 2026-08-08 22:48 NZ, 13 chunks.
  Audio `users/Ben_UCPK2/audio/2026-08-08/…_c{0000..0012}.wav`;
  sidecars and segments under `audio_segments/Ben_UCPK2/2026-08-08/`.
  Script: `Dropbox/temp/fieldsight-audio/录音脚本-2026-08-09.md`.
  Measured levels: wearer chunks −28 to −31 dBFS; the deliberately quiet turn (`c0003`)
  **−54.0 → −18.8 dBFS** after normalisation; silence chunks −53 to −59.
- **Hand-labelled 18-turn set (a different session):** `Dropbox/temp/fieldsight-audio/`
  (`UCPK2_2026-08-07_1522-1527_GROUNDTRUTH.md`).
- **Provider capability checks (2026-08-08):** ElevenLabs `/v1/speech-to-text/speakers`
  returns 401 (path exists, our key lacks scope; `voices_read` denied) and its speaker
  library is workspace-scoped — a multi-tenant hazard. Qwen3-ASR offers no voiceprints
  (verified 2026-08-04; the July paper bolted on an external CAMPPlus embedding).

**⚠️ This material is not sufficient for the decisive test.** See §5.

## 4. The constraint that decides everything

The device is chest-mounted: the wearer ~20 cm from the microphone, everyone else 2–5 m.
Distant speakers occupy roughly **5.3 of the available 16 bits**. Embedding discriminability
falls with SNR and channel mismatch, so the expected outcome is: the wearer separates
cleanly, near speakers probably separate, **and the two furthest people — the reason this
work exists — are unknown.**

Two further facts make the target population *partially unreachable*, which the draft missed:

1. **`DROP_SILENT_CHUNKS=true`.** A chunk VAD judges silent produces **no `audio_segments/`
   object at all**. Distant speech quiet enough to fall under `VAD_THRESHOLD` is absent from
   the corpus an embedder could read. That part of the failure is upstream of embedding and
   cannot be fixed by it.
2. **Normalisation is not a clean win for embeddings.** `normalise_for_asr` is *best-effort*:
   on ffmpeg failure or a timebase mismatch it silently returns the original, so
   `audio_segments/` is a **mixture** of conditioned and raw chunks. And the filter is
   `acompressor` 4:1 with 8 dB makeup plus `loudnorm`, applied **per 30-second chunk** —
   nonlinear, time-varying gain, so the same speaker's effective channel gain varies chunk to
   chunk. Compression alters exactly the spectral-dynamic cues embeddings rely on. The draft
   asserted this helps. **It must be measured both ways**; the untouched originals are at
   `users/…/audio/`.

## 5. Phase 0 — the measurement, and why the existing material is not enough

**The canonical failure this must detect:** with a dominant wearer and an SNR-asymmetric room,
the wearer forms one clean cluster and *every* distant speaker collapses into a single
"other". On a two-person recording that produces **two clusters and looks like success.**

So the 22:48 session **cannot validate the goal**. Phase 0 needs a session with **≥3 people,
at least two of them distant**. Getting that recording is the first task, not an afterthought.

What to measure, on both raw and normalised audio:

1. Do embeddings separate the speakers? Score **splitting / merging / phantoms** separately,
   as the existing ground-truth set does.
2. Does the quiet turn cluster with its speaker or collapse into the wearer?
3. Does the device announcement (`c0000`'s "Recording started") form its own cluster? It is a
   machine and must never become a person.
4. **Two thresholds, not one**: a within-session clustering threshold and a cross-session
   match threshold. Pick both from measured distributions and err toward "don't know".
5. **A minimum turn duration.** Word-level turns run well under a second, where ECAPA-class
   embeddings are noise that seeds phantom clusters. State the floor.

**Exit:** if speakers do not separate even for near speakers, ship Phase A only and stop.

## 6. The phases

### Phase A — stop presenting a label as an identity (no new infrastructure)

Today `spk_0` means the recording device in `c0000` and a person in `c0001` of the same
session. That reaches the extraction prompt, the minutes and the email.

**This is not free, and the earlier draft was wrong to say it was.** `speaker_count` is the
size of the label-string union, computed in **two** places —
`lambda_extract_session.py:1491` (single device) and **`:1240` (group merge, a union across
devices' independent label spaces, so group artifacts already inflate it)** — and consumed at
**`lambda_item_writer.py:580`**, gating `== 1` to resolve a self-referential responsible party
to the wearer. On the 22:48 session it read 2, **correct by coincidence**: every chunk emits
`spk_0`/`spk_1`, so the union is 2 regardless of how many people were present.

So Phase A must decide the gate deliberately and cover both computation sites. Do not
chunk-qualify labels and assume it still works.

### Phase B — the wearer, and nothing else

The device's owner is known from device assignment. Separating "the wearer" from "not the
wearer" needs **one** enrolled voiceprint per device owner, no naming UI, and no judgement
about anyone else. It also makes the `speaker_count == 1` gate correct rather than
coincidental, which is the concrete defect Phase A can only paper over.

Smallest thing that delivers real value. Depends on Phase 0 showing the wearer separates —
the one outcome that is close to certain.

### Phase C — within-session anonymous clusters

Consistent "Speaker A / Speaker B" across a whole session, surfaced in the transcript and the
minutes. **No storage of anything about a person, no consent question, no UI for naming.**

Requires: the embed + cluster worker, the session artifact, and — if merges matter — the
window-level embedding of §1. Delivers most of the readable-output value, and *is* the
measurement that gates Phase D.

### Phase D — cross-session names (the Plaud behaviour)

Only after Phase 0 proves distant separation. This is the part that needs the table, the UI,
the consent answer and the re-extraction plumbing.

```sql
-- next migration is 0038
CREATE TABLE speaker_voiceprints (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id) ON DELETE CASCADE,
  display_name  text,
  embedding     vector(192) NOT NULL,
  sample_count  int NOT NULL DEFAULT 1,
  s3_sample_key text,
  created_by    uuid REFERENCES users(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (user_id IS NOT NULL OR display_name IS NOT NULL)
);
CREATE UNIQUE INDEX uq_voiceprints_company_user
  ON speaker_voiceprints (company_id, user_id) WHERE user_id IS NOT NULL;
CREATE INDEX idx_voiceprints_company ON speaker_voiceprints (company_id);
CREATE INDEX idx_voiceprints_embedding
  ON speaker_voiceprints USING hnsw (embedding vector_cosine_ops);
```

**Centroid rules, stated because the draft left drift possible:**
re-normalise after every update (cosine space); **fold in only human-confirmed samples, never
an auto-match** — otherwise one bad match contaminates every future match; and the fold-in
needs an idempotency key, or a retried worker counts the same sample twice.

**Every query filters `company_id`. No cross-company matching, ever** — not as a feature, not
as a fallback.

## 7. The ordering problem, which is the core design decision

Names must reach `assemble_session_turns` *before* the prompt is built, or they change nothing
the user sees. Two candidates, and the draft declined to choose:

- **Gate the final extraction on identity — not viable.** The final pass is triggered at
  session close via `extraction_requests/` and feeds the confirmation email promised 1–2
  minutes after recording stops. A first-ever cluster needs a *human* to name it, so gating
  delays that email unboundedly.
- **One bounded re-run — viable, with conditions.** `_request_final_rerun` exists, but its
  budget (`FINAL_RERUN_MAX_GENERATIONS = 3`) is **shared with growth re-runs**, so a
  naming-triggered re-run silently spends it. And `lambda_item_writer` writes
  `session_finalize_requests/{sid}-updated.json` on update, so **a re-run can re-email
  attendees**. Therefore: a separate re-run reason with its own budget, and explicit email
  suppression for identity-only re-runs.

**Backfill is unsolved and must be stated as such**: sessions extracted before a name existed
have no path to acquire it. Either accept it (names apply forward only) or design a backfill —
but say which.

## 8. Consent — directed at the right person

The draft put the notice in the naming dialog. That informs the **namer**, not the subject.
Under the NZ Privacy Act and the Biometric Processing Privacy Code, notice and consent concern
the person whose biometric data is stored.

- Named **account holders** must be notified that a voice signature exists for them, and be
  able to have it deleted.
- People with **no account** (`display_name`-only rows) have no channel for notice at all.
  Either exclude them from voiceprint storage, or define how notice is given. **Do not ship
  this undecided.**
- What is stored is a **vector, not audio**, plus one short clip so a disputed match can be
  checked by ear — held under the same access rules as the recording it came from, and it
  must not become a back door around them.
- Per company. Never shared, never matched across companies. Deletable.

## 9. Remaining unspecified surface (all of Phase D)

Named so it is not discovered late: which org-api endpoint and which role may name a speaker
(and, per the standing trap, **every new write endpoint must teach `platform_admin` span-all
separately**); the playable sample must be presigned through **org-api**, not the legacy
gateway, whose media-presign 403s Aurora-only accounts; the naming UI lives in
`fieldsight-ui`, so Phase D spans repos; and the multi-device group path computes its own
`speaker_count` union across devices, which any restatement of the gate must cover.

## 10. What this does not do

- **No enrolment flow.** No "record your voice for 30 seconds". A voiceprint exists only
  because a human named a cluster that occurred in a real recording.
- **No provider speaker library.** Workspace-scoped, and our key cannot reach it (§3).
- **No re-transcription.** Words keep coming from the existing per-chunk path.
- **No fix for distance.** §4 — that is microphone placement and device-side gain, and it is
  irreversible once recorded.
