# Safety/Quality "resolved" state migration off the legacy check-off endpoint

**Date:** 2026-07-26
**Status:** Design (Option B — durable resolved-state table). No feature code in this document.
**Decision already made:** build the durable resolved-state table, NOT `findings.status`. This spec designs to that.

Repos / branches read for every claim below:
- backend `fieldsight-pipeline` @ `origin/develop` (the deployed `${P}-api` write lives on `origin/main`, cited as such)
- LIVE UI `fieldsight-ui` @ `origin/dev` (`fieldsight-pipeline/ui` is stale; not read)

All line numbers are `git show origin/<branch>:<path>` reads, not the stale working checkouts.

---

## 0. What the code actually says (verification of the brief)

Every claim in the brief was re-checked against the code. Results:

| Brief claim | Verdict | Evidence |
|---|---|---|
| `POST /api/actions/toggle` (`toggle_action`) has ZERO auth, writes DynamoDB | **TRUE** | `origin/main:src/lambda_fieldsight_api.py:731-771` — `toggle_action(body, caller)` takes `caller` but never checks it; writes `dynamodb.Table(AUDIT_TABLE)` current-state row + append-only audit row. Routed with no guard at `:1215`. |
| Ships via the normal SAM pipeline (`${P}-api`), not the frozen `deploy-prod-code.yml` | **TRUE** | `src/template.yaml:2072` `ApiFunction` `FunctionName: !Sub ["${P}-api", …]`, `Handler: lambda_fieldsight_api.lambda_handler`. Retiring the write = edit this file, normal `develop → main → deploy-prod.yml`. |
| Four remaining legacy-write call sites, all safety/quality | **TRUE** | `safety.js:419` (bulk), `:744` (single); `quality.js:382` (bulk), `:671` (single). Each calls `window.FS.api.actions.toggleAction({…})`. |
| `action_index` = `flag_<n>`/`obs_<n>` (safety), literal `'quality'` (quality) | **TRUE** | safety single derives `actionIndex = (row.source==='topic_flag' ? 'flag_' : 'obs_') + idxMatch[1]` (`safety.js:418`,`:680`); quality sends literal `'quality'` (`quality.js:385`,`:674`). |
| Safety/quality read resolved state ONLY from the DynamoDB overlay | **TRUE** | `compliance-aggregator.js:602` (`lookupAction(checkedMap, folder, t.topic_id, 'flag_'+idx)`), `:643` (`'obs_'+idx` under `topic_id -1`), `:813` (`'quality'`). Each sets `status: resolved ? 'resolved' : 'open'/'observed'`. Aurora `o.status` is ignored on these rows. **Killing the write with no Aurora read path loses the open/resolved split — this is a coupled front+back migration.** |
| Three row sources: findings-derived safety, legacy `safety_observations` fallback, report-prose (`obs_`/`topic_quality`) | **TRUE, with an important nuance** — see §1.2. The findings-first / `safety_observations`-fallback split is server-side in the timeline shim (`lambda_org_api.py:2451-2483`), collapsed into one `safety_flags[]` array by the time the aggregator sees it. The aggregator's *three* row shapes are `topic_flag` (from `t.safety_flags`), `observation` (from report prose `r.safety_observations`, synthetic `topic_id -1`), and `topic_quality` (from `t.category==='quality'` topics). |
| Re-extraction is delete+reinsert; topic_ids and finding UUIDs regenerate | **TRUE** | `topics.py:146-157` `DELETE FROM topics WHERE source_s3_key=%s`; `findings`/`action_items`/`safety_observations` have `ON DELETE CASCADE` (`0010_findings.sql`, `0006`/core). Reinsert mints fresh `gen_random_uuid()` PKs. The durable key must survive this. |
| `manual`-source rows already use `PATCH /api/org/observations/{id}` — leave alone | **TRUE** | `safety.js:715`, `quality.js:715` call `org.updateObservation(obs_id, {status})` → `patch_observation_status` (`lambda_org_api.py:1159`). Out of scope. |
| Precedent: action-item migration added Aurora status + `GET /api/org/action-items/closures` union read | **TRUE** | `get_action_closures` (`lambda_org_api.py:1372`), `content_edits.count_action_closures_by_day` (`content_edits.py:50`), consumed by `today.js:1449`. Follow this shape/ACL/testing style. |
| `~119` historical DynamoDB marks | **PLAUSIBLE, UNVERIFIED** | The `~119` figure appears only as prose in code comments (`today.js` closures comment; `lambda_org_api.py:1345` closures docstring) describing "tasks the retired overlay closed without ever clearing Aurora's status='open'". That is an **action-item** figure, not a safety/quality count. **The safety/quality overlay-mark count is genuinely unknown and must be measured from the `fieldsight-audit` table before backfill is decided (§6).** Treat `~119` as illustrative only. |

**One material contradiction with the brief — the durable key candidate.** The brief floats "the finding's `entity_name+observation`" as a content-identity option. The code makes `entity_name` **unavailable on the read path the UI uses**: the timeline shim projects safety findings to `{observation, risk_level, recommended_action, id, source_table}` (`lambda_org_api.py:2451-2456`) and the legacy `_derive_safety_flags` to `{observation, risk_level, recommended_action}` only (`lambda_extract_session.py:268-286`) — **`entity_name` is dropped in both**. The aggregator therefore never sees it (`compliance-aggregator.js:611-616`). So an `entity_name`-based key is **not reconstructable client-side** and is rejected. See §1.3 for the chosen key.

---

## 1. The durable table

### 1.1 Migration

Next number is **`0025`** (latest on `origin/develop` is `0024_keyframe_tombstones_events.sql`; `src/db/migrate.py` applies unseen `.sql` in `parse_version` order and records them in `schema_migrations`, so an additive new file is safe and idempotent).

**File:** `src/migrations/0025_compliance_resolutions.sql` — additive only, no change to existing tables.

```sql
-- 0025: durable safety/quality "resolved" state — retires the unauthenticated
-- DynamoDB check-off overlay (POST /api/actions/toggle) for compliance rows.
-- Keyed on a RE-EXTRACTION-STABLE natural identity (report_date + recorder
-- folder + domain + normalized-text hash), NOT on any regenerated DB uuid or
-- positional topic index. See spec 2026-07-26 §1.3.
CREATE TABLE compliance_resolutions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id        uuid NOT NULL REFERENCES sites(id)     ON DELETE CASCADE,
  report_date    date NOT NULL,
  user_folder    text NOT NULL,                 -- recorder folder from source_s3_key (stable)
  domain         text NOT NULL CHECK (domain IN ('safety','quality')),
  content_hash   text NOT NULL,                 -- sha256(normalize(row text)); see §2
  content_sample text NOT NULL,                 -- normalized text, stored for debug/audit + parity fallback
  resolved       boolean NOT NULL DEFAULT true, -- true=resolved/closed, false=reopened (row kept as a tombstone)
  resolved_by    uuid REFERENCES users(id),     -- who last set state (nullable: legacy/backfill)
  resolved_at    timestamptz NOT NULL DEFAULT now(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, site_id, report_date, domain, user_folder, content_hash)
);
CREATE INDEX idx_compres_range
  ON compliance_resolutions (company_id, site_id, domain, report_date);
```

Notes:
- **`resolved` is a boolean, not a row-presence flag.** Reopen must be representable (safety/quality both support un-resolve — `quality.js:648` `nextStatus = prevSel.status==='resolved' ? 'observed' : 'resolved'`). A reopen UPDATEs `resolved=false` rather than deleting, so the resolver line and audit survive and the union read (§4) never falls back to a stale overlay `true`.
- **`user_folder` is part of the key, not just `site_id`.** A single site's daily report is per-recorder (`lambda_org_api.py:2507-2508` — `site` is one name, but each report is one folder's day). Two recorders on the same site/day can each raise a safety flag with identical wording; without `user_folder` they collide. `user_folder` is derived from `source_s3_key` and is immune to identity churn (same property `topics.py:146` relies on for the DELETE key).
- Additive: no FK from `findings`/`observations` into this table, and none back. The table is deliberately **decoupled from the regenerated uuids** — that decoupling is the whole point.

### 1.2 The three row sources this table must cover

From `compliance-aggregator.js`, confirmed by reading `getSafetyRange`/`getQualityRange`:

| # | Source | Aggregator origin | `source` tag | Current overlay key |
|---|---|---|---|---|
| 1 | Findings-derived safety (findings-first) OR legacy `safety_observations` fallback | `t.safety_flags[]` (shim already collapsed findings→flags at `lambda_org_api.py:2451-2483`) | `topic_flag` | `lookupAction(map, folder, t.topic_id, 'flag_'+idx)` (`:602`) |
| 2 | Report-prose site observations | `r.safety_observations[]` (S3 doc prose, `lambda_org_api.py:2494`) | `observation` | `lookupAction(map, folder, -1, 'obs_'+idx)` (`:643`) |
| 3 | Quality topics | `t.category==='quality'` topics | `topic_quality` | `lookupAction(map, folder, t.topic_id, 'quality')` (`:813`) |

A findings-only durable store (e.g. `findings.status`, the rejected Option A) would miss **#2** entirely (prose has no findings row) and would re-key **#1/#3** on regenerated finding/topic uuids. The natural-key table covers all three because the key is computed from `report_date + user_folder + domain + text`, which every one of the three has.

### 1.3 The chosen natural key (and why it survives re-extraction)

```
UNIQUE (company_id, site_id, report_date, domain, user_folder, content_hash)
```

where `content_hash = sha256_hex( normalize(text) )` and `text` is:
- **#1 `topic_flag`:** `f.observation` (the flag's observation string)
- **#2 `observation`:** `o.observation` (the prose observation string)
- **#3 `topic_quality`:** `t.topic_title` (the quality topic title — the aggregator's `item` field, `compliance-aggregator.js:829`)

`normalize()` is defined in §2.

**Why each component survives delete+reinsert re-extraction:**
- `company_id`, `site_id` — sites/companies tables are untouched by re-extraction (only `topics` and its CASCADE children are deleted, `topics.py:146`). Stable.
- `report_date` — carried on the report/topic, regenerated identically from the same source. Stable.
- `user_folder` — parsed from `source_s3_key`, the one field re-extraction preserves verbatim (it's the DELETE key). Stable.
- `domain` — fixed per page. Stable.
- `content_hash` — depends only on the **displayed text**, not on any uuid or positional index. A delete+reinsert that re-ingests the *same* extraction JSON reproduces the same observation text → same hash → the mark re-associates. This is the property the positional overlay key (`TOPIC#{positional_topic_id}#ACTION#{flag_idx}`) never had.

**Explicitly NOT in the key** (all regenerate or reorder on re-extraction, confirmed):
- `findings.id` / `topic_row_id` (topics.id) — the shim exposes `f.id`/`topic_row_id` (`lambda_org_api.py:2454`,`:2471`) but both are `gen_random_uuid()` reissued on reinsert.
- positional `topic_id: i` (`lambda_org_api.py:2470`) — a mere index into D3 order; a reorder shifts it. This fragility is exactly why the legacy overlay's marks drift.

**Where the key is weakest — state the failure modes honestly:**

1. **Collision (distinct items → same key).** Two genuinely different rows with identical normalized text, same site/date/domain **AND same `user_folder`**, collide on one `content_hash` and resolve/reopen together. Because `user_folder` is now IN the key, a collision requires the **SAME recorder** to have two identical-text items on one site/day — materially narrower than "any recorder's identical text" (two recorders on one site who each raise the same-worded flag no longer collide; they key distinctly on their folders). Impact is bounded: to the user they are indistinguishable rows (same words, same author), so a shared resolved-state is defensible, not silent data loss. Accept it. (Adding positional index would disambiguate even that residual but reintroduce re-extraction fragility — the trade the brief asked to weigh; stability wins.)

2. **Content-correction edit orphans the mark — mitigated by re-keying (item 1b, adopted).** The Phase D editable-content feature (`patch_content`, `lambda_org_api.py:1413`) lets a user rewrite `finding.observation`; intra-topic propagation (`apply_topic_correction`, `:1625`) can rewrite the same fields across a topic's cells. After such an edit the row's text changes → new hash → a naive lookup no longer matches and the row reappears as open. This is a **real** orphan, distinct from re-extraction. **Chosen mitigation (was "accept for v1"; now adopted): re-key the mark on the edit.** When a content edit changes the text of a row that HAS a `compliance_resolutions` mark, recompute the hash old→new and UPDATE that row's `content_hash`/`content_sample` (an upsert-move on the 6-col key) so the resolved state follows the corrected text. Details — trigger set, hook placement, best-effort posture, and the residual merge case — are specified in §3.1 below.

3. **True LLM re-run (not re-ingest) reworic.** If re-extraction is a *fresh* Claude call that rewords the observation, the hash changes and the mark orphans. The brief frames re-extraction as delete+reinsert of the same content (topic_ids/uuids regenerate but text preserved); under that framing the key holds. If the pipeline ever re-LLMs, this key — and *any* text-based or uuid-based key — orphans; only a human-stable anchor (which does not exist here) would survive. Flag for ops: "re-extraction" must mean re-ingest, not re-LLM, for marks to persist.

4. **Source #2 (`obs_` prose) has NO identifier but its text.** Prose observations come straight from the S3 report doc (`doc.get("safety_observations")`, `lambda_org_api.py:2494`) with no DB row, no id, no `entity_name`. Its content identity is *only* the normalized observation string. It is therefore the most collision-prone and the least re-key-able source. This is inherent — there is nothing else to hash. Documented, accepted.

---

## 2. The content-key function (server + client parity)

The SAME normalization must run in Python (write + read endpoints) and JavaScript (aggregator), or a mark made in one place won't re-associate in the other. Codebase already has the `_name_key` precedent (`lambda_org_api.py` — trim / collapse whitespace / casefold); mirror its spirit.

**`normalize(text)` — the exact, minimal, cross-language-safe spec:**
1. Unicode NFC normalize.
2. Strip leading/trailing whitespace.
3. Collapse every internal run of whitespace to a single space (`" ".join(text.split())` in Python; `text.trim().replace(/\s+/g,' ')` in JS).
4. `casefold()` in Python / `.toLowerCase()` in JS. (Note the casefold-vs-lowercase divergence on exotic scripts — see risk below; for the ASCII/Latin site data in practice they agree.)
5. Do **not** strip punctuation (punctuation is meaningful and stripping widens collisions).

`content_hash = sha256_hex(utf8(normalize(text)))`.

**Derivation per source (must be reproducible from what the aggregator holds):**

| Source | Server input (write/read) | Client input (aggregator) | Reconstructable after re-extraction? |
|---|---|---|---|
| #1 `topic_flag` | `finding.observation` (or `safety_observations.observation` fallback) | `f.observation` (`compliance-aggregator.js:611`) | **Yes** — same text both sides. |
| #2 `observation` | prose `safety_observations[].observation` from the report doc | `o.observation` (`:652`) | **Yes**, provided the doc is unchanged; no other anchor exists. |
| #3 `topic_quality` | `topics.title` (safety/quality shim maps `topic_title` from `t["title"]`, `lambda_org_api.py:2464`) | `t.topic_title` (`:829`) | **Yes** — same title both sides. |

**Parity is the single most dangerous implementation detail.** A one-character drift between the Python and JS `normalize`/`sha256` produces silent, total mismatch (every mark orphaned) with no error. Two mitigations, both required:
- Ship a **golden-fixture parity test**: a JSON list of `[raw_text → expected_hash]` fixtures checked by BOTH a Python unit test and a JS test (§7). CI fails if either drifts.
- **De-risk fallback built into the read contract:** the read endpoint returns `content_sample` (the server's normalized text) alongside `content_hash`. If a hash-parity bug is ever suspected in prod, the aggregator can match on `normalize(row.text) === record.content_sample` without trusting the hash. Cheaper to reason about than a hash under incident.

---

## 3. Write endpoint

**New org-api route:** `PATCH /api/org/compliance/resolution` (single, domain-carried — not per-domain; one handler, `domain` in the body). Registered in `dispatch()` (`lambda_org_api.py:204+`) as a literal route.

**Request body (what the UI already has at the click site):**
```json
{
  "domain":      "safety" | "quality",
  "site":        "<site-slug>",          // from window.FS.siteContext.get() (safety.js:520)
  "report_date": "YYYY-MM-DD",           // row.date
  "user_folder": "<recorder_folder>",    // row.user_folder
  "text":        "<the row's observation/title>",  // row.observation (safety) / row.item (quality)
  "resolved":    true | false            // true = resolve, false = reopen
}
```

**Site resolution & ACL — mirror `_resolve_site_param` + `patch_action_item`:**
1. Resolve `site` (slug **or** uuid) → `site_id` via `_resolve_site_param` (`lambda_org_api.py:1932`), which already ACL-checks the resolved id against `_allowed_site_ids` (graded-aware, cross-company-aware). A resolver who cannot reach the site gets the standard `403 access denied to this site`.
2. **Fallback when the caller has no anchored site** (global/Insights view — `siteContext` null): resolve `site_id` server-side from `(company_id, report_date, user_folder)` by looking up the owning report's site (the report is per folder/day, one site). If that yields nothing, `400 site required` — the write refuses rather than guess. This keeps the ACL gate on a real `site_id`.
3. `company_id` comes from `caller` (never the body).
4. **Tier check identical to `patch_action_item` (`lambda_org_api.py:1267-1274`):** after the reach gate, require `is_admin (ALL scope or cross-company) OR this site's pm/site_manager`. Safety/quality resolution is a site-authority act, not an assignee act (these rows have no assignee), so drop the `_is_assignee` branch — site authority + admin only.

**Upsert:** `INSERT … ON CONFLICT (company_id, site_id, report_date, domain, user_folder, content_hash) DO UPDATE SET resolved=EXCLUDED.resolved, resolved_by=EXCLUDED.resolved_by, resolved_at=now(), updated_at=now()`. `content_hash`/`content_sample` computed server-side from `normalize(text)` (§2) — the client never sends the hash.

**Audit:** append a `content_edits` row (`content_edits.append_content_edit`, `content_edits.py:11`) with `table_name='compliance_resolutions'`, `field='resolved'`, `before_text`/`after_text` = prior/new bool — same audit trail the action-item close writes, so "who closed this compliance item and when" is queryable and the closures-style KPI could later count it. UPDATE + audit in ONE `conn.transaction()` (same posture as `patch_action_item`).

**Response (repoints the optimistic UI):**
```json
{ "resolved": true, "resolved_by": "<display name | null>", "resolved_at": "<ISO ts>" }
```
`resolved_by` MUST be the **null-safe** `NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')` join (`content_edits.py:25`) — the trailing-space trap: `CONCAT_WS` with one empty name yields `" "`, which is truthy in the UI and renders a blank resolver chip. `TRIM`+`NULLIF` collapses it to `null`.

**UI repointing (the field-name change the brief flags):** today the optimistic handlers read `res.checked_by` / `res.checked_at`:
- `safety.js:429` (bulk), `:759-760` (single) — read `res.checked_by`/`res.checked_at`.
- `quality.js:392` (bulk) reads `res.checked_by`; `quality.js:679` (single) currently reads **nothing** (just `setTogglePending(false)`), so quality's single-resolve never shows a resolver line today — a small improvement this migration can add.

Repoint all of these to `res.resolved_by` / `res.resolved_at`. Do not keep the `checked_*` names — they belong to the retired DynamoDB vocabulary.

### 3.1 Content-edit re-key (item 1b — keeps a resolved mark attached across a text correction)

Because the durable key hashes the row's **displayed text**, any content correction that changes that text moves the row to a new hash and would orphan an existing resolved mark (the item silently reopens — §1.3 failure mode 2). The fix is to migrate the mark when the text changes.

**Which edits trigger it — map each hashed source to the field a content edit changes:**

| Source (§1.3) | Hashed text | Domain | Editable via | Re-key trigger |
|---|---|---|---|---|
| #1 `topic_flag` | `finding.observation` | `safety` | `patch_content('findings','observation')` and `apply_topic_correction` rewriting that cell | **Yes** |
| #3 `topic_quality` | `topics.title` | `quality` | `patch_content('topics','title')` and `apply_topic_correction` rewriting that cell | **Yes** |
| #2 `observation` | prose `o.observation` from the S3 doc | `safety` | — no Aurora row, not in `content.EDITABLE` | **No trigger, none possible** (nothing to edit; the mark can only orphan via a doc re-ingest, out of scope) |

So exactly two `(table, field)` pairs re-key: `('findings','observation') → domain 'safety'` and `('topics','title') → domain 'quality'`. Any other editable field (`recommended_action`, `entity_name`, `entity_trade`, `action_items.text`, `topics.summary`, `action_items.responsible`) is **not** part of any compliance key and never re-keys.

**Where the hook sits:** immediately AFTER the existing `content_edits.append_content_edit` call — in `patch_content` (`lambda_org_api.py:1452`) and in `apply_topic_correction` (after the per-cell audit rows, alongside the post-commit `_enqueue_content_reindex`). It computes `old_hash = content_hash(before_text)`, `new_hash = content_hash(after_text)`, resolves the row's `(company_id, site_id, report_date, user_folder)` from its owning topic (the same join `_enqueue_content_reindex` already does), and calls `compliance_resolutions.rekey_resolution(...)` to UPDATE the mark from the old 6-col key to `new_hash`/`new_sample`.

**Posture — three invariants, all required:**
- **Best-effort.** A failed re-key must NOT fail or roll back the content edit — wrap in `try/except`, `logger.exception`, move on. Identical posture to the reindex enqueue (`patch_content:1458-1462`). The edit is the user's intent; the mark migration is a convenience.
- **Only-when-a-mark-exists.** `rekey_resolution` is an UPDATE matching the old key; it affects 0 rows and is a silent no-op when the row was never resolved. No SELECT-then-decide, no error when absent — the vast majority of edits touch unresolved rows and must cost nothing.
- **No-op on no text change.** If `before == after` (or the field isn't one of the two trigger pairs) the hook doesn't run — mirrors the audit trail, which already only appends on a real change.

**Residual (documented, accepted):** if a correction rewrites row A's text to become identical (post-`normalize`) to an already-resolved row B on the same site/day/domain/folder, the re-key's new hash collides with B's and the UPDATE either merges (both marks now share state) or hits the UNIQUE constraint. This is the *same class* as the ordinary collision (§1.3 failure mode 1) — two indistinguishable rows sharing one resolved-state — now reachable by edit as well as by chance. `rekey_resolution` therefore treats a unique-violation on the move as a benign no-op (the target mark already exists), not an error.

---

## 4. Read / union

**New org-api route:** `GET /api/org/compliance/resolutions?from=YYYY-MM-DD&to=YYYY-MM-DD&site=<slug>&domain=safety|quality` — **one aggregate call over the date range**, not per-row and not per-day (mirrors `get_action_closures`'s single-round-trip design, `lambda_org_api.py:1372`). ACL: same `_allowed_site_ids` reach gate; `site` optional (omitted ⇒ all reachable sites, for the global Insights view). Returns:
```json
{ "resolutions": [
    { "site_id": "...", "report_date": "YYYY-MM-DD", "domain": "safety",
      "user_folder": "...", "content_hash": "...", "content_sample": "...",
      "resolved": true, "resolved_by": "<name|null>", "resolved_at": "<ISO>" }
] }
```
`user_folder` is returned because it is part of the lookup key the aggregator rebuilds (`site_id|report_date|domain|user_folder|content_hash`). Backed by a new `compliance_resolutions.list_resolutions(conn, company_id, site_ids, date_from, date_to, domain)` repository fn using `idx_compres_range`. `resolved_by` joined null-safe as in §3.

**Where the aggregator swaps its status source** (`compliance-aggregator.js`):
- Add a `getCompliance*Range` fan-out sibling that fetches the resolutions map once for the range (keyed `site_id|report_date|domain|user_folder|content_hash`, or by `content_sample` under the fallback of §2), same place `actionsByDate` is prepared in `fanoutDates` (`:240`).
- At the three status-derivation sites — `:602-620` (`topic_flag`), `:643-661` (`observation`), `:813-831` (`topic_quality`) — replace the `lookupAction(checkedMap, …)`-derived `resolved`/`resolved_by`/`resolved_at` with a lookup into the Aurora resolutions map, computing `content_hash = sha256(normalize(row text))` per row.
- **Union during transition:** keep the legacy `checkedMap` lookup as a *fallback only*. Precedence: **Aurora record present ⇒ Aurora wins (its `resolved` bool, `resolved_by`, `resolved_at`)**; else fall back to the DynamoDB overlay entry (so historical marks made before cutover still render). New marks only ever land in Aurora, so the overlay is a shrinking read-only tail. This union is removable once §6 is decided.

`status` string mapping unchanged: `resolved ⇒ 'resolved'`; else `'open'` (safety) / `'observed'` (quality). Only the *source* of `resolved` changes.

---

## 5. Retirement sequence (exact order; FE vs BE marked)

1. **(BE) Ship table + write + read.** `0025` migration, `PATCH /compliance/resolution`, `GET /compliance/resolutions`, repository module, parity fixtures. Deploy `develop → main → deploy-prod.yml`. No UI change yet ⇒ zero behavior change (nothing calls the new routes). Verify migration applied (`schema_migrations` has `0025_…`) and both routes answer on prod with a smoke call.
2. **(FE) Repoint `safety.js` ×2.** `:744` single and `:419` bulk: swap `api.actions.toggleAction(...)` → new `api.compliance.setResolution(...)`; read `res.resolved_by/resolved_at`. Aggregator reads Aurora-first union for `topic_flag` + `observation`.
3. **(FE) Repoint `quality.js` ×2.** `:671` single and `:382` bulk: same swap, `action_index 'quality'` retired; aggregator Aurora-first for `topic_quality`.
4. **(verify, prod) Confirm both pages read Aurora resolved-state on prod.** Mark a safety flag, a prose observation, and a quality topic resolved; hard-reload (bypass the `_cache.js` TTL); confirm the resolved split and resolver line persist **from Aurora** (check the network call hits `/compliance/resolutions`, not `/api/actions`). Re-run an extraction for one test report and confirm the mark re-associates (the re-extraction-stability acceptance test, in prod). Confirm no safety/quality path still calls `POST /api/actions/toggle` (network tab + grep the shipped bundle).
5. **(BE) Only after (4) passes: make `toggle_action` return `410 Gone`.** Edit `lambda_fieldsight_api.py:731` to `return error('Gone — compliance resolution moved to org-api', 410)` (and drop the DynamoDB writes). **Keep `get_actions` (the READ, `:773`, routed `:1216`) fully alive** — `tasks-aggregator.js:220` (`getActionsRange`), `user-activity-aggregator.js:162`, and `compliance-aggregator.js:241` still read `/api/actions` for historical **action-item** check-offs and the overlay union tail (§4). Retiring the READ would break the Today/Tasks history. Deploy via the same SAM pipeline.

**Between (4) and (5), on prod, must be verified:** (a) zero live safety/quality writes to `/api/actions` (bundle grep + a period of access-log/network observation); (b) the union read still surfaces pre-cutover overlay marks (so the 410 doesn't visually "unresolve" history); (c) the 410 refuses a hand-crafted `toggle_action` write but `GET /api/actions` still returns the historical map.

---

## 6. Backfill question

**Recommendation: do NOT bulk-backfill; union the overlay read indefinitely for dates before the cutover.**

Reasoning grounded in the code:
- The overlay's own key is `TOPIC#{positional_topic_id}#ACTION#{flag_idx|obs_idx|quality}` (`lambda_fieldsight_api.py:745`). `positional_topic_id` is the *D3-order index* (`lambda_org_api.py:2470`), which any re-extraction since the mark was made has potentially shifted. **The overlay marks are already partially mis-keyed** — backfilling them would import that drift into the clean natural-key table and permanently launder wrong associations into "durable" state.
- To backfill *correctly* you would have to, per overlay entry, re-read that report, map the positional `topic_id` → the row's current text, `normalize`+hash it, and INSERT — a one-shot lambda that is itself only as correct as the reports are un-re-extracted. High effort, silent-wrong risk.
- The union read (§4) already renders old overlay marks with **zero migration**, and it degrades gracefully: as old dates age out of the pages' default ranges, the overlay tail becomes irrelevant on its own.
- The true safety/quality overlay-mark count is **unmeasured** (the `~119` in code is an action-item figure — §0). **Before finalizing:** run a scan of `fieldsight-audit` for `SK LIKE 'TOPIC#%#ACTION#flag_%' OR 'obs_%' OR '%#ACTION#quality'` current-state rows to size the tail. If it is a handful, union-forever is trivially fine. If it is large *and* the union read proves a perf drag, run the best-effort correct-backfill lambda for the recent window only and accept older marks aging out.

**Decision:** union-read the overlay for `report_date < cutover`, Aurora for `>= cutover`. Optional, deferred, best-effort backfill only if a measured tail justifies it.

---

## 7. Test plan

**Backend — `tests/unit/` (pytest, mirroring `test_lambda_org_api.py` / `test_repo_content_edits.py`):**
- `test_compliance_resolution_key.py` — `normalize()` + `content_hash` are stable across a **simulated re-extraction**: build a fixture finding, hash it; delete+reinsert with fresh uuids but same text; assert identical hash. Assert each of the three sources (`topic_flag` obs, prose `obs_`, `topic_quality` title) produces a hash and that distinct texts produce distinct hashes. **Collision requires the same `user_folder`:** identical text under two different folders keys to two distinct rows (the 1a fix). Assert the golden-fixture hashes match `content_hash` (the parity guard).
- `test_compliance_resolution_rekey.py` — the content-edit re-key (1b): editing a resolved row's text MOVES the mark to the new hash (old key gone, new key present); it is a no-op when the row has no mark; and a `rekey_resolution` failure never propagates out of `patch_content`/`apply_topic_correction` (best-effort).
- `test_compliance_resolution_write_acl.py` — write **requires site authority**: a caller with no reach 403s; a worker on the site 403s (site authority/admin only); a pm/site_manager of the site and an admin/gm succeed. Cross-company `platform_admin` reaches per `is_cross_company`. Slug and uuid `site` both resolve (mirror `_resolve_site_param` tests).
- `test_compliance_resolution_write_upsert.py` — resolve then reopen updates the same row (`resolved` flips, `resolved_at` moves, no duplicate); a `content_edits` audit row is appended per real change; no-op re-resolve doesn't duplicate audit.
- `test_compliance_resolution_read_union.py` — range read returns Aurora records keyed correctly; **resolver name is null-safe** (`first_name`/`last_name` both empty ⇒ `resolved_by is None`, not `" "`).
- `test_lambda_fieldsight_api_toggle_410.py` — after retirement, `toggle_action` returns **410 and writes nothing**, while `get_actions` still returns the historical map (the READ survives).
- `test_migrations_compliance_resolutions.py` — `0025` applies, is idempotent, and the `UNIQUE`/CHECK constraints hold (duplicate key rejected; bad `domain` rejected).

**UI — `tests/` (mirroring `today-*.test.js`):**
- `compliance-key-parity.test.js` — the JS `normalize`+`sha256` matches the **golden fixtures** the Python test also consumes (byte-for-byte). This is the load-bearing anti-drift test.
- `compliance-aggregator-union.test.js` — status derivation **unions overlay + Aurora with Aurora winning**: (a) Aurora `resolved:true` overrides an overlay-unresolved row; (b) an overlay-resolved row with no Aurora record still shows resolved (historical tail); (c) `resolved_by`/`resolved_at` come from the Aurora record when present. Covers all three sources (`topic_flag`, `observation`, `topic_quality`).
- `safety-resolve-repoint.test.js` / `quality-resolve-repoint.test.js` — the optimistic handlers call `compliance.setResolution` (not `actions.toggleAction`), send `{domain, site, report_date, user_folder, text, resolved}`, and render the resolver from `res.resolved_by`/`res.resolved_at`; a 403 envelope surfaces the rejection (no fake success), matching the manual-observation posture (`safety.js:715`).

---

## Executive summary

- **Durable key chosen:** `UNIQUE (company_id, site_id, report_date, domain, user_folder, content_hash)` in a new additive table `compliance_resolutions` (migration **`0025`**), where `content_hash = sha256(normalize(text))` and `text` is the row's own observation/title — `f.observation` (topic_flag), `o.observation` (prose obs_), `t.topic_title` (topic_quality). **`user_folder` is IN the key** (part of identity, from `source_s3_key`): a collision now requires the SAME recorder's identical-text items on one site/day, not any recorder's — materially narrower than site-only keying.
- **Why it survives re-extraction:** every key component is derived from stable inputs (companies/sites tables untouched; `report_date`/`user_folder` parsed from the preserved `source_s3_key`; text preserved by a re-ingest). It deliberately excludes `findings.id`, `topics.id`, and the positional `topic_id` — all of which `gen_random_uuid()`-regenerate or reorder on the delete+reinsert (`topics.py:146`, CASCADE), which is exactly why the legacy overlay's positional key drifts.
- **Weakest spot:** the `obs_` prose source (#2) has *no* identifier but its text — nothing else to hash — so it is the most collision-prone and least re-key-able (it also has no editable Aurora row, so it is the one source the content-edit re-key cannot cover). A **content-correction edit** to a `findings.observation` or `topics.title` row would orphan its mark, so the mark is **re-keyed on the edit** (item 1b, §3.1): best-effort, only-when-a-mark-exists, never fails the edit; hooked into `patch_content` and `apply_topic_correction`. Two distinct rows with identical normalized text (same site/day/domain/**folder**) still collide by design (acceptable: indistinguishable to the user; an edit that forces such a collision merges the marks, same class).
- **Endpoints:** `PATCH /api/org/compliance/resolution` (body `{domain, site, report_date, user_folder, text, resolved}`; ACL = `_resolve_site_param` reach gate + `patch_action_item`-style site-authority/admin tier; upsert; returns `{resolved, resolved_by (null-safe NULLIF/TRIM/CONCAT_WS), resolved_at}`) and `GET /api/org/compliance/resolutions?from&to&site&domain` (one aggregate range call; Aurora-first union with the overlay).
- **Retirement order:** (a) BE ship table+write+read; (b) FE repoint `safety.js` ×2; (c) FE repoint `quality.js` ×2; (d) verify on prod that both pages read Aurora resolved-state and marks re-associate after re-extraction, and nothing still POSTs `/api/actions`; (e) BE make `toggle_action` return **410** — **keeping `get_actions` READ alive** for historical action-item check-offs (`tasks-aggregator.js:220`).
- **Biggest single risk:** **Python↔JS parity of `normalize`+`sha256`.** A one-character drift silently orphans every mark with no error. Mitigated by a shared golden-fixture parity test (CI-enforced both languages) and a `content_sample` fallback in the read contract so matching can fall back to normalized-text equality under incident.
- **Contradictions with the brief:** (1) `entity_name` is **not** available on the UI read path (dropped by the shim, `lambda_org_api.py:2451-2456`, and by `_derive_safety_flags`, `lambda_extract_session.py:268`), so the brief's `entity_name+observation` key candidate is rejected — hash the observation/title text instead. (2) The `~119` figure is an **action-item** count in the closures comments, **not** a safety/quality overlay count; the safety/quality tail is unmeasured and must be scanned from `fieldsight-audit` before any backfill decision. Recommendation is to **not** backfill (the overlay's positional key is already drift-prone) and union-read the overlay tail indefinitely.
