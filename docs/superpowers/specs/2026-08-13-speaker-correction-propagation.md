# Correcting one turn should name the whole meeting — design

**Status:** spec, revision 2 (revision 1 had three blocking defects; they are recorded below
rather than deleted, because two of them were wrong in ways worth not repeating)
**Date:** 2026-08-13
**Extends:** `2026-08-09-speaker-identity-v2.md` (§6 enrolment by correction), phase 4 of
`../plans/2026-08-11-speaker-identity-implementation.md`

## The ask

> 有一句或者数句话现在层面也许是 speaker1，但是我的用户人工更改后比如改成 "Ben L"，
> 这个 meeting 中所有这个人的内容都会改成 "Ben L"？并且以后也能尽可能 detect？

Two things, separable and worth separating because they ship apart:

* **Backward, this meeting** — one correction names every *other* passage by the same person
  in the session just corrected.
* **Forward, future meetings** — the same correction becomes an enrolment sample. Already
  specified (v2 §6), built (phases 1–3), and gated behind `SPEAKER_IDENTITY_MODE`.

This document is about the first.

## The obvious implementation is the wrong one

The transcript carries a provider speaker label per turn (`spk_0`, `spk_1`, …), so the cheap
propagation is: rename every turn sharing the corrected turn's label. One SQL update, no
model, no cost.

**It inherits every diarization error the provider made, and those are measured to be large.**
From Phase 0 (2026-08-11 — the wearer had the device on his chest, and two other speakers
stood side by side at 5 m):

* a three-person passage came back with **two** labels — two people merged under one; and
* another had the **right number** of labels with the content **swapped between them**.

Under label propagation, one click naming "Ben L" names a second person's speech as Ben,
everywhere. The user asserted *"this passage is Ben"*; label propagation silently upgrades it
to *"every passage the provider grouped with it is Ben"* — a claim they never made and cannot
see the basis of. The failure is quiet: the transcript looks *more* confident.

Rejected as the mechanism. Retained only as a **tiebreaker** (below), where being wrong costs
an abstention.

## The mechanism: propagate by voice, against the room

The corrected window becomes a reference vector; every other turn in the session is scored
against it.

**Revision 1 said this reused `decide_name` unchanged, and that was wrong.** `decide_name`
(`voiceprint_utils.py:120`) returns `tentative` **unconditionally when there is only one
candidate**, without looking at the score at all — deliberately, because confirming on a
single candidate means confirming on an absolute similarity, which the overlapping
distributions forbid. With one corrected person there is exactly one candidate. So revision 1
could:

* never produce a confirmed name — the headline effect was arithmetically unreachable; and
* attach `(可能是 Ben L)` to a stranger's every turn, because at one candidate the cosine is
  ignored entirely, including a cosine of 0.02.

The fix is not to weaken the rule. It is to **supply the runner-up the rule needs**, which
this setting has for free and cross-session matching does not: *the other voices in the same
room*.

```
corrected window ──embed──► reference v
all turns in session ──embed──► v_i          (once, per session)
cluster {v_i} ──► session-local voices C_1..C_k
   candidates for turn i = { "Ben L": cos(v, v_i) } ∪ { C_j : cos(centroid_j, v_i) }
   decide_name(candidates, duration)          ← unchanged, now with real competition
```

This is closer to how Phase 0 actually measured its margins than matching against enrolled
profiles is: those margins are *"best minus closest other speaker"*, and the closest other
speaker is precisely what a session-local cluster supplies.

**It is new arithmetic** — clustering the session's own embeddings — and it needs its own
tests and its own measurement. Revision 1's claim that "nothing new is invented" was false in
two ways: this, and the deployed embedder's `op: match` only scores against **stored** profiles
(`load_profiles` → `profiles_for_matching`), with no path for a session-local reference and no
write of `speaker_turn_names` at all. A new op and a writer are real new surface.

### An absolute floor, as a refusal only

Below a floor cosine, the result is `unknown` — no name, not even tentative. This is **not** an
absolute threshold for confirming; it only stops the mechanism from leaning toward a name when
the evidence is nothing. Confirming still requires the margin.

### Three states, unchanged (v2 §1)

* **confirmed** — margin cleared. Renders as "Ben L".
* **tentative** — nearest but margin not met. `(可能是 Ben L)` in the transcript viewer
  **only**; the anonymous label in minutes, email, and action-item responsible party.
* **unknown** — below the duration floor, below the cosine floor, or no candidate.

The corrected turn itself is always confirmed: the user said so.

### Label agreement caps, never promotes

Where the provider label agrees with the voice match, nothing changes. Where they
**disagree**, the disagreement is recorded and the state is capped at `tentative`. Two
independent sources contradicting each other is where a confident name is least warranted.

### The reference window gets the same guard as an enrolment

v2 §2 measured **1 turn in 6 containing two voices**, and says plainly that any mechanism
treating a turn as one person's voice will sometimes be wrong — *including a user's manual
correction*. The enrolment path already refuses inhomogeneous windows
(`lambda_speaker_embed._enrol`). Propagation runs the same `window_is_homogeneous` check on
the reference; a window that fails, or that is too short to judge, propagates **nothing**. A
contaminated reference spreading a name across a whole session is the exact failure this
mechanism exists to prevent.

## Precedence: what happens when corrections disagree

Revision 1 left this undefined, and 0038 has no uniqueness constraint to fall back on. The
rule:

1. A **direct correction** always beats a propagated name for the same turn.
2. Among corrections, the **latest** wins.
3. A new correction **supersedes** every propagated row it contradicts — including rows an
   earlier correction produced for a different person. Superseded rows are marked, not
   deleted, so the audit shows what a correction undid.

Enforced by a unique index on `(company_id, session_base, turn_ref)` over live rows, so the
overlay cannot have two answers for one turn regardless of what the writer does.

A correction marks a **window**, which may not align to a turn boundary. The confirmed turn is
the one with the greatest overlap with the window; ties go to the earlier turn; a window
spanning several turns confirms all of them it covers by more than half.

## Schema: this needs migration 0039

Revision 1 said `speaker_turn_names` (0038) already stored what it needed. It does not. 0038
has `id, company_id, voiceprint_id NOT NULL, session_base, turn_ref, state, score, margin,
created_at` — **no source, no correction pointer, no disagreement marker**, and a `NOT NULL`
FK to `speaker_voiceprints` that directly contradicts the consent path below (a correction
that creates no profile has no id to point at, so every row would violate the FK).

0039 adds: `source text` (`correction` | `correction_propagation` | `match`), `correction_ref
text`, `label_disagreement boolean`, `superseded_at timestamptz`, makes `voiceprint_id`
nullable, and adds the unique index over live rows.

## Where the names live

An **overlay**, resolved at read time. Nothing is rewritten in the transcript artifact:

1. A correction can be withdrawn, and v2 §6 requires withdrawal to reach "everything it
   justified" — enumerable only if names are rows.
2. `fieldsight-programme-derived-doc-writers`: a derived document gets exactly one writer. The
   transcript artifact has its writer.
3. Re-running extraction rewrites the artifact; an overlay survives that, baked text does not.

**`turn_ref` stability is the weak point of (3).** It is `source_filename + start_sec`, and the
live/final two-layer extraction re-assembles turns — a seam dedup can shift `start_sec`, and a
row whose ref matches nothing makes a name **silently vanish**, which is the same class of
failure this section claims to avoid. So the join is by **overlap with tolerance**, not string
equality, and a row matching no turn is surfaced as an orphan count in the response rather
than dropped.

## Invocation: org-api cannot invoke this Lambda

Phase 4's plan says the corrections endpoint invokes `SpeakerEmbedFunction` directly, citing
the Matcher→SuggestionWriter precedent. **That precedent runs the other way.** The matcher is
*outside* the VPC; org-api is *inside* it, and an in-VPC function has no NAT — a
`lambda:InvokeFunction` call black-holes until timeout with no logs (BUG-36, and BUG-43 note 4
records the same rule). So the direct invoke would fail exactly the way this repo has already
been burned by twice.

The established pattern for an in-VPC function starting outside work is an **S3 request
artifact** (`extraction_requests/`, `session_finalize_requests/`, `reindex_requests/`). This
uses the same:

```
org-api (in VPC, has psycopg)
  ├─ reads the profiles in scope, applies the consent/withdrawn filters
  └─ writes voiceprint_requests/{id}.json  { session, turns[], reference window, profiles[] }
        │ S3 event
        ▼
  SpeakerEmbedFunction (NOT in VPC — pure compute, no database)
  ├─ reads raw audio + model from S3
  └─ writes voiceprint_results/{id}.json   { per-turn state, name, score, margin }
        │ S3 event
        ▼
  an in-VPC writer persists rows into speaker_turn_names
```

### This also resolves a defect found tonight in the deployed function

`fieldsight-test-speaker-embed` deployed green and is **100% non-functional**: both of its ops
raise `ModuleNotFoundError: No module named 'psycopg'`. It carries `fieldsight-vad-layer` for
onnxruntime, which is **cp312-only**, while `PsycopgLayer` is **cp311-only** — one function
cannot have both, and the unit tests stub `load_profiles`, so nothing ever imported psycopg.
Found by invoking the deployed function, not by any test.

Making the function **pure compute with no database** removes the conflict rather than working
around it with a second psycopg layer built for 3.12 (which also needs python3.12 added to
both deploy workflows). It also removes its `VpcConfig`, which it only ever had in order to
reach Aurora.

One thing must survive the move: `profiles_for_matching`'s consent and `withdrawn` filters are
the queries that fail *invisibly* — a withdrawn profile that still matches is not a
withdrawal. They stay in the repository, called by org-api, and the request artifact carries
only what those filters already allowed. A test pins that the embedder has **no** way to
obtain a profile other than from the artifact.

## Cost, stated because it is paid per correction

Revision 1 said "seconds of compute" for ~200 turns. Against the plan's own sizing (~100–400 ms
per turn at 1769 MB) that is **20–80 s of inference**, plus a per-turn S3 GET and WAV decode
(`_match` re-fetches per turn with no memoization), plus an 84 MB cold-start model download —
realistically 40–110 s against a **120 s timeout**, brushing the ceiling on the median case and
blowing it on a 40-minute session. And a synchronous path through API Gateway dies at 29 s
regardless.

Hence: **asynchronous** (the S3 artifact chain above is inherently so; org-api returns 202),
**one audio fetch per source file per run** rather than per turn, and an explicit
`SPEAKER_PROPAGATION_MAX_TURNS` cap whose value is **reported in the result artifact**. Silent
truncation reads as "covered everything" when it did not.

**Revision 1 proposed caching turn embeddings across corrections, and that is dropped.** A
cached ECAPA vector *is* a voiceprint — the same 192-d object `speaker_voiceprint_samples`
treats as biometric data requiring consent and deletion on withdrawal. Revision 1 justified
shipping without consent on the grounds that the overlay "carries no stored biometric,
discarded after the run", and then proposed storing exactly that. The two sentences cannot
both stand. The cache is dropped and the cost is accepted; a durable cache would have to be
declared as retained biometric data with a TTL and Phase 6 coverage, which is a bigger
decision than a speed-up deserves.

## Consent

The audio is already lawfully held and already processed. What is new is attaching a **name**
to a voice pattern — biometric information under the NZ Privacy Act — and consent must come
from **the person whose voice it is**, not the wearer and not the employer (v2 §10).

The product decision on record (2026-08-13) is to ship the mechanism and formalise consent as
a real-world process. The engineering consequence is that the seam exists now, because
retrofitting it means finding every profile created before it:

* the **within-session overlay** computes vectors, uses them, and retains none — with the
  cache dropped, that sentence is now true;
* the **enrolment** — a stored vector attached to a named person, which is what makes future
  detection possible — still requires `consent_at` (v2 §6, phase 4). Not relaxed here.

A correction with no consent recorded therefore propagates within the meeting and creates **no
stored profile**. The API reports which of the two happened; a single "success" would hide it.

## What the user sees

* **Transcript viewer** — names, with `(可能是 …)` for tentative, and a propagated name
  visibly distinct from a directly corrected one. The user needs to know which they asserted.
* **Minutes, email, action items** — `confirmed` only. The email is what leaves the building.
* **No extraction re-run.** Phase 5's `on` mode owns whether names re-enter the LLM artifact,
  with its own budget (`SPEAKER_RERUN_MAX`) and email suppression. Propagation must not
  quietly acquire that blast radius — in particular it must never cause
  `session_finalize_requests/{sid}-updated.json` to be written, because that object sends an
  email.

## Calibration, and what ships before it

`confirmed` needs a margin threshold, and no calibrated one exists (the plan forbids `on` mode
until it does). Phase 0's +0.33–0.44 margins cannot supply it: they are same-day,
channel-matched, n=3–4 per distant speaker, partly cross-session pairs, fitted on their own
data, and the artifacts were not preserved.

So propagation ships in two steps:

1. **tentative-only** — the mechanism runs, rows land, the viewer shows `(可能是 …)`, and
   nothing reaches minutes or email. This is usable (the user can correct what it suggests)
   and it is the calibration collector: propagated rows joined to subsequent corrections are a
   held-out measurement, on the exact distribution that matters.
2. **confirmed enabled** — only once step 1 has produced a measured threshold, per condition
   class, as v2 §9 requires.

Step 1 is the deliverable. Step 2 is a threshold change, not a code change.

## Rollback

`SPEAKER_IDENTITY_MODE=off` 404s the endpoint and resolves the overlay to nothing; rows are
inert without a reader. Verified by reading the deployed function's environment, not the PR
description (`fieldsight-unwired-toggle-trap`).

## Out of scope

* Cross-session propagation (Phase 5, gated on calibration).
* Renaming inside the LLM artifact (Phase 5 `on`).
* Identity merging across devices in a multi-device session group.
* Un-naming on withdrawal (Phase 6) — this spec's contribution is only that the rows exist to
  make it enumerable.

## Open question for review

Whether a propagated `tentative` name should be shown at all, or whether the viewer should
show only `confirmed` plus an "unnamed" count. Showing tentative gives the user something to
correct — the fastest path to more samples — but puts a name the system does not stand behind
in front of a human, and `(可能是 …)` has never been tested on a real user. Under the
tentative-only first step this is not a side question: it is the entire visible surface.
