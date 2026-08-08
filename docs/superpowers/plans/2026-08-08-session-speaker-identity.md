# Plan — session-level speaker identity

Design: `specs/2026-08-08-whole-session-diarization-design.md`

**This plan does not build anything in its first phase.** The design's own review found
that the premise everyone has been working from — one call over the session gives one
label space — is unvalidated at the lengths that matter, and that if it is false, four of
the five options collapse. Phase 0 is a measurement whose failure would save weeks.

Nothing here is scheduled against tonight's deploys. All of it is behind the current prod
behaviour and none of it changes a live path until Phase 3.

---

## Phase 0 — Does a single call hold speaker ids across a long file?

**Why first:** `elevenlabs_utils.py:40` — "scribe_v2 splits 8min+ audio into up to 4
parallel internal jobs". Real sessions are 70–130 minutes. If ids reset at the provider's
internal split boundaries, a session-level call is the per-chunk problem at coarser
granularity, bought at the price of transcribing everything twice.

**Cost:** one session's worth of ASR, once. **Do not** repeat it on five-minute clips —
that is what exhausted the previous allowance.

1. Build one contiguous audio file from a real session ≥30 minutes. Trim the ~2 s
   inter-chunk overlap (`PcmRingBuffer`) and record the resulting
   `session_time → wall_clock` map as a JSON sidecar. **This artifact is reused by every
   later phase**, so build it properly now, not as a throwaway.
2. Verify the map before spending anything: pick five known utterances from existing
   per-chunk transcripts, locate them in the concatenated file, confirm the offsets agree
   within 0.5 s. **A wrong map invalidates everything downstream and looks like a
   diarization failure.**
3. One `scribe_v2` call over the whole file.
4. Score id stability across the internal-split boundaries: does one voice keep one id at
   minute 8, 16, 24? Use T1 (three runs) on the boundary regions only — not the whole file.

**Exit:** a written yes/no. If **no**, stop and go to Phase 4 (option D). If **yes**,
continue.

---

## Phase 1 — Cost, in dollars, per meeting-hour

**Why before any code:** the design's most likely rejection reason, and the cheapest thing
to be wrong about.

1. Price a session-level pass per meeting-hour at current provider pricing.
2. Do the same for a **diarization-only** pass (option A) — self-hosted pyannote-class on
   the existing in-VPC estate, and any hosted diarization endpoint that does not
   re-transcribe.
3. Multiply by the real re-run behaviour, not one pass per session: sessions grow after
   close (`_rerun_if_the_session_grew`), uploads can arrive days late (upload freeze/thaw),
   and `FINAL_RERUN_MAX_GENERATIONS = 3`.
4. Compare against the $1,290/month tier. **Not** against the 10,000-credit evaluation
   allowance — that was an eval artifact and the wrong denominator.

**Exit:** option A vs B vs C chosen on numbers, written down with the numbers.

---

## Phase 2 — Prove the join on ground truth, offline

No pipeline changes. A script, the Phase 0 artifacts, and the hand-labelled clip.

1. Join session-level labels to per-chunk turn instances by timestamp overlap.
2. Score against the 4:49 clip's "actually said by" column, splitting the three failure
   modes the ground truth already names: **splitting** (one person, several ids),
   **merging** (several people, one id), **phantoms** (device audio given an id at all).
3. Handle the two-source turn explicitly. Three of eighteen turns hold two speakers, so the
   join must either split the turn or mark it ambiguous. **Silently assigning the whole
   turn to one speaker is the withdrawn spec's exact failure — a test must forbid it.**
4. Decide and record the `speaker_count` semantics (design §7): mapped labels before
   assembly, or raw labels keeping today's meaning. The `speaker_count == 1` gate at
   `lambda_item_writer.py:359` must still fire for a genuine solo session.

**Exit:** a measured accuracy figure and an explicit answer for two-source turns. If the
join does not beat today's per-chunk labels on ground truth, go to Phase 4.

---

## Phase 3 — Wire it, inert, behind a parameter

Only after 0–2 pass. Ships switched off.

1. New Parameter + both `--parameter-overrides` lines from the start —
   `tests/unit/test_template_workflow_parameter_wiring.py` now enforces this, and it exists
   because #294 shipped a rollback switch that was never wired.
2. Apply identity **inside turn assembly**, so every consumer sees the same thing at once:
   `assemble_session_turns`, `assemble_deduped_turns`, `assemble_group_turns`,
   `speaker_count`, and the viewer's `speaker_turns_from_items` — which reads transcript
   JSON directly and would otherwise **silently ignore** the mapping. A consumer left
   behind means the report and the email disagree about the same meeting, which is the
   inconsistency #291 just removed for device announcements.
3. Order it against the final pass explicitly. Either the session pass gates the
   `extraction_requests/` write, or its arrival triggers one bounded re-run through
   `_request_final_rerun`. Without this the authoritative pass runs before the identity
   work exists and never revisits it.
4. Make the session pass idempotent and supersede-aware, mirroring `_supersedes`:
   coverage-based, never discarding work already paid for.
5. Decide what the Tier-0 confirmation email says. It goes out 1–2 minutes after the last
   chunk, before idle close — any speaker claim it carries will never be mapped.
6. Multi-device: state whether the pass runs per member. N devices give N disjoint label
   spaces with no shared clock (BUG-37); cross-device identity stays out of scope, but the
   group path must not silently produce nonsense.

**Verify on test before prod**, and verify the deployed artifact rather than the workflow
log: read the live Lambda config, download the deployed zip, and check the real EventBridge
rule state.

---

## Phase 4 — The floor, if Phase 0 or 2 fails

Stop presenting per-chunk labels as identities. Chunk-scoped colouring only; no cross-chunk
speaker claims in the extraction prompt, the minutes or the email.

**This is not free** and must not be planned as if it were:

- `speaker_count` must stay computed on **unqualified** labels, or the
  `speaker_count == 1` gate stops firing and self-referential responsible parties silently
  stop resolving to the wearer — no error, no failing test.
- The extraction prompt's "one entry per distinct speaker label" would inflate
  `participants`.
- The viewer, minutes and email rendering live in `fieldsight-ui`, so this spans repos.

It is still worth doing on its own terms: **a wrong name on a commitment is worse than no
name**, and today the system produces wrong names for free.

---

## What this plan deliberately does not do

- Re-open provider selection (measured; prod moved).
- Attempt speaker naming — voiceprints are biometric data under the NZ Privacy Act, and no
  surveyed option is both usable and tenant-safe. Option A's anonymous within-session
  clustering is not affected by this.
- Try to fix distance. A chest microphone is 20 cm from the wearer and 2–5 m from everyone
  else, and the two people the diarizer merges away are the two furthest from it. That is
  placement and device-side AGC, and it is irreversible once recorded.
