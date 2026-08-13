# Correction propagation — implementation plan

**Spec:** `../specs/2026-08-13-speaker-correction-propagation.md` (revision 4, reviewed four
rounds)
**Date:** 2026-08-13

## Two gates before any propagation code is written

Both are cheap, both can kill or reshape the work, and building before either is answered
risks discarding it.

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

* **`τ′` failure semantics**: a cluster failing the homogeneity bound **caps at `tentative`**,
  mirroring the k=1 and label-disagreement idioms. Revision 4 defined `None` and left `False`
  undefined.
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

## Phase order

Each phase is independently revertible and inert until the one after it.

### P1 — clustering arithmetic in `voiceprint_utils` (inert)

**Tests first.** `cluster_turns(embeddings, tau)` → labels; `leave_one_out_centroid(members,
i)`; `assign(reference, centroids)` → (index, margin).

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
event**. Add `PutObject` on `voiceprint_results/*`; the function has `GetObject` only.

A test pins that the embedder has **no** way to obtain a profile except from the artifact, and
that neither artifact serializes a vector — one field is all it would take for biometrics to
land in S3 for the third time.

**This also fixes the defect found on 2026-08-13**: the deployed function is 100%
non-functional (`ModuleNotFoundError: No module named 'psycopg'`) because it carries the
cp312 VAD layer while `PsycopgLayer` is cp311-only. Pure compute removes the conflict instead
of building a second psycopg layer and adding python3.12 to both deploy workflows.

**Verify after deploy by invoking it**, not by reading the template — that is how the defect
was found, and no test could have caught it.

### P4 — the in-VPC writer (new function, inert without a caller)

Persists rows, applies precedence and supersession **in one transaction** (supersede then
insert) so an S3-event race cannot half-apply or collide on the unique index.

Needs: `lambda:InvokeFunction` granted to the embedder, and a **deploy-role check for every
new resource type** — a missing IAM prefix here has twice produced a silent success rather
than an error. `simulate-principal-policy` against the deployed roles, using the bucket name
read from the function's own environment (checking against a guessed bucket name produced
three false denials on 2026-08-13).

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
