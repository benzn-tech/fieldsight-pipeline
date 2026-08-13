# Correcting one turn should name the whole meeting — design

**Status:** spec, revision 4 — **built 2026-08-14, with the deferrals listed below**

> **What is implemented differs from this document, and the differences are deliberate.**
> Step 1 ships blanket-`tentative` for everything inferred, which subsumes several of the
> refinements described here as current. Still **deferred**, and none of them is inert
> silence — each was checked and left out:
>
> * **per-turn `decide_name` inside the named cluster** — every propagated turn is
>   `tentative`, so grading them against a margin would change nothing a reader can see.
> * **the 0.05 cosine floor** — the margin gate on the corrected turn already refuses the
>   case it was invented for, and Phase 0's distributions leave it almost nothing to do.
> * **the label-disagreement cap** — the column exists and the writer accepts it; the
>   embedder never sends it, because everything it sends is already capped.
> * **the k = 1 reason in the artifact** — a solo session's rows are indistinguishable from a
>   multi-voice session's. Worth adding before step 2, not before step 1.
> * **`SPEAKER_PROPAGATION_MAX_TURNS` reported in the result** — the cap is 300 and no real
>   session has approached it, but silent truncation is exactly what this document forbids
>   elsewhere, so it is a real debt.
> * **corrections processed together** — each correction is its own run. Same-voice
>   re-correction is safe (per-turn supersession); what is missing is the mutual competition
>   two named clusters would give each other.
> * **run-level supersession** — supersession is per `turn_ref`, so a re-extraction that
>   shifts offsets leaves prior rows live and they surface as `unmatchedNames`.
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

### One voice in the room: k = 1

A session that clusters to a **single voice** — a solo narration walk, which is the most
common FieldSight recording shape — hands `decide_name` one candidate, and that is the branch
that returns `tentative` unconditionally. Revision 3 did not mention this, so the first
defect ever found in this design would have come back untouched for the majority of
recordings.

It is not fixed by special-casing. It is the honest answer: with one cluster there is no
competing voice, so the only thing that could make the name wrong — clustering having merged
two people — is exactly the thing that has no evidence against it. **k = 1 caps at
`tentative`, permanently and by policy**, and the reason is written into the result artifact
so it does not read as a bug. A visitor's few turns absorbed into that single cluster then get
a tentative name and not a confirmed one, which is the correct outcome.

Propagation beyond the corrected turn therefore requires **k ≥ 2** to produce anything
`confirmed`, ever.

### The centroid must not include the turn being scored

`cos(centroid_own, v_i)` is inflated because `v_i` is part of that centroid, and inflated
*most* for the smallest clusters — a two-turn cluster scores its own members highly even when
they are barely alike. Margins would be systematically largest exactly where the evidence is
weakest, and step 1 would then calibrate on the biased quantity and freeze a cluster-size
dependence into the threshold.

Leave-one-out, which is two lines of numpy: `(n·c − v_i)/(n − 1)`, renormalized. Pinned by a
test that a two-member cluster does not out-score a ten-member one on identical audio.

### The short-window problem disappears

Revision 2 required `window_is_homogeneous` on the corrected window. That check returns `None`
("cannot tell") below **two frames**, and `FRAME_SECONDS = 5.0`, so **every corrected window
under 10 seconds propagated nothing** — which is the most natural gesture a user makes, and it
would have hollowed out the feature while looking like a safety property.

Under cluster naming the short turn only has to *indicate which cluster*, not carry the
evidence — **but indicating still requires an embedding**, and the pipeline refuses to embed
any turn below `DEFAULT_MIN_TURN_S` = 3.0 s before the model is asked
(`lambda_speaker_embed.py:197`). Revision 3 claimed the floor disappeared; it had moved from
10 s to 3 s. Two things follow, and both must be stated rather than discovered:

* **The corrected turn is embedded regardless of its duration.** Its *name* is user-asserted
  and carries no inference risk; only its *assignment to a cluster* does. So it is embedded,
  and if its distance to the top two clusters is too close to separate, it names **nothing**
  and says which two it was between. That replaces revision 3's undefined "sits between two
  clusters" hand-wave, and is the two-voice-turn detector that removing
  `window_is_homogeneous` left missing (v2 §2's 1-turn-in-6).
* **Turns under 3 s are in no cluster**, so "the whole meeting renamed" silently excludes
  every short interjection — "Yeah", "Right" — which the batching work measured as 18–20% of
  turns. They render as `unknown`, and the count is reported, because a user who sees a third
  of the transcript unnamed deserves to know it is a floor and not a failure.

The homogeneity requirement moves to the **cluster**, and needs its linkage stated or it is
decoration: with **complete linkage** at merge threshold τ, "all pairs within τ" holds *by
construction*, so the guard can never fire — the precise failure `window_is_homogeneous`'s own
docstring warns about. The check is therefore run at a **tighter bound than τ** and is a
genuine second opinion, and a cluster with fewer than two usable turns is `None` — "cannot
tell" — and names only the corrected turn itself.

### Clustering is the new load-bearing threshold, and it is uncalibrated

Pure numpy — `voiceprint_utils` is deliberately dependency-free and the VAD layer carries no
sklearn — so this is hand-rolled agglomerative clustering on cosine distance with a merge
threshold. That threshold now decides how many voices the meeting had, and it fails in two
opposite directions:

* **merged too far** → two people in one cluster → one correction names both, and the
  runner-up that would have caught it is gone. Over-confirmation.
* **split too far** → one person in two clusters → the second cluster is unnamed, and its
  turns compete with the named one at ~0 margin. Under-confirmation. The user then sees **half
  the meeting renamed and half not**, which reads as a broken feature rather than a cautious
  one — so the viewer asks directly: *"another unnamed voice resembles Ben L — same person?"*
  One tap merges the clusters, and that tap is also the highest-quality calibration row the
  system can obtain, because it is a human answering the exact question the threshold governs.

It has the same no-calibration problem as the margin, and **changing it invalidates every
calibration row collected before the change**. Revision 3 said to freeze it "before collection
starts, using the Phase 0 recordings plus whatever the tentative-only step produces" — which
is incoherent, because the tentative-only step *is* the collection and cannot supply an input
to a decision made before it runs.

So: frozen from the **Phase 0 recordings alone**, before any calibration row is written. The
per-row `cluster_threshold` column stays, not because the value is expected to change but as
the audit trail of a rule intended never to fire — if it ever does, every row not carrying the
current value is disqualified from calibration, automatically rather than by memory.

**A kill criterion, decided before building.** Phase 0's own numbers make the split case the
expected case, not a tail: same-person similarity spans +0.104 to +0.639 while different-person
reaches +0.205, so the bands overlap and for some speakers **no threshold exists** that holds
them together without merging others. Clustering the Phase 0 audio is therefore the first task,
not a later validation: if no threshold cleanly separates its three known speakers, this
mechanism does not ship and the effort moves to Phase 5 matching instead.

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

### Propagation must not promote profiles from its own output

`confirmations_count` (`repositories/voiceprints.py:169`) counts `DISTINCT session_base` over
`speaker_turn_names` rows with `state = 'confirmed'` — **regardless of source** — and that
count is what promotes a profile from `tentative` to `confirmed` after N *independent human*
confirmations (v2 §6). The moment propagation writes confirmed rows carrying a
`voiceprint_id`, the machine satisfies its own promotion criterion with its own output, across
sessions, and the profile that then names people was confirmed by nothing.

Two ways to cut it; both are taken, because either alone is one edit away from being undone:
propagation rows write `voiceprint_id NULL`, **and** `confirmations_count` filters
`source = 'correction'`. A test pins that a table full of propagated rows promotes nothing.

This is invisible in the schema — the loop only exists because two features share one table —
and it is the kind of thing that would have been found in production as "profiles confirming
themselves overnight".

### A cluster key is not a name

`decide_name` returns the winning key as `Decision.name`, and here the keys are cluster
references. When the best cluster is an **unnamed** one, the decision reads `confirmed C_3`,
and `C_3` must never reach a user-visible surface — the existing status-cap idiom in `_match`
keys off profiles and does not apply. Unnamed clusters render as the anonymous label, and the
Phase-A-style test applies: **fail if a raw cluster key or `spk_N` string reaches a
user-visible surface.**

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
                                  │ S3 event  (the ONLY new S3 trigger)
SpeakerEmbedFunction (NOT in VPC, pure compute, no database)
                   ──invokes──►  an in-VPC writer  ──persists──►  speaker_turn_names
```

**Only the first hop is an S3 artifact, and revision 3 got the second one wrong.** BUG-43
note 4 states the rule in both directions: an in-VPC function cannot invoke outward (no NAT),
but **non-VPC → in-VPC is explicitly permitted** — the callee is only a target and initiates
nothing — and the note warns by name against "forcing an S3 hop that should have been a direct
invoke", because BUG-33 makes every new S3 trigger hand-wired. Revision 3 applied the first
hop's constraint to both hops. The `Matcher → SuggestionWriter` precedent runs exactly this
way and is cited in the template.

Making the second hop a direct invoke also removes the enrolment vector's S3 residence
entirely, along with the deletion, the lifecycle rule, and the Phase 6 sweep that residence
would have required. That is the third time a biometric-storage problem has been solved by
noticing the storage did not need to exist.

**The one remaining S3 trigger must be wired manually outside the template** (BUG-33 — SAM
cannot attach events to an external bucket; every S3-triggered function here carries a "NO
Events: wired manually" comment and `scripts/wire-s3-events.sh` does it). Forgetting it
**deploys green with nothing firing**, this repo's canonical trap.

The in-VPC writer is a **new function**: it needs `lambda:InvokeFunction` granted to the
embedder, and the deploy role must be checked for every new resource type it introduces — a
missing IAM prefix here has twice produced a silent success rather than an error.

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
* **Results carry no vectors either** — assignments, scores, and references only, never a
  centroid. A centroid is the voice pattern of whoever is in that cluster, consenting or not,
  and serializing one would relocate this defect a third time, into `voiceprint_results/`.
  Pinned by a test, because it is a single field away at any moment.
* **Enrolment's vector never reaches S3 at all**, now that the second hop is a direct invoke:
  the embedder passes it in the invoke payload to the in-VPC writer, which stores it in the
  column that already requires consent. No artifact, no deletion, no lifecycle rule, nothing
  for Phase 6 to sweep.
* The consent section names these prefixes as **transient biometric storage** rather than
  claiming they do not exist.

## Consent

The audio is already lawfully held and processed. What is new is attaching a **name** to a
voice pattern — biometric information under the NZ Privacy Act — and consent must come from
**the person whose voice it is**, not the wearer and not the employer (v2 §10).

The product decision on record (2026-08-13) is to ship the mechanism and formalise consent as
a real-world process. The engineering seam exists now because retrofitting it means finding
every profile created before it:

* the **within-session overlay** stores no vector — structurally, since neither artifact
  carries one. That is a claim about *storage* only: clustering every speaker in the room is
  still biometric **processing** of people who have not consented, and the product decision
  above is what covers it. Revision 3's "structurally true" was scoped too widely;
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
