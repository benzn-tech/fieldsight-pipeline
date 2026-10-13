# Sub-project 4 — who owns a lake object (design, 2026-10-12)

Status: **owner-approved 2026-10-12: option B, export only (no `delete_company`).**
Roadmap decision **D3**. Master: `2026-10-05-tenancy-roadmap-design.md`. This changes D3's
mechanism (no company segment in the key path) and keeps its three goals.

## D3's goals

1. **Structural isolation.** A scoping bug cannot reach another tenant's objects.
2. **Per-tenant export and delete.**
3. **Bounded listing.** No code path enumerates every tenant to serve one.

## Inventory (2026-10-12, origin/develop; full table in the session scratchpad `lake-inventory.md`)

- **Prefixes:** 40 prefix families.
  - 21 are keyed by folder.
  - 5 are keyed by session, with the folder only in the payload.
  - 7 are already keyed by site or company: `voice/`, `voiceprint_requests/`, `match_requests/`, `weather/`, `programmes/`, `org-assets/`, `notices/`.
  - 7 are global.
- **Writers and readers:** about 45 writers in 25 modules and about 90 readers.
  - About 35 readers parse the key by position (`split('/')`, regexes, SQL `split_part`).
  - About 25 list by prefix.
- **Triggers:** 16 S3 → Lambda notifications (script-managed) and 4 EventBridge-on-S3 rules.
- **Keys stored elsewhere:** in Postgres (`recordings.s3_key`, `topics.source_s3_key`, `topic_photos.s3_key`, `redactions.target_key`, LIKE-prefix tombstones) and in DynamoDB (`batch_ledger.chunk_key`).
- **Upload key:** the device upload key is minted by org-api, `users/{folder}/{kind}/{date}/{file}`. Company and site are known at that moment.

## Why a company segment in the path does not deliver D3

**1. Ownership changes after the key is written.**
- The ingest site ladder can settle the site **after** upload (meeting session, group lead, recording day).
- A project can move to another company (company management, 2026-10-03), which changes `sites.company_id`.
- A group merge can span recorders of two home companies.
- In each case a company baked into the path becomes false. Making it true again means rewriting the object, which re-fires the pipeline: VAD, transcribe, extract-session, item-writer, embed and ingest all trigger on put.

**2. A path segment is not isolation by itself.**
- Every pipeline Lambda holds `prefix/*` grants. A function that can read `reports/A/…` can read `reports/B/…`.
- Isolation would need per-company credentials (session policies scoped to one company's prefix) in every reader. Nothing in the stack has that, and adding it is the expensive part whatever the key layout is.
- Today the isolation that matters is enforced in one place: org-api is the only presigner and the only reader serving browsers, and it checks site and company against Aurora (sub-project 3, R1–R8).

**3. Cost and risk are the largest of the four sub-projects.**
- About 35 positional parsers, about 25 listings, keys held in two databases, 16 triggers, about 34k objects.
- Dual-write would double Transcribe and LLM spend while it lasts.
- It mixes flat and scoped trees in every whole-lake scan.

## Options

| | what | goal 1 | goal 2 | goal 3 | cost / risk |
|---|---|---|---|---|---|
| **A** | Spec #979 mechanics: `{prefix}/{company}/…`, dual-write, backfill | partial (path only, no per-company credentials) | yes, until ownership changes | yes | weeks; every parser; re-fires the pipeline; rewrite on every site move |
| **B (recommended)** | **Ownership as data**: an object index plus object tags, written at put time; readers stay as they are; listings move to the index | same as today, plus a tag condition that can be added to IAM later | yes, by query; follows site moves without touching objects | yes | about 1 week; additive; reversible |
| **C** | A bucket per tenant | full (bucket policy plus KMS key per tenant) | yes (delete the bucket) | yes | only when a customer contractually needs it; B makes it a copy job |

## Recommended design (B)

**L1. Ownership index.**
- New table `lake_objects`: `(s3_key PK, company_id, site_id NULL, user_id NULL, session_base NULL, kind, created_at, deleted_at NULL)`.
- Ownership is resolved by the sub-project 3 rule: the site's company, else the recorder's home company.
- When `sites.company_id` changes, one `UPDATE … WHERE site_id = $1` moves every object of that site. No S3 rewrite.

**L2. Who writes the index.**
- org-api, the only VPC writer, records uploads when it mints the key (`recordings` already holds most of this).
- Non-VPC writers can't reach Aurora. They write their puts to an append-only manifest, `lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl`, which org-api drains on its daily task and on demand, then resolves the owner from `recordings`, `meeting_session` and the directory.
- One helper, `lake_put(bucket, key, body, *, owner_hint)`, wraps `put_object` in the about 25 writer modules. It also sets the S3 object tags `company` and `site` when known.

**L3. Backfill (one-off, read-only on the objects).**
- Walk the bucket once: about 34k objects.
- Resolve each owner from `recordings.s3_key`, `topics.source_s3_key`, session markers and the folder → home company.
- Objects that can't be resolved stay `company_id = NULL` and are listed for the owner. Nothing guesses.
- No S3 writes beyond optional tags. Tagging copies no data and triggers no notifications, because the bucket notifications fire on `ObjectCreated` only.

**L4. Bounded listing.** Every whole-lake listing in a request path is replaced by an index query scoped to the caller's sites or company:
- `count_transcripts_for_date` (lists all of `transcripts/` for one date),
- the report-history folder listing,
- the `admin_disambiguation` folder union,
- meeting-minutes' scans.

Nightly batch jobs keep their listings for now, because they are not request paths.

**L5. Per-tenant export.** A platform-admin task `{"task": "export_company", "company_id": …}`
writes a manifest and a zip to `exports/{company_id}/`. **No delete** (owner, 2026-10-12):
a per-company delete is irreversible and is left for a later design.

**L6. Retire the lake-wide summary.** `reports/{date}/summary_report.json` aggregates every tenant. Today only platform_admin is served it.
- Replace it with one summary per company: `reports/{date}/companies/{company_id}/summary_report.json`. This is a new key, so there is no parser conflict.
- Stop writing the lake-wide one after one release.

**L7. Later, optional.** Once the tags are complete, an IAM condition (`s3:ExistingObjectTag/company`) can scope a per-company role. That is the step that would actually deliver goal 1, and it is only needed if non-org-api code ever serves tenants directly.

## Out of scope

- Changing any existing key.
- Moving objects.
- Per-tenant KMS.
- Option C.
- Deleting a company's data (owner, 2026-10-12).

## Tests that must go red without the change

- An upload through org-api writes a `lake_objects` row with the site's company.
- A manifest line from a non-VPC writer is drained into the index with the resolved owner.
- Moving a site to company B changes the owner of every object of that site, and no S3 call is made.
- The backfill leaves unresolvable objects NULL and reports them. A second run changes nothing.
- `count_transcripts_for_date` and report history make no whole-prefix listing.
- `export_company(A)` contains no B object, including on a site that moved from B to A.
- The per-company summary for A contains no B folder.
