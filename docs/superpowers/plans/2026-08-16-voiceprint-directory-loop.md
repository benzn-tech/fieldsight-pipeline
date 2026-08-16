# Plan — voiceprint directory loop

**Spec:** `docs/superpowers/specs/2026-08-16-voiceprint-directory-loop.md`
**Date:** 2026-08-16

Two adversarial reviews produced 22 findings across the two drafts. The ordering below is
what survived them, and it is not the ordering the design started with — twice a step that
looked independent turned out to be inert or destructive without an earlier one.

Each task ships as its own PR: green CI, mutation-verified guards, merged to `develop`,
deployed to TEST, then verified against live resources. PROD stays `SPEAKER_IDENTITY_MODE=off`
throughout, so none of this reaches a customer until the switch is moved deliberately.

---

## T0a — write-time precedence

**Why first:** `record_turn_name` supersedes with no `source` or `state` predicate, so any
new source can bury a human correction by arriving later. Every rule in this spec about
"never overwriting a stronger row" is prose until this exists.

- `record_turn_name` gains a precedence rule: a weaker source may not supersede a stronger
  live row. It declines and reports, rather than winning on arrival order.
- Rank comes from one table (`turn_name_overlay._SOURCE_RANK`), now pinned to the writer's
  actual strings by `test_every_source_the_writer_writes_has_a_rank` (shipped in #505).
- Equal rank still supersedes — a newer match replacing an older match is the wanted
  behaviour; only *downward* writes are refused.

**Mutation:** remove the check → a `voiceprint_match` row buries a `correction` row.
**Live:** re-run `speaker-match` on a session holding a human correction; the correction
survives and the response reports what was declined.

---

## T0b — label inheritance

**Depends on T0a.** Delivers the short-turn case from the user's example.

1. `_session_turns` carries the transcriber's `speaker_label`, keyed with `source_filename`
   (which *is* the transcription-call identity — batching merges namespaces on purpose).
2. Group by `(source_filename, speaker_label)`; a group containing a named turn spreads that
   name to every member, **with no duration floor**.
3. `source='label_inheritance'`, state never above the source it came from.
4. Inherited rows carry the **provenance of what they inherited from** (`voiceprint_id` or
   `correction_ref`), or withdrawal returns 200 and the names stay on the transcript.
5. `unname` writes a tombstone; inheritance does not re-derive a name rejected for that
   session.
6. Grouping happens **before** `_split_for_budget`, or in a pass after all runs land — the
   split's stated invariant ("no turn's answer depends on another's") is violated by
   inheritance by construction.
7. `TOLERANCE_SEC` re-derived against sub-3 s turns; its current justification is the 3 s
   floor that this task removes.
8. Key on explicit label presence, not on the `"spk_0"` that both readers coerce absence to.
9. `unmatchedNames` reworked, or it reports hundreds of orphans per windowed request and the
   signal dies.

**Mutation:** each of 4–8 removed individually; see the spec's verification table.
**Live:** the user's own example session — `spk_0` turns of 1–2 s inherit Ivy, `spk_1` does
not, and a withdrawal afterwards removes all of them.

---

## T1 — profiles belong to people

**Why before T2:** every profile has `user_id IS NULL`, so the site branch's
`p.user_id IS NULL` escape passes all of them and narrowing is a no-op.

- Resolve the correction's name against the company directory: `safe_name(input)` vs
  `folder_name`, then `concat_ws(' ', first_name, last_name)`, then unique `first_name`.
  `archived_at IS NULL`.
- Ambiguous or absent → `user_id = NULL`, profile still works by name.
- Response carries `linkedTo: {userId, matchedOn} | null` with a reason.
- **`upsert_profile`'s found-branch must UPDATE**, and a one-off backfill runs for the
  existing population — otherwise only new profiles ever link and T2 stays inert for
  everyone who already has one.
- Record `linked_by` / `linked_at` / `linked_on` (migration), on the `consented_by`
  discipline: when somebody asks why the system believes a voice is a named person, there is
  an answer.

**Mutation:** drop `safe_name` → multi-word names resolve to nobody; drop `concat_ws` → a
NULL surname resolves to nobody; drop the ambiguity refusal → two same-named people collapse;
drop the found-branch UPDATE → an existing profile never links.
**Live:** correct a speaker on TEST with a name that matches a directory entry; the profile
row carries a `user_id`.

---

## T2 — narrow the candidate pool

**Two independent inertnesses; both must go, and neither alone moves anything.**

- Fix `profiles_for_matching`'s site branch: add the "belongs to no site" arm, or a
  `field_only` person becomes *less* matchable after T1 than before. Add `archived_at IS NULL`
  on the membership join — every other membership query in the repo has it.
- `speaker_match`'s artifact carries `site_id`, resolved by the established authority chain
  (`site_for_media` → `meeting_session.site_id` → `site_for_day`), **not** starting at the
  day-majority rung.

**Mutation:** remove `site_id` from the artifact → narrowing reverts to a no-op; remove the
"no site" arm → a `field_only` profile disappears.
**Live:** a match run with `site_id` must return **fewer distinct `person_key`s** than one
without. Count distinct people — `len(profiles)` is a *sample* count, already mislabelled
"profile(s)" in three log lines.

---

## T3 — harvest the cluster, labelled as inference

**The largest build, and the one whose claims the review cut down.**

- `source='correction_propagation'` on harvested samples. Harvested and human-vouched samples
  must stay separable forever.
- A profile holding only harvested samples stays `tentative` until it holds one
  `'correction'` sample — the loop may build coverage, never confidence.
- Floor **10 s**, not 5: `window_is_homogeneous` returns `None` under two frames and frames
  are 5 s, so a 5–9.99 s window is unjudgeable and the "refuse None" rule rejects it.
- Cap 6 samples / 60 s.
- `_propagate` must return the cluster with durations and offsets; the writer must accept a
  **list** of samples with per-sample refusal handling.
- Harvest runs **after** the anchor's own homogeneity and between-voices checks, not before.

**Mutation:** each guard removed individually.
**Live:** one correction on a multi-speaker TEST session produces a profile with several
samples, all `correction_propagation`, and the profile is `tentative`.

---

## T4 — dialogue inference (blocked until a host exists)

**Not implementable as written.** org-api is in-VPC with no NAT (an LLM call black-holes,
BUG-36) and does not import `llm_utils`. `SpeakerEmbedFunction` is non-VPC but carries no
provider key, so `call_llm` would return "not configured" and the tier would refuse every
window forever while every test stayed green. It also holds `ReservedConcurrentExecutions: 5`
against a worst case of 150 s × 4 attempts inside a 600 s timeout.

T4 begins with choosing a host and a timeout budget. Anything estimating it as prompt work is
wrong.

---

## T5 — names spoken in the room

Second-order. Adds directory entries mentioned in a transcript to the candidate pool. Not
attribution — a spoken name is evidence somebody is present, not evidence about which voice
is theirs.

---

## Conflict posture

Other sessions are working on PROD and TEST at the same time. This work:

- touches `voiceprints.py`, `turn_name_overlay.py`, `lambda_speaker_embed.py`,
  `lambda_voiceprint_writer.py`, `lambda_org_api.py` (speaker routes only), and adds
  migrations — no shared surface with the report, extraction or programme paths;
- is gated on `SPEAKER_IDENTITY_MODE`, which is **`off` on PROD** and stays there;
- adds only additive migrations (nullable columns), safe to run against PROD ahead of the
  switch.

Before each merge: re-fetch and compare against `origin/develop` rather than trusting local
state — parallel sessions have moved `main` under this work before.
