# Spec: assemble an enrolment from short utterances that already clustered together

**Status:** proposal, third draft after two reviews. One decision needed, at the end.
**Date:** 2026-08-18
**Repo:** `fieldsight-pipeline`. No frontend change.

> **The first draft of this document was wrong in a way worth keeping visible.** It proposed
> selecting candidates by ASR speaker label and claimed set agreement at `tau = 0.85` was
> "strictly stronger" than the guard it removed, citing Phase 0. Review found: the 31/32
> figure is a *nearest-profile* result that never ran clustering; `clusters == 1` is
> *identical* to `pair_max <= tau`, which this repository verified over 500 sets and which
> makes the "new question" the same statistic at 2.4x the threshold; and speaker labels are
> per transcription call, so selecting by label mixes people by construction. The idea
> survives. The argument did not.

---

## The idea

> 假设 Speaker zero 说了五句话…我把 Speaker zero 的名字改后…去现有的湖仓里面，
> 再切出来几段，把那五句话单独切出来，组成一个新的 30 秒到 60 秒的声纹。

Correct, and almost entirely built. What blocks it is one constant.

---

## What blocks it

`_admit_harvest` refuses any candidate under ten seconds (`lambda_speaker_embed.py:719`,
`ENROL_MIN_TURN_S = 10.0`). Five conversational utterances are three to eight seconds each.
**Every one is discarded before it is read.**

The floor is `2 x FRAME_SECONDS`. The homogeneity guard cuts a window into five-second frames
and compares them, so under ten seconds there are fewer than two frames,
`window_is_homogeneous` returns None — "cannot judge" — and the code refuses the unjudgeable
rather than assuming it. **The ten-second floor and `DEFAULT_MAX_FRAME_SPREAD = 0.35` are one
constraint seen twice**, and no setting of the threshold reaches a four-second utterance.

---

## Selection stays where it is: the session's own clustering

The first draft proposed selecting every segment sharing the corrected turn's
`speaker_label`. **That is worse than what exists**, for two reasons the code already states.

`lambda_voiceprint_writer.py:342`: *"Speaker labels are per transcription call; batching
merges namespaces across chunks on purpose … Grouping on the label alone would merge two
calls' `spk_0` into one person."* A session is many calls. "Speaker zero's five sentences"
spanning a session is a **mixed-person set as the normal case**, not as a failure case.

And it discards the only thing that makes the clustering trustworthy. Gate A
(`2026-08-13-gate-a-clustering-results.md`) is explicit that complete linkage does not work
because `tau` separates every pair — the measured distributions overlap heavily:

| cosine distance | same speaker (min/med/max) | different speaker (min/med/max) |
|---|---|---|
| session 1 | 0.245 / 0.538 / **0.813** | **0.747** / 0.897 / 1.039 |
| session 2 | 0.255 / 0.495 / **0.807** | **0.687** / 0.919 / 1.114 |

It works because with many turns per speaker, *some* cross pair exceeds `tau` and blocks the
merge. **A pool of five has no such statistical power.** Selecting by label and clustering
only the pool would put a different-speaker segment at 0.69–0.85 straight into a permanent
profile.

So candidates remain what they are today: members of the anchor's cluster, computed over the
whole session (`_propagate`, `lambda_speaker_embed.py:609-676`) — the configuration Gate A
actually validated.

---

## The change, in full

**One constant, one conflation removed, two guards added.**

1. `ENROL_MIN_TURN_S` falls from `10.0` to `DEFAULT_MIN_TURN_S` (3.0) — the floor below which
   this system already refuses to embed a turn at all (`:507`, `:583`), on record because
   Phase 0 found shorter utterances unreliable. Not lower: below it we would be inventing a
   number the way 0.35 was invented.
2. The per-candidate `window_is_homogeneous` call **stays, and refuses only on `False`**.
   Today `None` — "fewer than two frames, cannot judge" — is refused alongside `False`, and
   that conflation is the entire blocker: a four-second utterance is unjudgeable, not
   suspect. Admitting `None` removes the refusal without removing the guard.

   The second draft of this document proposed dropping the call outright. That was wrong for
   the same reason the first draft was: cluster members over ten seconds **are** judgeable —
   whole-chunk transcription routinely produces turns that long, a 108-second one is on
   record — so dropping it wholesale would remove a working, deployed guard from exactly the
   candidates it can still judge, to unblock a population it never applied to.
3. **The speech gate must be re-established, because it leaves with that call.**
   `FRAME_MIN_DBFS` (-55 dBFS) lives only inside `_frames` (`:425-458`); whole-turn embeddings
   never pass it. Today a near-silent candidate dies as "unjudgeable"; remove the homogeneity
   call and nothing on the harvest path looks at level at all — on a pipeline that has
   measured the ASR inventing 10.7 % of its words over VAD-zero audio. A candidate whose
   whole-turn dBFS is below the same floor is dropped.
4. **Each candidate faces the between-voices margin the anchor already faces.** `_propagate`
   computes anchor-versus-other-centroid margins against `DEFAULT_MIN_MARGIN = 0.15`
   (`:650-660`); at the point harvest is assembled the vectors, the groups and
   `leave_one_out_centroid` are all in hand (`:661-677`). A wrong-speaker cluster member is
   precisely one sitting close to another cluster's centroid — the 0.687–0.85 band Gate A
   measured — so extending that check per candidate costs a few cosines and aims directly at
   the residual risk this document could otherwise only declare.

Nothing else about selection changes. The second draft also listed "consume the existing
agreement order" as a change; it is not one — `_admit_harvest` already takes candidates in
that order under the caps (`:713-714`).

---

## What this is and is not, said plainly

**Only one thing is relaxed, and it is the conflation rather than the guard.** Today a
candidate must be a cluster member *and* produce a homogeneity verdict of `True`; `None`
("cannot judge") is refused with `False` ("two voices"). After this it must be a cluster
member, must not be judged `False`, must clear the level floor, and must clear the same
between-voices margin the anchor clears.

The first draft called its version "strictly stronger", which was backwards, and the second
draft over-corrected by dropping the guard entirely. Both were wrong in the same direction:
treating "the check is unhelpful here" as "the check is unhelpful".

**The residual risk is stated rather than mitigated:** a different-speaker segment that
survived session-wide clustering — Gate A measured 100 % purity on two sessions, 16 turns
each, same room, same device, same day — becomes a permanent sample. Nothing downstream
catches it in the case that matters most:

> `_agreement` (`voiceprints.py:185-222`) refuses a sample sitting closer to another profile
> than to its own. It fires only when **both** numbers exist: `own` is None for a profile's
> first sample, and `best_other` is None when the company holds no other consented profile.
> **In the bootstrap this change is for, no company holds any profile, so the backstop cannot
> refuse anything.** It starts working from the second profile onward.

---

## Therefore: measure before storing — and the switch for it does not exist yet

Because the admission decision has never been observed on real site audio, and because a
wrong acceptance is permanent, the first version of this **must not store**.

**The second draft claimed `SPEAKER_IDENTITY_MODE` already provides this. It does not, twice
over**, and the correction matters because it turns a free recommendation into a small piece
of work:

* **`shadow` never reaches the correction path.** `mode` is read only in the match path
  (`lambda_speaker_embed.py:1021`); `_from_request_artifact` propagates, enrols, harvests and
  invokes the writer without consulting it, and the writer has no mode gate at all. org-api
  404s on `off` and otherwise queues the artifact.
* **For corrections, `shadow` deliberately means "do write".** The template says so: under
  `shadow` corrections are accepted, rows land, and names appear in the viewer — that is the
  affordance the whole mode exists to collect evidence through.

So the measurement needs **its own switch** — `VOICEPRINT_HARVEST_MODE = measure | store`,
defaulting to `measure` — wired at all three segments the way `VOICEPRINT_MAX_FRAME_SPREAD`
was. Under `measure`, harvest computes the full selection and logs it: candidate count,
durations, each verdict and distance, the accumulated total, and which segments the budget
would have taken. It returns nothing to store.

**And the measurement has to be computed independently of the anchor's verdict**, or it
observes nothing. Harvest today runs only when the anchor enrolment was accepted (`:862`), the
anchor still faces the 0.35 check, and that check has refused every real window tried — so on
default settings zero selections would ever be logged, and a *short* corrected turn, the
motivating case, is unjudgeable, refused, and skips harvest entirely. Under `measure`, the
selection is computed and logged whatever the anchor did.

That turns the unanswerable question into a measurable one:

- do five real utterances of one speaker actually accumulate to thirty seconds?
- how often does a session's anchor cluster contain material the ordering ranks far below the
  rest — the shape a wrong-speaker segment would make?
- what is the real distribution of candidate durations, which decides the caps below?

**This is the same discipline the threshold work should have had and did not.** Three attempts
to justify moving 0.35 were withdrawn from one document because the measurement was built
after the conclusion. Here the measurement runs first and costs nothing irreversible.

---

## Arithmetic the first draft got wrong

Five utterances of three to eight seconds is **15 to 40 seconds**, not "thirty comfortably".
At the low end the target is not reached at all.

`ENROL_MAX_SAMPLES = 6` (`:135`) caps the assembly at six contributions — **18 seconds if they
run three seconds each**, and the anchor makes seven rows, not six. Reaching thirty seconds
from short utterances needs roughly ten.

So the caps have to move with the floor, and by how much is one of the things the shadow run
is for. A first guess of `ENROL_MAX_SAMPLES = 12` with `ENROL_MAX_SECONDS` unchanged at 60 is
a guess; the measurement replaces it.

**And if thirty seconds is not reached, store nothing** — a two-sample profile that names
people is worse than none, because it names them wrongly and nothing marks it as thin.

---

## The anchor is not touched

The corrected turn is what a human vouched for, and its protections stay: the between-voices
refusal in `_propagate` (`:648-659`), and its own homogeneity check.

The first draft proposed checking the anchor "the same way — does it join the cluster", which
is circular: the cluster is *defined* as the one containing the anchor. Worse, complete linkage
merges the smallest worst-pair first, so a two-voice anchor can merge with a wrong-person
segment before the genuine ones and produce a poisoned two-sample profile while the real
material is "dropped individually".

**Consequence, stated because it limits what this delivers:** while the anchor still faces the
0.35 check, a correction on a short turn still enrols nothing. This change makes the *harvest*
half work once a correction lands on a turn the anchor check accepts. It reduces the 0.35
problem; it does not remove it. The TEST override (`VOICEPRINT_MAX_FRAME_SPREAD`, now `0.7`)
is the lever for the anchor meanwhile, and it is a lever, not an answer.

---

## Store the pieces separately, not concatenated

The **selection** in 组成一个 30–60 秒的声纹 is right; the **assembly** should not happen.

One row per contribution keeps the populations separable — what a person vouched for versus
what the clustering suggested — which is the distinction the whole harvest design rests on,
and it keeps `samples` honest in `GET /api/org/voiceprints`.

Two corrections to the first draft's reasoning, which overstated the case:

- *"the model averages the embeddings either way"* is false. Matching takes the **max** over a
  person's samples (`aggregate_scores`, `voiceprint_utils.py:92-101`), and a spliced clip
  under `MAX_EMBED_SECONDS = 45` is a single forward pass (a 46–60 s splice would be
  piecewise-averaged, so the target range straddles the boundary).
  Splicing five utterances would produce **one** vector where five give five chances to match.
  That is a stronger argument for storing separately than the one first given.
- *"a profile is repairable by deleting the sample that poisoned it"* describes an intention,
  not the code. The only deletion is `withdraw` (`voiceprints.py:335`), which removes **every**
  sample. Per-sample deletion would have to be built.

---

## Provenance must stay distinguishable

`admitted_max_spread` is NULL today when a sample was admitted under the ordinary default
guard, and non-NULL rows are "exactly the set worth re-examining" (`lambda_speaker_embed.py:399`).
Samples admitted with **no spread check at all** must not also be NULL — that collapses the two
populations the column exists to separate.

Use a **new third** `source` value — not `correction_propagation`, which harvest samples
already carry and which currently means "harvested *and* homogeneity-passed"; TEST rows
written under the 0.7 override have exactly that shape, so reusing it collapses the two
populations this section exists to keep apart. A new value is safe downstream:
`has_human_sample` and `humanSamples` key on `'correction'` only (`voiceprints.py:567`,
`:611`), and `source` is unconstrained text (`migrations/0038`), so no migration.

---

## What changes in code

| file | change |
|---|---|
| `lambda_speaker_embed.py` | `ENROL_MIN_TURN_S` -> `DEFAULT_MIN_TURN_S`; drop the per-candidate homogeneity call; add a whole-turn dBFS floor; consume the existing agreement order; raise `ENROL_MAX_SAMPLES` |
| `lambda_speaker_embed.py` | under `shadow`, compute and log the selection, store nothing |
| `lambda_voiceprint_writer.py` | a distinct `source` for set-admitted samples |
| — | no migration, no schema change, no org-api change, no frontend change |

---

## The decision I need

**Does the first version store, or only measure?** I recommend measure — one session's real
numbers would settle the caps, the duration question and the wrong-segment question at once,
and a wrong acceptance cannot be taken back.

Everything else here follows from that answer.
