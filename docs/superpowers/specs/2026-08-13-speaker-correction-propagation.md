# Correcting one turn should name the whole meeting — design

**Status:** spec, revision 3
**Date:** 2026-08-13
**Extends:** `2026-08-09-speaker-identity-v2.md` §6, phase 4 of
`../plans/2026-08-11-speaker-identity-implementation.md`

Two review rounds each found the central mechanism broken. Both are recorded rather than
deleted, because the second failure was the first one wearing a different costume and that is
worth being able to recognise a third time.

## The ask

> 有一句或者数句话现在层面也许是 speaker1，但是我的用户人工更改后比如改成 "Ben L"，
> 这个 meeting 中所有这个人的内容都会改成 "Ben L"？并且以后也能尽可能 detect？

* **Backward, this meeting** — one correction names every other passage by that person.
* **Forward, future meetings** — the same correction becomes an enrolment sample (v2 §6,
  built in phases 1–3, gated by `SPEAKER_IDENTITY_MODE`).

This document is about the first.

## Rejected: propagate by the provider's speaker label

The transcript carries `spk_0`, `spk_1`, … so the cheap propagation is to rename every turn
sharing the corrected turn's label. Phase 0 (2026-08-11 — wearer's device on his chest, two
other speakers side by side at 5 m) measured what that inherits:

* a three-person passage came back with **two** labels — two people merged; and
* another had the **right number** of labels with the content **swapped between them**.

The user asserted *"this passage is Ben"*; label propagation silently upgrades that to *"every
passage the provider grouped with it is Ben"*. The failure is quiet — the transcript looks
*more* confident. Kept only as a tiebreaker that can cap, never promote.

## Rejected twice: score every turn against the corrected window

**Revision 1** scored each turn against the reference and called `decide_name`. That function
returns `tentative` unconditionally when there is one candidate, *without looking at the
score* (`voiceprint_utils.py:122`) — deliberately, because confirming on one candidate means
confirming on an absolute similarity. One corrected person is one candidate, so no name could
ever be confirmed, and a stranger at cosine 0.02 still got `(可能是 Ben L)`.

**Revision 2** tried to supply competition by clustering the session and adding cluster
centroids as rival candidates. It never excluded the cluster **containing the corrected
person's own turns**. For every genuine Ben turn, `cos(reference, v_i)` and
`cos(centroid_Ben, v_i)` are near-equal, the margin collapses to ~0, and nothing confirms —
the same dead end.

That failure is documented in the very module the design leans on. `aggregate_scores`
(`voiceprint_utils.py:76`) exists *only* because ungrouped rows make a person their own
runner-up: Phase 0's Ben holds two profiles ~0.08 apart, below the 0.15 margin, so he reports
`tentative` against himself. Revision 2 re-committed a failure the code carries a docstring
about.

## The mechanism: the session is clustered by voice, and a correction names a cluster

The unit of propagation is **not** a turn scored against a reference. It is a **voice**.

```
all turns in the session ──embed──► v_1 … v_n        (once per run)
cluster {v_i} by voice   ──────────► C_1 … C_k
user corrects a turn t to "Ben L"  ─► the cluster containing t is named "Ben L"
```

Self-competition cannot arise, because the reference is never a candidate alongside its own
cluster — it *selects* a cluster. Per-turn confidence is then an ordinary multi-candidate
question with genuinely different voices competing:

```
for turn i in the named cluster:
    candidates = { C_j : cos(centroid_j, v_i) }   for all j, including its own
    decide_name(candidates, duration)             ← unchanged, real competition
```

A turn sitting comfortably inside its cluster and far from the others confirms. A turn near a
boundary between two voices degrades to `tentative`. That is the right behaviour and it comes
out of the existing rule rather than a new one.

This also reframes what the feature *is*: **the voiceprint re-does the diarization the
provider got wrong, and the user's correction supplies the name.** Phase 0 measured the
voiceprint correcting exactly those provider errors. The user is not labelling turns one by
one; they are labelling a voice.

### The short-window problem disappears

Revision 2 required `window_is_homogeneous` on the corrected window. That check returns `None`
("cannot tell") below **two frames**, and `FRAME_SECONDS = 5.0`, so **every corrected window
under 10 seconds propagated nothing** — which is the most natural gesture a user makes, and it
would have hollowed out the feature while looking like a safety property.

Under cluster naming the short turn only has to *indicate which cluster*, not carry the
evidence. The homogeneity requirement moves to where it belongs — the **cluster**, whose
member turns are checked for coherence, with far more audio than any single turn. A corrected
turn that sits between two clusters (the 1-in-6 two-voice turn v2 §2 measured) names nothing
and says so, rather than silently naming the wrong voice.

### Clustering is the new load-bearing threshold, and it is uncalibrated

Pure numpy — `voiceprint_utils` is deliberately dependency-free and the VAD layer carries no
sklearn — so this is hand-rolled agglomerative clustering on cosine distance with a merge
threshold. That threshold now decides how many voices the meeting had, and it fails in two
opposite directions:

* **merged too far** → two people in one cluster → one correction names both, and the
  runner-up that would have caught it is gone. Over-confirmation.
* **split too far** → one person in two clusters → the second cluster is unnamed, and its
  turns compete with the named one at ~0 margin. Under-confirmation.

It has the same no-calibration problem as the margin, and **changing it invalidates every
calibration row collected before the change**. So it is measured and **frozen before
collection starts**, using the Phase 0 recordings plus whatever the tentative-only step
produces, and the frozen value is recorded in the artifact so a row can be attributed to the
clustering that produced it.

### An absolute floor, stated with its value and its modesty

Below cosine **0.05** to every cluster, a turn is `unknown` rather than tentative. This is not
a confirming threshold — it only stops the mechanism leaning toward a name when the evidence
is nothing. Phase 0's distributions box it in: same-person minimum +0.104, different-person
maximum +0.205, so **any floor above ~0.10 refuses genuine matches** and one low enough to be
safe does almost nothing. It is there for the cosine-0.02 pathology and is expected to fire
rarely; if it fires often, something upstream is wrong. `decide_name` has no floor parameter,
so the floor lives in the caller and is tested there.

### Label agreement caps, never promotes

Where the provider label disagrees with the cluster assignment, the disagreement is recorded
and the state is capped at `tentative`. Two independent sources contradicting each other is
where a confident name is least warranted.

## Three states (v2 §1, unchanged)

* **confirmed** — margin cleared. Renders as "Ben L".
* **tentative** — `(可能是 Ben L)` in the transcript viewer **only**; the anonymous label in
  minutes, email, and action-item responsible party.
* **unknown** — below the duration floor, below the cosine floor, or no cluster.

The corrected turn itself is always confirmed: the user said so.

## Corrections are processed together, not one at a time

Each correction naming a different cluster in the same session is applied in **one run**. This
is not only a cost decision:

* every named cluster becomes a *named* competitor for the others, which is strictly better
  evidence than competing against an anonymous centroid; and
* one run means one clustering, so two corrections cannot be resolved against two different
  partitions of the same meeting.

A correction arriving later re-runs the whole session with all corrections known. The run is
idempotent by construction: same audio, same frozen threshold, same corrections → same rows.

## Precedence, supersession, and withdrawal

1. A **direct correction** beats a propagated name for the same turn.
2. Among corrections, the **latest** wins; ties break on `(created_at, id)` so concurrent
   writes have a total order.
3. A run supersedes the rows of the run before it, in **one transaction** — supersede then
   insert — so an S3-event race (events are unordered, `ReservedConcurrentExecutions` > 1)
   cannot half-apply or collide on the unique index.
4. **Withdrawing a correction** supersedes its rows and triggers a re-run from the *surviving*
   corrections. Earlier superseded rows are **not** resurrected: the overlay is always the
   output of one clustering over the current correction set, never an accumulation of history.
   History remains readable, but it never decides anything.

A partial unique index on `(company_id, session_base, turn_ref) WHERE superseded_at IS NULL`
enforces one live row per turn — **at the string level only**. Since the read join is by
overlap with tolerance (below), the read-time resolver applies the same precedence rules
again; the index is a backstop, not the guarantee. Revision 2 claimed the index alone made two
answers impossible, and with a tolerance join it does not.

## Schema: migration 0040

0038's `speaker_turn_names` has `id, company_id, voiceprint_id NOT NULL, session_base,
turn_ref, state, score, margin, created_at` — no source, no correction pointer, no
supersession, and a `NOT NULL` FK that a no-profile correction cannot satisfy.

Added: `source text` (`correction` | `correction_propagation` | `match`), `correction_ref
text`, `cluster_ref text`, `label_disagreement boolean`, `superseded_at timestamptz`,
`cluster_threshold double precision`; `voiceprint_id` made nullable; the partial unique index.

**Numbered 0040, not 0039** — 0038's own header reserves 0039 as its revert migration, and
quietly repurposing that number would remove a recorded rollback path.

## Where the names live

An **overlay**, resolved at read time; the transcript artifact is not rewritten. Three reasons:
withdrawal must reach everything a correction justified (v2 §6), a derived document may have
exactly one writer (`fieldsight-programme-derived-doc-writers`), and re-running extraction
rewrites the artifact while an overlay survives it.

**`turn_ref` stability is the weak point.** It is `source_filename + start_sec`, and the
live/final two-layer extraction re-assembles turns — a seam dedup shifts `start_sec`, and a row
matching nothing makes a name **silently vanish**. So the join is by **overlap with
tolerance**, and rows matching no turn are reported as an orphan count rather than dropped.

## Invocation: org-api cannot invoke this Lambda

Phase 4's plan has the corrections endpoint invoking `SpeakerEmbedFunction` directly, citing
the Matcher→SuggestionWriter precedent. **That precedent runs the other way**: the matcher is
outside the VPC. org-api is inside it with no NAT, so `lambda:InvokeFunction` black-holes until
timeout with no logs (BUG-36; BUG-43 note 4 states the rule).

The established in-VPC→outside handoff is an S3 request artifact:

```
org-api (in VPC)  ──writes──►  voiceprint_requests/{id}.json
                                  │ S3 event
SpeakerEmbedFunction (NOT in VPC, pure compute, no database)
                   ──writes──►  voiceprint_results/{id}.json
                                  │ S3 event
an in-VPC writer  ──persists──►  speaker_turn_names
```

**Both S3 triggers must be wired manually outside the template** (BUG-33 — SAM cannot attach
events to an external bucket; every S3-triggered function here carries a "NO Events: wired
manually" comment and `scripts/wire-s3-events.sh` does it). Forgetting this **deploys green
with nothing firing**, which is this repo's canonical trap.

The in-VPC writer is a **new function**, not a reuse: it needs `GetObject` on
`voiceprint_results/*` and `DeleteObject` (below), and the deploy role must be checked for
every new resource type it introduces — a missing IAM prefix here has twice produced a silent
success rather than an error.

### This resolves the layer conflict, and a defect found tonight

`fieldsight-test-speaker-embed` deployed green and is **100% non-functional**: both ops raise
`ModuleNotFoundError: No module named 'psycopg'`. It carries `fieldsight-vad-layer` for
onnxruntime, which is **cp312-only**, while `PsycopgLayer` is **cp311-only** — one function
cannot have both, and every unit test stubs `load_profiles`, so nothing ever imported it.
Found by invoking the deployed function.

Pure compute with no database removes the conflict rather than working around it with a second
psycopg layer for 3.12 (which would also need python3.12 added to both deploy workflows), and
removes the `VpcConfig` that existed only to reach Aurora.

`profiles_for_matching`'s consent and `withdrawn` filters stay in the repository, called by
org-api: a withdrawn profile that still matches is not a withdrawal, and that query fails
invisibly. A test pins that the embedder has **no** way to obtain a profile except from the
artifact.

## The artifacts are biometric storage, and are treated as such

Revision 1 proposed caching embeddings and was told the cache was a stored biometric.
Revision 2 dropped the cache and asserted "retains none" — while routing 192-d vectors through
S3 objects with no deletion and no lifecycle rule. **The defect moved rather than died.** Both
reviews caught the same thing in a different place, which is the strongest available signal
that this is the design's soft spot.

So:

* **Propagation carries no profiles at all.** Its candidates are session-local clusters, so
  `voiceprint_requests/` for propagation contains audio references and windows — no vectors.
  This is not a mitigation, it is a consequence of the mechanism.
* **Enrolment must transit a vector** (the embedder computes it, an in-VPC writer stores it).
  That artifact is **deleted by the writer after commit**, with an S3 lifecycle rule as the
  backstop for a writer that dies mid-run, and Phase 6 withdrawal must sweep the prefix —
  `repositories.voiceprints.withdraw` deletes DB rows only, so a withdrawn person's vector
  would otherwise survive in a stale artifact.
* The consent section names these prefixes as **transient biometric storage** rather than
  claiming they do not exist.

## Consent

The audio is already lawfully held and processed. What is new is attaching a **name** to a
voice pattern — biometric information under the NZ Privacy Act — and consent must come from
**the person whose voice it is**, not the wearer and not the employer (v2 §10).

The product decision on record (2026-08-13) is to ship the mechanism and formalise consent as
a real-world process. The engineering seam exists now because retrofitting it means finding
every profile created before it:

* the **within-session overlay** stores no vector, and with propagation carrying no profiles
  that is now structurally true rather than asserted;
* the **enrolment** — a stored vector attached to a named person, which is what makes future
  detection possible — still requires `consent_at` (v2 §6). Not relaxed.

A correction with no consent recorded propagates within the meeting and creates **no stored
profile**. The API reports which of the two happened; a single "success" would hide it.

## Cost and shape

Per-turn embedding is ~100–400 ms at 1769 MB, so a 200-turn session is 20–80 s of inference
plus a per-source-file audio fetch (`_match` currently re-fetches **per turn** — memoized per
file) plus an 84 MB cold-start model download. The path is **asynchronous** (the S3 chain
above; org-api returns 202), so API Gateway's 29 s ceiling does not apply and the **timeout
goes to 600 s**, matching extract-session. Revision 2 kept 120 s and capped coverage instead,
which traded user-visible completeness for a number that costs nothing to raise.

`SPEAKER_PROPAGATION_MAX_TURNS` remains as a backstop and its value is **reported in the
result artifact** — silent truncation reads as "covered everything" when it did not.

## What the user sees, and the confirm affordance

* **Transcript viewer** — names, `(可能是 …)` for tentative, and a propagated name visibly
  distinct from a directly corrected one.
* **Minutes, email, action items** — `confirmed` only. The email is what leaves the building.
* **No extraction re-run.** Phase 5's `on` mode owns whether names re-enter the LLM artifact,
  with its own budget and email suppression. Propagation must never cause
  `session_finalize_requests/{sid}-updated.json` to be written — that object sends an email.
* **A tap-to-confirm affordance on a tentative name is a requirement, not a nicety.** See
  calibration.

## Calibration, and what ships first

`confirmed` needs a margin threshold and none is calibrated. Phase 0's +0.33–0.44 cannot
supply it, and revision 2's claim that session-local centroids are "closer to how Phase 0
measured" was rhetorical: Phase 0's runner-up was another **enrolled profile** from a clean
30–45 s read, not a centroid of 3–4 noisy in-session turns at 5 m. Same-session centroids are
channel-matched, so runner-up scores run higher and margins smaller. The margin distribution
against centroids is a **new, unmeasured quantity**.

So propagation ships in two steps:

1. **tentative-only** — the mechanism runs, rows land, the viewer shows `(可能是 …)`, nothing
   reaches minutes or email.
2. **confirmed enabled** — a threshold change, not a code change, once step 1 has measured one.

**Step 1's measurement is biased unless the affordance exists.** Corrections only label rows a
user noticed were wrong; an uncorrected row is "correct *or* unreviewed" and the two are
indistinguishable, so a naive join measures noticed-false-positives and not accuracy. Worse,
showing `(可能是 X)` anchors the reader — a plausible wrong name gets accepted — biasing the
"held-out" set toward confirming. A one-tap confirm turns silence into a signal, and a sampled
audit covers what is never tapped. Without one of the two, step 1 collects a set that argues
for a threshold lower than the truth.

## Rollback

`SPEAKER_IDENTITY_MODE=off` 404s the endpoint and resolves the overlay to nothing; rows are
inert without a reader. Verified by reading the deployed function's environment, not the PR
description (`fieldsight-unwired-toggle-trap`).

## Out of scope

* Cross-session propagation (Phase 5, gated on calibration).
* Renaming inside the LLM artifact (Phase 5 `on`).
* Identity merging across devices in a multi-device session group.
* Un-naming on withdrawal (Phase 6) — this spec only ensures the rows make it enumerable.

## Open question

Whether tentative names should be shown at all in step 1, or whether the viewer should show
only confirmed names plus an "unnamed" count. Showing them is the fastest path to correction
data; it also puts a name the system does not stand behind in front of a human, and
`(可能是 …)` has never been tested on a real user. Under the two-step plan this is the entire
visible surface of step 1, so it is a product decision that has to be made before build, not
during.
