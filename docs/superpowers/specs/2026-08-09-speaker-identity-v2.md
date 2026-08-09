# Who is talking — level, clusters, and names

**Status:** Design · 2026-08-09
**Relation to prior work:** refines `2026-08-08-speaker-identity-design.md`. That document's
phases B/C/D and its Phase 0 gate still stand. This one adds the product decisions the user
asked for — the wearer from level alone, enrolment by correction, proactive enrolment, and
what a low-confidence match is allowed to say — and corrects the sequencing between them.

---

## 1. The rule everything else is subordinate to

**A name the system is not sure about must never reach an extraction, a report, an action
item, or an email.** It may appear in the transcript viewer, marked as a guess, and nowhere
else.

This is not caution for its own sake. A name that enters the extraction becomes a durable
record that propagates: into topics, into who is responsible for an action item, into the
minutes that get emailed. FieldSight has already shipped that exact failure once — a site
name guessed from an environment variable was believed by ingest, and the result was users
who could not find their own recordings while another site could. The correction took two
releases and left four rows that still cannot be fixed.

So identity has three states, not two, and the boundary between them is enforced at the point
where data leaves the transcript layer.

| state | shown as | may enter extraction / reports |
|---|---|---|
| confirmed | `Ben L` | yes |
| tentative | `(可能是 Ben L)` | **no** — degrades to the anonymous label |
| unknown | `Speaker A` | n/a |

---

## 2. What is already measured, and what it forces

These are results, not assumptions. Each one removes a design option.

**The transcript cannot be the source of identity.** A turn's boundaries *are* the provider's
`speaker_label`. Two people wrongly merged land inside the same turn — and those are exactly
the people this work exists to separate. Identity therefore has to be computed from audio,
and only then joined onto turns.

**One turn can contain two people** — 3 of 18 in the measured session. Any mechanism that
takes a turn as a unit of one person's voice will sometimes be wrong, including a user's
manual correction (§6).

**`speaker_count` is the size of a label-string union, not a headcount.** Every chunk emits
`spk_0`/`spk_1`, so the union is 2 regardless of how many people were present; on the 22:48
session it read 2 and was *correct by coincidence*. It is computed in two places —
`lambda_extract_session.py:1491` (single device) and `:1240` (group merge, a union across
devices' independent label spaces, so group artifacts inflate it further) — and consumed at
`lambda_item_writer.py:580`, where `== 1` resolves a self-referential responsible party
("I'll do it") to the wearer.

**The device applies no automatic gain.** Reverse-engineered from the F2SP: `NS`/`AGC`/`AEC`
are bound only to `voice_communication` (which this recorder does not use), the DMNR tuning
table is all zeros, and `MIC` already selects the highest fixed gain table. **Level
differences between a near and a far talker survive into the file.** This is what makes §5
possible at all.

**Distant speakers occupy about 5.3 of the available 16 bits.** The wearer is ~20 cm from the
microphone, everyone else 2–5 m. Inverse-square gives a large, physical, non-inferred gap.

**The recordings are quiet in absolute terms** — median −36.0 and −33.0 dBFS across two
measured days, where normal is −20 to −12. This is a property of the device, not of a bad
day. It matters here because it means *absolute* thresholds must be calibrated on this
hardware; it does not weaken the *relative* gap, which is what §5 uses.

**Audio is retained indefinitely.** The `users/` prefix has no lifecycle rule (the bucket's
three rules cover `transcripts/` 90d, `pending_downloads/` 7d, `voice/` 30d). Every past
recording is still readable. §6 depends on this.

**ElevenLabs' speaker library is not usable for us.** `/v1/speech-to-text/speakers` does not
accept an API key (a different failure shape from `/v1/voices`, which reports a missing
permission — this one reports no credentials at all, suggesting dashboard-only auth), and the
library is **workspace-scoped**: every customer's profiles would share one pool. Voiceprints
stay self-hosted. The reason is tenancy and compliance, not capability.

---

## 3. The measurement must not run on `audio_segments/`

`normalise_for_asr` applies `acompressor` 4:1 with 8 dB makeup plus `loudnorm`, **per 30-second
chunk**. That is non-linear, time-varying gain — it compresses precisely the near/far level
gap that §5 reads, and it does so by a different amount in every chunk. It is also
best-effort: on an ffmpeg failure or a timebase mismatch it silently returns the original, so
`audio_segments/` is a *mixture* of conditioned and raw audio with no marker saying which.

`NORMALISE_AUDIO=true` is live on prod. **Any level comparison must read the untouched chunk
at `users/…/audio/…_c####.wav`**, never the segment. Turn times map back through the segment
filename's `_off{X}_to{Y}_` offset, the same arithmetic the transcript layer already uses.

A second consequence: `DROP_SILENT_CHUNKS=true` means a chunk judged silent produces no
segment at all, so speech quiet enough to fall under `VAD_THRESHOLD` is simply absent
downstream. Distant talkers are the population most likely to be missing entirely. No
identity mechanism can recover them; this is upstream of everything below.

---

## 4. Phase 0 — the free measurement that decides Phase A

Before building anything: does level actually separate the wearer?

Measure on existing prod recordings, on the raw chunks. For each transcript turn, take the
distribution of short-frame (≈1 s) RMS inside the turn — **a distribution, not one number**,
because a turn is not reliably one person (§2) and the spread is itself the signal that says
so. Then ask:

1. Is the per-turn level distribution across a session **bimodal**, and what is the gap
   between modes in dB?
2. Does the loud mode correspond to the device owner, on sessions where the answer is known?
3. How wide is the overlap region — i.e. what fraction of turns would land in "not sure"?
4. Does the gap survive on a session where the wearer speaks little?

**Decision rule.** Phase A proceeds only if the modes are separated by a margin large enough
that a conservative threshold leaves the overlap region small — the exact number comes out of
this measurement and is not guessed here. If the distributions overlap broadly, level becomes
a weak prior feeding §7 rather than a mechanism of its own, and Phase A is dropped.

Cost: zero. No new infrastructure, no ASR spend, no consent question, and the material
already exists. This is deliberately the first thing done, because it is the only step whose
outcome can cancel later steps.

---

## 5. Phase A — the wearer, from level alone

If Phase 0 passes: label the loud mode as the device's assigned owner, by name.

No model, no stored voiceprint, no consent question — the device owner is already known from
device assignment, and nothing about anyone's voice is retained.

**Two failure modes, both handled by the same three-state output rather than by a better
threshold:**

- *The wearer never speaks.* Then the loudest voice in the session belongs to someone else,
  and a two-way decision confidently attaches the owner's name to a stranger. So the test is
  not "which turn is loudest" but "is this turn above the absolute level a mouth 20 cm from
  this microphone produces" — calibrated in Phase 0 on this hardware, whose quiet baseline
  (§2) makes borrowing a number from elsewhere invalid.
- *Someone leans in.* A visitor speaking close to the chest mount reads as the wearer. Level
  cannot distinguish this; only a voiceprint can (§6). Until then such turns land in
  *tentative* and are shown as a guess.

**What Phase A concretely fixes.** With the wearer identified, `lambda_item_writer.py:580`
stops depending on `speaker_count == 1` to resolve "I'll do it" — it can ask whether the
wearer said it. That gate is currently correct only by coincidence, and both computation
sites (`:1491`, `:1240`) inflate the number they feed it. Phase A must change the gate
deliberately and cover both sites; chunk-qualifying the labels and assuming the old gate
still works is the specific mistake to avoid.

---

## 6. Phase B — enrolment by correction (before proactive enrolment, deliberately)

A user with permission marks a passage in the transcript as "Ben L". The system fetches that
audio window from the retained original, computes an embedding, and adds it to Ben's profile.

This is ordered **before** asking people to record a sample, for two reasons: the material is
real speech in real site acoustics rather than a clean sample recorded in a quiet room, and it
costs the organisation nothing to collect. If corrections accumulate enough profiles, Phase C
may never be needed.

Because audio is retained with no expiry (§2), **embeddings do not need to be computed and
stored up-front**. A correction can reach back to any past recording. Storage cost of the
retroactive design is zero and the whole history is enrollable.

**The contamination guard is not optional.** One turn in six contained two speakers in the
measured session. Enrolling from a mixed passage poisons the profile permanently and every
later match inherits it. So:

- the marked window is embedded in short frames and must be **acoustically homogeneous** —
  one cluster — before any of it is stored;
- a profile becomes *confirmed* only after **N independent confirmations** from different
  sessions, not one. Until then it is *tentative* and can only produce tentative matches;
- every stored vector keeps a pointer to the correction that produced it, so a profile can be
  audited and a bad enrolment withdrawn along with everything it justified.

---

## 7. Phase C — proactive enrolment

Ask each person to record one sentence, starting with our own site.

Same worker, same table, same consent flow as Phase B — it is a different way to obtain the
first vector, not a different mechanism. It is last because Phase B may make it unnecessary,
and because it is the step that requires organising people.

The clean-room sample has a known weakness worth recording: it is recorded in different
acoustics from the site material it will be matched against, and channel mismatch is one of
the main reasons embedding similarity degrades. A profile built only from a clean sample
should stay *tentative* until at least one site-condition confirmation joins it.

---

## 8. The table

```sql
-- next migration is 0038
CREATE TABLE speaker_voiceprints (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES users(id) ON DELETE CASCADE,
  display_name  text,
  status        text NOT NULL DEFAULT 'tentative',  -- tentative | confirmed | withdrawn
  consent_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE speaker_voiceprint_samples (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  voiceprint_id  uuid NOT NULL REFERENCES speaker_voiceprints(id) ON DELETE CASCADE,
  embedding      vector(192) NOT NULL,
  source         text NOT NULL,          -- correction | enrolment
  s3_key         text,                   -- the audio the vector came from
  window_start_s double precision,
  window_end_s   double precision,
  created_at     timestamptz NOT NULL DEFAULT now()
);
```

Vectors live in their own table, one row per enrolment event, because §6's audit and
withdrawal requirements need each contribution to remain individually identifiable — a single
averaged vector per person cannot be un-poisoned.

`company_id` is `NOT NULL` and every query is scoped by it. `user_id` is nullable so a
recurring unnamed voice can hold a profile before anyone names it.

Matching uses the pgvector index already in the stack; **no company_id-less query may exist**
on these tables.

---

## 9. Confidence, and what "(可能是 XXX)" is allowed to be

**The threshold is measured, not chosen.** Run the enrolled profiles against held-out audio
with known answers and plot the same-person and different-person similarity distributions.
The confirmed cut-off goes where the different-person tail becomes negligible — deliberately
conservative, because the cost of a wrong confident name (§1) is much higher than the cost of
showing a guess. Until that measurement exists, **everything below a very conservative bar is
tentative**, including matches that "look obviously right".

Distances are not comparable across conditions, so the cut-off is calibrated per condition
class (near/far, normalised/raw) rather than as one global number.

A tentative name appears in the transcript viewer as `(可能是 Ben L)` with the same
one-click correction affordance as any other passage — which feeds §6. That is the loop worth
building: the place where the system is least sure is exactly where a human correction is
cheapest to collect and most valuable.

---

## 10. Consent and tenancy

A voiceprint is biometric information. Under the NZ Privacy Act it needs informed consent
from the person whose voice it is — not from the person who wears the device and not from
their employer. `consent_at` is on the row and a profile without it cannot be used for
matching.

Starting with our own site is the right sequencing precisely because it lets the consent flow
be exercised before it is asked of a customer.

Cross-company matching must be impossible by construction, not by convention: there is no
global speaker pool, which is the same reason ElevenLabs' workspace-scoped library was
rejected (§2).

---

## 11. What this does not do

- It does not recover speech that VAD dropped (§3). The quietest talkers may not be in the
  corpus at all.
- It does not fix within-turn splitting. A turn containing two people stays one turn; the
  most this design does is decline to name it.
- It does not make the two furthest people in a room separable. That was the expected outcome
  of the prior design's Phase 0 and nothing here changes the physics.
- Phase A names exactly one person — the wearer. Everyone else stays anonymous until a
  profile exists.
