# Correction propagation — implementation plan

**Spec:** `../specs/2026-08-13-speaker-correction-propagation.md` (revision 4, reviewed four
rounds)
**Date:** 2026-08-13

## Two gates before any propagation code is written

Both are cheap, both can kill or reshape the work, and building before either is answered
risks discarding it.

### Gate A — **CLOSED 2026-08-13: it separates them, τ = 0.85**

Results: `../specs/2026-08-13-gate-a-clustering-results.md`. Both Phase 0 sessions give k = 3
at 100% purity across τ ∈ [0.82, 0.88]; frozen at **0.85**, measured with onnxruntime on the
exported model and with the same `cluster_turns` that ships. In one session the provider had
returned **two labels for three people** and clustering recovered all three.

**τ′ and the cluster homogeneity check are removed from this plan.** Same-speaker pairs reach
0.813 against τ = 0.85 — a 0.037 gap, which is noise — so any τ′ tight enough to mean anything
fires on legitimate clusters. It is dropped rather than tuned, and the `False` → cap-at-
tentative decision below is withdrawn with it.

The original text follows, unchanged, because the reasoning is what made the answer worth
having.

### Gate A — does clustering separate Phase 0's three known speakers?

The spec's kill criterion. Phase 0's numbers are **profile-vs-turn** (same-person +0.104…+0.639,
different-person ≤ +0.205); clustering runs on **turn-vs-turn**, a distribution nobody has
measured. If no merge threshold separates three speakers whose ground truth is known, the
mechanism does not ship and the effort moves to Phase 5 matching.

1. ~~**Preserve the inputs first.**~~ **Checked 2026-08-13: they are safe.** 117 chunks
   remain under `users/Ben_UCPK2/audio/2026-08-11/` in prod, and the ground-truth fixture is
   in the repo. The bucket's three lifecycle rules cover `transcripts/` (90 d),
   `pending_downloads/` (7 d) and voice clips — **none reaches `users/`**, which is the
   deletion mechanism this step existed to get ahead of. So no copy is made: a second copy in
   the same bucket shares the blast radius anyway, and the risk it would insure against does
   not exist. Re-check if a lifecycle rule is ever added over `users/`.
2. Ground truth is checked in (`scripts/fixtures/2026-08-11-blockv-scripts.json`) and
   `scripts/speaker_session_eval.py` already derives per-turn truth from it. Extend it to
   emit the **turn-vs-turn cosine distance matrix** rather than adding a new script.
3. Sweep the merge threshold τ. Report, per τ: cluster count, purity against ground truth,
   and how many turns land in a cluster with fewer than two usable members.
4. **Freeze τ, and freeze τ′ in the same run** — the homogeneity bound is a *second*
   uncalibrated threshold and must not be invented later at a keyboard. Record both in the
   result artifact's `cluster_threshold` field so any row produced under a different value is
   automatically disqualified from calibration.

Expect a weak test: the distant speakers have 3–4 turns each and sub-3 s turns are excluded, so
some clusters will have one or two usable members. Report that honestly rather than reading a
clean number off a thin sample.

### Gate B — does step 1 show tentative names at all?

The spec calls this "a product decision that has to be made before build", and it is: under
tentative-only, `(可能是 …)` **is** the entire visible surface. Showing them is the fastest
route to correction data and also puts a name the system does not stand behind in front of a
human. The viewer work cannot be sequenced without the answer.

**Decided 2026-08-13: show them, in the transcript viewer only, with a one-tap confirm.**

The alternative — confirmed-only plus an "unnamed" count — makes step 1 collect nothing.
Corrections are the calibration input, and a user cannot correct a name they were never shown;
step 1 would run for weeks and produce no threshold, which is the one thing it exists to do.

The anchoring objection is real and is answered by the affordance rather than by hiding the
name: `(可能是 …)` next to a **confirm** and a **correct** control asks the reader a question
instead of asserting an answer, and turns silence — today indistinguishable from agreement —
into an explicit signal. Two constraints hold it in place: the tentative name never leaves the
viewer (not minutes, not email, not the responsible party), and a propagated name renders
visibly differently from one the user asserted.

Reversible: it is what step 1 displays, and step 2 replaces it either way. If the first real
session shows users accepting plausible wrong names, the fallback is the confirmed-only
display, and that is a viewer change with no effect on what has been collected.

## Decisions this plan makes, so they are not made silently later

* ~~**`τ′` failure semantics**~~ — **withdrawn**: Gate A measured no room for a second
  threshold at all. See above.
* **Ambiguous assignment of the corrected turn** uses the frozen `DEFAULT_MIN_MARGIN` (0.15)
  between the top two cluster distances — the floor was lifted for this one turn, and lifting
  a floor without naming the number replacing it is how the last three defects travelled.
  Additionally a **sub-3 s correction caps its propagation at `tentative`**: Phase 0's single
  miss was a 2.1 s turn.
* **The invoke is `RequestResponse`**, with the matcher's `FunctionError` check
  (`lambda_programme_matcher.py:701`). Under `Event` the 192-d enrolment vector would sit in
  Lambda's internal async queue and any DLQ — the biometric-residence defect relocating a
  fourth time, into a queue nobody would think to sweep.
* **Singleton clusters**: leave-one-out is undefined at n=1, so a cluster with fewer than two
  usable turns names only the corrected turn and propagates nothing.

## P0 — batching already broke the audio contract, and TEST is live

Found by the closing verification pass, confirmed against the code and against TEST's actual
objects. **This blocks P3 and changes the spec's audio rule.**

A batched turn's `source_filename` is `{first_chunk_stem}_bn{K}_off{T}_to{E}_srcwav.wav`, and
`_raw_key` splits on `_off`, so it builds `users/{f}/audio/{d}/…_c0000_bn4.wav` — **a key that
does not exist**, because raw uploads are per chunk. Worse, `start_sec`/`end_sec` are
**deliberately batch-relative** (`batch_stitch.py:417`): they index into the batch WAV, so even
a corrected filename would cut the wrong audio.

`TEST_BATCH_TRANSCRIPTION` is **true**, and the batch objects are already in TEST
(`audio_segments/Ben_UCPK2/2026-08-13/…_c0000_bn4_off0.0_to114.0_srcwav.wav`, today). So every
turn from a newly recorded TEST session is batched, and `match` would raise `NoSuchKey` on all
of them. It is inert — nothing invokes it — so nothing breaks tonight, but it is an interface
defect, not a bug in either feature: **both features' tests pass**, because the speaker tests
only ever feed per-chunk filenames.

The spec says "always the raw upload, never `audio_segments/`" because Phase 0's numbers are
raw-audio numbers. Batching puts the audio a turn points at **inside `audio_segments/`**, so
that rule and the deployed pipeline contradict each other. The resolution is neither to
abandon the rule nor to read the stitched copy:

**Translate through the batch map.** `{batch}_batch_map.json` sits beside the batch WAV and
exists for exactly this — it maps batch-relative coordinates back to member chunks. So when a
`source_filename` carries `_bn`, `match` reads the map, converts (batch WAV, batch offsets) →
(member chunk, chunk offsets), and then reads the **raw** chunk as before. The raw-audio rule
survives intact.

IAM: `GetObject` on `audio_segments/*_batch_map.json` only — narrow enough that the function
still cannot read the normalised audio, which is the thing the rule exists to prevent.

Tests must feed **both** filename shapes. A test suite that only knows one of them is what let
two green features disagree.

## Phase order

Each phase is independently revertible and inert until the one after it.

### P1 — clustering arithmetic in `voiceprint_utils` (inert)

**DONE 2026-08-13.** `cluster_turns(embeddings, tau)` → labels; `leave_one_out_centroid(
members, i)`; `assign(reference, centroids)` → (index, margin). `scripts/speaker_session_eval.py`
delegates to these rather than holding a copy, so the frozen τ stays measured against the code
that ships — re-running Gate A after the move reproduced every number.

Assert: two synthetic clusters separate and one does not split; **a two-member cluster does not
out-score a ten-member cluster on identical audio** (the leave-one-out guard — without it
margins are largest where evidence is weakest); n=1 raises rather than dividing by zero; pure
numpy, no scipy/sklearn (the VAD layer has neither and `voiceprint_utils` is deliberately
dependency-free); complete linkage stated in the docstring, since at complete linkage a
τ-bound homogeneity check can never fire.

**Rollback:** delete the functions. Nothing imports them.

### P2 — migration 0040 + repository (inert)

`source`, `correction_ref`, `cluster_ref`, `label_disagreement`, `superseded_at`,
`cluster_threshold`; `voiceprint_id` made nullable; partial unique index on
`(company_id, session_base, turn_ref) WHERE superseded_at IS NULL`.

**Numbered 0040** — 0038's header reserves 0039 as its own revert. The runner sorts by parsed
version with no contiguity check, so the gap is safe.

**Tests first**, and one of them is not about this migration at all: `confirmations_count`
must count **only** `source = 'correction'`. It currently counts every confirmed row
regardless of source, so propagation would satisfy the "N independent human confirmations"
criterion with its own output. Propagation rows also write `voiceprint_id NULL`. Both, because
either alone is one edit from being undone. A test asserts a table full of propagated rows
promotes nothing.

Phase 6 enumerability survives the NULL: propagation rows were justified by a *correction*,
and `correction_ref` chains them to it.

### P3 — the embedder becomes pure compute (changes a deployed function)

Remove `VpcConfig`, the PG environment, and every database import path. Add the S3-artifact
entry point — the handler is op-keyed today and **raises `ValueError` on an S3 `Records`
event**. ~~Add `PutObject` on `voiceprint_results/*`~~ — **stale**: the result goes to the writer by direct invoke, so that prefix does not exist. What the function actually needed was `GetObject` on `voiceprint_requests/*`, its own input, which was missed until a real correction was denied on the very object that triggered it.

A test pins that the embedder has **no** way to obtain a profile except from the artifact, and
that neither artifact serializes a vector — one field is all it would take for biometrics to
land in S3 for the third time.

**This also fixes the defect found on 2026-08-13**: the deployed function is 100%
non-functional (`ModuleNotFoundError: No module named 'psycopg'`) because it carries the
cp312 VAD layer while `PsycopgLayer` is cp311-only. Pure compute removes the conflict instead
of building a second psycopg layer and adding python3.12 to both deploy workflows.

**Verify after deploy by invoking it**, not by reading the template — that is how the defect
was found, and no test could have caught it.

### P4 — the in-VPC writer — **DONE 2026-08-13**

Deployed to test and verified by invoking it, not by reading the template: a deliberately
fake `company_id` came back `ForeignKeyViolation`, which proves psycopg imported, the VPC
reached Aurora, the SQL executed against the real table and 0040's columns exist — with zero
rows written. That is the same check that caught the embedder being 100% non-functional
behind a green deploy.

`simulate-principal-policy` on the live roles: the embedder may invoke the writer, `allowed`.

### P4 — the in-VPC writer (new function, inert without a caller)

Persists rows, applies precedence and supersession **in one transaction** (supersede then
insert) so an S3-event race cannot half-apply or collide on the unique index.

Needs: `lambda:InvokeFunction` granted to the embedder, and a **deploy-role check for every
new resource type** — a missing IAM prefix here has twice produced a silent success rather
than an error. `simulate-principal-policy` against the deployed roles, using the bucket name
read from the function's own environment (checking against a guessed bucket name produced
three false denials on 2026-08-13).

### P5 — the org-api endpoints — **HELD 2026-08-13, deliberately**

Not blocked on anything technical. **Another session is actively editing `lambda_org_api`'s
tests**, and P5 is the first phase here that touches that file — the highest-traffic function
in the platform and the one synchronous, no-retry path (a throttle there is an immediate 5XX
and permanently lost device data, BUG-43).

Two sessions editing it the same night is how a merge conflict becomes a silent behaviour
change in the one place that cannot absorb one. Everything before this point ships inert and
composes with anything; this does not. It waits until the other session's work has landed and
the file has one owner again.

### P5 — the org-api endpoints (changes live behaviour, gated)

`POST /api/org/sessions/{base}/speaker-corrections` — writes the request artifact. Reads
profiles through `profiles_for_matching` so the consent and `withdrawn` filters stay in the
one query where they cannot be forgotten.

Routes 404 when `SPEAKER_IDENTITY_MODE=off`. **`platform_admin` span-all must be taught to
each write endpoint separately** — the standing trap.

**BUG-33: the S3 trigger is wired manually outside the template** (`scripts/wire-s3-events.sh`).
Forgetting it deploys green with nothing firing.

### P6 — the read-time overlay

Precedence applied **at read**, not only at write: the join is by overlap with tolerance
because re-extraction shifts `start_sec`, so two live rows can match one physical turn even
with the unique index satisfied. Orphan rows are reported as a count, never dropped silently.

`confirmed` only into minutes, email, and action-item responsible party. The Phase-A-style
test applies: **fail if a raw cluster key, a `spk_N` string, or a tentative name reaches a
user-visible surface.** `decide_name` returns the winning cluster key as `name`, and `C_3` must
never be shown.

Must not write `session_finalize_requests/{sid}-updated.json` — that object sends an email.

## What ships and what does not

Step 1 (**tentative-only**) is the deliverable. `confirmed` is a threshold change afterwards,
not a code change, and only once step 1 has measured one — with the tap-to-confirm affordance,
because corrections alone label only what a user *noticed*, and `(可能是 X)` anchors the reader
toward accepting a plausible wrong name.

## Not in this plan

Cross-session propagation (Phase 5), names inside the LLM artifact (Phase 5 `on`), identity
merging across a multi-device group, un-naming on withdrawal (Phase 6).


## Known latent item, deliberately not touched (2026-08-13)

`batch_seal.measure_trim` measures seam overlap against the **raw** chunk but trims the head
of the **unit** pcm. Today every unit carries `off0.0` because `TRANSCRIBE_WHOLE_CHUNK` is on,
so the two coincide and nothing is wrong. **If whole-chunk transcription is ever switched off
and units acquire non-zero offsets, the trim misaligns** — and it misaligns quietly, as audio
cut from slightly the wrong place rather than as an error.

The code's own docstring records the assumption; there is no guard enforcing it.

Not fixed here on purpose: that is live batching code owned by another session tonight, and
the same reasoning that holds P5 applies — a change there costs a real feature if it collides,
while the item itself is latent and cannot fire under the current configuration. It belongs
with whoever next changes `TRANSCRIBE_WHOLE_CHUNK`, and the guard to write is a loud refusal
when a member's `_off` is non-zero.
