# Topics / Actions line — taxonomy, photo attribution, Morning Brief

Investigation + design, 2026-09-23. Worktree `C:/Users/camil/fswork/topics-actions`
off `origin/develop` @ 2aee278. No prod writes; every DB statement below was a SELECT
through the RDS Data API against database `fieldsight`.

## Phase 1 — findings (evidence inline)

### 1. No tag/taxonomy field exists anywhere
- `topics.category text` — `src/migrations/0003_dashboard_readmodel.sql:8`. Free text,
  constrained only by the prompt to `safety | progress | quality`
  (`src/lambda_extract_session.py:923`, rule at `:1120`).
- `action_items` has NO category/tag column. Base table
  `src/migrations/0003_dashboard_readmodel.sql:14-24`; the only later ALTERs are
  `deadline_text` (0011:14) and `updated_at/updated_by` (0017:6-7).
- `grep 'CREATE TABLE.*tag' src/migrations/*.sql` → nothing.
- Other classification-ish fields that already exist: `findings.domain`
  (safety|quality|progress), `topics.work_class`, `topic.origin`
  (inspection|meeting|mixed) — all flat single-valued enums in the extraction schema
  `src/lambda_extract_session.py:911-963`.
- The extraction prompt is IN CODE, not in S3 (no template fetch in
  `lambda_extract_session`), so a taxonomy change is a code deploy, not a bucket edit.
- Measured distribution, prod, all time: `progress` 384, `quality` 85, `safety` 47
  (n=516). 74% of topics carry the same value — the existing category carries almost
  no information.

### 2. Photo → topic binding, and the duplicate-attribution bug

Binding rule lives in `src/photo_binding.py`. Within ONE call, `photos_for_topics`
(`:146-228`) is strictly one-photo-one-topic: `result[target].append(p)` at `:204`,
single `target` chosen at `:200`. So the bug is NOT inside the matcher.

**The bug is that the matcher is called once per EXTRACTION, over the WHOLE DAY's photos.**

- `src/lambda_item_writer.py:925` — `pictures_prefix = f"users/{user_folder}/pictures/{date}/"`
  → the photo list is DAY-scoped.
- `src/lambda_item_writer.py:928` — `_photos_for_topics(photo_objects, extraction_topics)`
  → the topic list is SESSION-scoped (one extraction artifact).
- `src/lambda_item_writer.py:907` — `topics.delete_topics_for_source(conn, extraction_key)`
  → idempotency is keyed on the SOURCE KEY, so session B's run never clears session A's
  bindings.
- `src/repositories/topics.py:129-133` — plain `INSERT INTO topic_photos`, and the table
  has no unique constraint on `s3_key` (only `idx_topic_photos_topic` on `topic_id`,
  `src/migrations/0003_dashboard_readmodel.sql:48`).
- Aggravated by `_carry_forward` (`src/photo_binding.py:76-117`), whose window is
  `PHOTO_CARRY_FORWARD_MIN = 30` (`:63`): an earlier session's last topic can claim a
  photo up to 30 minutes later, while a later session's topic claims the same photo
  through the ±`PHOTO_TOLERANCE_MIN = 2` tolerance (`:64`).
- A second, independent caller exists on the report path:
  `src/lambda_ingest.py:685-687`, same function, same day-wide prefix.

**Reproduced, then confirmed in prod.** 2026-09-22 afternoon, `Ben_UCPK2` produced SEVEN
separate extraction artifacts (`extractions/Ben_UCPK2/2026-09-22/sid*.json`). Replaying
`photos_for_topics` locally over each artifact predicted duplicates; the prod DB agrees:

```
ben_ucpk2_2026-09-22_15-22-34.jpg
  -> "Recording Device Operation Demo"    15:21-15:22  src sid095b...727e.json
  -> "Video Recording Preview Function"   15:24-15:24  src sid1b89...e9ec.json
```

Worst case in prod, one photo under SIX topics spanning 7 minutes, six different
source keys:

```
ben_lin_2026-09-11_14-37-31.jpg
  14:31-14:32 Recording Indicator Light Design      src ...63dbe5e2b.json
  14:33-14:34 App Lockdown and Connectivity         src ...9bc0d431b.json
  14:37-14:37 Recording Setup -- NZ Accent          src ...24f71a8f0.json
  14:37-14:37 Voice Prompt Disable Practice         src ...a48ed13a5.json
  14:37-14:38 Indicator Lights - Red Recording S    src ...da29ffb4b.json
  14:38-14:39 Join Meeting Multi-Device Sync        src ...db2845fe8.json
```

Scale: `topic_photos` = 193 rows / 161 distinct photos; **22 photos (13.7%) are bound to
more than one topic.**

Is it a side effect of the 19→90 fix? Partly. Raising `PHOTOS_PER_TOPIC_CAP` 10→60 and
adding `_carry_forward` both widened the per-run claim, so more runs now claim the same
photo. But the duplicate is structural and predates them: it follows from day-scoped
photos × session-scoped topics × source-keyed idempotency. The three-layer fix made it
visible and more frequent; it did not create it.

### 3. Photos taken outside a recording

- The Aurora row is written at PRESIGN, synchronously — `src/lambda_org_api.py:790`
  (`recordings.insert_pending`), `kind='photo'`. `/complete` only stamps `uploaded_at`
  (`:995`), and `photo_list_for_day` deliberately does not filter on it
  (`src/repositories/recordings.py:439-462`).
- The day view reads that table live on every request:
  `_day_photo_block` (`src/lambda_org_api.py:7045-7078`) → `photo_filenames` (`:6982`)
  and `photo_groups` (`:6987`); the "no report" 404 carries the same block (`:7172`).
- **There is NO sweep and NO queue.** There is also no S3 event on photo uploads: the
  only notification under `users/*/pictures/*` is the keyframe marker
  `*_kf_s*.jpg` (`src/template.yaml:3629`).
- So end-to-end latency for the DAY LIST = device upload latency only. Measured over
  384 real prod photos (filename clock vs S3 LastModified, both NZ local):
  - online cohort (Neil_Blunden, Ben_UCPK, Ben_UCPK2, Petros_Pan, James_Alcock; n=305):
    p50 **6 s**, p75 15 s, p90 84 s, p95 245 s
  - backlog cohort (Sam_Yu, David_Barillaro, Jarley_Trainor, James_Lamb; n=79):
    p50 **3.0 h**, p75 6.6 h, p90 19.9 h

  (Ben_Lin / Ben_Test / MPI1 excluded: bulk-imported, deltas of weeks.)
- What is NOT real-time is TOPIC BINDING. For a photo taken while nothing is recording,
  binding never happens at all — the only trigger is an extraction landing in
  `lambda_item_writer`. It does still reach `photo_groups` (location markers, migration
  0054) provided at least one recording that day produced a marker; with zero recordings
  that day there are no markers and only the flat list exists.

### 4. Morning Brief is empty by construction

- Source: `morningBrief.bullets ← report.executive_summary`
  (`ui/scripts/api/today-adapter.js:10`, mapped at `:109-124`).
- `executive_summary` is merged ONLY from the nightly S3 document
  `reports/{date}/{user}/daily_report.json` — `src/lambda_org_api.py:6750`.
- That document is written by `DailyReportSchedule: cron(0 16 * * ? *)`
  (`src/template.yaml:1687-1690`) = 04:00 NZ the NEXT day, keyed to the PREVIOUS date.
  Verified in prod: `reports/2026-09-21/Ben_UCPK2/daily_report.json` written
  2026-09-22 04:00:35.
- The Today page asks for `date = today` (`ui/scripts/pages/today.js:141,182`). Today's
  report will not exist until tomorrow morning, and by then the page has moved on.
  **So the field is null every day, for everyone.**
- Generation itself is healthy: `EnableSchedules=true` on the prod stack, and
  `reports/2026-09-21/Ben_UCPK2/daily_report.json` has four substantive
  `executive_summary` bullets. Nothing is failing to generate.
- Two cosmetic lies alongside: `generatedAt` is the hardcoded string `'5:42 AM'`
  (`today-adapter.js:118`) and the card subtitle reads "Generated from overnight
  transcripts" (`ui/scripts/composites/morning-brief-card.js:59-60`). The card renders an
  empty `<ul>` with no empty-state guard (`:69-77`).
- `EnableSessionBrief=false` on prod (checked) — but that is the stop-recording SESSION
  brief, a different feature; it is not the source of this card.

## Phase 2 — design

### A/B. Taxonomy

Two levels, multi-label, additive. `topics.category` STAYS (it is load-bearing for the
Urgent card and the safety KPI, `src/repositories/topics.py:639,673`).

Proposed base set — 12 level-1 × ~5 level-2 (~65 leaves):

| L1 | L2 |
|---|---|
| structure | foundations, slab, framing, steelwork, concrete, precast |
| architecture | walls, ceilings, floorings, roofing, windows-and-doors, joinery, cladding, stairs |
| envelope | waterproofing, insulation, air-barrier, glazing-seals, external-drainage |
| mechanical | hvac, ductwork, plumbing, drainage, fire-services, lifts |
| electrical | power, lighting, data-comms, switchboard, security-and-access |
| finishes | plastering, painting, tiling, floor-finish, fixtures-and-fittings |
| sitework | earthworks, excavation, services-trenching, paving, landscaping, temporary-works |
| quality | defect, rework, inspection, test-and-commissioning, consent-and-code, as-built |
| safety | hazard, incident-or-near-miss, ppe, permit-and-isolation, traffic-management, induction |
| commercial | variation, cost-and-pricing, claim-and-payment, contract-and-scope, procurement |
| programme | schedule, delay-and-disruption, delivery-and-materials, plant-and-equipment, access-and-sequencing, labour |
| stakeholders | client, consultant-and-designer, subcontractor, supplier, council-and-inspector, neighbour-and-public |

Data model (new migration):

```sql
CREATE TABLE tag (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = global base set
  site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,      -- NULL = company-wide
  parent_id   uuid REFERENCES tag(id) ON DELETE RESTRICT,       -- NULL = level 1
  slug        text NOT NULL,          -- 'architecture.walls', stable machine key
  label       text NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  sort_order  int NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  uuid REFERENCES users(id)
);
CREATE UNIQUE INDEX ux_tag_scope_slug
  ON tag (COALESCE(company_id,'00000000-0000-0000-0000-000000000000'::uuid),
          COALESCE(site_id,   '00000000-0000-0000-0000-000000000000'::uuid),
          slug);

CREATE TABLE tag_run (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id        uuid NOT NULL,
  taxonomy_version  int  NOT NULL,
  method            text NOT NULL,      -- 'classifier' | 'embedding' | 'extraction'
  status            text NOT NULL,      -- 'running' | 'done' | 'rolled_back'
  stats             jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,
  created_by        uuid
);

CREATE TABLE topic_tags (
  topic_id   uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  tag_id     uuid NOT NULL REFERENCES tag(id)    ON DELETE CASCADE,
  source     text NOT NULL,             -- 'extraction'|'classifier'|'embedding'|'human'
  confidence real,
  run_id     uuid REFERENCES tag_run(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (topic_id, tag_id)
);
-- action_item_tags: identical, keyed on action_item_id.
```

Scope resolution: a caller sees global tags (company_id IS NULL) UNION its company's tags
UNION tags on sites it can reach. A company "overrides" a global label by creating a row
with the same slug — shadowing, never deletion, so historical assignments never dangle.
Deactivating is `is_active=false`, never DELETE.

### C. Re-tagging cost — and the headline: this is not a pricing lever

Constraint honoured: re-tagging writes ONLY tag rows. Enforced structurally —
a new `src/repositories/tags.py` exposes exactly two write functions:

```python
def apply_tags(conn, kind, entity_id, tag_ids, *, source, run_id, confidence=None) -> int
def rollback_run(conn, run_id) -> int   # DELETE ... WHERE run_id=%s AND source<>'human'
```

Neither takes any content argument, so there is no parameter through which body text
could travel. Backed by a unit test (FakeConn, the repo's existing double) that captures
every statement a full re-tag run issues and asserts the set is exactly
`{INSERT INTO topic_tags, INSERT INTO action_item_tags, INSERT INTO tag_run, UPDATE tag_run SET status/finished_at}`
— any `UPDATE topics` / `UPDATE action_items` fails the test. Re-entrant via
`ON CONFLICT (topic_id, tag_id) DO NOTHING`; reversible via `rollback_run`; `source='human'`
rows are never written over and never rolled back.

Measured inputs (prod, 2026-09-23):

- corpus all time: 516 topics, 509 action_items, 290 findings
- last 30 days, all pilot folders: 50 extraction artifacts → 164 topics, 107 actions
  (3.28 topics per session)
- topic title+summary: mean 284 chars (~80 tok), p90 399 chars (~110 tok)
- action text: mean 51 chars (~15 tok)
- prod model `meta/muse-spark-1.3-contributor`, price read from OpenRouter today:
  **$0.10 / M input, $0.20 / M output** (the non-contributor `meta/muse-spark-1.3` is
  $1.25 / $4.25 — both shown below, because the contributor tier is not a thing to build
  a price list on)

| | Option 1: light classification call | Option 2: embedding nearest-neighbour |
|---|---|---|
| model calls | 1 per 20 items (batched) | 0 for tagging |
| what is sent | taxonomy leaf list + item text, returns slugs only | nothing; cosine against existing vectors |
| tokens, whole prod corpus (1025 items) | ~110K in / 26K out | ~51K embed tokens (backfill, see blocker) |
| $ whole corpus, contributor tier | **$0.016** | **~$0.004** |
| $ whole corpus, standard tier | **$0.25** | same (~$0.004, embeddings priced separately) |
| $ / month, 10-seat customer (~3,300 items) | $0.052 contributor / $0.79 standard | ~$0.02 |
| $ full re-tag of 12 months for that customer (~40K items) | $0.63 contributor / $9.50 standard | ~$0.21 |
| wall clock, whole prod corpus, 8-way concurrency | ~52 calls, ~26 s | ~103 embed requests, ~7 s |
| wall clock, 40K items | ~2,000 calls, ~17 min | ~4,000 requests, ~4 min |
| quality | unmeasured, but the model sees the actual sentence | unmeasured, and I expect it weaker — see blocker |

Embedding price: DashScope `text-embedding-v4` list price ~$0.07 / M tokens.
**I did not verify that figure this session** — treat it as the one soft number in the table.
Everything else above is measured.

**Blocker on option 2, measured:** topic embeddings do exist (`report_chunks`,
`chunk_type='topic'`, `vector(1024)`, `topic_id` FK) — but only
**171 of 516 topics (33%) have a chunk with a non-NULL `topic_id`**
(`chunks=1339, topic_chunks=276, topic_chunks_linked=171`). This is the known
authority-flip damage. Action items have no embeddings at all — they are folded into the
topic chunk text. So "zero model calls" is only true after an embedding backfill of ~67%
of topics plus every action.

**Second concern on option 2:** this repo's own retrieval eval already showed that
matching short strings by vector underperforms badly (list 20.5% → topic 51% → hourly
grouping 74%). A taxonomy leaf is 1-3 words. I would not bet the feature on cosine
distance between "walls" and a 284-character summary.

**Recommendation:** build both behind one `Tagger` interface and pick with a 100-item
bake-off — at these prices the bake-off costs under $0.10 and settles the question that
the cost table cannot. If forced to choose today: **classifier call, with the embedding
NN used as a candidate generator** (top-8 leaves by cosine go into the prompt instead of
all 65), which cuts prompt tokens ~5× and keeps the model's judgement.

**The answer to the pricing question:** re-tagging is not a cost driver and should not
appear in the price list. The largest realistic bill measured here is **$9.50 to re-tag a
full year for a 10-seat customer at the non-discounted model price**, against an Aurora
standby floor of ~$71/month. Price the taxonomy as a feature tier if you want; do not
price it as consumption.

### D. Management surface and permissions

Mostly as proposed, with one correction.

- **Global base set** — shipped by us, `company_id IS NULL`, read-only to every customer.
  Cannot be deleted, only deactivated per company.
- **Company taxonomy** — `admin` / `gm` only, under Settings → Taxonomy. Matches the
  existing company-level gate `_MANAGER_ROLES = ("admin","gm","pm")`
  (`src/lambda_org_api.py:5475`).
- **Project-level children** — the peer said "项目负责人". In this codebase `pm` is a
  COMPANY-wide role; the per-site role is `site_manager`. So the right holder is
  `site_manager` for sites they are a member of, plus `pm`/`gm`/`admin` company-wide —
  i.e. exactly `_THREAD_MANAGER_ROLES = ("admin","gm","pm","site_manager")`
  (`:6368`), which already exists for the same shape of decision.
- **Everyone else** — may only pick from existing tags on a topic. Enforced SERVER-side,
  not by hiding the button: the frontend's role rendering is already known to be wrong in
  at least one place, and this repo has shipped an endpoint with zero authorization before.
- **Where it is least intrusive:** consumption goes in the filter bar of Timeline /
  Actions as chips — that is where a tag earns its keep. Management lives in Settings.
  Add ONE inline affordance: "+ new under &lt;parent&gt;" inside the topic tag picker, visible
  only to those who may create, because a missing word is noticed on a topic, not in
  Settings.

### E. Photo attribution fix

Rule: **a photo belongs to at most one topic per DAY**, not per extraction. Multiple
attribution only when a human did it.

1. Migration: `ALTER TABLE topic_photos ADD COLUMN source text NOT NULL DEFAULT 'binding'`
   ('binding' | 'keyframe' | 'human'). `lambda_keyframe` writes 'keyframe'; the org-api
   manual bind writes 'human'.
2. Move binding from per-extraction to per-day in `lambda_item_writer`:
   after this extraction's topics are written, load ALL Aurora topics for
   (folder, date) — not just `extraction_topics` — run `photos_for_topics` ONCE over that
   set, and replace every `source='binding'` row for the day. `photos_for_topics` is
   unchanged: it already guarantees uniqueness within a call, and giving it the whole day
   makes that guarantee global. `lambda_ingest:685` follows the same change.
3. Day-level advisory lock `pg_advisory_xact_lock(hashtext(folder||date))` — two sessions
   of the same day can now race, which they could not before.
4. Hard filter before the tolerance rule: **a photo taken inside session A's recording
   span may not bind to a topic from session B.** `recordings.session_span`
   (`src/repositories/recordings.py:478`) and `photo_keys_in_span` (`:509`) already exist.
   This alone removes most of the 22 cases.
5. Tie-break among day-topics that still qualify, in order:
   (a) window contains the photo instant; (b) same location group
   (`day_location_markers` / `photo_groups`) — this is "那个地方" made mechanical;
   (c) smaller distance; (d) earliest start, then lowest id. Deterministic.
6. `source='human'` rows are never touched by the rebind.

Reproduction cases for the regression test (both live in prod today):

- `Ben_Lin / 2026-09-11 / ben_lin_2026-09-11_14-37-31.jpg` — 6 rows now, must become 1.
- `Ben_UCPK2 / 2026-09-22 / ben_ucpk2_2026-09-22_15-22-34.jpg` — 2 rows now, must become
  1 (the 15:21-15:22 topic: the photo is 34 s past its end, vs 86 s before the other's
  start).

Expected blast radius: 22 of 161 distinct bound photos lose rows, `topic_photos`
193 → ~161. No photo disappears from any screen, because the day list is independent of
binding.

### F. Non-recording photos, closer to real time

The day list is ALREADY near real-time (p50 6 s for online devices — there is no backend
queue to shorten). The gap is that a non-recording photo has no evidence surface, not
that it is slow.

- **F1 (recommended, no backend work):** make `photo_groups` a first-class evidence
  surface in the UI rather than an appendix to topics. A photo taken outside any recording
  is already grouped by where the person said they were, the moment its row exists.
  Latency then equals upload latency: p50 6 s. Cost: frontend only.
- **F2 (after E lands):** add an S3 event on `users/*/pictures/*` → a small binder lambda
  that runs the day rebind from E. Latency in seconds, ~500 invocations/month today.
  Cost: one new CFN resource, which in this repo means remembering the deploy-role IAM
  grant (the missing-grant 403 has recurred eight times). Do NOT do this before E, or it
  multiplies the duplicates.
- **F3 (reject):** a periodic sweep. It adds latency by construction, and a per-minute
  sweep is exactly what keeps Aurora from auto-pausing (~$71/month standby floor).

### G. Morning Brief — fix, do not delete

The content exists and is good; the page asks for the wrong date. Deleting the module
would throw away a report that is already being generated every night.

- **G-a (recommended, frontend only): a morning brief is about YESTERDAY.** Point the card
  at the most recent date that has a report. `/api/dates` already returns `hasReport`, and
  `findLatestReportDate` already exists at `ui/scripts/pages/today.js:85` — it is used
  only in the fallback branch today. Label the card "Brief for &lt;date&gt;". Also: delete the
  hardcoded `generatedAt: '5:42 AM'` and use the document's own timestamp, and add an
  empty state ("No recordings yesterday") instead of rendering an empty `<ul>`.
- **G-b (later, not now): generate a live brief for today.** `src/session_brief.py` and
  `lambda_rolling_summary.py` exist, but that is a SESSION brief, `EnableSessionBrief=false`
  on prod, and it costs a model call while duplicating the nightly report.

Deletion would only be right if the nightly report were empty. It is not — verified
content in `reports/2026-09-21/Ben_UCPK2/daily_report.json`.

## Rollout batches

1. **Frontend only** — G-a (brief date + empty state + real timestamp), F1 (photo_groups
   as evidence). No schema, no model. Note: the `ui/` repo has no CI, so this needs manual
   browser verification, desktop only.
2. **Photo attribution** — migration `topic_photos.source`; day-scoped rebind + day
   advisory lock + session-span hard filter; one-off backfill collapsing the 22 existing
   duplicates; regression test on the two cases above.
3. **Taxonomy store, no tagging yet** — migrations for `tag` / `topic_tags` /
   `action_item_tags` / `tag_run`; seed the global base set; org-api read routes + admin
   write routes with the role gates from D; UI filter chips and read-only display.
4. **Auto-tag at extraction (B)** — see the warning below.
5. **Re-tag runs (C)** — `Tagger` interface, both methods, run ledger, rollback, and the
   100-item bake-off that picks the method.

**Warning attached to batch 4:** adding tags to the extraction schema changes the prompt
that also decides WHICH topics and actions get admitted — admission and style have drifted
together in this repo before. Tags must therefore be either a SEPARATE call, or measured
against a fixed fixture set with a before/after count of admitted topics and actions. Do
not fold a new field into `EXTRACTION_SCHEMA` and ship on the assumption that only the new
field changed.
