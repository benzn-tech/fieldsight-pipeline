# Voiceprint line — Phase 1 investigation and Phase 2 design

Base: `origin/develop` @ 2aee278 (2026-09-22). Read-only; no prod change, no merge.
UI evidence from `fieldsight-ui` `origin/main` @ ddef080.

## 0. Headline

This line is not a blank page. It is a built, deployed subsystem: migrations 0038-0060,
two dedicated Lambdas (`lambda_speaker_embed`, `lambda_voiceprint_writer`), eight org-api
routes, one UI entry point. TEST runs it fully on; prod runs it in `shadow`.

More than half of the requested design (A-H) already exists, is already wired, or has
already been measured. Requirement A — cross-company automatic renaming — requires
deliberately reversing a prohibition this repository currently enforces with a test, and
its precondition (a usable cross-session cosine threshold) is not met by any measurement
taken so far.

## 1. Current shape of speaker identification

**In-session diarisation** comes from the ASR (`speaker_label`), scoped per transcript call.

**In-session embedding merge exists.** `_rebind` (lambda_speaker_embed.py:899) groups
per-call speaker centroids with average-linkage clustering at a cosine similarity floor of
0.45 (`voiceprint_utils.py:269`, `cluster_centroids` at :272), writes `speaker_label_groups`,
and the read path overlays it at `lambda_org_api.py:8386` (`_apply_speaker_groups`).
Measured 55.4% -> 96.3% (voiceprint_utils.py:284-286). `REBIND_SPEAKERS=true` on both TEST
and prod.

**1:N matching also exists.** `POST /api/org/sessions/{s}/speaker-match`
(lambda_org_api.py:694-696, handler at :2009) -> `_match` (lambda_speaker_embed.py:612) ->
profiles fetched in-VPC by `profiles_for_matching` (repositories/voiceprints.py:471) ->
`decide_name` (voiceprint_utils.py:140).

Three gates, in this order (voiceprint_utils.py:165-198):

1. duration >= `DEFAULT_MIN_TURN_S` = 3.0s (:39)
2. margin over runner-up >= `DEFAULT_MIN_MARGIN` = 0.15 (:45), widened linearly once the
   candidate pool exceeds 10 (`effective_margin`, :121)
3. a per-company calibrated floor, demotion-only (migration 0060)

**Cosine is computed in Python, not by pgvector.** `vector_math.cosine`; pgvector is only
the column type. There is **no ANN index** on `speaker_voiceprint_samples.embedding` — the
only `hnsw` index in the repo is `0004_report_chunks.sql:16`. Every match pulls all of a
company's samples into memory. This is adequate per-company and is **not** adequate for a
global registry (see A.5).

## 2. Scope of a manual rename in the web UI

Single entry point: `transcript-list.js:521` -> `scripts/api/org.js:943 setSpeakerName` ->
`POST /api/org/sessions/{ref}/speaker-corrections`.

**Scope = one session, one voice cluster.** Not one segment, not the day, not cross-session.
`_propagate` (lambda_speaker_embed.py:689) complete-linkage clusters the whole session at
tau=0.85, reads the label of the turn the user clicked, and names every other member of that
cluster — all capped at `tentative` (:801-805).

Persisted to `speaker_turn_names` (company_id, session_base, turn_ref, state, source).
`session_base` = `sid<32hex>` only (`turn_name_overlay.session_base`, :67-83).
`turn_ref` = `{stem(source_filename)}@{start_sec}` (:101-112).

**API-layer whitelist: yes, but not in org.js.** `org.js:943` passes `body` straight through.
The whitelist is `scripts/api/speaker-naming.js:137 correctionBody`, which hard-codes the
field list. Consequences:

- `employer_name` / `employer_source`, which the backend accepts (lambda_org_api.py:2329),
  can never be sent by the web UI.
- `consent_given` is hard-coded `false` (speaker-naming.js:146), documented at :132-135
  ("Phase 1 ships no consent UI").

## 3. Voiceprint storage

Exists. `speaker_voiceprints`, `speaker_voiceprint_samples` (`vector(192)`, ECAPA-TDNN,
one row per enrolment event by design — migration 0038:36-53), `speaker_turn_names`,
`speaker_voiceprint_company_floors` (0060).

**Company-isolated, strictly.** `company_id NOT NULL` on every table;
`_require_company` (repositories/voiceprints.py:93) refuses an absent company_id.
There is **no global tier today**, and its absence is deliberate — see A.1.

## 4. The transcription panel under evidence

The Evidence page's Transcripts tab **is** `TranscriptList` (`scripts/pages/evidence.js:1134-1136`),
fed by `GET /api/org/transcripts`, with names overlaid at read time by `_apply_speaker_names`
(lambda_org_api.py:8444).

**`turn_name_overlay.build`/`resolve` has exactly one call site in the whole repo**
(lambda_org_api.py:8472, 8476). Topics, findings, action items, minutes, email and reports
never receive the overlay. `confirmed_only=True` has zero callers.

So if "not synced" means *other panels on the same page*, that is by design, argued at
lambda_org_api.py:8496-8505: no find-and-replace, because `action_items` has no speaker
column and a substring replace would reassign a task from one Jesse to another and would
corrupt fields naming two people ("Karina and Anton").

If it means the Transcripts tab itself lags, there are three known real causes:

(a) the POST returns 202 and the UI re-fetches on a backoff (transcript-list.js:505-508);
(b) the overlay join is a +/-0.5s one-to-one proximity match, so a final-pass re-assembly
that shifts `start_sec` orphans the row — counted in `unmatchedNames` (turn_name_overlay.py:12-21);
(c) on prod `mode=shadow`, names *do* reach the transcript viewer deliberately
(template.yaml:1252-1256).

**A real reproduction is needed to tell these apart. I will not characterise it without one.**

## 5. Turns under 3 seconds

- Backend: `DEFAULT_MIN_TURN_S = 3.0` (voiceprint_utils.py:39). `decide_name` returns
  `unknown` below it (:165-168). `_propagate` filters short turns out of the candidate list
  entirely (lambda_speaker_embed.py:707-709), so they are **not** renamed by the cluster.
- The turn the user personally clicked has **no** duration check on the write path
  (speaker-naming.js:89-94; written `source='correction'` whatever its length).
- The UI has a **render-layer only** compensation (speaker-naming.js:211-230): within one
  `source_filename` and one diarisation label, if the turns the voiceprint did reach agree
  unanimously on a name, lend it to the ones it could not. Rendered `tentative`, never
  written back. Measured background: `spk_0` had 26 turns, 4 of them >= 3s.

So requirement E holds today only at the render layer, and only within one file.

## 6. Switch chain and live values

Fully wired, all three segments: repo variable -> workflow -> template Parameter -> Lambda env
(deploy.yml:244-245, deploy-prod.yml:294-295, template.yaml:1234/1247/2163-2164,
lambda_org_api.py:226/238).

| | SPEAKER_IDENTITY_MODE | ENROL_ON_CORRECTION | REBIND_SPEAKERS |
|---|---|---|---|
| TEST | `on` | `true` | `true` |
| PROD | **`shadow`** (set 2026-09-06) | *unset* -> `false` | `true` |

Consequences on prod:

- The correction routes are **reachable** (only `off` 404s — lambda_org_api.py:2300), and
  names **do** appear in the transcript viewer.
- But `ENROL_ON_CORRECTION=false` **and** the UI hard-codes `consent_given:false`, so
  **no rename on prod can ever create a voiceprint row.** Two independent doors.
- A third door: `companies.voiceprint_consent_basis` (0049). Memory records all four prod
  companies as NULL. **Not verified in this session** (needs in-VPC DB or owner) — open item.

---

# A. Cross-company identity with automatic renaming

Requirement as revised: a global library participates in matching and may write a name
directly; isolation moves off the naming boundary and onto the *data* boundary.

## A.1 What has to be reversed, deliberately

Today the repository forbids this in writing and in a test:

> migration 0049:22-32 — "a table saying 'these two company profiles are the same person'
> IS the cross-company disclosure, whether or not a vector ever moves. ... The system should
> be unable to answer the question."

`tests/unit/test_no_cross_company_voice_identity.py` exists specifically to stop a future
`people` table, a `person_id`, or a nightly dedup job — it scans every migration for
cross-company linkage.

That test will have to be **rewritten, with its rationale replaced, not deleted quietly.**
The honest form is: the prohibition becomes "no cross-company disclosure of *attribution
data*", and the test moves from "no linkage table may exist" to "no cross-company read may
return a field outside the identity allow-list" (A.3). A guard weakened silently is the
failure mode this repo has already paid for repeatedly.

Two facts to put in front of the owner once, then proceed:

- A voiceprint is biometric data under the NZ Privacy Act / Biometric Processing Privacy
  Code. A global registry means the consent basis can no longer be a company fact, which is
  exactly what 0049 made it. It has to become a **person-level** consent that survives an
  employer change — a different consent object, collected differently.
- The registry answers "this person was previously somewhere else" by construction. That is
  a disclosure about the person, not about either company, and it is the person's consent
  that makes it lawful. A.2 is built so that consent is the only thing holding it up.

## A.2 The two layers

**Layer 1 — `voice_identities` (global, tenant-free).** A person, once, across all companies.

```
voice_identities
  id                uuid PK
  display_name      text NOT NULL        -- the ONLY field that crosses a tenant boundary
  consent_state     text NOT NULL        -- pending | granted | withdrawn
  consent_at        timestamptz
  consent_by        uuid                 -- the SUBJECT, not the labeller
  created_at        timestamptz

voice_identity_samples
  id                uuid PK
  identity_id       uuid NOT NULL REFERENCES voice_identities(id) ON DELETE CASCADE
  embedding         vector(192) NOT NULL
  -- provenance is held but NEVER served across a tenant boundary (A.3)
  origin_company_id uuid NOT NULL
  origin_s3_key     text
  window_start_s    double precision
  window_end_s      double precision
  created_at        timestamptz
```

No company id on `voice_identities` itself: it is not a tenant row. `origin_company_id` on
the *sample* is what makes a withdrawal by one company enumerable, and it is the single most
dangerous field in the design — A.3 governs it.

**Layer 2 — `speaker_voiceprints` (unchanged, company-scoped).** Everything that is
*attribution*: which sessions, which sites, which employer, which recordings, which topics.
This layer keeps `company_id NOT NULL` and keeps every existing guard.

The link: one nullable column, `speaker_voiceprints.identity_id uuid REFERENCES
voice_identities(id)`. A company profile may point at a global identity; a global identity
never enumerates its company profiles through any served route.

## A.3 The cross-tenant field allow-list

This is the isolation boundary, and it has to be a positive list, because the failure mode is
adding a field, not removing one.

When company B matches a turn against the global registry, the response may contain
**exactly**:

| Field | Served across tenants |
|---|---|
| `identity_id` | yes (opaque uuid) |
| `display_name` | **yes — this is the whole point** |
| `score` (cosine) | yes |
| `matched_sample_count` | yes |

And **never**, under any role including `platform_admin`:

`origin_company_id` · any company name · `employer_name` · `origin_s3_key` · any session id,
`turn_ref`, site, membership, topic, finding, action item · any timestamp that reveals when
or where the identity was enrolled · any count or listing of which companies hold a profile.

Enforced in three places, because one is a rule until somebody adds a second caller
(the argument migration 0050:26-28 already makes):

1. a repository function `identity_match_row()` that constructs the dict field by field —
   never `SELECT *`, never a dict passed through;
2. a unit test asserting the served dict's key set equals the allow-list exactly, so a new
   column fails the test rather than leaking;
3. the rewritten `test_no_cross_company_voice_identity.py`, re-aimed at the served shape.

Ordinary `GET /api/org/voiceprints` (the company library, F) stays company-scoped and
unchanged. The global registry is reachable only through matching and through a
consent-holder's own record.

## A.4 Automatic renaming, with provenance

`decide_name` gains a tier-aware result, and every automatic name records how it was reached.
New columns on `speaker_turn_names` (migration 0061):

```sql
ALTER TABLE speaker_turn_names
  ADD COLUMN IF NOT EXISTS match_tier      text,     -- company | global
  ADD COLUMN IF NOT EXISTS match_identity  uuid,     -- voice_identities.id when tier=global
  ADD COLUMN IF NOT EXISTS match_score     double precision,
  ADD COLUMN IF NOT EXISTS match_threshold double precision;  -- the value in force at decision time
```

`match_threshold` is stored per row deliberately: the threshold is configurable and will
move, and a row that does not record the threshold it was judged under cannot be re-judged
or rolled back. `source` gains `'global_match'`, which **must** also be added to
`turn_name_overlay._SOURCE_RANK` (:48) — a source missing from that table silently ranks
below everything, a failure recorded at :40-44 and guarded by
`test_every_source_the_writer_writes_has_a_rank`.

Rollback is then one query: every row with `match_tier='global'` and
`match_score < new_threshold`.

Precedence: `correction` (3) > `correction_propagation` (2) > `voiceprint_match` (1, company
tier) > `global_match` (0.5, new) > `label_inheritance` (0). A human's assertion and a
same-company match both outrank a global one.

## A.5 Order of matching, and the pool-size problem

Company tier first. Only if it yields no confirmed name does the global tier run. Two
reasons, one of them measured:

- `effective_margin` (voiceprint_utils.py:121) widens the required margin as the candidate
  pool grows, because the runner-up gets closer for reasons unrelated to match quality. A
  global pool is unbounded, so running it first would make every company match harder to
  confirm. The current scaling is a declared placeholder (:56-60) and cannot be trusted at
  global pool sizes.
- The per-company floor (0060) is calibrated from that company's own corrections and has no
  meaning against a global pool. The global tier needs its **own** floor, calibrated from
  global confirmed matches, and until there are any it has none.

**And the infrastructure does not currently support a global pool at all:** matching is
brute-force cosine in Python over every row (`profiles_for_matching`,
repositories/voiceprints.py:471; `_agreement`, :354). A global registry needs an ANN index —
`CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`, the same shape as
`0004_report_chunks.sql:16` — and a top-K SQL query rather than a full fetch. That is real
work, not a configuration change, and it belongs in the same batch as the global table.

## A.6 The configurable threshold

```
GLOBAL_MATCH_THRESHOLD   (template Parameter -> env, default unset = feature off)
GLOBAL_MATCH_MODE        off | suggest | auto     (default off)
```

Three segments as always: repo variable, workflow override, template Parameter. `suggest`
writes the candidate to the admin list and renames nothing; `auto` renames. Below the
threshold, `auto` degrades to `suggest` — never to nothing, so a near-miss stays visible.

**The value of `GLOBAL_MATCH_THRESHOLD` is left unset in this design on purpose.** The
Feasibility section explains why no number can honestly be written today.

---

# Feasibility — the precondition for A.4 and C

## The 2026-08-30 "no signal" result is superseded. Do not quote it as current.

`docs/superpowers/specs/2026-08-30-voiceprint-cross-session-measured.md` measured 4 real prod
sessions (119 turns >= 3s, deployed `ecapa_tdnn.onnx`) and found cross-session centroids at
**0.45 at best, most 0.12-0.25** — a stranger's range — against a within-session 0.517-0.539.

That document itself named the one design change its data suggested: **profiles pooled across
several recording sessions** rather than single-session. All three changes that followed from
it **shipped on 2026-09-20**, and this investigation initially missed that:

| Prescribed change | Shipped | Evidence |
|---|---|---|
| multi-occasion enrolment | yes | `repositories/voiceprints.py:398` `add_sample` docstring, "2026-09-20 spec §3 is this function, called more than once, from more than one recording session" |
| mean, not max, aggregation | yes | `voiceprint_utils.py:95-106`; commit 2026-09-20 "Aggregate a person's sample scores by mean, not max" |
| a rejection floor under the margin | yes | migration `0060`; commit 2026-09-20 |

Every number in the 08-30 document was produced under **max** aggregation and
**single-session** profiles. Both are now false of the deployed system.

## What the current configuration measures (2026-09-18)

Ben's profile rebuilt from clean read-aloud + an 08-13 session centroid + a 09-17 simulated
site centroid, then tested **only on days that did not contribute to the profile** — 08-07
(Ben present) and 09-10 (negative control, nobody in the library attended):

- **Recall rises sharply, on all three embedders.** 08-07 margins went from negative/marginal
  to >= +0.15 throughout (ECAPA top score .439 -> .635).
- **Max aggregation manufactures a new false positive**: on 09-10 Ben became top scorer
  (ECAPA margin +.055; CAM++ +.195/+.252, i.e. *confirmed* for a person who was not there).
  **Mean pooling removes it**: ECAPA 08-07 margins +.313/+.272/+.366/+.219 all confirmed,
  09-10 margins -.141/-.111, Ben no longer surfaces. This is exactly the change that shipped.
- **A usable absolute window appears, and does not exist without pooled enrolment**: ECAPA
  under mean pooling gives 08-07 true positives at .574-.586 against a 09-10 maximum of .445.
  A floor near **0.50** separates them — which is what migration 0060 is for.
- **Do not change the embedder.** Three models compared on one 265-turn set: ERes2NetV2 has
  the best CN-Celeb EER and gives a 09-10 false positive of .528 against a .545 true positive
  — no usable absolute threshold exists on it at all. CAM++ suppresses false positives only
  by flattening every score, taking the true positives down with them. ECAPA is the only one
  with a threshold window. Benchmark EER ranking has **zero** correlation with this scenario.

## So the honest verdict

**Cross-session signal exists under the shipped configuration.** The earlier "no signal"
statement described a configuration that no longer runs.

It is still not enough to open `auto`, for three specific reasons:

1. **n = 2 days, one rebuilt profile.** One person's profile was rebuilt; the pre-existing
   false positives for the other two people on 09-10 were untouched, so the floor is doing
   real work and has not been stress-tested.
2. **A known optimistic bias.** The two site-condition samples were *multi-turn centroids*
   (71 and 5 turns); production samples are single windows. Centroids are smoother, which
   flatters the result.
3. **Still no human ground truth.** Nobody has listened to this audio. 09-10 scoring as
   "Mike" may mean the speaker really was not Ben. Without labels there is no ROC, and a
   threshold quoted off unlabelled data is a threshold fitted to its own material — the
   mistake 08-30 explicitly warns about.

And cross-company matching adds a change of device, site and acoustic environment on top of
cross-session, none of which the 09-18 run varied deliberately.

**Consequence for requirement A: build all of it, ship it to TEST behind
`GLOBAL_MATCH_MODE=suggest`, and let M1/M2 decide whether `auto` opens and at what number.**
The starting hypothesis for the threshold is ~0.50 from the 09-18 window — a hypothesis to be
tested, never a constant to be written into source.

## Measurement plan (M0-M4)

- **M0 — remove known confounders first.** The 08-30 author listed three untested items. The
  most likely to overturn the result: **profiles pooled across sessions** rather than
  single-session (that document's own "one design change the measurement actively suggests").
  Separately, `aggregate_scores` changed from max to **mean** on 2026-09-20
  (voiceprint_utils.py:95-106) — 08-30's conclusion was reached under max semantics, so it
  strictly needs a re-run before being quoted again.
- **M1 — real ground truth.** 08-30 states plainly "Nobody listened to this audio"; it argues
  by contradiction, which cannot produce an ROC. Scripted-read labelling
  (`speaker_session_eval.py` + a scripts fixture) is cheap but a performance, not a site.
  So: **human-listened labels over real recordings, >= 6 sessions, >= 3 people, spanning
  >= 3 weeks, and at least one person recorded on two different devices** (the last is what
  makes it a cross-company proxy rather than a cross-session one). This is the one item that
  needs owner time and cannot be worked around.
- **M2 — distributions and ROC.** For every (enrolled profile, later-session turn) pair,
  cosine, split same/different, report histogram + ROC + AUC, **stratified** by turn duration
  (3-5 / 5-10 / 10-30 / >30s), pooled enrolment length (10/30/60/120s), same vs different
  device, and same-day / cross-day / cross-week.
  **Pass line for `GLOBAL_MATCH_MODE=auto`: AUC >= 0.90 and TPR >= 50% at FPR <= 0.5%.**
  Stricter than a company-tier match would need, because the global pool is larger and the
  wrong answer names a stranger in another tenant's report. Fail the line -> ship `suggest`
  only, and say so plainly.
- **M3 — thresholds only from held-out data.** 08-30 warns that +0.262 was fitted on the
  material that produced it. Split train/held-out; quote the threshold from train, report it
  on held-out. If there is not enough to split, **publish no threshold**, only distributions.
- **M4 — ablation, only if M2 fails.** One question: material or model? Same audio, a second
  embedder (e.g. WeSpeaker / 3D-Speaker ONNX), identical M2 metrics. If both fail it is the
  material — device audio is measured at median -36 dBFS — and the precondition for this
  whole line is audio capture, not voiceprints.

**`GLOBAL_MATCH_MODE=auto` will not be enabled, and no threshold constant will be written,
before M2 produces held-out numbers that clear the line.** Everything else in A can be built
and shipped to TEST in the meantime, behind `suggest`.

---

# Phase 2 — the remaining items

- **B (label = enrol)** — **already implemented backend-side.** Prod is missing three things:
  (1) `PROD_ENROL_ON_CORRECTION=true`; (2) a real consent surface in the UI and stopping the
  hard-coded `consent_given:false`; (3) a `voiceprint_consent_basis` for each company (route
  already exists: `PUT /api/org/company/voiceprint-basis`). "Splice enough clean audio" is
  also already implemented — `_admit_harvest` (lambda_speaker_embed.py:816) ranks cluster
  members by agreement and adds them as samples.
  Under A this grows one step: a granted **person-level** consent also creates/links a
  `voice_identities` row. Company-level basis alone must **not** put a vector in the global
  registry — that is the one place where 0049's argument survives intact.
- **C (rescan recent days)** — same mechanism as A.4, same threshold, same provenance
  columns. `GLOBAL_MATCH_MODE=suggest` makes it a candidate list; `auto` makes it the
  automatic rescan the brief asks for. Two new routes, **no new table**: writes still go to
  `speaker_turn_names`.
- **D (rewrite "Speaker 1" in topic/action text)** — **recommend neither DB rewrite nor render
  substitution. Use the existing `POST /api/org/sessions/{sid}/regenerate`**
  (lambda_org_api.py:8491), which re-runs extraction with the confirmed names and lets the
  model re-reason. It already sends only `confirmed` names (:8551-8553). The work is purely
  wiring a button in the UI — there is currently no caller.
- **E (<3s follows the label)** — two options: (i) push the UI's label-lending down into the
  backend as real rows; (ii) keep it at the render layer but widen its key from
  `source_filename` to `speaker_group`. **Recommend (ii)**: cheaper, does not freeze an
  inference into DB rows, and `speaker_group` is already in the transcript response
  (lambda_org_api.py:8425).
- **F (admin voiceprint library UI)** — **backend is complete**: `GET /api/org/voiceprints`
  (:1891, roles `_CORRECTION_ROLES` = admin/gm/pm/site_manager/platform_admin, :1777),
  `DELETE /api/org/voiceprints/{id}` (:2139), `PUT /api/org/company/voiceprint-basis` (:2165),
  `GET /api/org/speakers/known` (:2203). `list_profiles` already returns samples and
  humanSamples. F is **front-end work only**, plus one column in the listing saying whether
  this profile is linked to a global identity (never which other companies hold one).
- **G (success toast)** — the backend **already returns everything needed**: `enrolment`,
  `enrolmentMayBeRefused`, `linkedTo`, `linkReason`, `employer` (:2508-2514). The UI discards
  all of it and shows only "Naming..." (transcript-list.js:537-540). Pure wiring.
  **Copy constraint:** the backend deliberately says `"requested"`, not `"queued"` (:2496-2500)
  because the embedder may still refuse. The UI must not say "successfully enrolled" unless we
  add a status read-back route.
- **H (self-stated names)** — half exists: `users.resolve_display_name` (called at :2434)
  matches the company directory on folder_name / full_name / first_name and records which rule
  matched in `linked_on` (migration 0045). Recommended mechanism is exactly the brief's
  candidate: a self-stated name is a proposal only, must fuzzy-match that company's
  users/site-membership roster to land, otherwise pending. **But nothing extracts self-stated
  names from transcripts today** — that field does not exist in the extraction layer.

## New org-api routes

| Route | Method | Roles | New CFN resource? |
|---|---|---|---|
| `/api/org/voiceprints/{id}/candidates?days=N` | GET | `_CORRECTION_ROLES` | No |
| `/api/org/voiceprints/{id}/confirm` | POST | `_CORRECTION_ROLES` | No |
| `/api/org/identities/{id}/consent` | PUT | `admin`, `platform_admin` | No |
| `/api/org/identities/{id}` | DELETE | `admin`, `platform_admin` | No |

All hang off the existing `lambda_org_api` `/api/org/{proxy+}` proxy. **No new CFN resource,
so no deploy-role IAM change** — that trap only fires on new resources. The one place new IAM
*would* be needed is if the global-registry rescan runs as a new scheduled Lambda rather than
inside the existing embedder; recommendation is to reuse the embedder, which already holds
the bucket grants.

## Rollout batches

0. **Zero-risk, TEST now** — G's UI wiring, F's admin page, D's regenerate button. All pure
   consumption of shipped backend.
1. **TEST** — E option (ii).
2. **TEST** — A built end to end with `GLOBAL_MATCH_MODE=suggest`: migration 0061, the
   `voice_identities` tables, the hnsw index, the field allow-list and its three guards, the
   rewritten cross-company test, provenance columns. Renames nothing; produces the data M2
   needs.
3. **PROD, owner decision** — `PROD_ENROL_ON_CORRECTION=true` + per-company
   `voiceprint_consent_basis` + the consent surface. This is the step that writes biometric
   data to prod; the consent surface must land **before** the flag flips.
4. **Blocked on M1/M2** — `GLOBAL_MATCH_MODE=auto` and a threshold value.

## Decisions needed from the dispatcher / owner

1. A.1: `test_no_cross_company_voice_identity.py` and migration 0049's stated rule are being
   deliberately reversed. Confirm the owner has decided this, so the reversal is recorded as
   a decision rather than a regression.
2. A.2: a global registry makes consent a **person-level** fact that outlives an employer.
   Who collects it, and on what surface? Company-level `voiceprint_consent_basis` cannot
   carry it.
3. M1 needs owner time to pick and human-label >= 6 real sessions including one person on two
   devices. Without it, `auto` never opens. Agreed?
4. Prod is on `shadow` today: names **already** reach a customer-visible transcript viewer
   while enrolment is off. Does the owner know this combination is live?
5. "Transcription doesn't sync" needs one real reproduction (which page, which session, what
   name was changed) before I will characterise it.
