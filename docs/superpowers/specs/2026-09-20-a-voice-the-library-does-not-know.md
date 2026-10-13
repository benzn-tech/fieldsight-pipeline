# A voice the library does not know: rejection floor, mean pooling, multi-occasion enrolment

**Result: the deployed pipeline has a hole exactly where 2026-09-10 sits, and two independent
levers close most of it at once — but only if built together.** On 09-10, nobody in the
enrolment library was in the room. Every one of the 265 slices scored there is a false
positive by construction; the only open question was how high the worst one climbs. On the
deployed model, ECAPA-TDNN, it climbs to **+0.445** (Mike) with a margin over the runner-up of
0.268 — comfortably clear of `DEFAULT_MIN_MARGIN = 0.15`. `decide_name`
(`src/voiceprint_utils.py:96-126`) has no step that asks whether 0.445 is a plausible score for
a real match at all; it only asks whether it beats the second place. On this room, it would
confirm Mike.

| model | 08-13 true positive | 09-10 worst false positive | 08-07 best Ben |
|---|---|---|---|
| ECAPA-TDNN (deployed) | +0.655 | +0.445 (Mike) | +0.484 |
| CAM++ | +0.467 (beaten by Mike 0.484) | +0.343 | +0.360 |
| ERes2NetV2 | +0.545 | **+0.528 (Leo)** | +0.439 |

ECAPA is the only model of the three that keeps a positive margin on every label where Ben was
actually present, and it is the deployed model — no change proposed here touches it (see
§6, Rejected Alternatives). The gap this spec closes is not in the embedder; it is in what the
decision chain does with the embedder's output, and in how sparse each profile's evidence is.
Three changes, specified below, only work together: a floor with nothing to compare a lone
sample against is noise; mean pooling with a profile of one clean read-aloud sample is just a
smoother version of the same single data point; and multi-occasion enrolment without a floor
just gives the false positive a *better* score to hide behind (pooled MAX turns Ben into the
top scorer on 09-10, at **+0.054 / +0.055**, worse than doing nothing).

## 1. A rejection floor, calibrated per company

### 1.1 What is missing today

`decide_name` (`src/voiceprint_utils.py:96-126`) runs two checks, in this order: a duration
floor (`duration_s < min_turn_s` -> `unknown`, lines 108-111), then a margin over the runner-up
(`margin >= min_margin` -> `confirmed`, else `tentative`, lines 121-125). There is no third
check that asks whether the winning score itself is high enough to mean anything. A turn with
no enrolled speaker present still produces a winner — the *least* dissimilar profile in the
list — and if that profile happens to beat the next one by 0.15, the chain confirms it. That is
exactly the 09-10 shape: Mike is not in the room, but he is the least-wrong guess among the six
enrolled voices, and he clears the margin by a wide margin (0.268, almost double the 0.15
threshold).

### 1.2 Why the obvious fix was already rejected, and why this is not that fix

The module docstring (`src/voiceprint_utils.py:20-26`) records that an absolute threshold was
measured and rejected once already:

> "A margin, never an absolute cut. Same-person scores ran 0.104…0.639 and different-person
> −0.114…0.205: the distributions overlap. The best absolute threshold measured, +0.262 for
> 99%, was fitted on the material that produced it and is an upper bound, not a setting."

That objection is specific: +0.262 was fit on Phase 0's 32 turns from one session, one pair of
speakers, one microphone arrangement, and shipping it as a constant would silently carry all of
that session's acoustic idiosyncrasy into every company's decisions forever. The 09-10 evidence
sharpens rather than overturns the objection: ECAPA's worst false positive there is +0.445,
already above the old candidate cut of +0.262. A single global floor picked to clear 09-10
would have to sit above 0.445, and nothing in the data says that number generalises to a
different device, a different room, or a different company's set of voices any better than
0.262 did.

The floor proposed here is not a second attempt at the same global constant. **It is derived
per company, from that company's own history of human-asserted names, and recalculated as that
history grows — never authored as a number in source.**

### 1.3 Derivation

**The calibration population must be human assertions, never the system's own matches, or the
floor calibrates itself out of existence.** Consider the alternative directly: if the
calibration set were "turns that reached `confirmed` status under the existing chain," then the
09-10 Mike confirmation — `best = 0.445`, the exact error this floor exists to catch — would
itself join the set that computes the floor. A low percentile of a distribution that already
contains 0.445 sits at or below 0.445, so the floor could never reject a future 0.445. Worse,
this is a *closing* loop: every wrong confirmation the system makes lowers the bar for the next
one, so the more the guard errs the weaker it gets. The floor must instead be built only from
evidence a human vouched for.

The repository already distinguishes exactly this in `speaker_turn_names.source`:

- `source='correction'` — **included**. "the turn the user actually asserted is written
  `source='correction'` — the one row a person vouched for"
  (`src/lambda_voiceprint_writer.py:106`), and "only `source='correction'` counts"
  (`:116`) for the same reason `confirmations_count` already uses it: it is the one source the
  system did not generate about itself.
- `source='correction_propagation'` — **excluded**. Written when a single human correction is
  spread by similarity to other turns the human never looked at
  (`src/lambda_voiceprint_writer.py:137`, `:219`). One assertion, multiplied by the model's own
  similarity judgment, is partly the model's opinion re-imported as if it were more human
  evidence than it is.
- `source='voiceprint_match'` — **excluded**. "`source='voiceprint_match'`, never
  `'correction'`... writing machine output under [`correction`] would let the system promote
  its own profiles from its own guesses, and the promotion would then justify more confident
  guesses" (`src/lambda_voiceprint_writer.py:308`, written at `:330`) — the same circularity
  this floor exists to avoid, already named and guarded against elsewhere in this file for a
  different purpose (`confirmations_count`). The floor derivation must honor the same line.
- `source='label_inheritance'` — **excluded**. Written at `src/lambda_voiceprint_writer.py:409`
  (checked at `:396`) for a turn that inherits a tentative label rather than being independently
  asserted or matched; no human looked at this specific turn either.

(Migration `0038:44` comments the column as `-- correction | enrolment`; the code that actually
writes it uses `correction`, `correction_propagation`, `voiceprint_match`, and
`label_inheritance` — the comment is stale and `enrolment` is not a value produced anywhere in
`lambda_voiceprint_writer.py`. This spec calibrates against the write path's real values, not
the migration's comment.)

So: for each company, maintain the distribution of `best` scores (the winning score before the
margin check, i.e. `ranked[0][1]` in `decide_name`) restricted to turns whose current name comes
from a `speaker_turn_names` row with `source='correction'` — human-asserted evidence, whether
that assertion happened to agree with what the matcher would have guessed or not. This is
narrower than "everything `decide_name` confirms," which is the point: it breaks the loop
between the guard and the errors it is meant to catch.

- **Store**: one row per company in a new table, e.g. `speaker_voiceprint_company_floors
  (company_id, floor, sample_count, computed_at)`, recomputed by a scheduled job (matching the
  existing pattern of derived/materialized state elsewhere in this codebase, e.g. programme
  derived documents) rather than at match time — the floor must not move mid-decision because
  one turn happened to land during recomputation.
- **Statistic**: the floor is a low percentile (e.g. the 5th) of that company's own
  `source='correction'` `best` scores, not a fixed offset from the mean and not the company's
  own minimum — a single low outlier in the corrected history should not veto the whole
  company's future matches. The exact percentile is a tuning knob to be set against held-out
  per-company data once a company has enough of it (§1.5); it is deliberately not fixed here as
  a number, for the same reason the module docstring gives for margin: a value chosen once
  against the wrong company's distribution is exactly the kind of overfit this document is
  arguing against everywhere else.
- **Applied where**: as a new, final check in `decide_name`, after the margin check, before
  returning `confirmed`. If `best < floor`, downgrade what would have been `confirmed` to
  `tentative` (not `unknown` — the profile and lean are still worth showing as a guess; see
  §1.4 for the label itself) with a `reason` naming the floor and the company's sample count,
  so the decision is auditable the same way the existing reasons are (lines 122, 124-125).

This keeps the calibration data honestly separated by company. Companies differ in device,
room, and enrolled-voice count; a floor derived from one company's confirmed history says
nothing that generalizes to another's, which is the same reasoning `profiles_for_matching`'s
docstring (`src/repositories/voiceprints.py:449-454`) already applies to margin: "the size of
this result, not the number of people in the room, is what the margin has to survive." The
company boundary that already scopes the candidate pool is the natural boundary for the floor
too.

### 1.4 What the decision returns when the floor is not met

**Never a name promoted to `confirmed`.** Two sub-cases:

- If the margin check already passed (score clears the runner-up by >= 0.15) but the floor
  check fails, the result is `tentative` with the candidate name attached — consistent with the
  existing `Decision` contract, where `tentative` always carries a `name` as a shown-as-unsafe
  lean (`src/voiceprint_utils.py:59-61`, `decide_name` docstring). The UI already renders
  `tentative` as unconfirmed; nothing new is asked of it.
- The `spk_N` / unnamed-speaker label is a **display fallback for `unknown`**, not a distinct
  decision status — the codebase has exactly three statuses (`confirmed`, `tentative`,
  `unknown`). The floor never produces `unknown` by itself (duration and "no profiles" already
  own that path, lines 108-113); it only ever demotes a would-be `confirmed` to `tentative`. A
  turn that fails the floor keeps whatever label the transcript/diarisation layer already
  assigns to an unresolved speaker (`spk_N`) exactly as it does today for any `tentative` or
  `unknown` turn — this spec does not add a new label, it adds a reason more turns land on the
  labels that already exist.

### 1.5 Companies with too few human-asserted samples to calibrate

A brand-new company, or one that has only just turned enrolment on, has no `source='correction'`
history to derive a percentile from. Fallback: **apply no floor (behave exactly as today,
margin-only) until the company crosses a minimum count of `source='correction'` rows**, tracked
by `sample_count` in the floor table. Below that count, `speaker_voiceprint_company_floors`
simply has no row (or a row with `floor = NULL`), and `decide_name` treats a missing/NULL floor
as "no floor check" rather than substituting a global default.

This is a deliberate choice among three options, and the other two are worse:

- **A global fallback floor** (e.g. some fixed absolute number for young companies) is the
  exact rejected shape from §1.2, just deferred to apply only during a company's early life —
  and early life, with the fewest enrolled voices, is when the room is smallest and a stranger
  is *most* likely to be the whole population, i.e. exactly when a borrowed threshold is least
  trustworthy.
- **Refusing to confirm anything until calibrated** removes the feature during exactly the
  window (a new company's first weeks) when the product needs to prove itself, for a benefit
  (guarding against a false positive that has not yet been observed for that company) that is
  speculative until there is data.
- **No floor until calibrated** costs nothing that was not already the status quo for every
  company today — every company currently runs margin-only. It only asks the floor to start
  protecting once there is enough of that company's own evidence to set it responsibly, which
  matches the "budgets computed from samples too small" caution already in house practice:
  do not compute a boundary statistic from a sample too small to show its own tail.

**Restricting calibration to `source='correction'` (§1.3) makes this fallback path the normal
state for a new company, not an edge case.** Corrections are a human clicking a name onto a
turn; matches are every turn `decide_name` confirms on its own — the latter vastly outnumber the
former in ordinary operation, precisely because the point of the feature is that most turns
should not need a human. A company can run for a long time, accumulate plenty of *matched*
turns, and still have too few *corrected* ones to calibrate a floor. This does not change the
fallback recommendation — no floor is still the right behaviour below the minimum, for the same
three reasons above — but it does mean §1.5 should be read as describing most companies' steady
state for a meaningful stretch of time, not a brief startup phase, and the minimum count below
must be set with that in mind rather than assumed to be crossed quickly.

The minimum count is a threshold to be set from the shape of the percentile estimate's variance
as more companies' data becomes available; a reasonable starting point is the same order of
magnitude `confirmations_count` already uses to promote a profile from human corrections
(`src/lambda_voiceprint_writer.py:106-124` — that mechanism already treats a handful of
`source='correction'` rows as enough to trust for profile promotion, which is a comparable
claim: "this many human assertions is enough to act on"). This spec fixes the *rule* (no floor
below N `source='correction'` rows, N large enough that a 5th-percentile estimate is not
dominated by one or two points) and leaves the exact number to whoever calibrates against real
per-company correction volumes, for the same reason §1.3 leaves the percentile unfixed.

## 2. Mean pooling over a person's samples

### 2.1 Current behaviour and what it protects

`aggregate_scores` (`src/voiceprint_utils.py:68-93`) reduces a person's multiple sample scores
to one by **max** (lines 89-92: `if key not in out or score > out[key]: out[key] = score`). The
docstring is explicit about why: "Max is what 'nearest profile' already did implicitly, and a
mean would dilute a genuinely matching sample against a weak one — the enrolment that fits this
turn is the evidence, and averaging it with an unrelated one throws that away." Max protects a
person who has one bad enrolment sample (noisy recording, atypical read-aloud take) from having
that sample drag down every future match.

### 2.2 What max costs, measured

Max is exactly the mechanism that turns pooled enrolment into a liability on 09-10. Pooled
MAX enrolment (built from clean read-aloud + the 08-13 site meeting + the 09-17
simulated-environment recording, tested only on days not used to build the pool) gives:

| | 08-07 margins (Ben present) | 09-10 margins (nobody enrolled present) |
|---|---|---|
| clean enrolment only | +0.137 … +0.277 (two below 0.15) | −0.194 / −0.311 |
| pooled, **MAX** (production today) | +0.249 … +0.362 | **+0.054 / +0.055 — Ben becomes the top scorer** (CAM++ reaches +0.195/+0.252, CONFIRMED) |
| pooled, **MEAN** | +0.219 … +0.366 | −0.111 / −0.141 (Mike/Leo stay top, as before) |

Max is a per-turn maximum over an already-multi-sample profile. Adding more samples to a
profile under max does not average out an unusual sample — it keeps the single highest score
any sample happens to produce against *this* turn, and pooling in a site-condition recording
adds exactly the kind of sample most likely to spike upward against unrelated site audio (same
room tone, same device, same background noise floor), which is what happens on 09-10: Ben's
pooled-MAX profile's highest-scoring sample against a stranger's turn is his site-condition
sample, not because Ben is present, but because the *acoustic conditions* match. Mean pooling
absorbs that spike into an average across samples recorded in different conditions, and on
09-10 it keeps the true negatives negative while, per the table's own 08-07 row, still slightly
*improving* the true-positive range (+0.219…+0.366 vs +0.249…+0.362 — narrower ceiling, higher
floor).

### 2.3 What mean pooling costs, and why 09-10 outweighs it

The scenario mean pooling was written to protect against — one badly-recorded enrolment sample
permanently dragging a person's future matches down — still happens under mean: a bad sample
now always contributes its bad score to every future turn, rather than only being ignored when
a better sample exists. The cost is real and this spec does not claim otherwise. But it is a
cost that shows up as a *missed* confirmation (a real match demoted to `tentative`), the failure
mode this whole design already treats as the safe direction: "a wrong confident name costs much
more than a missing one... the missing one is visibly missing" (`src/voiceprint_utils.py:27-28`).
Max's failure mode on 09-10 is the opposite: a wrong confident name, the expensive kind. Given
the two errors are not symmetric in cost, and pooled-mean's 08-07 true positives still land in a
usable range (see §2.4), the trade favors mean.

### 2.4 Where the change applies, and where it does not

`aggregate_scores` changes from max to mean over a person's sample scores for a given turn,
computed at match time:

```
out[key] = mean(scores for that key)   # was: max(scores for that key)
```

This is a match-time arithmetic change only. **The one-row-per-contribution storage in
`speaker_voiceprint_samples.embedding` (migration 0038) does not change.** Averaging vectors
at write time was explicitly ruled out for withdrawal: a withdrawal must be able to remove
exactly one contribution's effect, which requires each contribution's embedding to still exist
as its own row, unaveraged, at withdrawal time. Mean pooling is computed fresh, over whichever
samples currently remain unwithdrawn, every time `profiles_for_matching`'s rows are aggregated
— so a withdrawal changes the mean on the next match, exactly as removing an outlier from a
running average should, and requires no schema or write-path change at all.

With mean pooling on ECAPA, the measured window widens further: 08-07's true positives reach
**.574–.586**, while 09-10's worst false positive stays **.445** — the combination that first
makes a usable absolute threshold window (~0.50) possible (see §1, which is what turns that
window into a decision rule rather than an observation).

## 3. Multi-occasion enrolment

### 3.1 What one recording condition costs

Every number in §2's "clean enrolment only" row comes from six read-aloud files recorded in one
sitting on 2026-08-11. A profile built from a single recording condition carries that
condition's channel and room characteristics baked into the vector, indistinguishable to the
embedder from voice identity itself — which is the mechanism the cross-session measurement
(`voiceprint-cross-session-no-signal`) already found: enrolments from one session do not
recognise the same speaker in a different session's audio. The strongest single lever measured
against that ceiling was not a different model or a different pooling rule — it was **a sample
recorded in the target environment**: on one day this moved a score from **0.40 to 0.66**.

### 3.2 What the spec requires

A person's enrolled profile should be built from more than one recording condition: at minimum,
one clean read-aloud sample (for admission speed and consent clarity — see
`voiceprint-enrolment-must-not-be-a-performance`) plus, as it becomes available, one or more
samples drawn from real site audio the person actually spoke in. This is additive to the
existing one-row-per-contribution design (§2.4) — a multi-occasion profile is simply a person
with more than one `speaker_voiceprint_samples` row, spanning different recording sessions
rather than multiple takes of the same session. No new storage shape is required; `enrol_sample`
/ `add_sample` already support adding a contribution to an existing profile.

**How such a sample is obtained.** A site-condition sample cannot be manufactured — it is a
window of audio, already captured during ordinary recording, in which the target person is
confidently the only speaker. The practical source is a turn already confirmed under the
existing (or floor-augmented) decision chain: once a turn is `confirmed` for a person with high
margin and duration comfortably over the 3.0 s floor, it is a candidate for promotion into that
person's enrolment set, subject to the guard in §3.3. This is consent-compatible with the
existing model only when the underlying recording already carries consent for voiceprint use
from that person — this spec does not relax `upsert_profile`'s consent preconditions
(`src/repositories/voiceprints.py`, `consent_at`/`consented_by`/`attested` checks): a
site-condition sample is enrolled the same way any other sample is, through the same consented,
non-withdrawn profile.

### 3.3 The guard, and an honest answer about what it currently rejects

`window_is_homogeneous` (`src/voiceprint_utils.py:129-147`) with
`DEFAULT_MAX_FRAME_SPREAD = 0.35` (line 49) is the existing contamination guard: a window whose
internal frames disagree by more than 0.35 cosine distance is presumed to hold more than one
voice and is refused rather than enrolled, because "a poisoned profile cannot be cleaned — only
the whole contributing sample deleted" (lines 137-139).

The owner's own 2026-09-17 simulated-site recording — the sample this spec's own multi-occasion
measurement in §2.2/§3.1 relies on — had one chunk whose pairwise similarity against its
siblings fell to **0.47–0.57**, i.e. a frame spread (1 − similarity) of **0.43–0.53**, above the
0.35 threshold. Under the guard as it stands today, that chunk would be refused.

**That refusal is correct and should not be loosened for this feature.** The guard's job is to
keep a genuinely mixed-voice window out of a profile, and it cannot distinguish "this chunk has
two voices in it" from "this chunk has more background noise than its neighbours" — both
produce the same symptom, a wider internal spread. Loosening the threshold to admit this one
known-good chunk would also admit genuinely contaminated windows the guard exists to catch,
which is the failure this whole document treats as the expensive one. The correct action is not
to weaken the guard but to **enrol from the chunks of the 09-17 recording that pass it, and
discard the one that does not** — a multi-occasion profile does not need every chunk of a
target-environment recording, only enough of one that is internally homogeneous. This does cost
some of the material behind the 0.40 -> 0.66 measurement, since that number is not yet
recomputed chunk-by-chunk against the guard; see §5 for what remains unmeasured about this.

### 3.4 The centroid caveat, stated plainly

The two site-condition samples used to build the pooled profile above were **centroids** built
from many turns each — 71 turns for the 08-13 sample, 5 for the 09-17 sample — not single
enrolment windows. Production stores one sample vector per contribution
(`speaker_voiceprint_samples`, one row per enrolment), not a centroid of many turns. A centroid
is smoother than any one of its constituent windows, so the pooled-enrolment numbers in §2.2
likely overstate what production will see once multi-occasion enrolment stores individual
homogeneous-window samples rather than a pre-averaged stand-in for many of them. This is not a
reason to withhold the feature — the direction of the effect (pooling helps, mean beats max) is
consistent across every row measured — but the magnitude, specifically how close the 0.50
threshold window gets in production, should be re-measured against single-window multi-occasion
samples before the floor's percentile (§1.3) is calibrated on live traffic, so that the
percentile reflects real single-sample variance rather than centroid-smoothed variance from this
one-time measurement.

## 4. The margin must scale with the candidate pool

`profiles_for_matching` (`src/repositories/voiceprints.py:437-468`) returns every consented,
non-withdrawn profile for a company, unlimited, and its own docstring names the consequence:
"the size of this result, not the number of people in the room, is what the margin has to
survive... A company that accumulates profiles across many sites makes every turn harder to
confirm" (lines 449-454). `DEFAULT_MIN_MARGIN = 0.15` was set against a 6-profile enrolment
library (§ intro measurements). As a company grows to dozens of enrolled people, the runner-up
in `decide_name`'s ranked list is drawn from a larger pool each time, and the expected gap
between the best score and the best-of-the-rest mechanically shrinks — more candidates means a
higher chance one of them, by nothing but noise, sits close to the winner. A fixed 0.15 margin
that was comfortable at 6 profiles becomes a harder bar at 60 not because matching got better,
but because the runner-up got luckier at showing up close.

This spec does not fix a formula for margin-vs-pool-size, for the same reason §1.3 does not fix
the floor's percentile: the only data available (six enrolled voices) cannot support fitting a
scaling curve without repeating the exact overfitting mistake the docstring at
`src/voiceprint_utils.py:20-26` already warns against. What it specifies is the *shape* the
mechanism must have, so the margin does not silently drift as `profiles_for_matching`'s scope
does:

- `decide_name` should receive, alongside `scores`, the size of the candidate pool it was drawn
  from (`len(scores)` after `aggregate_scores`, i.e. per-person, not per-sample-row), and use it
  to look up or compute an effective margin rather than always applying the module constant.
  `site_id` narrowing in `profiles_for_matching` already gives companies a lever to shrink the
  pool per turn; the margin logic should benefit from that narrowing being used, rather than
  penalizing a company that leaves it off with the same fixed threshold it would get at 6
  profiles.
- The `site_id`-narrowed pool from `profiles_for_matching` (lines 460-468) should be the
  default matching mode once companies have enough enrolled people that the company-wide pool
  would meaningfully move a scaled margin — narrowing by site is already implemented and paid
  for, it is currently opt-in.
- As with §1.5, a company below whatever pool size triggers scaling keeps today's fixed
  `DEFAULT_MIN_MARGIN`, exactly as it does now.

## 5. What remains unmeasured

- **No ground truth exists for who actually spoke on 08-07, 08-13, or 09-10.** Every number in
  this spec and in the measurements it cites is a similarity score against an enrolment
  library, not a verified transcript of who was in the room. 09-10 is trusted as a negative
  control because the library's members are independently known not to have attended, not
  because anyone has confirmed who did.
- **The centroid-vs-single-window gap (§3.4)** is not measured; the 0.40 -> 0.66 and pooled-MAX
  09-10 numbers both rest on centroids of many turns, and production will store single windows.
- **The floor's percentile (§1.3) and the margin-scaling curve (§4)** cannot be fit from six
  enrolled voices; both need real multi-company confirmed-match volume before a specific number
  is chosen, and this spec deliberately stops at specifying the mechanism rather than inventing
  numbers to fill that gap.
- **Chunk-level re-measurement of the 09-17 recording against the 0.35 spread guard (§3.3)**
  has not been done; the 0.47-0.57 pairwise figure is known for one chunk, not a full
  accounting of how much of that recording the guard would admit.

## 6. Rejected alternatives

**Change the embedding model.** CAM++ and ERes2NetV2 were measured on the identical 265 slices.
Neither improves on ECAPA where it matters: CAM++'s best true positive (+0.467) is beaten by
ECAPA's *worst false positive on the negative control* (+0.484 — from the 08-07 Ben row, not
even 09-10); ERes2NetV2's worst false positive on 09-10 (+0.528, Leo) exceeds its own true
positive on 08-13 (+0.545) by only 0.017, leaving no usable threshold on that model at all.
ECAPA is the only one of the three holding a positive margin on every label where Ben was
actually present. Closed; not revisited by this spec.

**One global threshold.** Considered twice — once already, in the existing codebase (the
rejected +0.262, `src/voiceprint_utils.py:20-26`), and again implicitly by anyone reading the
09-10 numbers and wanting a single fixed cut (e.g. "just require score > 0.5"). Rejected both
times for the same reason: any single number is fit to whichever company's, device's, and
room's data produced it, and this spec's own measurements are drawn from one company's audio on
four dates with one enrolment library of six people — not a basis for a constant that follows
every company forever. The per-company calibration in §1 is offered as the alternative that
keeps the same intent (a floor under the margin) without repeating that mistake.

**Store averaged vectors instead of computing the mean at match time.** Rejected because it
would remove the ability to withdraw a single contribution's effect — the entire reason
`speaker_voiceprint_samples` stores one row per enrolment rather than one accumulated vector per
profile (migration 0038; see §2.4). Mean pooling is specified as a match-time reduction over
currently-unwithdrawn rows precisely so withdrawal keeps working unchanged.

**Loosen `DEFAULT_MAX_FRAME_SPREAD` to admit the 09-17 recording's noisy chunk.** Rejected in
§3.3: the guard cannot tell a noisy-but-single-voice chunk from a genuinely contaminated one,
and loosening it to admit the former admits the latter too, which is the expensive failure this
whole design is shaped to avoid.

**Calibrate the floor from the system's own confirmed matches.** This is the easiest data to
obtain — every `decide_name` call that reaches `confirmed` produces a `best` score for free,
with no human involved — and it will be proposed again for exactly that reason, which is why
it is recorded here rather than left implicit. It is rejected because it is circular: on
2026-09-10 the existing duration+margin chain CONFIRMED Mike at `best=0.445` in a room holding
nobody enrolled. Under this derivation, that 0.445 joins the calibration set that computes the
floor, pulls the low percentile to at or below itself, and the floor can then no longer reject
the very class of error it exists to stop. The failure does not stay flat — it compounds: the
more often the guard is wrong, the more of its own wrong scores enter the calibration set, and
the weaker the floor becomes, in direct proportion to how much it is needed. §1.3 calibrates
from `source='correction'` rows instead for exactly this reason.

## 7. Risk table

| Risk | Where it enters | Consequence if unaddressed | Mitigation specified here |
|---|---|---|---|
| Floor calibrated on too little company history is noisy or wrong | §1.3, §1.5 | A young company gets an unreliable floor, either too strict (missed confirmations) or too loose (09-10-shaped false positive survives) | No floor applied below a minimum confirmed-match count (§1.5); percentile choice deferred to real multi-company data, not invented here |
| Mean pooling lets one bad enrolment sample permanently suppress a real match | §2.3 | Increase in `tentative`/missed confirmations for people with one poor-quality sample | Accepted deliberately: costs a missed confirmation, not a wrong name, which this design already treats as the cheaper error; not mitigated further here |
| Centroid-based measurement overstates single-window production performance | §3.4 | Floor/threshold calibrated optimistically, then underperforms once real multi-occasion samples are single windows | Called out explicitly; floor percentile calibration (§1.3) should wait for live single-window traffic before being finalized |
| Homogeneity guard refuses part of the best available target-environment sample | §3.3 | Feature loses some of its strongest lever (0.40 -> 0.66) to noise-vs-contamination ambiguity | Guard left unchanged; enrol from the homogeneous chunks only, accept the partial loss rather than weaken contamination protection |
| Margin scaling mechanism ships without a fitted curve | §4 | Large companies could still see margin become effectively unreachable, or a rushed number gets fit on insufficient data and repeats the +0.262 mistake | Mechanism (pool-size-aware margin lookup, site narrowing as default at scale) specified now; numeric curve explicitly deferred to real data, with today's fixed margin kept as the fallback below a size threshold |
| Floor and pooling interact in a way that must be handled at cutover, not left implicit | §1, §2 | A `source='correction'` row's stored `best` score was computed under whichever aggregation (max or mean) was live when that correction was written. A floor built by mixing pre-cutover (max-pooled) and post-cutover (mean-pooled) correction scores describes neither distribution correctly, since mean pooling systematically shifts `best` (§2.2's table: pooled-mean's true-positive range differs from pooled-max's) | At the mean-pooling cutover, discard pre-cutover `source='correction'` `best` scores from the floor's calibration set and rebuild from corrections written after cutover only — re-scoring old corrections under mean pooling would require re-running match arithmetic against embeddings that may since have been withdrawn, while a clean discard-and-rebuild costs only a return to the §1.5 no-floor fallback until enough post-cutover corrections accumulate, which is a safe, already-specified state |
| The `source='correction'` calibration set stays below the minimum count indefinitely, because corrections are far rarer than matches | §1.3, §1.5 | Companies never leave the §1.5 no-floor fallback; the 09-10 class of false confirmation (a wrong name confirmed on margin alone, with no floor to catch it) remains possible for those companies indefinitely, not just during onboarding | Track `sample_count` per company in `speaker_voiceprint_company_floors` (§1.3's stored table) as a standing metric, and alert on how many companies sit below the minimum for longer than expected — observable directly from that table without new instrumentation, since it already records the count that gates the fallback |
