# A to-do that carries its own history — and what production data says it currently is

The asked-for surface, in the user's own sketch:

```
Hemi — install joinery Thu, double crew, done Fri        [CURRENT]
From Tue site meeting · Tue 2:45 pm · open the meeting
  v3 · Thursday install, double crew          Tue 2:45 pm
  v2 · Rain delay, materials on hold          Tue 2:22 pm
  v1 · Supplier confirmed Wednesday           Sat  9:41 am
```

Requirements as stated: the system links related actions **by itself**; it would rather miss a
link than make a wrong one; and **nothing that gets folded in is deleted** — it becomes an
earlier version, with the current status and description carried on the top line.

This spec is written after measuring, and the measurement moves the feature.

## 1. What the production corpus actually holds

Every extraction artifact in the prod bucket, read read-only on 2026-08-30: **117 artifacts,
223 action items.**

| | |
|---|---|
| artifacts with **zero** action items | 62 / 117 |
| sessions holding >= 1 action | 55 (median **3** per session, max 14) |
| `responsible` present | 126 / 223 (57%) |
| `deadline` present | 102 / 223 |
| `declared_site` present | **0 / 223** |
| tier | 167 final, 4 live, 52 unlabelled |

Near-duplicate candidates, restricted to pairs from **different sessions of the same recorder**
(token Jaccard >= 0.4 or difflib ratio >= 0.6): **29 pairs**.

Two things about those 29 decide the design.

**a. Only ONE of them shares a non-empty `responsible`.** The conservative rule proposed
before any of this was measured — *same thread AND near-duplicate text AND same responsible* —
would therefore fire approximately once across the entire production history. As a conjunct,
`responsible` is not a safety rail; it is an off switch.

**b. They split 17 same-day / 12 cross-day, and the two halves are different phenomena.**
The same-day 17 collapse to **6 clusters** and look like this:

```
[2026-08-10] Scaffolding -- inspect before Monday
[2026-08-10] Scaffolding -- inspect before Monday      <- identical, different session
```

That is one event extracted more than once (live tier and final tier, or a split session), not
a commitment restated at a later meeting. It is a de-duplication defect, and rendering it as
`v1`/`v2` would invent a history for something said once.

The cross-day 12 — the only shape a version history is for — collapse to **4 distinct
subjects** in the entire production corpus:

```
timber/screws   2026-08-09 -> 08-10 -> 08-12   "Buy timber and screws" ->
                                               "Timber and screws -- buy at Mitre 10" ->
                                               "Mitre 10 timber and screws purchase"
balustrade      2026-08-07 -> 08-10
scaffolding     2026-08-10 -> 08-12
FieldSight      2026-08-10 -> 08-13 -> 08-27
```

The first three are what the feature is for. **The fourth is the counter-example, and it is a
quarter of the evidence:**

```
[2026-08-10] FieldSight AI -- promote with DeAndre
[2026-08-13] FieldSight roadmap -- sync with Benny
[2026-08-27] FieldSight features -- meet James & Benny
[      ... ] FieldSight mobile -- upgrade and deploy
```

One subject, four different commitments to four different people. A text-similarity rule folds
these into one card and buries three real actions as superseded versions of a fourth. That is
precisely the failure the requirement forbids, and it is not hypothetical: it is 1 of the 4
clusters that exist.

This is the same wall migration 0032 hit from the other side and recorded in its own comment:
*"the similarity lives in the subject while the promises differ every time."* Threading was
therefore built for **subjects**, and a thread id means a human said yes.

The corpus limit, stated plainly: 117 artifacts over 18 dates (2026-02-09 .. 2026-08-28),
against 22 dates carrying content in the database. Artifacts and database coverage are the
same order, so this is not a small sample of a large history — but it is a small history.

## 2. The consequence: split the feature in two

The valuable half does not need any merging at all, and the risky half should not be built on
four clusters, one of which is a trap.

### V1 — the card, with provenance and edit history. Buildable now.

Everything it needs already exists:

* `GET /api/org/content/action_items/{id}/history` is **live** and returns `content_edits`
  rows (migration 0019 — a first-class history table, deliberately without a foreign key so
  the audit outlives the row).
* Provenance is one join away: `action_items.topic_id -> topics.source_s3_key` (the session)
  and `topics.occurred_at` / `topics.time_range` (the time). That is exactly the sketch's
  *"From Tue site meeting · Tue 2:45 pm · open the meeting"* line.

So V1 renders **CURRENT + where it came from + every human edit**, ordered newest first. On
today's data the version list will usually hold one entry, and that is the honest rendering.

**V1 has no merging in it, therefore it cannot merge anything wrongly.**

### V2 — folding a later meeting's restatement in as a new version. Gated.

Not built yet, and not built on this evidence. The gate to open it:

> Re-measure when the corpus holds **>= 20 distinct cross-day clusters** — clusters, not
> pairs. The 12 pairs measured are 4 subjects, and pair count inflates with the length of a
> chain rather than with the number of things to learn from. Below 20, precision cannot be
> estimated: today one known bad cluster out of four is a 25% error rate, and no rule can be
> shown to beat it.
>
> `scripts/measure_action_duplicates.py` prints both counts and every cross-day cluster in
> full, so re-running it is the gate check.

## 3. The identity problem V2 must solve first

`lambda_ingest` **deletes a day's rows and re-inserts them with new uuids** when the nightly
report supersedes the live extraction. `src/lambda_item_writer.py` and `_source_is_deleted`
both carry this warning, and the deletion work has already been bitten by it.

Consequences, in order:

1. `content_edits.row_id` is a soft reference. It **survives** the supersession (no FK, by
   design) but stops joining to any current row. A version list keyed on `action_items.id`
   silently empties overnight, which is the shape this repository keeps producing: a read that
   works, over a link that has gone.
2. A chain therefore cannot be keyed on `action_items.id` or `topics.id`.
3. It also cannot be keyed on `content_hash(text)` the way `compliance_resolutions` (0025) is
   — that table's key works precisely because the text it hashes does not change, and here
   **the text changing is the entire feature**.

The shape that survives both: a chain row with its own id, and membership recorded by the
**occurrence's** re-extraction-stable natural key, following 0025's precedent —
`(company_id, site_id, report_date, user_folder, content_hash)`. An occurrence's text is fixed
once extracted; the chain moves, its members do not.

One residual exposure, inherited from 0025 and worth stating rather than discovering: if the
nightly pass re-words a live-tier action ("Order door handles" -> "Order the door handles"),
its `content_hash` changes and that membership row orphans. 0025 lives with this today.

**`declared_site` is null on every artifact measured**, so `site_id` must come from the
database side (`topics.site_id`), not from the artifact. Any measurement script that groups by
the artifact's site is grouping by null.

## 4. Rules V2 must hold to

* **Read-side collapse, never a write-side merge.** No row is deleted, updated, or rewritten to
  fold two actions together. The chain is a link; the card assembles the view. This is what
  makes "would rather miss than mis-link" cheap: a wrong link is undone by deleting the link,
  and nothing has been lost in the meantime.
* **Every automatic link is reversible from the card**, and an undo must be recorded, not
  silent — an auto-linker whose mistakes are invisible cannot be measured.
* **One site.** Copy 0032's reasoning verbatim: "door hardware" at two schools is two jobs.
* **Same-day pairs are not versions.** They are duplicate extraction of one event and belong to
  a different fix. Folding them into a version list would present `v1`/`v2` of a thing that was
  said once — the most damaging possible reading of this UI, because it invents a history.
* **Do not use `responsible` as a required conjunct.** Measured: it is absent on 43% of actions
  and agrees on 1 of 29 candidate pairs. It may be used as a *tie-breaker* or as a reason to
  raise confidence, never as a gate.
* **A superseded version keeps its own status.** The requirement says the folded-in item is not
  deleted; it must also not silently inherit "done" from the current version. `v2 · Rain delay,
  materials on hold` was true when it was said.

## 5. What the same-day duplicate actually needs

Out of scope here, recorded so it is not lost: the frequent, real defect this measurement
surfaced is **one event extracted twice into two sessions on one day** with identical text.
That is a de-duplication question at ingest, not a version chain, and it is worth its own
measurement — it is currently the majority of every near-duplicate pair in production.

## 6. Recommendation

Build V1. It delivers the sketch's top two lines and its version list, on data that exists,
with an endpoint that is already live and no new table.

Hold V2 behind the re-measurement gate in §2. The design above is what it should be built as
when the gate opens; building it now would be tuning a matcher against four clusters, and
the rule that fits three of them is the rule that folds the fourth.

---

## 7. Corrections, same day — an adversarial review of §2 and §3

Six things above were wrong or unsupported. They are corrected here rather than edited away,
because the wrong version was specific and someone will otherwise re-derive it.

### 7.1 `occurred_at` is NULL on every topic ever written. V1 must not use it.

§2 promised the sketch's *"Tue 2:45 pm"* from `topics.occurred_at`. Neither writer passes it:
`lambda_item_writer` (the extraction path) and `lambda_ingest` (the report path) both call
`topics.upsert_topic` without the kwarg, and it defaults to `None`. Confirmed independently
against production — `GET /api/org/timeline?date=2026-07-17` returns topics that do not carry
the field at all.

`time_range` is not the substitute: `src/session_scope.py` states it is LLM free text that may
LABEL a session and must never DECIDE one.

**The real source already exists and is already served.** `/api/org/timeline` returns
`session_id` and `session_kind` per topic, and `/api/org/sessions?date=&user=` returns
`started_at`, `ended_at` and `title` per session — computed by `build_day_sessions` through
`session_scope.session_start` with the `meeting_session` fallback for `sid…` bases that carry
no time in their name.

### 7.2 So V1 needs no new endpoint at all — and that is a better V1 than §2 described

`/api/org/timeline` already returns each action item with its **`id`**, alongside its topic's
`session_id`, `session_kind` and `time_range`. Composed with `/api/org/sessions` for the start
time and title, and `GET /api/org/content/action_items/{id}/history` for the edits, every line
of the sketch is served by endpoints that are live today.

**V1 is therefore a frontend composition over three existing reads, and the backend work for it
is zero.** §2's claim that "everything it needs already exists" was right for the wrong reason:
there is no `GET /api/org/action-items/{id}` — the only route on that path is PATCH — but the
card does not need one, because it is rendered inside a day view that has already fetched the
data.

If a single-item fetch is ever wanted (a deep link straight to one to-do), it should copy
`get_content_history`'s READ-tier authorization — tenant pin, 404 for cross-company, 403 for an
out-of-reach site — and **not** `patch_action_item`'s write tier, whose assignee conjunct would
hide the card from people entitled to read it.

### 7.3 The gate is in clusters and the harness now counts clusters

§2 states the gate in clusters while `scripts/measure_action_duplicates.py` printed only pairs;
the cluster numbers were derived by hand. The script now does connected components and prints
`pairs -> clusters` for both halves, names which number the gate reads, and lists each cross-day
cluster in full. Re-running it now answers the gate as written.

### 7.4 Same-day duplicates are not the live tier against the final tier

`extraction_key` puts both tiers on one S3 key so the final pass supersedes the live one in
place, and the harness skips pairs sharing a `session_base` regardless. What remains is a split
session and **group merge**: the merged artifact takes its own `grp…` base while the members'
artifacts stay in S3 even after `_delete_member_topics` removes the members' database rows.

### 7.5 …which means the artifact corpus can hold duplicates that never coexist — so it was checked

An S3-only measurement cannot tell a customer-visible duplicate from a superseded copy the
bucket still holds. Checked through the live read model on 2026-08-30:

```
GET /api/org/timeline?date=2026-08-10&user=Ben_UCPK2
  -> 39 topics, 35 action items, 12 sessions
     "Scaffolding -- inspect before Monday"   x3
     "Site meeting -- attend tomorrow 8 AM"   x2
     "Wood and screws -- purchase at Mitre 10" x2
     "Shelf brackets -- install two additional" x2
```

So §5's claim survives, now with a source: the duplication is in what the customer sees, not
only in the bucket. It is still a de-duplication question, not a version chain.

### 7.6 A dead id 404s, it does not render an empty list — and it is not nightly

§3 said a version list keyed on `action_items.id` "silently empties overnight". Two corrections.
`GET /content/action_items/{id}/history` **404s** for a superseded row, because the row lookup
fails before the edits are read — so 0019's "the audit outlives the row" is true of the table
and unreachable through the only endpoint that reads it. And "overnight" overstates it: under
`AUTHORITY_FLIP` with `_should_defer`, a day that has extractions keeps its extraction topics
and no uuids regenerate. The instability is real on non-defer days and on a re-triggered
item-writer event; it is not the nightly norm.

The 18-dates-vs-22-dates comparison in §1 also came from a second measurement, not from the
harness: `GET /api/org/dates` for the database side, and the artifact listing for the other.
