# Lake Ownership (Sub-project 4, option B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "who owns this lake object" a queryable fact: an ownership index (`lake_objects`) written at put time, kept correct when a site changes company, backfilled for the ~34k existing objects, used to bound the request-path listings, and used to export one company's data. No object is renamed, copied or deleted.

**Architecture:** Ownership is data, not path. org-api (in-VPC, the only presigner) records a row when it mints an upload key. Non-VPC writers cannot reach Aurora, so every put goes through one helper, `lake_put`, which sets S3 tags `company`/`site` when known and appends a line to an append-only manifest object `lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl`. A new longer-running function (`LakeOpsFunction`, same code and VPC as org-api) runs the operator tasks `drain_lake_manifest`, `backfill_lake_objects` and `export_company`. One resolver, `lake_owner.resolve`, is the single place that turns a key into (company, site, user): it applies the sub-project 3 rule (the site's company, else the recorder's home company). `sites.company_id` changes call `lake_objects.reassign_site` in the same transaction, so a moved project's objects follow with no S3 call.

**Tech Stack:** Python 3.11 Lambdas, psycopg 3, Aurora PostgreSQL 16, boto3 (S3 multipart), AWS SAM (`src/template.yaml`), cfn-lint 1.53.3.

**Spec:** `docs/superpowers/specs/2026-10-12-lake-ownership-design.md` (owner-approved 2026-10-12: option B, export only, **no delete**). Master: `docs/superpowers/specs/2026-10-05-tenancy-roadmap-design.md` (D3). Inventory: the session scratchpad `lake-inventory.md` (file:line below are at `origin/develop` 2026-10-12 and drift; re-grep before editing).

## Global Constraints

- Code comments, commits, docs in English. Never `git add -A`, never `git stash`. Add files by name.
- **No S3 object is ever written, copied, renamed or deleted by this plan's code except:** (a) the new `lake_manifest/` and `exports/` prefixes, (b) the manifest objects the drain deletes after commit, (c) optional S3 *tags* behind a flag, default off. Bucket notifications fire on `ObjectCreated` only; tagging and the `lake_manifest/`/`exports/` prefixes match none of the 16 notification filters or 3 EventBridge rules (verify in Task 11).
- **Deploy order:** `sam deploy` runs BEFORE `migrate` in both deploy workflows, so new code runs for about a minute against a database without `lake_objects`. Every repository function catches `psycopg.errors.UndefinedTable` / `UndefinedColumn`, logs a WARNING, and falls back (the `memberships.external` / 0085 pattern in `src/repositories/memberships.py:_exec`, `_is_missing_schema`). Inside a transaction use a savepoint (`with conn.transaction():`) around the statement, or the missing table poisons the caller's transaction. **The drain must not delete a manifest object when the table is missing.**
- **The index is a side record, never on a user's critical path.** A failure to record an upload row logs and continues (BUG-43: the upload route is synchronous and no-retry). The drain/backfill is the safety net.
- **`lake_put` never raises because of the manifest.** The object put itself raises exactly as `put_object` did.
- The index is not trusted for reads until the backfill has run: reads that switch from S3 listing to the index are behind `LAKE_INDEX_READS` (Parameter default `false` -> template env -> code), all three pieces wired and pinned by a test (the unwired-toggle trap).
- Non-VPC components never touch Aurora. In-VPC functions (org-api, item-writer, ingest, keyframe, embed-report, speaker-embed, finalize-sweep, voice-audit) also use `lake_put`/manifest, not direct inserts, to keep one write path.
- Pipeline unit: `python -m pytest tests/unit -q` (report passed/skipped/failed). Integration (real Postgres): `bash /c/Users/camil/fswork/run-tests.sh tests/integration/<file> -q`, one pytest at a time. Run the full unit suite against the CI `pip install` line (a local env missing a CI dependency has caused false reds twice).
- Every new test is red-proofed: revert the change, see the test fail, restore. A test green on old code is labelled a regression pin. Fake connections never execute SQL, so each SQL-bearing behaviour has an integration test.
- Setup: `cd C:\Users\camil\fswork\lake-design && git fetch origin && git checkout -b feat/lake-ownership origin/develop` (or a fresh worktree). Migration number: highest on `origin/develop` is `0086_membership_audit.sql`; this plan uses **0087**. Re-check `ls src/migrations | tail -3` after fetching; renumber if taken.

## Review Focus

1. **A put that succeeds must never be lost from the index silently, and a manifest object must never be deleted before its rows are committed.** -> Tasks 3, 5 (drain deletes after `conn.commit()`; missing table = no delete; failed upsert = keep the object).
2. **No other company's object in an export, including after a site move.** -> Tasks 6, 9.
3. **The resolver never guesses.** Unresolvable -> `company_id` NULL, counted, listed; a second backfill run changes nothing. -> Tasks 5, 7.
4. **Request paths stop enumerating the lake, but not before the index is complete.** `LAKE_INDEX_READS` is off until backfill + a drain have run. -> Tasks 8, 11.
5. **New IAM is the widest grant in the plan** (bucket-wide read for backfill/export on `LakeOpsFunction` only; `s3:PutObjectTagging` on every `lake_put` caller). -> Task 11.
6. **Deploy tolerance:** every new repository/handler path survives the table missing for a minute. -> Tasks 1, 2, 5, 6.

## Task map and boundaries

| # | Task | Why this boundary |
|---|---|---|
| 1 | Migration 0087, `lake_keys.py` (pure key parser), `repositories/lake_objects.py` | Everything else depends on the table and on one key-shape parser. The parser is pure so every later task unit-tests against it. |
| 2 | org-api records rows at key-mint time | Spec L2 first half. Separate from the manifest because it uses `conn`, not S3. |
| 3 | `src/lake_put.py` + manifest format + kill switch | The helper must exist and be proven before 4 adopts it. |
| 4 | Adopt `lake_put` in writers (3 batches: 4a pipeline, 4b reports/sessions, 4c shared modules + legacy) | Mechanical; split by blast radius so one batch can be reverted alone. |
| 5 | `lake_owner.resolve` + `drain_lake_manifest` task | Split from Task 1: the resolver reads recordings/meeting_session/users and is the riskiest logic. |
| 6 | Site-move hook | Tiny; separate so its red-proof is unambiguous. |
| 7 | `backfill_lake_objects` task | Reuses the resolver; adds the bucket walk and the dry-run/idempotency contract. |
| 8 | Bounded listings (8a org-api, 8b non-VPC: day index doc) | The non-VPC half cannot query Aurora, so it needs a published index document (see Ambiguities). |
| 9 | `export_company` engine (9a core, 9b platform_admin route) | Engine first, route second; the engine is testable with a fake S3. |
| 10 | Per-company summary report | Independent of the index; only needs `directory.json`. |
| 11 | `template.yaml`: `LakeOpsFunction`, IAM, schedule, env, `LAKE_INDEX_READS` | Last, so cfn-lint runs once over the final shape. Earlier tasks add only code. |

(Total: 11 numbered tasks; 4, 8 and 9 each split into sub-commits, 15 commits in all.)

## Ambiguities in the spec resolved here (flag to the owner if any is wrong)

- **A1. `count_transcripts_for_date` is not a request path and cannot reach Aurora.** It lives in the nightly, non-VPC `lambda_report_generator.py:1552`. The spec lists it as a whole-prefix listing to replace. Resolution: org-api publishes a small day-index document `lake_index/days/{date}.json` (counts per kind and per company) from the index; the generator reads it and falls back to the old listing, with a WARNING, only when the document is absent. Same for meeting-minutes' folder discovery (`lambda_meeting_minutes.py:405-417`), which reads folders from `config/directory.json` instead (already read by that lambda).
- **A2. Extra columns on `lake_objects`.** The spec lists `(s3_key PK, company_id, site_id, user_id, session_base, kind, created_at, deleted_at)`. The report-history and export paths need `folder`, `day`, `size_bytes`, `source`, `updated_at`; they are additive.
- **A3. Primary key is `s3_key` only, no bucket column.** On prod both buckets are one physical bucket; on TEST they differ but key spaces are disjoint in practice. The drain reads manifests from both configured buckets.
- **A4. Spec signature `lake_put(bucket, key, body, *, owner_hint)`** gains an optional `client=` keyword (writers hold their own boto3 clients and tests inject fakes) and passes through any other `put_object` kwargs. It keeps the spec's positional shape.
- **A5. Per-company summary costs one LLM call per company per day**, in addition to the lake-wide one during the one-release overlap. Guarded by `SUMMARY_LAKE_WIDE` (default on for one release).
- **A6. Summary grouping is by the folder's home company** (the only company a non-VPC job can know from `directory.json`), so an external member's transcript on company A's site lands in B's summary. This matches the per-folder daily reports today; D1-exact grouping needs the index in the generator and is out of scope.
- **A7. Not indexed:** global prefixes (`config/ models/ app-releases/ scripts/ pending_downloads/ download_claims/ other/`) and the lake-wide `reports/{date}/summary_report.*` and `reports/{date}/{type}_report.*` (company NULL, `kind='lake_wide'`, never exported). `meeting_minutes/{date}/{title}.*` is title-keyed and stays unresolved unless a manifest hint carries a company. `traces/` IS indexed (folder known) and manifested.
- **A8. `voice/{company}/{site}/` and `voiceprint_requests/{company}/` carry the CALLER's company in the key** (inventory 1.3 #27/#28), contradicting D1. The resolver uses the site's company for `voice/` (site id is in the key) and the key's company for `voiceprint_requests/`; the mismatch count is reported by the backfill.
- **A9. Long-running operator tasks need a second function.** `OrgApiFunction` has `Timeout: 30` and `ReservedConcurrentExecutions` for the mobile upload path; drain/backfill/export must not share it.

---

## Task 1: Migration 0087, key parser, repository

**Files:**
- Create: `src/migrations/0087_lake_objects.sql`
- Create: `src/lake_keys.py`
- Create: `src/repositories/lake_objects.py`
- Test: `tests/unit/test_lake_keys.py`, `tests/integration/test_lake_objects_repo.py`

**Interfaces:**
- `lake_keys.parse(key) -> dict | None`: `{"scope": "tenant"|"lake_wide"|"global", "kind": str, "folder": str|None, "day": "YYYY-MM-DD"|None, "session_base": str|None, "sid": 32-hex|None, "site_id": uuid|None, "company_id": uuid|None}`; `None` for a key that matches no known family (reported by backfill as `unknown_shape`).
- `lake_objects.upsert(conn, key, *, company_id, site_id=None, user_id=None, kind, folder=None, day=None, session_base=None, size_bytes=None, source) -> bool`
- `lake_objects.upsert_many(conn, rows) -> int`
- `lake_objects.reassign_site(conn, site_id, company_id) -> int`
- `lake_objects.owners_for_company(conn, company_id, *, after_key=None, limit=1000) -> list[dict]` (keyset-paginated, `deleted_at IS NULL`)
- `lake_objects.count(conn, company_id=None) -> int`, `lake_objects.unresolved(conn, limit=100, after_key=None) -> list[dict]`
- `lake_objects.list_reports(conn, *, company_ids=None, folders=None, limit=200) -> list[dict]`, `lake_objects.report_folders_for_day(conn, day, company_ids=None) -> list[str]` (used by Task 8a)
- `lake_objects.MissingTable` sentinel behaviour: every function returns its documented empty value and logs a WARNING when `UndefinedTable`/`UndefinedColumn` is raised.

- [ ] **Step 1: Migration.** Create `src/migrations/0087_lake_objects.sql`:

```sql
-- Sub-project 4 (design 2026-10-12): who owns a lake object, as data. One row per S3 key
-- under a tenant prefix. company_id/site_id/user_id are SOFT references (no FK, no cascade)
-- on purpose: the record must outlive the people and projects it describes, like
-- membership_audit. company_id NULL = unresolved (never guessed); the backfill lists them.
-- A row is moved between companies by one UPDATE ... WHERE site_id (sites.company_id change);
-- the S3 object is never touched.
CREATE TABLE IF NOT EXISTS lake_objects (
  s3_key       text PRIMARY KEY,
  company_id   uuid,
  site_id      uuid,
  user_id      uuid,
  session_base text,
  folder       text,
  day          date,
  kind         text NOT NULL,
  size_bytes   bigint,
  source       text NOT NULL CHECK (source IN ('upload','manifest','backfill')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  deleted_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_lake_objects_company      ON lake_objects (company_id);
CREATE INDEX IF NOT EXISTS idx_lake_objects_site         ON lake_objects (site_id);
CREATE INDEX IF NOT EXISTS idx_lake_objects_company_kind ON lake_objects (company_id, kind);
CREATE INDEX IF NOT EXISTS idx_lake_objects_kind_day     ON lake_objects (kind, day);
CREATE INDEX IF NOT EXISTS idx_lake_objects_unresolved   ON lake_objects (s3_key) WHERE company_id IS NULL;
```

- [ ] **Step 2: Failing unit test for the parser.** `tests/unit/test_lake_keys.py` pins one example per family in the inventory (the ones that matter for ownership) plus the negative cases:

```python
import pytest
import lake_keys

U = "11111111-1111-1111-1111-111111111111"
S = "22222222-2222-2222-2222-222222222222"
SID = "a" * 32

CASES = [
    ("users/Ada_L/audio/2026-10-01/Ada_L_2026-10-01_09-00-00_sid%s_c0001.wav" % SID,
     dict(scope="tenant", kind="media", folder="Ada_L", day="2026-10-01", sid=SID)),
    ("audio_segments/Ada_L/2026-10-01/x_off0.wav", dict(kind="segment", folder="Ada_L", day="2026-10-01")),
    ("web_video/Ada_L/2026-10-01/x.mp4", dict(kind="web_video", folder="Ada_L")),
    ("transcripts/Ada_L/2026-10-01/x.json", dict(kind="transcript", folder="Ada_L", day="2026-10-01")),
    ("extractions/Ada_L/2026-10-01/x_sid%s.json" % SID, dict(kind="extraction", sid=SID)),
    ("reports/2026-10-01/Ada_L/daily_report.json", dict(kind="report", folder="Ada_L", day="2026-10-01")),
    ("reports/2026-10-01/Ada_L/daily_report.docx", dict(kind="report", folder="Ada_L")),
    ("reports/2026-10-01/summary_report.json", dict(scope="lake_wide", kind="lake_wide")),
    ("reports/2026-10-01/daily_report.json", dict(scope="lake_wide", kind="lake_wide")),
    ("embeddings/2026-10-01/Ada_L/vectors.json", dict(kind="embedding", folder="Ada_L")),
    ("session_reports/Ada_L/2026-10-01/day/r1.docx", dict(kind="session_report", folder="Ada_L")),
    ("extraction_pending/Ada_L_2026-10-01_09-00-00_sid%s.json" % SID, dict(kind="session_marker", sid=SID)),
    ("extraction_requests/%s.json" % SID, dict(kind="session_marker", sid=SID)),
    ("voice/%s/%s/a_b.m4a" % (U, S), dict(kind="voice", company_id=U, site_id=S)),
    ("voiceprint_requests/%s/base/r.json" % U, dict(kind="voiceprint", company_id=U)),
    ("weather/%s/2026-10-01/actual.json" % S, dict(kind="weather", site_id=S)),
    ("programmes/%s/programme.json" % S, dict(kind="programme", site_id=S)),
    ("traces/2026-10-01/Ada_L/090000-x-1.jsonl", dict(kind="trace", folder="Ada_L", day="2026-10-01")),
    ("config/directory.json", dict(scope="global")),
    ("models/silero.onnx", dict(scope="global")),
    ("download_claims/x", dict(scope="global")),
]


@pytest.mark.parametrize("key,want", CASES)
def test_parse(key, want):
    got = lake_keys.parse(key)
    assert got is not None, key
    for k, v in want.items():
        assert got[k] == v, (key, k, got)


def test_unknown_shape_is_none_not_a_guess():
    assert lake_keys.parse("weird/thing") is None
    assert lake_keys.parse("users/onlyfolder") is None
```

- [ ] **Step 3: Run, expect red.** `python -m pytest tests/unit/test_lake_keys.py -q` -> ImportError.
- [ ] **Step 4: Implement `src/lake_keys.py`.** A table of `(regex, builder)` pairs, first match wins; every regex anchored; `folder` taken from the named group, never `split('/')[N]` at call sites. Skeleton (complete the remaining families from inventory 1.1-1.4 in the same style):

```python
"""One place that knows the shape of every lake key. Callers get named fields, never an index."""
import re

_DATE = r"(?P<day>\d{4}-\d{2}-\d{2})"
_FOLDER = r"(?P<folder>[^/]+)"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SID_RE = re.compile(r"sid(?P<sid>[0-9a-f]{32})")

_TENANT = [
    ("media",        rf"^users/{_FOLDER}/(?:video|audio|pictures)/{_DATE}/[^/]+$"),
    ("segment",      rf"^audio_segments/{_FOLDER}/{_DATE}/[^/]+$"),
    ("web_video",    rf"^web_video/{_FOLDER}/{_DATE}/[^/]+$"),
    ("transcript",   rf"^transcripts/{_FOLDER}/{_DATE}/[^/]+$"),
    ("extraction",   rf"^extractions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report",       rf"^reports/{_DATE}/{_FOLDER}/[^/]+$"),          # after lake_wide / sites checks
    ("embedding",    rf"^embeddings/{_DATE}/{_FOLDER}/[^/]+$"),
    ("reindex",      rf"^reindex_(?:requests|vectors)/{_DATE}/{_FOLDER}/[^/]+$"),
    ("session_report", rf"^session_report(?:s|_requests|_results)/{_FOLDER}/{_DATE}/.+$"),
    ("session_aux",  rf"^session_(?:rolling|brief)/{_FOLDER}/{_DATE}/.+$"),
    ("redaction",    rf"^redactions/{_FOLDER}/{_DATE}/[^/]+$"),
    ("voice_ask",    rf"^voice_ask/{_FOLDER}/{_DATE}/[^/]+$"),
    ("trace",        rf"^traces/{_DATE}/{_FOLDER}/[^/]+$"),
    ("keyframe_req", rf"^keyframe_requests/{_FOLDER}/{_DATE}/[^/]+$"),
    ("report_request", rf"^report_requests/{_FOLDER}/[^/]+$"),
    ("photo_selection", rf"^report_photo_selection/{_FOLDER}/{_DATE}\.json$"),
]
_SESSION = [  # folder only in the payload; the session id is the only handle
    ("session_marker", r"^(?:extraction_requests|extraction_pending|extraction_empty|session_finalize_requests|session_finalize_results)/(?P<base>[^/]+)\.json$"),
]
_SITE_OR_COMPANY = [
    ("voice",       rf"^voice/(?P<company_id>{_UUID})/(?P<site_id>{_UUID})/[^/]+$"),
    ("voiceprint",  rf"^voiceprint_requests/(?P<company_id>{_UUID})/(?P<base>[^/]+)/[^/]+$"),
    ("match",       rf"^match_requests/(?P<site_id>{_UUID})/{_DATE}/[^/]+$"),
    ("weather",     rf"^weather/(?P<site_id>{_UUID})/{_DATE}/[^/]+$"),
    ("programme",   rf"^programmes/(?P<site_id>{_UUID})/[^/]+$"),
]
_LAKE_WIDE = [rf"^reports/{_DATE}/[^/]+_report\.[a-z]+$", rf"^reports/{_DATE}/summary_report[^/]*$",
              r"^meeting_minutes/.+$"]
_GLOBAL = re.compile(r"^(?:config|models|app-releases|scripts|pending_downloads|download_claims|other|org-assets|notices|lake_manifest|lake_index|exports)/")
```

`parse()` checks `_GLOBAL`, then `_LAKE_WIDE` (so `reports/{d}/summary_report.json` never matches the folder regex), then `reports/{d}/sites/...` (kind `site_report`, `site_slug` field, unresolved by the resolver), then the tables; for session-base keys it also extracts `sid` with `SID_RE` and `session_base` = the base. `meeting_minutes/` returns `scope="lake_wide", kind="meeting_minutes"`. Return `None` on no match.

- [ ] **Step 5: Run parser tests, expect green.** `python -m pytest tests/unit/test_lake_keys.py -q`.
- [ ] **Step 6: Failing integration test for the repository** `tests/integration/test_lake_objects_repo.py` (`pytestmark = pytest.mark.integration`, style of `test_external_members.py`): build two companies, a site, then assert:
  - `upsert` twice with the same key leaves one row; a second upsert with `company_id=None` does not erase an existing company (the COALESCE rule below);
  - `reassign_site(site, B)` updates exactly that site's rows, returns the count, and leaves rows with `site_id` NULL and rows of other sites untouched; a second call returns 0;
  - `owners_for_company(A)` never returns B's rows and paginates by `after_key`;
  - `unresolved()` returns only `company_id IS NULL` rows; `count()` agrees;
  - indexes exist: `SELECT indexname FROM pg_indexes WHERE tablename='lake_objects'` contains `idx_lake_objects_company`, `_site`, `_company_kind`.
- [ ] **Step 7: Run, expect red** (`bash /c/Users/camil/fswork/run-tests.sh tests/integration/test_lake_objects_repo.py -q`).
- [ ] **Step 8: Implement the repository.** Core SQL:

```python
_UPSERT = (
    "INSERT INTO lake_objects (s3_key, company_id, site_id, user_id, session_base, folder, day, "
    "kind, size_bytes, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON CONFLICT (s3_key) DO UPDATE SET "
    "company_id = COALESCE(EXCLUDED.company_id, lake_objects.company_id), "
    "site_id = COALESCE(EXCLUDED.site_id, lake_objects.site_id), "
    "user_id = COALESCE(EXCLUDED.user_id, lake_objects.user_id), "
    "size_bytes = COALESCE(EXCLUDED.size_bytes, lake_objects.size_bytes), "
    "deleted_at = NULL, updated_at = now()")

_REASSIGN = ("UPDATE lake_objects SET company_id=%s, updated_at=now() "
             "WHERE site_id=%s AND company_id IS DISTINCT FROM %s")
```

Wrap each public function in a helper `_run(conn, fn, empty)` that does `with conn.transaction(): return fn()` and catches `psycopg.errors.UndefinedTable/UndefinedColumn` -> `logger.warning("lake_objects missing (migration 0087 not applied yet); ...")` -> `empty` (copy `_is_missing_schema` from `repositories/memberships.py:33`). `upsert_many` uses `cursor.executemany`.
- [ ] **Step 9: Add a deploy-tolerance unit test** (`tests/unit/test_lake_objects_missing_table.py`): a fake conn whose `execute` raises `psycopg.errors.UndefinedTable` -> every public function returns its empty value and logs a WARNING (caplog). Red-proof by removing the catch.
- [ ] **Step 10: Run all three test files green; commit.**
  `git add src/migrations/0087_lake_objects.sql src/lake_keys.py src/repositories/lake_objects.py tests/unit/test_lake_keys.py tests/unit/test_lake_objects_missing_table.py tests/integration/test_lake_objects_repo.py && git commit`

**Tests that must go red without the change (spec list):** the repository half of "moving a site to company B changes the owner of every object of that site" (`reassign_site`), and the table itself for every other item.

---

## Task 2: org-api records the row when it mints an upload key

**Files:**
- Modify: `src/lambda_org_api.py` (`create_recording_upload_url` ~L990-1060, and the minters listed below)
- Create (helper, small): add `record_best_effort(conn, key, **fields)` to `src/repositories/lake_objects.py`
- Test: `tests/unit/test_lake_objects_recorded_on_mint.py`, `tests/integration/test_upload_records_lake_object.py`

**Interfaces:**
- `lake_objects.record_best_effort(conn, key, *, company_id, site_id=None, user_id=None, source="upload", **fields) -> bool`: runs inside `with conn.transaction():` (savepoint), catches **every** exception, logs WARNING, returns False. Parses `kind/folder/day/session_base` from `lake_keys.parse(key)` itself.

**Call sites in org-api (src/lambda_org_api.py at origin/develop):**

| line | function | key | owner known |
|---|---|---|---|
| ~1034 / 1059 | `create_recording_upload_url` | `users/{folder}/{kind}/{date}/{file}` | `_capture_company_id(caller, site)`, `site_id`, `caller["id"]` |
| 1722 | `regenerate_own_report` | `report_requests/{folder}/{rid}.json` | caller company, caller id |
| 1814 | `session_report_generate` | `session_report_requests/...` (and the result key minted next to it) | `_session_site_company_guard` already resolves site/company |
| 2127 | `day_report_generate` | `session_report_requests/.../day/...` | folder's company |
| 2568 | `speaker_match` | `voiceprint_requests/{company_id}/{base}/{rid}.json` | company in key |
| 3565 | `speaker_corrections` | `voiceprint_requests/...` | company in key |
| 4029 | `put_photo_selection` | `report_photo_selection/{folder}/{date}.json` | caller company |
| 4253 / 4262 | `session_expedite` | `extraction_pending/{base}.json` | session's company (meeting_session) |
| 11400 | `regenerate_session` | `extraction_requests/{sid}.json` | session's company |
| 4388 | voice upload-url (`_voice_s3_key` ~4353) | `voice/{company}/{site}/...` | site's company (A8) |
| `deletion_mirror.write_mirror` (via `delete_recordings_endpoint`) | `redactions/{folder}/{date}/deleted_sessions.json` | recorder's company | covered in Task 4c (module is shared with non-VPC readers/writers) |

(`org-assets/*`, `notices/*`, `config/*` are global per A7 and are not recorded.)

- [ ] **Step 1: Failing unit test** (`tests/unit/test_lake_objects_recorded_on_mint.py`). Use the `FakeConn`/`CALLER` style of `tests/unit/test_upload_completion_verified.py` and monkeypatch `org.recordings.insert_pending`/`org.sites.get_site`; monkeypatch `org.lake_objects.record_best_effort` with a recorder. Assert: after `create_recording_upload_url` with a `siteId` of company B and a caller of home company A, the recorder was called once with `key == response s3Key`, `company_id == B`, `site_id == siteId`, `user_id == caller id`. Second test: an idempotent resend (existing row path) records again (harmless upsert) with the same key. Third: when `record_best_effort` raises, the response is still 200 with the upload URL (best-effort contract). Fourth: for each other table row above, one parametrized test that the handler calls the recorder with the key it also passed to `put_object` (use a `FakeS3` capturing `put_object`).
- [ ] **Step 2: Run, expect red** (`python -m pytest tests/unit/test_lake_objects_recorded_on_mint.py -q`; the module has no `lake_objects` import yet).
- [ ] **Step 3: Implement.** In `lambda_org_api.py` add `from repositories import lake_objects` next to the other repository imports. In `create_recording_upload_url`, immediately after the `try/except UniqueViolation` block resolves `rec_id, key` and before `generate_presigned_url`:

```python
    lake_objects.record_best_effort(
        conn, key, company_id=_capture_company_id(caller, site),
        site_id=site_id or None, user_id=caller["id"], source="upload")
```

  For each remaining row of the table add the same one-liner right after the `put_object` (or after the key is minted, for presign-only routes). `record_best_effort` runs in a savepoint, so a failure cannot abort the handler's transaction.
- [ ] **Step 4: Run unit green. Write the integration test** `tests/integration/test_upload_records_lake_object.py`: real Postgres, call `api.create_recording_upload_url(db, caller, body)` for (a) a no-site upload -> row company = caller's home, `site_id` NULL, `source='upload'`; (b) an upload on a site of another company the caller is an external member of -> row company = the SITE's company (the D1 rule; fails if the home company were used). Assert `SELECT ... FROM lake_objects WHERE s3_key=%s` returns exactly one row. This is the spec's first red test.
- [ ] **Step 5: Red-proof** (remove the `record_best_effort` call; both fail), restore; run unit+integration green.
- [ ] **Step 6: Commit** `git add src/lambda_org_api.py src/repositories/lake_objects.py tests/unit/test_lake_objects_recorded_on_mint.py tests/integration/test_upload_records_lake_object.py && git commit`.

**Tests that must go red without the change:** "An upload through org-api writes a `lake_objects` row with the site's company."

---

## Task 3: Shared helper `lake_put` and the manifest

**Files:**
- Create: `src/lake_put.py`
- Test: `tests/unit/test_lake_put.py`

**Interfaces:**

```python
def lake_put(bucket, key, body, *, owner_hint=None, client=None, manifest=True, **put_kwargs):
    """put_object + S3 tags + manifest line. Returns put_object's response."""
def lake_upload_file(bucket, key, filename, *, owner_hint=None, client=None, manifest=True, ExtraArgs=None):
    """upload_file twin for vad/keyframe (same tags, same manifest)."""
def manifest_key(now=None, uid=None) -> str   # lake_manifest/{yyyy}/{mm}/{dd}/{uuid}.jsonl (UTC date)
```

`owner_hint` is a dict with any of `company_id, site_id, user_id, folder, session_base`; all optional. Env: `LAKE_MANIFEST` (`on` default; `off` skips manifest and tags — the kill switch), `LAKE_TAGS` (`on` default; tags are set only when the hint carries a company or site).

Manifest line (one JSON object per line, one line per manifest object): `{"v":1,"bucket":..,"key":..,"size":len(body) or null,"t":"<utc iso>","hint":{...only non-null...}}`.

- [ ] **Step 1: Failing tests** `tests/unit/test_lake_put.py` with a `FakeS3` recording `put_object` calls and able to raise:
  1. happy path: two `put_object` calls — the object (with `Tagging="company=<id>&site=<id>"` when hint has both, no `Tagging` when hint empty) then one to `lake_manifest/YYYY/MM/DD/<uuid>.jsonl` in the **same bucket**, body is valid JSONL with `key`, `bucket`, `size`, `hint`.
  2. object put raises -> `lake_put` re-raises the same exception and writes **no** manifest line.
  3. manifest put raises -> `lake_put` returns the object's response, logs a WARNING (caplog), does not raise.
  4. `LAKE_MANIFEST=off` -> exactly one put, no `Tagging`.
  5. tenant-less keys (`config/...`, `download_claims/...`) are never manifested (`lake_keys.parse(key)["scope"] == "global"`), so the global writers can adopt the helper without noise.
  6. `put_kwargs` (`ContentType`, `IfNoneMatch`) pass through untouched.
  7. `lake_upload_file` passes `Tagging` inside `ExtraArgs` and writes the manifest line.
- [ ] **Step 2: Run, expect red.** `python -m pytest tests/unit/test_lake_put.py -q`.
- [ ] **Step 3: Implement `src/lake_put.py`:**

```python
import json, logging, os, uuid
from datetime import datetime, timezone
import lake_keys

logger = logging.getLogger(__name__)
_CLIENT = None


def _client(client):
    global _CLIENT
    if client is not None:
        return client
    if _CLIENT is None:
        import boto3
        _CLIENT = boto3.client("s3")
    return _CLIENT


def manifest_key(now=None, uid=None):
    n = now or datetime.now(timezone.utc)
    return f"lake_manifest/{n:%Y/%m/%d}/{uid or uuid.uuid4()}.jsonl"


def _tagging(hint):
    if os.environ.get("LAKE_TAGS", "on") != "on" or not hint:
        return None
    pairs = [f"{k}={hint[k]}" for k in ("company_id", "site_id") if hint.get(k)]
    return "&".join(p.replace("company_id", "company").replace("site_id", "site") for p in pairs) or None


def _wants_manifest(key):
    p = lake_keys.parse(key)
    return p is None or p["scope"] != "global"      # unknown shapes ARE recorded: the drain reports them


def _write_manifest(c, bucket, key, size, hint):
    line = {"v": 1, "bucket": bucket, "key": key, "size": size,
            "t": datetime.now(timezone.utc).isoformat(),
            "hint": {k: str(v) for k, v in (hint or {}).items() if v}}
    try:
        c.put_object(Bucket=bucket, Key=manifest_key(), Body=(json.dumps(line) + "\n").encode(),
                     ContentType="application/x-ndjson")
    except Exception:  # noqa: BLE001 - the object is already stored; the backfill is the safety net
        logger.warning("lake_put: manifest write failed for %s", key, exc_info=True)


def lake_put(bucket, key, body, *, owner_hint=None, client=None, manifest=True, **put_kwargs):
    c = _client(client)
    tag = _tagging(owner_hint)
    if tag:
        put_kwargs["Tagging"] = tag
    resp = c.put_object(Bucket=bucket, Key=key, Body=body, **put_kwargs)   # raises exactly as before
    if manifest and os.environ.get("LAKE_MANIFEST", "on") == "on" and _wants_manifest(key):
        size = len(body) if isinstance(body, (bytes, bytearray)) else None
        _write_manifest(c, bucket, key, size, owner_hint)
    return resp
```

  `lake_upload_file` mirrors this with `c.upload_file(filename, bucket, key, ExtraArgs=...)`; size from `os.path.getsize`.
- [ ] **Step 4: Run green; red-proof** (swap the order so the manifest is written before the put: test 2 goes red).
- [ ] **Step 5: Commit** `git add src/lake_put.py tests/unit/test_lake_put.py && git commit`.

**Tests that must go red without the change:** the "writer side" of "A manifest line from a non-VPC writer is drained into the index" (the drain test in Task 5 cannot even build its fixture without `manifest_key`/the line format).

---

## Task 4: Adopt `lake_put` in the writers (three mechanical batches)

Each batch replaces `X.put_object(Bucket=B, Key=K, Body=..., **kw)` with `lake_put(B, K, ..., owner_hint=..., client=X, **kw)` (keeping each module's existing client so existing fakes still work). Every module that imports `lake_put` is **already bundled** (`CodeUri: src/`). Each batch ends with a source-scan test that names the files and asserts no raw `put_object`/`upload_file` remains on a tenant key in them — a wiring pin only; the behavioural proof is Task 3's tests plus one per-batch behavioural test below.

`owner_hint` per site (S = folder only; Y = company/site known): pass what the call site already holds; never add a DB or directory read just to fill a hint (the drain resolves the rest). Where a module holds a folder, pass `{"folder": folder}`; where it holds `company_id`/`site_id`, pass them.

**Left raw on purpose (global, per A7):** `lambda_downloader.py:225` and `lambda_orchestrator.py:577,609` (`download_claims/`), `lambda_downloader.py:239` (`pending_downloads/`), `lambda_extraction_backlog.py:539` (`notices/` copy), `batch_seal.py:133` and `batch_redrive.py:113` (self-copy to re-fire S3 events; `copy_object`, no new tenant object), `fargate_downloader.py` (not in `src/*.py` put grep; scripts/ prefix).

### Task 4a: Pipeline stages (audio -> transcript -> extraction)

**Files (all `src/`):** `lambda_vad.py`, `lambda_transcribe.py`, `batch_seal.py`, `lambda_extract_session.py`, `lambda_finalize_claim.py`, `lambda_item_writer.py`, `keyframe_request.py`, `lambda_keyframe.py`, `lambda_extraction_backlog.py`, `extraction_pending.py`. Test: `tests/unit/test_lake_put_adopted_pipeline.py`.

| file:line (origin/develop) | prefix | hint available |
|---|---|---|
| `lambda_vad.py:907` (`upload_file`, web_video), `:1003`, `:1083` (`upload_file`, segments), `:1043` (`put_object` sidecar), `:1148` (`put_object`) | `web_video/`, `audio_segments/` | folder |
| `lambda_transcribe.py:543` | `transcripts/` | folder |
| `batch_seal.py:217` (map), `:220` (batch wav) | `audio_segments/` | folder |
| `lambda_extract_session.py:2072` (merged), `:2574` (extraction), `:2609` (empty marker), `:2676` (request) | `extractions/`, `extraction_empty/`, `extraction_requests/` | folder, session_base |
| `lambda_finalize_claim.py:342, :373, :396, :681` | `extraction_requests/`, `extractions/` | folder, sid |
| `lambda_item_writer.py:763` (`_put_finalize_request`) | `session_finalize_requests/` | sid |
| `keyframe_request.py:22` | `keyframe_requests/` | folder |
| `lambda_keyframe.py:334` (`upload_file`, derived `users/.../pictures/`) | `users/` | folder + topic's `company_id`/`site_id` (the topic row is in scope: **Y**) |
| `lambda_extraction_backlog.py:468` | `extraction_requests/` | sid |
| `extraction_pending.py:118, :151, :157` | `extraction_pending/` | session_base |

- [ ] Failing behavioural test: for `extraction_pending.write_marker` and `keyframe_request.emit` (the two small, importable writers) inject a `FakeS3`, call them, and assert that a `lake_manifest/` object naming the key was also put. Red because the modules still call `put_object` directly. Failing scan test: the listed files contain no `\.put_object\(|\.upload_file\(` on the lines above.
- [ ] Replace each call site. For `lambda_vad.py` and `lambda_keyframe.py` use `lake_upload_file`. `lambda_extract_session.py`'s `s3()` accessor stays; pass `client=s3()`.
- [ ] `python -m pytest tests/unit -q` (full unit suite: existing module tests use fakes with `put_object`; a fake that lacks `Tagging` support still works because `lake_put` only adds the kwarg when a hint exists — if any existing test asserts the exact `put_object` kwargs, update it to ignore `Tagging`).
- [ ] Commit by file name.

### Task 4b: Reports, sessions, embeddings

**Files (all `src/`):** `lambda_report_generator.py`, `lambda_meeting_minutes.py`, `lambda_embed_report.py`, `lambda_rolling_summary.py`, `lambda_session_finalize.py`, `lambda_session_report.py`, `transcript_utils.py`, `lambda_voice_audit.py`. Test: `tests/unit/test_lake_put_adopted_reports.py`.

| file:line | prefix | hint |
|---|---|---|
| `lambda_report_generator.py:537` (debug), `:635` (weather), `:1732` (weather actual), `:1874`/`:1885` (daily json/docx), `:1983`/`:1994` (lake-wide summary json/docx), `:2128`/`:2138` (per-user period), `:2213`/`:2223` (`reports/{date}/sites/{slug}`), `:2261`/`:2270` (lake-wide period) | `reports/`, `weather/` | folder for per-user keys; `{}` for lake-wide (parser marks `lake_wide`); `site_id` for weather |
| `lambda_meeting_minutes.py:631, :1404, :1441, :1456, :1465, :1482` | `reports/{date}/{folder}/meeting_minutes.*`, `meeting_minutes/` | folder when keyed by user |
| `lambda_embed_report.py:115, :168, :185` | `embeddings/`, `reindex_vectors/` | folder |
| `lambda_rolling_summary.py:193` | `session_rolling/` | folder |
| `lambda_session_finalize.py:499, :551, :914, :927` | `session_brief/`, `session_finalize_results/`, `users/.../pictures/` or reports | folder / sid |
| `lambda_session_report.py:614, :879, :1225` | `session_report_results/`, `session_reports/` | folder |
| `transcript_utils.py:532` (`write_meeting_manifest`) | `reports/.../.meeting_manifest.json` | folder |
| `lambda_voice_audit.py:54` | `voice_ask/` | folder |

- [ ] Failing behavioural test: `transcript_utils.write_meeting_manifest` with a fake client puts the manifest line; `lambda_embed_report`'s vectors writer likewise (import-safe functions only). Failing scan test over the listed files.
- [ ] Replace call sites; run full unit suite; commit by file name.
- [ ] **Do NOT add a second `reports/{date}/companies/...` writer here**; Task 10 owns it.

### Task 4c: Shared modules, trace, legacy ingest

**Files (all `src/`):** `pipeline_trace.py`, `reindex.py`, `deletion_mirror.py`, `match_request.py`, `lambda_downloader.py`, `lambda_orchestrator.py`, `fargate_downloader.py` (if it writes a media object: grep `put_object`/`upload`), `repositories/programme.py` is NOT touched (org-api in-process; recorded via Task 2-style `record_best_effort` if it has a conn — it does: add the call in `programme.py:20` writer path only if the write happens through a function that already holds `conn`; otherwise leave to the backfill).

| file:line | prefix | hint |
|---|---|---|
| `pipeline_trace.py:143` | `traces/` | `{"company_id": st company}` (`begin(company_id=)` exists, `:104` key_for) + folder |
| `reindex.py:75, :112` | `reindex_requests/` | company (`_company_id_for_site` ~`:41`) + folder |
| `deletion_mirror.py:48` (`write_mirror`) | `redactions/` | folder, company |
| `match_request.py:39` | `match_requests/` | site |
| `lambda_downloader.py:159` (`put_object(**params)` legacy vendor media) | `users/` | folder only (device account via frozen `user_mapping`; documented) |

- [ ] Failing behavioural test: `pipeline_trace` flush with a fake client emits one manifest line per trace object (hot path: confirm the extra put is one per invocation, not per event); `deletion_mirror.write_mirror` likewise.
- [ ] Replace call sites; full unit suite; commit by file name.

**Tests that must go red without Task 4:** none in the spec list directly; the per-batch behavioural tests above, plus the Task 5 drain test whose fixture is a manifest produced by a real writer function (`extraction_pending.write_marker`), not a hand-made line.

---

## Task 5: Resolver and `drain_lake_manifest`

**Files:**
- Create: `src/lake_owner.py`
- Modify: `src/lambda_org_api.py` (task constant + dispatch + `drain_lake_manifest`), `src/repositories/recordings.py` (add `owners_for_keys`), `src/repositories/meeting_session.py` (add `owners_for_sids`), `src/repositories/users.py` (add `homes_for_folders`)
- Test: `tests/unit/test_lake_owner.py` (pure rules), `tests/integration/test_lake_manifest_drain.py`

**Interfaces:**
- `lake_owner.Resolver(conn)` with a per-instance cache; `resolve_many(items) -> dict[key, Owner]` where `items` are `(key, hint)` and `Owner = {"company_id", "site_id", "user_id", "kind", "folder", "day", "session_base", "reason"}`; `reason` in `recording | session | folder_day_site | folder_home | key | hint | unresolved | global`. **Rule order (first non-null wins, never a guess):**
  1. `recordings.s3_key == key` -> that row's company/site/user.
  2. `lake_keys.parse(key)` carries `site_id` (weather, programme, match, voice) -> `sites.company_id` of that site; carries only `company_id` (voiceprint) -> it.
  3. a `sid` in the key/base -> `meeting_session` (company, site, user).
  4. `folder` -> user (`users.folder_name` is globally unique, migration 0012) -> if exactly one distinct non-null `recordings.site_id` exists for (user, day) use that site and the **site's** company; else the user's home company, site NULL.
  5. the manifest `hint` (`company_id`/`site_id`) only fills fields still NULL (it can be stale for a moved site; the DB wins).
  6. otherwise company NULL, `reason="unresolved"`.
- `repositories.recordings.owners_for_keys(conn, keys) -> {key: {company_id, site_id, user_id}}` (one `WHERE s3_key = ANY(%s)`); `meeting_session.owners_for_sids(conn, sids)`; `users.homes_for_folders(conn, folders) -> {folder: {id, company_id}}`; `recordings.sites_for_folder_days(conn, pairs) -> {(folder, day): site_id|None}` (None when 0 or >1 distinct).
- Task: `DRAIN_LAKE_MANIFEST_TASK = "drain_lake_manifest"`, event `{"task": ..., "dry_run": bool, "max_objects": int (default 500), "buckets": [..] (default distinct(S3_BUCKET, LAKE_BUCKET))}`. Returns `{"manifests": n, "lines": n, "upserted": n, "unresolved": n, "unknown_shape": n, "deleted_manifests": n, "kept": n, "table_missing": bool}`.

Drain algorithm (inside `drain_lake_manifest(conn, s3_client, ...)`):
1. For each bucket, paginate `list_objects_v2(Prefix="lake_manifest/")` up to `max_objects` manifest objects.
2. `get_object` each; parse JSONL; a malformed line is counted and skipped but the object is **kept** (never silently dropped).
3. Group lines, `Resolver.resolve_many`, `lake_objects.upsert_many(..., source="manifest")`. Lines with `scope != tenant` are counted and dropped (they should not be there; the helper filters them).
4. **`conn.commit()`**, then `delete_objects` for exactly the manifest objects whose lines were all upserted. If the table is missing (`upsert_many` returned the missing-table sentinel) or the transaction failed: delete nothing, `table_missing=True`.
5. `dry_run`: resolve and count, write nothing, delete nothing.
Idempotent: a re-run sees no manifest objects (deleted) or upserts the same rows (COALESCE rule).

- [ ] **Step 1: Failing unit tests** `tests/unit/test_lake_owner.py` for the pure parts: rule order with fake lookups (recording beats folder; key-embedded site beats hint; hint only fills NULLs; a (folder, day) with two sites falls back to home company with site NULL; unknown folder -> unresolved, never a default company).
- [ ] **Step 2: Failing integration test** `tests/integration/test_lake_manifest_drain.py` with a `FakeS3` that stores objects in a dict and supports `list_objects_v2`/`get_object`/`delete_objects`/`put_object`:
  - the manifest is produced by **calling a real writer** (`extraction_pending.write_marker(...)` or `lake_put` directly) so a writer/drain format mismatch fails here;
  - world: company A (site SA, recorder `Ada_L`, a `recordings` row for the users/ key), company B; manifest lines for `transcripts/Ada_L/2026-10-01/x.json` (resolved via folder/day -> SA -> A), for a key whose folder has no user (-> NULL), a malformed line;
  - after `drain_lake_manifest`: the rows exist with the resolved owner; the unresolved row has `company_id IS NULL`; the manifest object holding the malformed line is **kept**, the clean ones deleted; second run: `upserted == 0`, same row count;
  - table missing (drop the table inside the test's transaction, or monkeypatch the repo to raise `UndefinedTable`): no manifest object deleted, `table_missing` True;
  - ordering: monkeypatch `conn.commit` to record a call index and `delete_objects` another; assert commit happened first.
- [ ] **Step 3: Run, expect red.**
- [ ] **Step 4: Implement** `lake_owner.py` (pure rule functions + `Resolver` caching the four lookups per run), the three repository lookups, and `drain_lake_manifest` in `lambda_org_api.py` beside `republish_directory` (~L5096). Dispatch in `lambda_handler` next to the other tasks (the same envelope-rule comment as `REPUBLISH_DIRECTORY_TASK`, L383-400):

```python
DRAIN_LAKE_MANIFEST_TASK = "drain_lake_manifest"
...
    if isinstance(event, dict) and event.get("task") == DRAIN_LAKE_MANIFEST_TASK:
        with get_connection() as conn:
            return drain_lake_manifest(conn, s3(), dry_run=event.get("dry_run") is True,
                                       max_objects=int(event.get("max_objects") or 500),
                                       buckets=event.get("buckets"))
```

- [ ] **Step 5: Run unit + integration green; red-proof** (delete before commit; drop the malformed-line keep rule; each should fail a named test).
- [ ] **Step 6: Commit** `git add src/lake_owner.py src/lambda_org_api.py src/repositories/recordings.py src/repositories/meeting_session.py src/repositories/users.py tests/unit/test_lake_owner.py tests/integration/test_lake_manifest_drain.py && git commit`.

**Tests that must go red without the change:** "A manifest line from a non-VPC writer is drained into the index with the resolved owner."

---

## Task 6: A site's company change moves its objects (same transaction)

**Files:**
- Modify: `src/lambda_org_api.py` `patch_org_site` (~L4859; the move is `sites.move_site_company(...)` at ~L4970)
- Test: `tests/integration/test_site_move_moves_lake_objects.py`, `tests/unit/test_site_move_calls_reassign.py`

**Interfaces:** call `lake_objects.reassign_site(conn, site_id, move_to)` immediately after `move_site_company` succeeds and before `_publish_site_coords`, inside `with conn.transaction():` (savepoint) so a missing table cannot poison the move (deploy tolerance); on any other failure the whole request rolls back via the existing `conn.rollback()` pattern at that spot (a move that cannot move its objects must not commit, or A would keep seeing B's data).

```python
    if move_to is not None:
        row = sites.move_site_company(conn, site_id, scope_company_id, move_to)
        if row is None:
            conn.rollback()
            return error("site not found in your company", 404)
        moved = lake_objects.reassign_site(conn, site_id, move_to)
        logger.info("site %s moved to company %s: %d lake object(s) re-owned", site_id, move_to, moved)
```

(`reassign_site` itself swallows only `UndefinedTable`/`UndefinedColumn`, via savepoint, returning 0.)

- [ ] **Step 1: Failing integration test.** World: companies A and B, site S in B, three `lake_objects` rows (two with `site_id=S`, `company_id=B`; one with `site_id` NULL, `company_id=B`), a `FakeS3` that **raises on any call** installed as `api.s3`. Call `patch_org_site(db, platform_admin_caller, S, {"company_id": A})` (use the real move payload; see the `move_to` parsing a few lines above L4955). Assert: both S rows now `company_id=A`, the NULL-site row still B, and the FakeS3 recorded zero calls. This is the spec's third red test.
- [ ] **Step 2: Unit test** with fakes: `reassign_site` is called exactly once, after the move and only when the move happened; not called on a name-only edit; a 404 from a lost race triggers rollback and no reassign.
- [ ] **Step 3: Run, expect red. Implement. Run green. Red-proof.**
- [ ] **Step 4: Search for any other writer of `sites.company_id`:** `grep -n "company_id *=" src/repositories/sites.py src/lambda_org_api.py | grep -i "update\|set"` and `grep -rn "move_site_company" src`. If another path exists (e.g. a company merge), add the same call there; otherwise record "only path" in the commit message.
- [ ] **Step 5: Commit** `git add src/lambda_org_api.py tests/integration/test_site_move_moves_lake_objects.py tests/unit/test_site_move_calls_reassign.py && git commit`.

**Tests that must go red without the change:** "Moving a site to company B changes the owner of every object of that site, and no S3 call is made."

---

## Task 7: `backfill_lake_objects`

**Files:**
- Modify: `src/lambda_org_api.py` (task constant, dispatch, `backfill_lake_objects`)
- Test: `tests/integration/test_lake_backfill.py`

**Interfaces:** event `{"task": "backfill_lake_objects", "dry_run": bool (default **True** unless `apply` is exactly `True`, the `COLLAPSE_PHOTOS_TASK` convention — the spec says `dry_run supported`, so accept `dry_run: false` OR `apply: true` to write), "bucket": optional, "prefixes": optional list (default all tenant families), "start_after": optional key, "time_budget_s": default 780, "tag": bool default False}`. Returns `{"walked": n, "tenant": n, "global_skipped": n, "lake_wide": n, "unknown_shape": n, "resolved": n, "unresolved": n, "mismatch_voice_company": n, "tagged": n, "next_start_after": key|None, "unresolved_sample": [first 50 keys]}`.

Algorithm: paginate `list_objects_v2` (1000/page, `StartAfter`), per page: `lake_keys.parse` each key, `Resolver.resolve_many`, `lake_objects.upsert_many(rows, source="backfill")` (skipped when dry-run), stop when `time_budget_s` is nearly spent and return `next_start_after` so the operator re-invokes (the response is the cursor). Objects with `company_id` NULL are upserted with NULL and counted. Tagging (`put_object_tagging`, no data copy) only if `tag=true` AND apply, one call per resolved object, counted in `tagged`; **default off**. **No S3 write other than tags.**

Idempotency contract (second red test): a second full apply run reports `resolved`/`unresolved` identical and `rows_changed == 0` (compare `SELECT count(*), max(updated_at)` before and after, plus `xmin` unchanged is too strict; assert row set and `company_id` values equal and no `updated_at` bump: make `upsert_many` skip the UPDATE when nothing differs — add `WHERE (lake_objects.company_id, lake_objects.site_id, lake_objects.user_id, lake_objects.size_bytes) IS DISTINCT FROM (EXCLUDED....)` to the `DO UPDATE`).

- [ ] **Step 1: Failing integration test** (`FakeS3` with ~12 keys across families): resolvable objects get the right company (including a site that belongs to B but whose recorder's home is A -> B); an orphan folder with no user and a title-keyed `meeting_minutes/` object stay NULL and appear in `unresolved_sample`; `config/directory.json` is `global_skipped`; `reports/2026-10-01/summary_report.json` is `lake_wide`; dry run writes zero rows and zero S3 mutating calls (`FakeS3` raises on `put_object`/`delete_*`/`copy_*`, allows `put_object_tagging` only when `tag=True`); `time_budget_s=0` returns a `next_start_after` and a second invocation resumes without duplicating; second apply run changes nothing.
- [ ] **Step 2: Run red; implement; green; red-proof.**
- [ ] **Step 3: Commit** `git add src/lambda_org_api.py src/repositories/lake_objects.py tests/integration/test_lake_backfill.py && git commit`.

**Tests that must go red without the change:** "The backfill leaves unresolvable objects NULL and reports them. A second run changes nothing."

---

## Task 8a: Bounded listings in org-api request paths

**Files:**
- Modify: `src/lambda_org_api.py`: `_list_report_folders` (L9032-9058, called from `admin_disambiguation` L9780-9830 at ~L9801), `_read_org_report_history` (L11045-11110, `REPORT_LAKE_PREFIX` whole-prefix paginator), and its callers (`grep -n "_read_org_report_history" src/lambda_org_api.py`).
- Modify: `src/repositories/lake_objects.py` (use `report_folders_for_day`, `list_reports`)
- Modify: `src/template.yaml` only in Task 11 (`LAKE_INDEX_READS`)
- Test: `tests/unit/test_report_listings_bounded.py`, `tests/integration/test_report_history_from_index.py`

**Replacements:**

| function | today | after (when `LAKE_INDEX_READS == "true"`) |
|---|---|---|
| `_list_report_folders(date)` | paginates `reports/{date}/` | `lake_objects.report_folders_for_day(conn, date, company_ids=<caller scope>)`; needs `conn` and `caller` (add parameters; one caller) |
| `_read_org_report_history(folder_scope, limit, keep)` | paginates the **whole** `reports/` | `lake_objects.list_reports(conn, folders=folder_scope, limit=limit)` ordered by `day DESC`; `docx_key`/`docx_size` filled from the sibling `report` row with the same stem; `generated_at` from `updated_at`; row shape byte-identical to today's (`{key,type,date,generated_at,size[,docx_key,docx_size]}`) |

When the flag is off, or the repository reports the table missing (WARNING + sentinel `None`, **not** an empty list: empty means "no reports"), both functions run the old S3 code unchanged. Never fall back on an empty result when the flag is on.

- [ ] **Step 1: Failing unit test:** with `LAKE_INDEX_READS=true` and a `FakeS3` whose `get_paginator` raises, `_list_report_folders` and `_read_org_report_history` complete from a fake repository without touching S3 (this is the spec's "make no whole-prefix listing", and it also covers a cross-check: `count_transcripts_for_date`'s non-VPC half is Task 8b). Flag off: the old S3 path is used (regression pin). Table missing: old path + WARNING.
- [ ] **Step 2: Integration test:** real rows for companies A and B; `list_reports(company_ids=[A])` returns none of B's; docx pairing; ordering; `limit`.
- [ ] **Step 3: Run red; implement; green; red-proof; commit** `git add src/lambda_org_api.py src/repositories/lake_objects.py tests/unit/test_report_listings_bounded.py tests/integration/test_report_history_from_index.py`.

### Task 8b: `count_transcripts_for_date` and meeting-minutes scans (non-VPC)

Per A1 these cannot query Aurora. Two parts:

**Files:**
- Modify: `src/lambda_org_api.py` (new task `publish_lake_day_index` folded into the drain: after a drain with upserts, write `lake_index/days/{date}.json` for each affected date via `lake_put(..., manifest=False)`), `src/lambda_report_generator.py` (`count_transcripts_for_date` L1552, caller `check_stale_reports` L1587), `src/lambda_meeting_minutes.py` (L405-417 folder discovery; `list_s3_objects` L227 whole-prefix scan).
- Test: `tests/unit/test_count_transcripts_uses_day_index.py`, `tests/unit/test_meeting_minutes_folder_discovery.py`

Day-index document: `{"date": "...", "transcripts": {"total": n, "by_company": {company_id|"unresolved": n}}, "published_at": ...}`, computed by `SELECT company_id, count(*) FROM lake_objects WHERE kind='transcript' AND day=%s AND key LIKE '%.json' GROUP BY 1`.

- [ ] **Step 1: Failing tests.** (a) `count_transcripts_for_date(bucket, date)` with the day-index object present returns its `total` and calls `list_s3_objects` **zero times** (patch it to raise). (b) Absent document -> old listing, WARNING logged (regression pin). (c) Meeting-minutes discovers folders from `directory.load(...)["people"]` (already read by that lambda via `directory.py`) when `user_filter` is empty and calls `list_s3_objects('transcripts/')` zero times; absent directory -> old scan + WARNING.
- [ ] **Step 2: Run red; implement; green; red-proof.**
- [ ] **Step 3:** The per-folder fallback scan at `lambda_meeting_minutes.py:~413` (`transcripts/{user}/` then filter by date) is already per-folder and stays.
- [ ] **Step 4: Commit** by file name.

**Left alone (nightly batch jobs, spec "keep their listings for now"):** `lambda_report_generator.py:1442-1444, :1554, :1659, :2079`, `lambda_ingest.py:964-970`, `lambda_extraction_backlog.py:173`, `batch_redrive.py:86`. Listed in the final report so they are not mistaken for misses.

**Tests that must go red without the change:** "`count_transcripts_for_date` and report history make no whole-prefix listing."

---

## Task 9a: `export_company` engine

**Files:**
- Create: `src/company_export.py` (pure engine; no Lambda glue)
- Modify: `src/lambda_org_api.py` (task constant + dispatch)
- Test: `tests/unit/test_company_export_engine.py`, `tests/integration/test_export_company.py`

**Interfaces:**
- Task event (operator invoke, IAM-gated, same envelope rule as the other tasks): `{"task": "export_company", "company_id": uuid, "export_id": optional, "cursor": optional, "time_budget_s": 780, "part_bytes": 536870912}`. Returns `{"company_id", "export_id", "done": bool, "next_cursor": key|None, "parts": [keys], "objects": n, "bytes": n}`.
- `company_export.run(conn, s3, company_id, *, bucket_for, export_id, cursor, time_budget_s, part_bytes, now) -> dict`.

Design (memory-safe on a 1 GiB Lambda):
- Select with `lake_objects.owners_for_company(conn, company_id, after_key=cursor, limit=1000)` — keyset by `s3_key`; `WHERE company_id = %s AND deleted_at IS NULL` only. **There is no code path that lists the bucket**, so another company's object cannot enter by prefix accident. `bucket_for(kind)` picks `S3_BUCKET` vs `LAKE_BUCKET` (TEST buckets differ).
- Output: `exports/{company_id}/{export_id}/part-0001.zip, part-0002.zip, ...` plus `manifest.json` (written last, `done` only): `{company_id, export_id, generated_at, parts:[{key, objects, bytes}], objects:[{s3_key, kind, size, part, site_id}]}`.
- Each part is a ZIP written with `zipfile.ZipFile(sink, "w", ZIP_STORED, allowZip64=True)` onto a non-seekable `_MultipartSink` (a file-like with `write()`/`flush()` that buffers >= 8 MiB and calls `upload_part`; `close()` completes the multipart upload; abort on any exception). Objects are copied with `s3.get_object(...)["Body"].iter_chunks(1 << 20)` into `zf.open(name, "w", force_zip64=True)`: **no object and no zip is ever held whole in memory**. A part is closed when `bytes >= part_bytes` (default 512 MiB), bounding each multipart upload far below the 10,000-part cap and making retries cheap. ZIP_STORED because media is already compressed.
- Resumability: after each closed part, a `state.json` (`{export_id, cursor, parts, objects, bytes}`) is written under the export prefix; the invocation stops when `time_budget_s` is nearly spent and returns `done: False, next_cursor`. The operator (or the Task 9b route's self-invoke) calls again with the same `export_id`. A missing source object (deleted/never uploaded) is recorded in the manifest as `missing` and does not abort the export.
- Entry names inside a part: `{kind}/{original s3_key}` (key preserved verbatim so a restore is a path copy).
- Overwriting: `exports/` objects are written under a fresh `export_id` (uuid), never overwritten.
- Exports are themselves not indexed (`exports/` is `scope=global` in `lake_keys`).

- [ ] **Step 1: Failing unit tests** (engine with fake conn returning canned `owners_for_company` pages and a `FakeS3` with real multipart semantics: `create_multipart_upload`/`upload_part`/`complete_multipart_upload`/`abort`): (a) two parts are produced when `part_bytes` is small; (b) peak buffered bytes in the sink never exceeds 2x the part size (assert on the fake); (c) a source `get_object` failure for one key -> `missing` entry, export continues; (d) a multipart error aborts the upload (no orphan multipart); (e) time budget exhausted -> `done False` with a cursor, and resuming with that cursor yields no duplicate entry; (f) the zip is readable with `zipfile.ZipFile` and contains every selected key byte-for-byte.
- [ ] **Step 2: Failing integration test** `test_export_company.py`: companies A and B; objects (rows) for both; one site that **moved from B to A** via `reassign_site` (its rows were B's, now A's). `FakeS3` holds bytes for every key. `export_company(A)` -> manifest entries and zip members include the moved site's objects and **none** of B's (assert on the set of `s3_key`s and on every zip member name); `export_company(B)` excludes the moved site's. Also: a row with `company_id` NULL is in neither. This is the spec's sixth red test.
- [ ] **Step 3: Run red; implement `company_export.py`; wire the task constant `EXPORT_COMPANY_TASK = "export_company"` in `lambda_handler`; green; red-proof** (change the selection to `WHERE site_id IN (...)` or remove the company predicate: integration test goes red).
- [ ] **Step 4: Commit** `git add src/company_export.py src/lambda_org_api.py tests/unit/test_company_export_engine.py tests/integration/test_export_company.py && git commit`.

### Task 9b: platform_admin invocation path and download

**Files:**
- Modify: `src/lambda_org_api.py` (two routes + `start_company_export`, `company_export_status`; `LAKE_OPS_FUNCTION` env name for async self-invoke), `src/template.yaml` in Task 11 (IAM `lambda:InvokeFunction` on LakeOps)
- Test: `tests/unit/test_company_export_routes.py`

**Interfaces:** `POST /companies/{company_id}/export` -> `start_company_export(conn, caller, company_id)`; `GET /companies/{company_id}/export/{export_id}` -> status + presigned GET URLs (15 min) for finished parts and `manifest.json`. Both require `caller["global_role"] == "platform_admin"` (not admin, not gm — an export is the whole company's data and crosses the "caller's company" boundary the other routes enforce), else 403; the company must exist, else 404. The POST invokes the LakeOps function asynchronously (`InvocationType="Event"`) with the task payload and returns `202 {"exportId": ...}`; the task re-invokes itself with the cursor until `done`.

- [ ] **Failing tests:** 403 for admin/gm/pm; 404 unknown company; platform_admin POST invokes the lambda client once with `task == "export_company"`, correct `company_id`; status route presigns only keys under `exports/{company_id}/{export_id}/` (try a crafted `export_id` of `../other-company/x` -> 400; validate `export_id` as a uuid).
- [ ] Implement; green; red-proof; commit by file name. Add the routes to the route table comment block at the top of `lambda_org_api.py` (the `POST /api/org/...` list near L27).

**Tests that must go red without the change:** "`export_company(A)` contains no B object, including on a site that moved from B to A."

---

## Task 10: Per-company summary report

**Files:**
- Modify: `src/lambda_report_generator.py` (summary block ~L1626 `check_summary_freshness`, ~L1940-2000 `combined_report`), `src/directory.py` (add `company_of_folder(doc, folder)`), `src/lambda_org_api.py` `admin_disambiguation` (L9780)
- Test: `tests/unit/test_summary_per_company.py`, `tests/unit/test_admin_disambiguation_company_summary.py`

**Interfaces:**
- Generator: refactor the combined-summary block into `_build_summary(users_data, scope_label, triggered_by) -> dict`. Group `all_users_data` by `directory.company_of_folder(doc, folder)` (`people[folder]["company_id"]`; folders with no company are **omitted from every per-company summary** and counted in the log). For each company with at least one folder, write `reports/{date}/companies/{company_id}/summary_report.json` (and `.docx`) through `lake_put` with `owner_hint={"company_id": cid}`. `_report_metadata` carries `scope: "company"` and `company_id`. The lake-wide `reports/{date}/summary_report.json` is still written while `SUMMARY_LAKE_WIDE != "off"` (default on for one release; A5), and `check_summary_freshness` is applied per company key.
- `lake_keys` gains `reports/{date}/companies/{company_id}/summary_report.*` -> `kind="company_summary"`, `company_id` from the key (add the case + test in Task 1's file as part of this task).
- org-api `admin_disambiguation`: for `is_cross_company(caller["global_role"])` keep serving the lake-wide document (unchanged); for every other caller serve `reports/{date}/companies/{caller["company_id"]}/summary_report.json` verbatim, guarded by the same `_day_has_deleted_sources` check, but scoped (`_day_has_deleted_sources(conn, None, date)` today is lake-wide; reuse it unchanged as the conservative guard and note the over-block in a comment). Nobody is ever served another company's key: the key is built from `caller["company_id"]`, never from a request parameter.

- [ ] **Step 1: Failing tests.** (a) Generator: with `directory` doc folders {A: Ada, Al; B: Bo}, the company-A summary input contains Ada and Al and **not** Bo, and `build_summary` is called once per company; the written keys are exactly the two company keys plus (flag on) the lake-wide key; flag `SUMMARY_LAKE_WIDE=off` -> no lake-wide put. (b) org-api: gm of company A calling `admin_disambiguation(date)` gets A's document (patch `_get_lake_json` to return distinct docs per key and assert the key requested); platform_admin still gets the lake-wide one; a caller with no document for their company falls through to the folder-union path exactly as before. This is the spec's seventh red test ("The per-company summary for A contains no B folder").
- [ ] **Step 2: Run red; implement; green; red-proof** (group everything into one company: test (a) goes red).
- [ ] **Step 3: Commit** `git add src/lambda_report_generator.py src/directory.py src/lambda_org_api.py src/lake_keys.py tests/unit/test_summary_per_company.py tests/unit/test_admin_disambiguation_company_summary.py tests/unit/test_lake_keys.py && git commit`.

**Tests that must go red without the change:** "The per-company summary for A contains no B folder."

---

## Task 11: `template.yaml` — function, IAM, schedule, env, flag

**Files:**
- Modify: `src/template.yaml`
- Test: `tests/unit/test_lake_ownership_template.py` (parses the YAML with the repo's CFN-aware loader if one exists in `tests/unit/` — `grep -ln "yaml" tests/unit/*template*` — else a text-level assertion list)

**Changes (follow the commented precedents named):**

1. **`LakeOpsFunction`** (new, after `OrgApiFunction`; copy `OrgApiFunction`'s `VpcConfig`, `Layers: PsycopgLayer`, PG env block and `S3_BUCKET`/`LAKE_BUCKET` verbatim, as `RagSearchFunction` does at ~L4266). `Condition: HasOrgApi`, `Handler: lambda_org_api.lambda_handler`, `FunctionName: ${P}-lake-ops`, `Timeout: 900`, `MemorySize: 1024`, `EphemeralStorage: {Size: 1024}`, **no** `ReservedConcurrentExecutions` and **no** API events (A9; the mobile upload path must not share a pool with a 15-minute export). Extra env: `LAKE_MANIFEST: "on"`, `LAKE_OPS_FUNCTION: <own name via !Ref>`, `SUMMARY_LAKE_WIDE` is generator-only (below).
2. **Schedule** on `LakeOpsFunction`: `DrainLakeManifest` daily at `cron(25 15 * * ? *)` (03:25 NZST ... same convention as the existing `cron(30 15 * * ? *)` site-coords event at ~L2660 which is 04:30 NZ; this runs **before** the 04:30 site-coords and 04:35 `DirectoryRepublish`), `State: !If [ShouldEnableSchedules, ENABLED, DISABLED]`, `Input: '{"task": "drain_lake_manifest"}'`. On demand = `aws lambda invoke --function-name <prefix>-lake-ops --payload '{"task":"drain_lake_manifest"}'`. Re-verify the cron against the republish_directory comment block ("04:35 NZ") before committing; NZ is UTC+12/13 (`src/nz_time.py`), so pick the cron that is earlier than 15:30 UTC in both seasons.
3. **IAM, LakeOpsFunction only:** `s3:GetObject`, `s3:ListBucket` (no prefix condition: backfill and export walk the DB, backfill walks the bucket) on both `DataBucketName` and `IngestBucketName`; `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` on `exports/*` and `lake_index/*`; `s3:DeleteObject` on `lake_manifest/*` only; `s3:PutObjectTagging` bucket-wide (backfill `tag=true`); `lambda:InvokeFunction` on its own ARN (export self-chaining). **Not** granted: delete anywhere else, put on any tenant prefix.
4. **IAM, `lake_put` callers:** a single `AWS::IAM::ManagedPolicy` `LakeManifestWritePolicy` allowing `s3:PutObject` on `lake_manifest/*` and `s3:PutObjectTagging` on `*` in both buckets; attach to every function whose code now calls `lake_put`: OrgApi (for `deletion_mirror`/`reindex`), Vad, Transcribe, ExtractSession, FinalizeSweep, ItemWriter, Keyframe, EmbedReport, Rolling, SessionFinalize, SessionReport, ReportGenerator, MeetingMinutes, VoiceAudit, ExtractionBacklog, Downloader (legacy media), plus every function that calls `pipeline_trace` (it writes `traces/` via `lake_put`): re-derive the list with `grep -ln "lake_put\|pipeline_trace" src/*.py` mapped to template functions. A managed policy keeps this to one definition plus one line per function instead of ~20 copied statements.
5. **org-api (`OrgApiFunction`):** add `GetObject`/`PutObject` where Task 2/4 added new writes only if not already covered (it already has `redactions/*`, `report_requests/*` ... per inventory §6); add env `LAKE_INDEX_READS: !Ref LakeIndexReads`. **Parameter** `LakeIndexReads` (default `'false'`, `AllowedValues ['true','false']`) next to `GradedRoles`, comment stating "flip only after backfill + a drain".
6. **Report generator / meeting-minutes:** env `SUMMARY_LAKE_WIDE: 'on'` (generator). They already read `config/directory.json` and (Task 8b) `lake_index/days/*`: add `GetObject` on `lake_index/*` to both.
7. **S3 triggers / lifecycle:** none. Add no notification, no EventBridge rule; the prefixes `lake_manifest/`, `lake_index/`, `exports/` match none of the 16 `wire-s3-events.sh` filters (verify by reading `scripts/wire-s3-events.sh`) nor the three `transcripts/` EventBridge rules. Lifecycle for `lake_manifest/` (7 days, a safety net for an undrained manifest) is **not** added: lifecycle is hand-managed on prod and `wire-bucket-lifecycle.sh` refuses prod (inventory §4); record it in the rollout section instead.

- [ ] **Step 1: Failing test** `tests/unit/test_lake_ownership_template.py`: assert (a) `LakeOpsFunction` exists with `Timeout` 900 and no `ReservedConcurrentExecutions`; (b) its schedule input is exactly the drain task and its cron is earlier than the `republish_directory` schedule's; (c) the three-piece wiring of `LAKE_INDEX_READS` (Parameter exists with default `'false'`, `OrgApiFunction` env references it, `lambda_org_api.py` reads `os.environ.get("LAKE_INDEX_READS")`); (d) no statement grants `s3:DeleteObject` outside `lake_manifest/*`; (e) `LakeManifestWritePolicy` is attached to every function that appears in `grep -l lake_put src/*.py`'s module -> function map (hard-code the map in the test with a comment saying to extend it).
- [ ] **Step 2: Run red; edit the template; green.**
- [ ] **Step 3: cfn-lint:** `pip install 'cfn-lint==1.53.3' && cfn-lint src/template.yaml` (the version pinned in `.github/workflows/ci.yml:33`; match its exact flags — read the step first). Must be clean.
- [ ] **Step 4: Full suites:** `python -m pytest tests/unit -q`, then the integration files from Tasks 1, 2, 5, 6, 7, 8a, 9a one at a time through `run-tests.sh`.
- [ ] **Step 5: Commit** `git add src/template.yaml tests/unit/test_lake_ownership_template.py && git commit`.

**Tests that must go red without the change:** none from the spec list; the template test pins the wiring (and only the wiring).

---

## Rollout order (not a task; the owner runs prod steps)

1. Merge to `develop`; TEST deploy. Deploy runs `sam deploy` then `migrate`; the minute between is covered by the deploy-tolerance rules.
2. TEST: invoke `backfill_lake_objects` with `dry_run` (default), read `unresolved_sample` and the counts; then apply; re-run and confirm `rows_changed == 0`.
3. TEST: `drain_lake_manifest` after a real upload and a real pipeline run; confirm rows appear with the resolved owner and manifest objects disappear.
4. TEST: flip `LakeIndexReads=true`; compare Reports history and the admin day picker before/after for one company.
5. TEST: `export_company` for the smallest company; `unzip -l` the parts; check no other company's key.
6. Prod (owner executes the writes): same order; **backfill apply before `LakeIndexReads` is flipped**; the prod `lake_manifest/` 7-day lifecycle is added by hand (`scripts/wire-bucket-lifecycle.sh` refuses prod).
7. One release later: set `SUMMARY_LAKE_WIDE=off`, remove the lake-wide summary write and the platform_admin serving branch.

## Out of scope (do not do here)

Changing any existing key; moving objects; per-tenant KMS; option C; deleting a company's data; L7 IAM tag conditions.

## Self-review checklist

- Spec L1 (Task 1,5,6), L2 (2,3,4,5), L3 (7), L4 (8a,8b), L5 (9a,9b), L6 (10), L7 (out of scope) each map to a task.
- Spec test list: 1 -> T2, 2 -> T5, 3 -> T6, 4 -> T7, 5 -> T8a/8b, 6 -> T9a, 7 -> T10.
- Names consistent across tasks: `lake_objects.upsert/upsert_many/reassign_site/owners_for_company/unresolved/report_folders_for_day/list_reports/record_best_effort`, `lake_keys.parse`, `lake_owner.Resolver`, `lake_put/lake_upload_file/manifest_key`, task constants `DRAIN_LAKE_MANIFEST_TASK`, `BACKFILL_LAKE_OBJECTS_TASK`, `EXPORT_COMPANY_TASK`.
