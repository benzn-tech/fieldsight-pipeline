# Session-level speaker identity — design

**Date:** 2026-08-08
**Status:** Design, **and it does not yet recommend building the obvious thing.**
Supersedes the diarization half of
`2026-08-07-speaker-attribution-measurement-design.md`, withdrawn in its own §0.
**Evidence:** the hand-labelled 4:49 UCPK2 clip (2026-08-07), the prod session
`sid754a6990ca35491d9b896b6326643016` (2026-08-08), and the adjudication runs in §3.
**Review:** an adversarial pass on the first draft found that its central premise was
unvalidated at real session lengths. §4 is the result; the draft's design survives only as
option C in §5.

## 0. What this is about

"Who said this" is wrong on prod, and every previous attempt aimed at the wrong layer.

**The failure is that a speaker label means nothing outside the call that produced it** —
not that the diarizer is weak, not that chunk boundaries confuse it, and not something a
language model can repair by reading the text afterwards.

## 1. The ground truth, and what it killed

Eighteen hand-labelled turns from five minutes of a real site meeting:

| observation | consequence |
|---|---|
| One person occupies **eight label instances** in five minutes | labels are not identities |
| The same person is **both `spk_0` and `spk_1` inside one 30-second job** | fragmentation is *not* a boundary artefact |
| **Three of eighteen turns contain two different sources** — one is a device announcement followed by a person | a label instance is not an atomic unit |
| `c0142/spk_1` is James A but `c0143/spk_1` is James L | the same string means different people in adjacent chunks |

Row three withdrew the earlier spec. It proposed a reasoning pass mapping `person →
labels`; a turn holding two speakers cannot be expressed in that shape at all. The scheme
was not insufficient — it could not represent the data.

## 2. Prod state, with where each was checked

Stated with provenance on purpose: a design built on an assumed baseline misdiagnoses its
own results. All verified 2026-08-08 by reading the **live Lambda configuration**, not the
template defaults or the workflow logs (the template's defaults differ, and repo variables
are what actually decide).

| setting | value | how checked |
|---|---|---|
| `ASR_PROVIDER` | `elevenlabs` (`scribe_v2`) | `get-function-configuration fieldsight-prod-transcribe` |
| `TRANSCRIBE_WHOLE_CHUNK` | `true` | same, on `fieldsight-prod-vad` |
| `DROP_SILENT_CHUNKS` | `true` | same |
| `VAD_THRESHOLD` | **`0.15`** (was 0.2) | same. Reverses the "0.2 stays" decision in `2026-08-07-asr-hallucination-and-vad-findings.md`; re-measured on 2026-08-08 with the retry at `threshold/2` modelled, which the earlier sweep omitted |
| `NORMALISE_AUDIO` | **`true`** (was false) | same; end-to-end confirmed on test, sidecar `-33.0 → -18.8 dBFS` |

So the unit of transcription is **already the 30-second chunk**, and per-segment jobs were
measured worse (broken diarization on 1–2 s clips, language-ID flips). Nothing below should
re-litigate that.

One correction to the record while it is being cited: ElevenLabs returned **423 words to
AWS Transcribe's 285** on the hardest audio — 1.5×, not 2×. And the "15 s minimum billing"
that justified whole-chunk emission is an **AWS Transcribe** billing floor; it is not a
reason that carries to the current provider.

## 3. The constraint that bounds every option: the audio is quiet

1. **The device records systematically quiet.** Two sessions on two days: medians −36.0 and
   −33.0 dBFS against a normal −20 to −12.
2. **The distant speakers sit at about −58.7 dBFS — roughly 5.3 of the available 16 bits.**
   The two people the diarizer merges away are the two furthest from a chest-mounted
   microphone: 20 cm from the wearer, 2–5 m from everyone else, 20–30 dB of inverse-square
   before anything else happens. (Separately, the median sits ~44 dB below the peak within
   a recording — that is the dynamic spread that motivates compression, a different number
   from the one above.)

**A share of the attribution error is therefore unrecoverable by any backend change.** The
honest target is "labels consistent across the session", not "every voice separated".

The 2026-08-08 work also produced the way to check claims about this cheaply, and every
evaluation below should use it instead of ears:

- **T1 repeatability** — the same clip three times through one engine. Invented text varies
  between runs; real speech is reproduced verbatim. (A 39-word "conversation" produced once
  from a normalised noise-only window never reappeared in three further runs.)
- **T2 cross-engine agreement** — the same clip through a second engine. Two models do not
  invent the same sentence; overlap means real speech.

## 4. Why the obvious design is not safe to build yet

The draft of this spec argued: one call over the whole session gives one label space *by
construction*. Three findings undercut that.

### 4.1 The provider already splits long audio internally

`src/elevenlabs_utils.py:40`: **"scribe_v2 splits 8min+ audio into up to 4 parallel
internal jobs."** Every session this design targets is 70–130 minutes — deep inside that
regime. Whether ids stay consistent across the provider's *own* internal splits is exactly
the assumed property, and it has never been observed. The 4:49 evaluation says nothing
about it: it is below the threshold.

**If ids fragment across internal splits, a session-level call is the per-chunk problem at
larger granularity, for the price of transcribing everything twice.**

### 4.2 There is no session-level audio to send

The bucket holds ~30-second objects: originals under `users/{user}/audio/{date}/…_c{NNNN}`,
per-chunk normalised copies under `audio_segments/`. A whole-session artifact does not
exist, and building one is not concatenation:

- **Chunks deliberately overlap.** The mobile contract has each chunk carry ~2 s of the
  previous chunk's PCM so "a sentence crossing a boundary appears whole in both"
  (`_dedup_turn_boundaries`, `lambda_extract_session.py:418`). Naive concatenation of a
  78-minute session duplicates ~5 minutes of audio, feeds every seam sentence to the
  diarizer twice, and **skews every timestamp after the first seam** — which is precisely
  what any timestamp-based join depends on.
- **Which copy?** `audio_segments/` is normalised but has holes (silent chunks are
  dropped); `users/…` is complete but raw, and whole-file `loudnorm` is not the
  concatenation of per-chunk `loudnorm` results.
- **Practicality.** 78 minutes of 16 kHz mono PCM is ~150 MB, and the ElevenLabs path is a
  synchronous multipart POST with `HTTP_TIMEOUT = 280.0`.

Any option that needs session audio must first define **the session audio and its
time map** — overlap trimming, gaps, late chunks, and which normalisation.

### 4.3 A "mapping" that can split turns is not a mapping

§1 says three of eighteen turns hold two speakers. A join that must be able to *split* a
per-chunk turn is not a label→label map — it is a time-ranged relabelling that rewrites
turn boundaries, i.e. a new turn stream. Any design claiming to be "just a mapping" has to
confront this rather than inherit the reassurance.

## 5. The options, ranked

### A. Diarization-only pass over the session (recommended to evaluate first)

The session pass needs **labels and timestamps, not words**. A diarization-only model
(pyannote-class, self-hostable on the existing in-VPC estate) produces exactly the input a
timestamp join needs at a fraction of ASR cost, with **no hallucination surface at all** —
it cannot invent sentences because it does not emit sentences.

It also sidesteps §6's privacy objection: anonymous within-session clustering is not
enrollment, holds no voiceprint across sessions, and needs no workspace-scoped speaker
library.

Still requires §4.2's session audio and time map.

### B. Session transcript becomes the final record

Total ASR spend is identical to option C (each minute paid once live, once at session
level), but the session transcript is used *as* the final pass's input. This deletes the
join, the turn-splitting problem and the mapping-consumer problem outright.

The draft rejected this on one weak ground — "overwriting the text discards the timestamp
chain". It does not: absolute time is `session_start + concat-offset`, the same arithmetic
`transcript_utils` already performs, and a session-level transcript written under the
`sid{…}` session base flows through `gather_session_segments` unchanged. If B is rejected,
reject it on real grounds — the transcript viewer is keyed to per-chunk files, and the live
and final records would then differ in shape.

### C. Session ASR produces a mapping applied at assembly (the draft's design)

Kept for completeness. Most expensive, most moving parts, and §4.3 says its central
artifact is not the simple thing it claims to be. Only reach for it if A is unavailable and
B is rejected.

### D. Narrow the claim (the no-cost floor)

Stop presenting per-chunk labels as identities: consistent colouring *within* a chunk, no
cross-chunk speaker claims in the extraction prompt, the minutes or the email.

**This is not free, and the draft was wrong to say so.** See §7.

## 6. What must be measured before choosing

**Step 0, before any cost model:** take one real session over 30 minutes and check whether
a single provider call keeps speaker ids stable across the provider's internal splits
(§4.1). Score with T1/T2, not by reading it. **If ids do not hold, options A/B/C all
collapse to D**, and everything after this line is moot.

Then, in order:

1. **Cost per meeting-hour** in dollars against the $1,290/month tier — not against the
   10,000-credit *evaluation* allowance, which was an eval artifact and is the wrong
   denominator.
2. **Does the join reproduce ground truth?** Run it against the hand-labelled clip's
   "actually said by" column.
3. **What happens to a two-source turn?** It must split, or be explicitly marked ambiguous.
   Silently assigning the whole turn to one speaker is the withdrawn spec's failure.

## 7. The consumers, and the trap in option D

Every per-chunk-label consumer must be updated together, or the same session reads
differently depending on which surface you look at — the exact inconsistency PR #291 just
removed for device announcements:

| consumer | path |
|---|---|
| live + final extraction prompt | `assemble_session_turns` → `render_transcript` (emits raw `t['speaker']`) |
| rolling summary, confirmation email | `assemble_deduped_turns` |
| transcript viewer | `speaker_turns_from_items` (`transcript_utils`) — **reads transcript JSON directly and would silently ignore a mapping** |
| `speaker_count` | computed from turn labels |
| multi-device group | `assemble_group_turns` |

**The `speaker_count == 1` gate is where option D bites.** `lambda_item_writer.py:359` uses
it to resolve a self-referential responsible party to the wearer's name. A genuine solo
session yields `{spk_0}` → 1 *because labels are unqualified across chunks*. Chunk-qualify
them and the union size becomes roughly the chunk count: the gate never fires again, self
references stop resolving, **no error and no failing test** — the "consumers hanging on a
literal" failure this codebase has hit repeatedly. The same applies to the extraction
prompt's "one entry per distinct speaker label", which would inflate `participants`.

So D must either keep `speaker_count` computed on unqualified labels, or restate the gate —
and it spans repos, since the viewer, minutes and email rendering live in `fieldsight-ui`.

**Decision to write down when a option is chosen:** if the mapping is applied *before* turn
assembly, `speaker_count` over mapped labels becomes strictly more meaningful and the gate
survives; over raw labels it keeps today's semantics. Pick one explicitly.

## 8. Failure modes the design must answer

- **Ordering against the final pass.** The final extraction is triggered by an
  `extraction_requests/` artifact at session close; a session-level pass also starts at
  close and takes minutes. Nothing orders them, and the final pass does not re-run except
  on transcript-set growth, capped at `FINAL_RERUN_MAX_GENERATIONS = 3`. Without an
  explicit rule, **the authoritative pass routinely runs before the identity work exists
  and never revisits it.** Either the session pass gates the final request, or its arrival
  triggers one bounded re-run through the existing `_request_final_rerun` channel.
- **Re-runs multiply the cost.** Sessions grow after close — idle close fires ~15 minutes
  after the last chunk, and uploads can arrive far later (that is why
  `_rerun_if_the_session_grew` exists). A cost model of one pass per session is wrong
  unless the session pass is idempotent and supersede-aware, mirroring `_supersedes`:
  coverage-based, never discarding work already paid for.
- **Session-level ASR re-opens the silence surface.** `DROP_SILENT_CHUNKS` exists because
  transcribing VAD-silent audio manufactured 10.7% of one meeting. A pass over concatenated
  raw audio hands the engine exactly that material again. Either concatenate only
  speech-bearing chunks (which changes the time map — §4.2) or state how invented speech is
  gated at session level. **Option A is immune to this; that is a point in its favour.**
- **Multi-device groups are unscoped.** The group-merge design (2026-08-04, Phase C wiring
  in flight) merges N devices' transcripts with no shared clock (BUG-37). Per-device session
  passes give N disjoint label spaces — the same person is a different stable id on every
  device, and a four-device group was the real 2026-08-07 meeting. Cross-device identity
  stays open; the spec must at least say whether the pass runs per member and how it
  interacts with group finalize ordering.
- **The email precedes everything.** The Tier-0 confirmation email goes out 1–2 minutes
  after the last chunk, before idle close even fires. Any speaker claim it carries will
  never be mapped. Decide whether it carries speaker claims at all.

## 9. What this does not do

- **Name speakers.** That is voiceprint territory: biometric data under the NZ Privacy Act,
  and the provider survey found nothing both usable and tenant-safe (ElevenLabs' speaker
  library is workspace-scoped). Note this objection does **not** apply to option A's
  anonymous within-session clustering.
- **Re-open provider selection.** Measured; prod moved.
- **Fix distance.** §3 — that is microphone placement and device-side AGC, and it is
  irreversible once baked into a recording.
