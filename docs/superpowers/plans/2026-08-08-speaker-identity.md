# Plan — speaker identity

Design: `specs/2026-08-08-speaker-identity-design.md`

**Phase A ships value with no new infrastructure. Phase 0 is a recording session and a
measurement whose failure cancels Phases C and D.** Nothing here touches a live path until
Phase A, and nothing stores data about a person until Phase D.

Sequence: **Phase 0 (measure) → A (stop lying) → B (the wearer) → C (session clusters) → D
(cross-session names)**. B is deliberately before C: it is smaller, it delivers a correct
`speaker_count` gate, and its one required outcome from Phase 0 is the one most likely to hold.

---

## Phase 0 — get the right recording, then measure

**The existing material is not sufficient.** The 2026-08-08 22:48 session has two speakers, and
the canonical failure — the wearer forming one clean cluster while *all* distant speakers
collapse into a single "other" — produces exactly two clusters on a two-person recording and
**looks like success**.

### 0.1 Record the session that can fail honestly

Needs **≥3 people, at least two of them distant (3–5 m)**, chest-mounted as in production.
Write down who said what and roughly when — that is the ground truth, and it costs a notepad.
Reuse the segment structure that worked: normal, quiet/turned-away, noise-only, silence.

**This is a blocking human task.** Everything below waits on it.

### 0.2 Build the evaluation harness (free, no ASR credits)

Inputs already in place — the slicing contract is verified:
turns carry `source_filename` + in-file `start_sec`/`end_sec`; under
`TRANSCRIBE_WHOLE_CHUNK=true` the `audio_segments/` WAV is the whole 30 s chunk, so offsets are
sample-exact against it.

Before slicing, the harness must replicate what assembly does, or the corpus is wrong:
- `_dedup_turn_boundaries` — the mobile ~2 s ring-buffer overlap otherwise yields **duplicate
  embeddings at every seam** (and note it is currently unreliable — see the seam-duplication
  investigation; fix that first or the duplicates are silent)
- `filter_device_announcements` — a machine must never seed a cluster
- `filter_audio_event_tags` — `[background noise]` is not a speaker

Pick and export the model: **WeSpeaker ships ONNX; SpeechBrain ECAPA→ONNX is not turnkey.**
Choose deliberately, record the choice.

### 0.3 Measure, both raw and normalised

Not "assume normalised is better". `normalise_for_asr` is best-effort and falls back silently,
so `audio_segments/` is a **mixture**; and `acompressor` 4:1 + `loudnorm` applied **per 30 s
chunk** is nonlinear, time-varying gain that alters the very cues embeddings use. The
untouched originals are at `users/…/audio/`.

Report, per condition:

1. **splitting / merging / phantoms**, scored separately, against the written ground truth
2. whether each distant speaker separates, or collapses into the wearer
3. **two thresholds** — within-session clustering, and cross-session matching — each chosen
   from the measured distribution, erring toward "don't know"
4. a **minimum turn duration** floor: sub-second turns give noise embeddings that seed phantom
   clusters

**Exit gates:**
- wearer separates → Phase B is viable
- near speakers separate → Phase C is viable
- **distant speakers separate → Phase D is viable. If not, stop after C.**

---

## Phase A — stop presenting a label as an identity

No new infrastructure. Ships regardless of Phase 0.

1. Stop emitting per-chunk `spk_N` as though it were a person, in the extraction prompt, the
   minutes and the email. Today `spk_0` is the recording device in `c0000` and a person in
   `c0001` of the same session.
2. **Decide the `speaker_count` gate deliberately.** It is the label-string union, computed in
   **two** places — `lambda_extract_session.py:1491` (single device) and **`:1240` (group
   merge, a union across devices' independent label spaces, already inflated)** — and consumed
   at **`lambda_item_writer.py:580`** gating `== 1` to resolve a self-referential responsible
   party to the wearer. It read 2 on the 22:48 session **by coincidence**: every chunk emits
   `spk_0`/`spk_1`, so the union is 2 whoever was in the room.
   Either keep it computed on unqualified labels and document that it is a label count, or
   restate the gate — **covering both computation sites**.
3. Add a test that fails if a `spk_N` string reaches a user-visible surface.

**Verify:** re-run the 22:48 session's extraction and confirm no `spk_N` appears in the
artifact, the minutes or the email, and that a genuine solo session still resolves its
self-referential responsible party.

---

## Phase B — the wearer, and nothing else

Depends on Phase 0 exit gate 1 (near-certain).

The device's owner is known from device assignment, so this needs **one** voiceprint per owner,
**no naming UI**, and no judgement about anyone else. It converts the `speaker_count == 1` gate
from coincidentally-right to right.

1. Embed the wearer's turns from sessions already recorded on their device; store one
   company-scoped voiceprint (schema in Phase D, created early — one migration, not two).
2. Per session, label turns wearer / not-wearer, above the measured threshold only.
3. Below threshold → **not-wearer is "unknown", never a guess.**

---

## Phase C — within-session anonymous clusters

Depends on Phase 0 exit gate 2. **No storage about any person, no consent question, no UI.**

1. **Name the compute unit and its trigger.** There is no worker for this today, and the
   obvious trigger is unavailable: **S3 event configs are managed manually outside the stack
   and `audio_segments/*.wav` already notifies the transcribe function — S3 rejects
   overlapping notification configs.** Use the existing request-artifact pattern (as
   `extraction_requests/` does) or an explicit fan-out. Decide before writing code.
2. **Budget the runtime.** A 70–130 minute session is ~140–260 chunks to fetch and embed
   against a 900 s timeout. Per-chunk embedding at ingest plus clustering at close is the
   likely shape; measure before committing.
3. **Design the session artifact.** Nothing today can express turn → cluster, cluster → sample
   clip, or "this cluster is unnamed". Without it the UI cannot enumerate clusters and
   extraction cannot consume labels.
4. **Decide the merge question.** Clustering *provider* turns fixes cross-chunk label
   instability but **inherits every merge the provider made** — and for distant speakers the
   merge is the normal case, not an edge case, because a turn is built from consecutive items
   sharing the provider's `speaker_label`. Escaping it means fixed sub-turn windows plus
   re-attributing words to windows — **building a diarizer.** Either budget for that or state
   plainly that Phase C inherits provider merges.
5. Deployment facts to settle first, none of which are template-only edits:
   **no `scipy`/`sklearn` in the layer** (hand-roll agglomerative clustering on numpy, or
   rebuild the layer); `sitesync-vad-layer:2` vs **cp312-only** `fieldsight-vad-layer` are
   different things and `template.yaml:355–359` warns about conflating them; layers are built
   outside the stack and passed as ARN parameters; Aurora access needs `PsycopgLayer` +
   `VpcConfig` and the combined unzipped size against the 250 MB limit is unverified; and
   **IAM must be checked with `simulate-principal-policy` for both the new function's role and
   `github-actions-fieldsight-deploy`** — a missing deploy-role permission fails stack
   creation and blocks the pipeline.

---

## Phase D — cross-session names

**Only if Phase 0 exit gate 3 passed.** This is the regulated, cross-repo part.

1. Migration `0038` for `speaker_voiceprints` (schema in the design). Centroid rules are
   load-bearing: re-normalise after every update, **fold in only human-confirmed samples —
   never an auto-match**, and give the fold-in an idempotency key so a retried worker cannot
   double-count.
2. **Ordering — the core decision, already made:** gating the final extraction on identity is
   **not viable** (a first-ever cluster needs a human, and the final pass feeds the email
   promised 1–2 minutes after stop). Use **one bounded re-run**, with:
   - a **separate re-run reason and budget** — `FINAL_RERUN_MAX_GENERATIONS = 3` is shared with
     growth re-runs and would be silently consumed
   - **explicit email suppression**, because `lambda_item_writer` writes
     `session_finalize_requests/{sid}-updated.json` on update and a re-run would otherwise
     **re-email attendees**
3. **Backfill:** sessions extracted before a name existed have no path to acquire it. Decide
   and write it down — forward-only, or build backfill. Do not leave it implicit.
4. **Consent, directed at the subject, not the namer.** Named account holders get notice and
   deletion. People with **no account** have no channel for notice: either exclude them from
   voiceprint storage or define how notice is given. **Do not ship this undecided.**
5. UI surface: which org-api endpoint, which role — and per the standing trap **every new write
   endpoint must teach `platform_admin` span-all separately**. The sample clip must be
   presigned through **org-api**, not the legacy gateway, whose media-presign 403s Aurora-only
   accounts. The naming UI lives in `fieldsight-ui`, so this phase spans repos.

---

## Prerequisite outside this plan

**Fix the chunk-seam duplication first.** `dedup_overlap` compares word lists for exact
equality and the same ~2 s of overlap audio does not transcribe identically — measured on the
22:48 session: one seam deduped, one failed (`gully` vs `galley`), one over-trimmed a Chinese
word. Every phase above slices audio by turn, so duplicated or truncated turns become
duplicated or truncated embeddings. The fix is to drop the later turn's leading words **by
absolute timestamp** rather than by string comparison — the timestamps are already there.

## What this plan will not do

- Re-open provider selection.
- Use a provider speaker library (workspace-scoped; our key cannot reach it).
- Re-transcribe anything.
- Attempt to fix distance. A chest microphone puts the wearer at 20 cm and everyone else at
  2–5 m, and distant speakers occupy ~5.3 of 16 bits. Some of the target audio is **not even
  captured**: `DROP_SILENT_CHUNKS=true` means a chunk VAD judges silent produces no
  `audio_segments/` object at all, so speech below `VAD_THRESHOLD` never reaches an embedder.
  That part is upstream and unfixable here.
