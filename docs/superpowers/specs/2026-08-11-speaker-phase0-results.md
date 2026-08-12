# Phase 0 ran on real material: two people five metres away are separable

**Status:** findings · 2026-08-11
**Gates:** `2026-08-08-speaker-identity.md` §0.3, refined by `2026-08-09-speaker-identity-v2.md`
**Material:** two sessions recorded 2026-08-11 16:48 and 16:52 NZ, `Ben_UCPK2`,
`sid318601b2…` and `sidb49627d7…`

## The question, and why the earlier material could not answer it

Everything measured before this answered "does the person at six metres still resemble
*himself*" — held-out 86%, 58 other-speaker turns with no false positive. That says nothing
about telling two *distant* people apart, and the canonical failure is exactly there: the
wearer forms one clean cluster while every distant speaker collapses into a single "other".
On a two-person recording that failure produces two tidy clusters and **reads as success**.

So Phase 0 needed ≥3 people with at least two of them distant, at the same distance.

## What was recorded

Two scripted site conversations, read naturally — no artificial gaps between turns. Device
worn on the chest by Ben throughout; **the other two speakers side by side at 5 m**, directly
in front.

| session | script | speakers | staging |
|---|---|---|---|
| `318601b2…` | slab / rebar / pre-pour | Ben (worn) + Zoe + Mike | Zoe and Mike **both at 5 m, side by side** |
| `b49627d7…` | panels / crane / lift | Ben (worn) + Leo + Joe | Leo and Joe **both at 5 m, side by side** |

Enrolment is a **separate** 30 s read per person (33–45 s actual), never cut from the
conversation — scoring a profile against the audio it was built from is self-validation.
Ben also has a Chinese-language profile.

Ground truth comes from the script rather than from hand labelling: the dialogues were read
in order and every line is textually distinctive, so matching a turn's words to a script line
names the speaker. All 32 turns matched a line confidently.

Embeddings: ECAPA-TDNN, 192-d, run locally. Scored on the **raw** chunk audio under
`users/…/audio/`, the copy normalisation never touches.

## Result: the gate passes, and not narrowly

**31 of 32 turns were attributed to the right person** (session A 15/16, session B 16/16) by
nearest enrolment profile.

```
COLLAPSE: no — every speaker matches their own profile best
```

| speaker | own profile | closest other | margin |
|---|---|---|---|
| Ben | **+0.425** (n=36) | Zoe +0.078 | +0.347 |
| Joe | **+0.506** (n=3) | Mike +0.073 | +0.433 |
| Leo | **+0.481** (n=4) | Zoe +0.079 | +0.401 |
| Mike | **+0.548** (n=3) | Zoe +0.113 | +0.436 |
| Zoe | **+0.408** (n=4) | Leo +0.074 | +0.334 |

Same-person similarity median **+0.463**; cross-person median **+0.034**. This is not a
marginal separation.

Against §0.3's gates: the wearer separates (**Phase B viable**) and the two speakers at 5 m
separate **from each other** (**Phase D viable**). Phase C's gate — "near speakers separate" —
was **not measured**: nobody stood at 1–3 m in either session. It follows *a fortiori* from
the 5 m result, and that is an inference, not a measurement.

## The finding that matters more than the gate

**The provider's speaker labels are badly wrong on this material, and the voiceprint fixes
the naming — though not the segmentation.**

| session | labels the transcriber produced | what they actually contained |
|---|---|---|
| A | `spk_0`, `spk_1`, `spk_2` (3 for 3 people) | `spk_1` = Zoe ×4 **+ Mike ×1 + Ben ×1**; `spk_0` = Ben ×8 **+ Mike ×1** |
| B | `spk_0`, `spk_1` — **two labels for three people** | `spk_1` = Leo **+ Joe + Ben** |

So the attribution in today's reports is largely wrong, and the label count does not even
reveal it: session A produced the right *number* of labels while scrambling their contents.
On the same audio the voiceprint got 31 of 32 right.

This reframes the identity layer. It is not a naming convenience bolted onto correct turns —
**it is the repair for turn attribution the ASR already got wrong.** That is a much stronger
reason to build Phases B–D than "users would like to see names".

**But it repairs naming, not segmentation, and the metric cannot see the difference.** Both
the 97% and the table above are computed over the *diarizer's own turn units*: a turn that
actually contains two people is scored fully correct whenever the dominant voice wins. On
earlier material 3 of 18 turns contained two speakers, and every one of those would pass this
metric while the minority words kept the wrong name. So the honest claim is: a turn's *label*
becomes reliable; a turn's *boundary* does not, and sub-turn misattribution is untouched and
unmeasured here.

## The one miss, and the two rules it dictates

```
session A, turn 13 — Zoe, 2.1 s
  "Don't know. Maybe the facade subbie."
  Zoe +0.104   Ben +0.111   → named Ben
```

Short and quiet. It is also the *lowest same-person score in the whole set*, and the
distributions overlap exactly there:

| | n | median | range |
|---|---|---|---|
| same person | 50 | +0.463 | +0.104 … +0.639 |
| different person | 142 | +0.034 | −0.114 … +0.205 |

20 pairs fall in the overlapping band 0.104–0.205. Two consequences, both of which must be
in the implementation:

1. **A minimum turn duration** (≥3 s on this evidence). Below it, say nothing — a wrong
   confident name costs more than a missing one (v2 §1).
2. **Nearest-profile with a required margin, not an absolute threshold.** Relative matching
   scored 97%; the best absolute cut (+0.262) reaches 99% but was **fitted on this same
   data**, so it is an upper bound and not a threshold anyone may ship.

## Three smaller results

**Cross-language costs about 0.08 here, not 0.21.** Ben's Chinese profile scored ~0.08 below
his English one on English speech, and twice it was the nearest profile of all — both times
still Ben, so no error. One person holding two language profiles works.

**Two chunks were dropped by VAD as no-speech**, including the **first 30 s of session B**:

```
b49627d7…_c0000   Loudness: -44.6 dBFS   No speech at 0.15, retried at 0.075 — still none
318601b2…_c0005   Loudness: -49.0 dBFS   same
```

Not a defect: −44.6 dBFS is 8 dB below this device's already-low median (−36 / −33). But any
spoken announcement at the head of a session is likely to be lost this way, which is one more
reason ground truth came from the script instead.

**The non-wearer sample sizes are small** — 3 to 4 turns each. The direction of the result is
not in doubt at these margins; the exact threshold needs more material before it is fixed.

## What this does not establish

- **Nothing about six metres.** 5 m passed; the distance ladder's hardest point was 6 m and
  was not part of this.
- **Nothing about overlapping speech.** These were read dialogues; turn-taking was clean.
  A turn containing two people (measured before: 3 of 18) is still unaddressed.
- **Nothing held out.** Every number here is on the material that produced it. The threshold
  must be set on new material before it is trusted.
- **Nothing about noise.** A quiet indoor read is not a site.
- **Nothing about the production turn pipeline.** The evaluator builds its own turns from the
  transcript items (1.2 s gap rule, crude 2 s head-skip for the device overlap) rather than
  going through `normalize_transcript` / `_dedup_turn_boundaries` / the announcement filter.
  Turn boundaries in production will differ, and the 08-08 plan's §0.2 asks for the real path.
- **Nothing about within-turn attribution**, per the section above.
- **No preserved artifacts.** The audio, the run output and the loudness figures are not
  checked in anywhere, so a reader can re-run the script but cannot re-check these numbers
  against what produced them. The material lives in `AI/Field_Sight/diarization/voiceprint/`
  (enrolments) and prod S3 (the two sessions).

## Review corrections (2026-08-11, adversarial pass against the scripts and repo)

Checked and confirmed: the arithmetic is internally consistent (32 turns; Ben's two profiles
merged under one key make same-person n = 36+3+4+3+4 = 50 and cross-person n = 18×4 + 14×5 =
142, exactly as reported); next migration is 0038; the FINAL_RERUN budget and re-email risks
cited by the design exist in code. Unverifiable from the repo: the audio, the run output, the
enrolment provenance ("separate 30 s read"), and the loudness figures — no artifacts were
preserved, so those rest on the author's word. Corrections that are material:

1. **97% is turn-unit accuracy over the diarizer's own units, and that flatters the headline.**
   Ground truth names each *turn* by its best script line, so a turn that actually contains
   two people is counted fully correct when the dominant voice wins — the within-turn
   misattribution (3 of 18 turns on earlier material) is invisible to this metric by
   construction. "The voiceprint repairs the diarization" therefore means it repairs turn
   **naming**, not turn **segmentation**; merged turns stay merged and their minority words
   still get the wrong name. The same caveat applies to the label-scramble table, whose
   contents were derived by the same per-turn matching.
2. **The eval did not use the production turn pipeline.** `speaker_session_eval.py` builds its
   own turns (1.2 s gap merge, a crude skip of the first 2 s of each chunk) instead of
   `normalize_transcript` + `_dedup_turn_boundaries` + the announcement/tag filters. Production
   units will differ at seams and around announcements; the implementation must embed the
   assembled production turns, and the numbers here are approximate for those units.
3. **The Phase C gate ("near speakers separate") was never measured.** This material had the
   wearer plus two people at 5 m — no one at 1–3 m. "Phase C viable" is an a fortiori
   inference from the 5 m result, reasonable but not a measurement.
4. **The headline is wearer-heavy and the distant evidence is thin.** 18 of 32 turns and 36 of
   50 same-person scores are Ben at 20 cm; distant-vs-distant separation rests on 14 turns,
   n = 3–4 per person — the document says this lower down, but the "not narrowly" framing
   belongs next to it. Also: the "closest other" margins are partly cross-session pairs; the
   co-present-partner margins are bounded by them but not separately shown.
5. **Selection effects run one way.** Turns < 1 s, clips < 1 s, chunk-head turns, VAD-dropped
   chunks and unmatched turns are all excluded before scoring — the denominator omits exactly
   the hardest material. The ≥3 s floor in the rules section is the right response; the
   accuracy number should not be quoted without it.
6. **Which provider diarized this material is not stated.** "The provider's diarization is
   badly wrong" is a claim about whichever ASR produced these transcripts; it should name it
   before being generalised.

None of these overturn the gate: the ~0.33–0.44 margins dwarf plausible contamination from
1–5, and session B's two-labels-for-three-people is objective. Phase D is unlocked **as a
build decision**, not as a shipping threshold — every number here is fitted on its own data.
