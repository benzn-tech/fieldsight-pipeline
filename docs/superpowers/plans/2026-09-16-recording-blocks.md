# Recording Blocks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /api/org/sessions` returns a day's `recording_blocks` — runs of recorded audio split on silence, each with its topics — computed in the background from transcript object names and merged at read time.

**Architecture:** A new in-VPC Lambda (`RecordingSegmentsFunction`) listens to `transcripts/` Object Created events, LISTs the day's transcript objects, derives one raw segment per object from its filename with `transcript_utils`, and upserts them monotonically into a new Aurora table `day_recording_segments` (debounced: a day computed < 30 s ago is only marked dirty, and a gated `rate(5 minutes)` trailing pass recomputes dirty days). org-api is the single reader of the two thresholds: `get_org_sessions` filters the stored segments against deletions and wholly-excluded sessions, merges them with a pure module `recording_blocks.py`, and places the surviving topics into each block.

**Tech Stack:** Python 3.11 Lambdas (psycopg 3 via `PsycopgLayer`, boto3), Aurora PostgreSQL Serverless v2, DynamoDB (existing items table, via `sweep_state`), AWS SAM (`src/template.yaml`), EventBridge rules and schedules, GitHub Actions (`deploy.yml` / `deploy-prod.yml`), pytest, cfn-lint 1.53.3 (pinned in CI).

**Spec:** `docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md` (on PR #838, branch `docs/reports-over-any-stretch`) — §4.1, §5.4, §5.5, §7, §10 and §11 findings F1, F4, F8, F9. Where §11 amends an earlier section, §11 wins. This is §9 step 7.

## Global Constraints

- Work only in the worktree `C:\Users\camil\Dropbox\fs-blocks`, branch `feat/recording-blocks`, based on `origin/develop` 74fe3b4. Run every command from the worktree root; on Windows use `git -C C:/Users/camil/Dropbox/fs-blocks ...`.
- **Nothing in this plan merges to `main` or deploys to prod.** Migration `0056_day_recording_segments.sql` must not reach `main` until the whole recording-blocks step is ready: migrations on `main` run against the prod database.
- Merging to `develop` deploys TEST. Only Task 6 does that, and only with the owner's explicit approval.
- Switch `EnableRecordingBlocks`: template default `'false'`; `deploy.yml` passes `${{ vars.TEST_ENABLE_RECORDING_BLOCKS || 'true' }}`; `deploy-prod.yml` passes `${{ vars.PROD_ENABLE_RECORDING_BLOCKS || 'false' }}`.
- Thresholds (D2, D3): `REPORT_BLOCK_GAP_SECONDS` / `ReportBlockGapSeconds` default `'600'`; `REPORT_LONG_BLOCK_SECONDS` / `ReportLongBlockSeconds` default `'5400'`. They are system configuration, never customer-facing.
- F9: the table stores **segments, not blocks**. org-api (`OrgApiFunction`) is the ONLY function given the threshold env vars; the compute function gets neither.
- D4: `session_scope.SESSION_GAP_MINUTES = 15` and the `gap_minutes` field in `GET /sessions` are not touched.
- D5: never on the finalize/email path. `SessionActivityFunction` and `src/session_activity.py` are not modified.
- F4: debounce `DEBOUNCE_SECONDS = 30` (code constant); monotonic upsert (replace only when the new `source_object_count >=` stored, clear `dirty` on write); trailing pass `rate(5 minutes)` gated by the same switch; `ReservedConcurrentExecutions: 2`.
- F1: deleted recordings and wholly-excluded sessions are filtered at READ time in org-api, never removed from storage.
- F8: before the PR merges, the deploy role `github-actions-fieldsight-deploy` is checked with `aws iam simulate-principal-policy` **with resource ARNs** for every action the new resources need (Task 6, Step 1). A missing permission fails the whole stack with CREATE_FAILED and rolls it back.
- BUG-36: an in-VPC function black-holes on any AWS call without a VPC endpoint. `RecordingSegmentsFunction` may reach only Aurora, S3 (existing gateway endpoint) and DynamoDB (gateway endpoint `vpce-01233d5b756ffefcb`). No Lambda invoke, no Secrets Manager at runtime, no EventBridge API calls.
- BUG-33: the trigger is a template-declared `EventBridgeRule` (the `SessionActivityFunction` pattern), never a bucket notification and never `scripts/wire-s3-events.sh`.
- Event shapes: an EventBridge object event carries `detail.object.key` (already decoded); an S3 notification carries `Records[].s3.object.key` (url-encoded); the schedule event carries `source: aws.events`. Handle all three.
- BUG-01 / BUG-09: filename times come only from `transcript_utils.extract_base_time_from_filename`, `extract_vad_offsets_from_filename` and `extract_session_id_from_filename`. No hand-written time regex.
- Aurora auto-pause: `SecondsUntilAutoPause` is 600, and `tests/unit/test_sweep_cadence_vs_autopause.py` forbids a scheduled Aurora client more frequent than every 1200 s unless it gates its connection. The trailing pass therefore connects only when its `sweep_state` flag (`RECORDING_BLOCKS#{stage}`) is set, plus one hourly safety tick.
- Unwired-toggle trap: every Parameter is wired in all three places (template Parameter → function env → both workflows' `--parameter-overrides`). After deploy, read the DEPLOYED function's env; do not trust the template.
- S3: without `s3:ListBucket` a missing key answers 403, not 404. The compute function gets `s3:ListBucket` on the ingest bucket with `s3:prefix` `transcripts/*`, and no `GetObject`.
- SQL: unit-test connection doubles never execute SQL. The repository's SQL is exercised by `tests/integration/test_day_recording_segments.py` against a real Postgres (CI). Locally it reports `7 skipped`, and **a skip is not a pass**.
- Every invocation of the compute function logs exactly what happened (`computed` / `debounced-dirty` / `skipped-unresolved` / `ignored` / `sweep skipped` / `swept N`). A guard that passes silently cannot be told from one that never ran.
- `topics.time_range` format, `topics.occurred_at` and `photo_binding.parse_time_range` are not changed.
- Merge-conflict minimisation: branch `feat/day-scoped-reports` also edits `src/lambda_org_api.py` and `tests/unit/test_org_api_sessions.py`. Do not edit `test_org_api_sessions.py`. The org-api change is two import lines, two new helpers, and ONE line inside `get_org_sessions`.
- Line endings: every `.py` file in this checkout is CRLF; YAML files are LF (`.gitattributes`). Edits to `.py` files use the one-line anchors given.
- Git: stage by path, never `git add -A`; commit messages via `git commit -F <file>`; English only; every message ends with the two trailer lines shown in each commit step. Tasks 1–5 never push; only Task 6 Step 2 pushes, and only with the owner's approval.
- Test commands: `python -m pytest tests/unit/<file> -q`. The full unit suite (`python -m pytest tests/unit -q`) was 4848 passed, 2 skipped with this plan applied.

## Where the existing code amended the brief

Each point below was found by running the code, not by reading it. The plan encodes the resolution.

1. **A `rate(5 minutes)` sweep that connects to Aurora breaks scale-to-zero.** `test_no_ungated_scheduled_client_outpaces_auto_pause` failed with `RecordingSegmentsFunction runs every 300s (rate(5 minutes)), needs >= 1200s`. The cadence is kept, and the connection is gated like `FinalizeSweepFunction`'s. The function sets its own `sweep_state` item (`SWEEP_STATE#RECORDING_BLOCKS#{stage}`, so it can never wake or clear the finalize flag) when it marks a day dirty. The tick skips Aurora when the flag reads idle, and connects unconditionally once an hour, in minutes 05–09 (the window contains the finalize sweep's minute 7 on the shared cluster). This adds `SWEEP_STATE_TABLE`, `STAGE` and `DynamoDBCrudPolicy` on the items table, plus a `GATED_EXCEPTIONS` entry.
2. **`tests/unit/test_template_pgdatabase.py` pins the number of in-VPC functions.** It must go 18 → 19 in the same change as the new function.
3. **Which sessions count as excluded.** The brief's rule — sessions present in the segments but absent from `build_day_sessions` — would hide every session whose extraction has not produced topics yet: a day still being recorded, and the untopic'd audio design §4.1 wants a whole-block window to cover. The plan excludes sessions that HAVE topic rows of which none survived `build_day_sessions` (all redacted or `non_work`). Deleted sessions are removed by the source-prefix tombstone arm, whether or not they have rows.
4. **No existing helper resolves a folder to its owner safely here.** `_timeline_target_id` falls back to the CALLER on a miss and is company-pinned, so it would serve the caller's own blocks under someone else's day. The read uses `users.get_by_folder_name_global`, the same lookup the writer keys the row with, after `_resolve_org_media_folder` has authorised the folder.
5. **`photo_binding.parse_time_range(time_range)` returns `(start_minutes, end_minutes)` or `None`** — minutes, not seconds. A topic covers from the start of its first minute to the end of its last (`end*60 + 59`), so `"12:07 – 12:07"` is not zero-width. Unparseable and end-before-start topics are placed in no block.
6. **`test_the_code_defaults_match_the_template_defaults` reads only `lambda_extract_session.py`.** Its pattern does not fit, so a sibling test for the org-api pair is added instead.
7. **`_day_report_rows` applies only the topic arm of deletion** (design §11.2). The blocks read therefore applies the source-prefix arm itself (`redactions.deleted_source_prefixes(conn, folder, date)`).
8. **Transcripts under `transcripts/` feed three other consumers** (extract-session via S3 notification; rolling-summary and session-activity via EventBridge). Task 6 does not copy a real transcript onto itself to trigger the rule: that would touch sessions and re-drive extraction. It verifies with a direct invoke instead, plus an optional probe object whose name has no base time.

## Out of scope

- Search-side rollup and re-chunking (a separate plan).
- UI picker rendering of blocks (`fieldsight-ui`).
- Template storage and versioning (design §6), and report generation (§5.3).
- Any change to `topics.occurred_at`, the `time_range` format, or `SESSION_GAP_MINUTES` / `gap_minutes`.
- A CloudWatch alarm on the new function's `Throttles` metric (design §7 lists it as a mitigation). Not built here; Task 6 reads the metric by hand. Raised for the owner.
- Deletion of a merged multi-device meeting: its tombstone is `extractions/{lead}/{date}/grp{id}`, which names no device `sid`, so member segments are not filtered by that tombstone. Recorded as an open question for the owner, not solved here.
- Backfilling days recorded before the switch is enabled: a day gets a row only when a transcript lands after deploy.

## File map

| File | Change | Responsibility |
|---|---|---|
| `src/migrations/0056_day_recording_segments.sql` | Create | Table `day_recording_segments` + partial index on dirty rows |
| `src/repositories/day_recording_segments.py` | Create | `get`, `upsert_monotonic`, `mark_dirty`, `list_dirty` |
| `src/recording_blocks.py` | Create | PURE: `merge_segments`, `filter_segments`, `topic_ids_in_block` |
| `src/lambda_recording_segments.py` | Create | Compute function: parse keys, debounce, LIST, upsert, gated trailing pass |
| `src/template.yaml` | Modify | 3 Parameters, 1 Condition, `RecordingSegmentsFunction`, 2 env vars on `OrgApiFunction` |
| `.github/workflows/deploy.yml` | Modify | 3 `--parameter-overrides` lines (TEST) |
| `.github/workflows/deploy-prod.yml` | Modify | 3 `--parameter-overrides` lines (prod) |
| `src/lambda_org_api.py` | Modify | 2 import lines, `_block_thresholds`, `_recording_blocks_entry`, 1 line in `get_org_sessions` |
| `tests/unit/test_migration_0056_day_recording_segments.py` | Create | Migration shape + unique version |
| `tests/integration/test_day_recording_segments.py` | Create | Real SQL: monotonic upsert, dirty, list_dirty, FK |
| `tests/unit/test_recording_blocks.py` | Create | Pure merge/filter/placement, 2026-09-02 fixture |
| `tests/unit/test_recording_segments_compute.py` | Create | Segment derivation, debounce, gate, handler, seam |
| `tests/unit/test_template_recording_segments.py` | Create | Resource shape, triggers, IAM, isolation from finalize |
| `tests/unit/test_org_api_sessions_recording_blocks.py` | Create | The `recording_blocks` key end to end through the route |
| `tests/unit/test_template_workflow_parameter_wiring.py` | Modify (append) | `_BLOCK_TUNABLES` wiring assertions |
| `tests/unit/test_template_pgdatabase.py` | Modify | Guarded PGDATABASE count 18 → 19 |
| `tests/unit/test_sweep_cadence_vs_autopause.py` | Modify | Gated exception for the trailing pass |

Not modified: `src/session_activity.py`, `SessionActivityFunction`, `src/session_scope.py`, `src/photo_binding.py`, `src/transcript_utils.py`, `src/sweep_state.py`, `tests/unit/test_org_api_sessions.py`.

---

### Task 1: The `day_recording_segments` table and its repository

**Files:**
- Create: `src/migrations/0056_day_recording_segments.sql`
- Create: `src/repositories/day_recording_segments.py`
- Test: `tests/unit/test_migration_0056_day_recording_segments.py`
- Test: `tests/integration/test_day_recording_segments.py`

**Interfaces:**
- Consumes: the `users(id)` table (FK); `psycopg.rows.dict_row`.
- Produces (module `repositories.day_recording_segments`; repositories never commit, the caller owns the transaction):
  - `get(conn, user_id, report_date) -> dict | None` — keys `user_id`, `report_date` (`datetime.date`), `folder_name`, `segments` (always a `list`), `source_object_count` (`int`), `dirty` (`bool`), `computed_at` (timezone-aware `datetime`).
  - `upsert_monotonic(conn, user_id, report_date, folder_name, segments: list[dict], source_object_count: int) -> bool` — `True` when written (and `dirty` cleared); `False` when the stored row was computed from more objects (then `dirty` is left as it was).
  - `mark_dirty(conn, user_id, report_date) -> bool` — `False` when there is no row.
  - `list_dirty(conn, limit=50) -> list[dict]` — keys `user_id`, `report_date`, `folder_name`; least recently computed first.
  - Segment shape stored in `segments`: `{"start": float, "end": float, "session_id": "<32 hex>" | None, "key": "transcripts/{folder}/{date}/{file}"}` — seconds since midnight on the device wall clock.

The version number 0056 is free: the highest existing migration is `0055_topic_open_questions.sql`. `src/db/migrate.py` orders by `(int(version), filename)`, and two collisions (0041, 0044) already exist, so the unit test pins that nothing else uses 0056.

- [ ] **Step 1: Write the failing migration-shape test**

Create `tests/unit/test_migration_0056_day_recording_segments.py`:

```python
"""Unit: migration 0056 has the shape the writer and reader rely on.

Text checks only -- the SQL is applied for real by tests/integration (CI Postgres).
The version guard matters because two version collisions have already shipped
(0041, 0044) and the runner orders ties by filename, so a second 0056 from a
parallel branch would apply in an order no one chose.
"""
import os

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0056_day_recording_segments.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def test_no_other_migration_uses_version_0056():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0056_")] == [NAME]


def test_it_keys_one_row_per_user_and_day_and_references_users():
    sql = " ".join(_sql().split())
    assert "CREATE TABLE IF NOT EXISTS day_recording_segments" in sql
    assert "user_id uuid NOT NULL REFERENCES users(id)" in sql
    assert "PRIMARY KEY (user_id, report_date)" in sql


def test_it_stores_segments_and_the_debounce_state_not_blocks():
    sql = " ".join(_sql().split())
    for column in ("folder_name text NOT NULL", "segments jsonb NOT NULL",
                   "source_object_count int NOT NULL",
                   "dirty boolean NOT NULL DEFAULT false",
                   "computed_at timestamptz NOT NULL DEFAULT now()"):
        assert column in sql, column
    assert "gap_seconds" not in sql and "blocks jsonb" not in sql
```

- [ ] **Step 2: Write the integration test for the real SQL**

Create `tests/integration/test_day_recording_segments.py`:

```python
"""Integration: repositories.day_recording_segments against a real PostgreSQL.

The unit tests replace this repository with an in-memory double that implements the
monotonic rule in Python. That proves the callers and nothing about the SQL: the
`ON CONFLICT ... DO UPDATE ... WHERE` guard is the entire F4 protection, and a WHERE
on the wrong side of the comparison would shrink a day silently while every unit test
stayed green. This file is where that rule is actually checked.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py). A SKIP IS NOT A PASS:
CI provides the database; locally, run it against a disposable Postgres with pgvector.
"""
import datetime
import uuid

import psycopg
import pytest

from repositories import day_recording_segments as drs

pytestmark = pytest.mark.integration

DAY = datetime.date(2026, 9, 2)
SID = "81a64d850bb34d8bbf01ec11fd2af02f"
SEGS = [{"start": 39318.0, "end": 39323.0, "session_id": SID,
         "key": f"transcripts/Blocks_T/2026-09-02/ben_ucpk2_2026-09-02_10-55-18_sid{SID}_c0001_off0.0_to5.0_srcwav.json"}]
NEWER = SEGS + [{"start": 39902.0, "end": 39930.0, "session_id": SID,
                 "key": f"transcripts/Blocks_T/2026-09-02/ben_ucpk2_2026-09-02_11-05-02_sid{SID}_c0021_off0.0_to28.0_srcwav.json"}]


def _user(db, folder):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    return db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (cid, f"{folder.lower()}@example.test", folder)).fetchone()[0]


def test_a_first_write_round_trips_segments_as_a_list(db):
    uid = _user(db, "Blocks_T")
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == SEGS
    assert row["source_object_count"] == 5
    assert row["dirty"] is False
    assert row["report_date"] == DAY and row["folder_name"] == "Blocks_T"
    assert row["computed_at"] is not None


def test_a_listing_with_fewer_objects_does_not_shrink_the_day(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5)
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4) is False
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["source_object_count"] == 5


def test_an_equal_or_larger_listing_replaces_the_day_and_clears_dirty(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 5)
    assert drs.mark_dirty(db, uid, DAY) is True
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5) is True
    row = drs.get(db, uid, DAY)
    assert row["segments"] == NEWER and row["dirty"] is False


def test_a_refused_write_leaves_the_dirty_mark_in_place(db):
    uid = _user(db, "Blocks_T")
    drs.upsert_monotonic(db, uid, DAY, "Blocks_T", NEWER, 5)
    drs.mark_dirty(db, uid, DAY)
    assert drs.upsert_monotonic(db, uid, DAY, "Blocks_T", SEGS, 4) is False
    assert drs.get(db, uid, DAY)["dirty"] is True


def test_mark_dirty_without_a_row_creates_nothing(db):
    uid = _user(db, "Blocks_T")
    assert drs.mark_dirty(db, uid, DAY) is False
    assert drs.get(db, uid, DAY) is None


def test_list_dirty_returns_only_dirty_rows_least_recently_computed_first(db):
    older = _user(db, "Blocks_Old")
    newer = _user(db, "Blocks_New")
    clean = _user(db, "Blocks_Clean")
    for uid, folder in ((older, "Blocks_Old"), (newer, "Blocks_New"), (clean, "Blocks_Clean")):
        drs.upsert_monotonic(db, uid, DAY, folder, SEGS, 1)
    drs.mark_dirty(db, older, DAY)
    drs.mark_dirty(db, newer, DAY)
    # now() is constant inside the test transaction, so order is set explicitly.
    db.execute("UPDATE day_recording_segments SET computed_at = now() - interval '10 minutes' "
               "WHERE user_id = %s", (older,))
    rows = drs.list_dirty(db)
    assert [r["folder_name"] for r in rows] == ["Blocks_Old", "Blocks_New"]
    assert rows[0]["report_date"] == DAY and str(rows[0]["user_id"]) == str(older)
    assert [r["folder_name"] for r in drs.list_dirty(db, limit=1)] == ["Blocks_Old"]


def test_a_row_cannot_name_a_user_that_does_not_exist(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        drs.upsert_monotonic(db, uuid.uuid4(), DAY, "Nobody", SEGS, 1)
```

- [ ] **Step 3: Run the unit test to verify it fails**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_migration_0056_day_recording_segments.py -q
```

Expected: `3 failed` — `AssertionError: assert [] == ['0056_day_recording_segments.sql']` and two `FileNotFoundError` for the missing migration.

- [ ] **Step 4: Write the migration**

Create `src/migrations/0056_day_recording_segments.sql`:

```sql
-- When a person was recording, as raw segments, so a day can be shown as blocks.
--
-- The report picker needs to offer "10:55-11:35" as one selectable stretch: the
-- stretches are the runs of recorded audio separated by more than ten minutes of
-- nothing. Topics alone cannot do it -- on an intermittent day they over-split one
-- meeting into its agenda items, and they drop audio no topic was extracted from
-- (2026-09-02 has 17:14-17:28 recorded and no topic for it).
--
-- SEGMENTS, not blocks (design review finding F9). The first design stored merged
-- blocks with the thresholds they were computed with, which means changing a
-- threshold never reaches a day that has stopped receiving transcripts. Stored raw,
-- org-api merges at read time with whatever the thresholds are now, and it is the
-- single reader of both -- two functions can never disagree about a block.
--
-- segments: [{"start": s, "end": s, "session_id": "<32hex>"|null, "key": "transcripts/..."}]
-- start/end are seconds since midnight on the DEVICE wall clock -- the same clock
-- the transcript filenames and topics.time_range carry. No timezone conversion.
-- `key` is kept so a deleted recording can be filtered out at read time by the same
-- prefix tombstones every other reader uses (finding F1); nothing here is ever
-- deleted in place.
--
-- source_object_count makes the write monotonic (finding F4): a LIST that saw fewer
-- objects than the stored one is an older, out-of-order view of the same day and must
-- not shrink it. dirty is the debounce's memory -- a transcript that landed while the
-- day had just been computed marks the row, and the five-minute trailing pass
-- recomputes it, so the last transcripts of a day are never lost to the debounce.
--
-- Keyed by user rather than folder because users.folder_name can be changed and the
-- person cannot; folder_name is carried so the trailing pass can LIST without a join.
CREATE TABLE IF NOT EXISTS day_recording_segments (
    user_id             uuid        NOT NULL REFERENCES users(id),
    report_date         date        NOT NULL,
    folder_name         text        NOT NULL,
    segments            jsonb       NOT NULL,
    source_object_count int         NOT NULL,
    dirty               boolean     NOT NULL DEFAULT false,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, report_date)
);

-- The trailing pass reads "every dirty row, oldest first" every five minutes. Partial,
-- because almost every row is clean almost all of the time.
CREATE INDEX IF NOT EXISTS day_recording_segments_dirty_idx
    ON day_recording_segments (computed_at) WHERE dirty;
```

- [ ] **Step 5: Write the repository**

Create `src/repositories/day_recording_segments.py`:

```python
"""A day's recorded segments, one row per (user, date). See migration 0056.

Written only by lambda_recording_segments; read only by org-api's GET /sessions.
Repositories never commit -- the caller owns the transaction.
"""
import json

from psycopg.rows import dict_row

_COLS = "user_id, report_date, folder_name, segments, source_object_count, dirty, computed_at"


def get(conn, user_id, report_date) -> dict | None:
    """The stored row, with `segments` always a list, or None when the day was never computed."""
    row = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM day_recording_segments WHERE user_id = %s AND report_date = %s",
        (str(user_id), report_date),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row["segments"], str):     # psycopg returns jsonb as str on some paths
        row["segments"] = json.loads(row["segments"])
    return row


def upsert_monotonic(conn, user_id, report_date, folder_name, segments,
                     source_object_count) -> bool:
    """Write the day's segments unless the stored row was computed from MORE objects.

    Returns True when the row was written (and `dirty` cleared), False when an older,
    smaller listing was refused. A refused write leaves `dirty` as it was on purpose:
    clearing it would drop the mark a newer transcript left.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO day_recording_segments "
        "(user_id, report_date, folder_name, segments, source_object_count, dirty, computed_at) "
        "VALUES (%s, %s, %s, %s::jsonb, %s, false, now()) "
        "ON CONFLICT (user_id, report_date) DO UPDATE SET "
        "folder_name = EXCLUDED.folder_name, "
        "segments = EXCLUDED.segments, "
        "source_object_count = EXCLUDED.source_object_count, "
        "dirty = false, "
        "computed_at = now() "
        "WHERE EXCLUDED.source_object_count >= day_recording_segments.source_object_count "
        "RETURNING user_id",
        (str(user_id), report_date, folder_name, json.dumps(segments),
         int(source_object_count)),
    ).fetchone()
    return row is not None


def mark_dirty(conn, user_id, report_date) -> bool:
    """Flag an existing row for the trailing pass. False when there is no row to flag."""
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE day_recording_segments SET dirty = true "
        "WHERE user_id = %s AND report_date = %s RETURNING user_id",
        (str(user_id), report_date),
    ).fetchone()
    return row is not None


def list_dirty(conn, limit=50) -> list[dict]:
    """Dirty rows, least recently computed first: [{user_id, report_date, folder_name}]."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT user_id, report_date, folder_name FROM day_recording_segments "
        "WHERE dirty ORDER BY computed_at, user_id LIMIT %s",
        (int(limit),),
    ).fetchall()
```

- [ ] **Step 6: Run the unit test and the integration test**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_migration_0056_day_recording_segments.py -q
python -m pytest tests/integration/test_day_recording_segments.py -q
```

Expected: `3 passed` for the unit test. The integration test reports `7 passed` where `TEST_DATABASE_URL` points at a Postgres with pgvector (CI), and `7 skipped` without it. A local skip is not a pass: confirm `7 passed` in the CI run of the PR before Task 6.

- [ ] **Step 7: Commit**

Stage by path only (never `git add -A` in this repo). The message goes through a file so no BOM or shell quoting reaches git.

```bash
MSG="$(mktemp)"
cat > "$MSG" <<'EOF'
feat(db): store a day's recorded segments for recording blocks

Migration 0056 adds day_recording_segments: one row per (user, date) holding
raw segments derived from transcript names (design 2026-09-15 F9), a
monotonic source_object_count and the debounce's dirty flag (F4).
The repository's SQL is exercised against real Postgres in
tests/integration. Must not reach main before the recording-blocks step
is ready: migrations on main run against prod.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M
EOF
git -C C:/Users/camil/Dropbox/fs-blocks add \
  src/migrations/0056_day_recording_segments.sql \
  src/repositories/day_recording_segments.py \
  tests/unit/test_migration_0056_day_recording_segments.py \
  tests/integration/test_day_recording_segments.py
git -C C:/Users/camil/Dropbox/fs-blocks commit -F "$MSG"
git -C C:/Users/camil/Dropbox/fs-blocks show --stat --oneline HEAD
```

Expected: one new commit whose `--stat` lists exactly: `src/migrations/0056_day_recording_segments.sql`, `src/repositories/day_recording_segments.py`, `tests/unit/test_migration_0056_day_recording_segments.py`, `tests/integration/test_day_recording_segments.py`.

### Task 2: Pure merge, filter and topic placement (`recording_blocks.py`)

**Files:**
- Create: `src/recording_blocks.py`
- Test: `tests/unit/test_recording_blocks.py`

**Interfaces:**
- Consumes: `photo_binding.parse_time_range(time_range) -> tuple[int, int] | None` — `(start_minutes, end_minutes)` from `"HH:MM – HH:MM"` (hyphen, en dash or em dash), `None` when missing or unparseable. Verified in `src/photo_binding.py:125`; not modified.
- Produces (module `recording_blocks`, no I/O):
  - `merge_segments(segments: list[dict], gap_seconds: float, long_block_seconds: float) -> list[dict]` — blocks ordered by start: `{"from": "HH:MM", "to": "HH:MM", "start": float, "end": float, "minutes": int, "selectable_as_whole": bool, "session_ids": list[str]}`. Merge when `next.start - current.end <= gap_seconds`; `selectable_as_whole = (end - start) <= long_block_seconds`; `session_ids` sorted, unique, non-null.
  - `filter_segments(segments: list[dict], excluded_session_ids: set[str] | None, deleted_prefixes: list[str] | None) -> list[dict]`.
  - `topic_ids_in_block(block: dict, topic_rows: list[dict]) -> list[str]` — ids (as `str`) of rows whose `time_range` overlaps the block, in `topic_rows` order.

The fixture is CONSTRUCTED: three chunk sessions whose segments reproduce the blocks design §4.1 measured on Ben_UCPK2 for 2026-09-02 (10:55–11:35, 17:14–17:47, 17:57–18:15). The full-day check was measured separately against the manifest; this fixture pins the merge rule, not the data. The 17:47:05 → 17:57:30 silence is 625 s, deliberately just over the 600 s threshold.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_recording_blocks.py`:

```python
"""Unit: recording_blocks -- merge, read-time filtering (F1), topic placement.

Pure module; every test drives the real functions.

The 2026-09-02 fixture is CONSTRUCTED: three chunk sessions whose segments are
consistent with the blocks design §4.1 measured for that day on Ben_UCPK2
(10:55–11:35, 17:14–17:47, 17:57–18:15). The full-day check was measured separately
against the day's real manifest; this fixture pins the merge rule, not the data.
"""
import pytest

import recording_blocks as rb

SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"
PREFIX = "transcripts/Ben_UCPK2/2026-09-02/"


def _seg(start, end, sid, name):
    return {"start": start, "end": end, "session_id": sid, "key": PREFIX + name}


# Seconds since midnight, derived by hand from the key names beside them.
DAY_0902 = [
    _seg(39318.0, 39323.0, SID_A, f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json"),
    _seg(39902.0, 39930.0, SID_A, f"ben_ucpk2_2026-09-02_11-05-02_sid{SID_A}_c0021_off0.0_to28.0_srcwav.json"),
    _seg(40482.0, 40510.0, SID_A, f"ben_ucpk2_2026-09-02_11-14-40_sid{SID_A}_c0041_off2.0_to30.0_srcwav.json"),
    _seg(41050.0, 41080.0, SID_A, f"ben_ucpk2_2026-09-02_11-24-10_sid{SID_A}_c0061_off0.0_to30.0_srcwav.json"),
    _seg(41630.0, 41660.0, SID_A, f"ben_ucpk2_2026-09-02_11-33-50_sid{SID_A}_c0079_off0.0_to30.0_srcwav.json"),
    _seg(41712.0, 41732.0, SID_A, f"ben_ucpk2_2026-09-02_11-35-12_sid{SID_A}_c0081_off0.0_to20.0_srcwav.json"),
    _seg(62045.0, 62075.0, SID_B, f"ben_ucpk2_2026-09-02_17-14-05_sid{SID_B}_c0001_off0.0_to30.0_srcwav.json"),
    _seg(62620.0, 62650.0, SID_B, f"ben_ucpk2_2026-09-02_17-23-40_sid{SID_B}_c0019_off0.0_to30.0_srcwav.json"),
    _seg(63175.0, 63205.0, SID_B, f"ben_ucpk2_2026-09-02_17-32-55_sid{SID_B}_c0037_off0.0_to30.0_srcwav.json"),
    _seg(63691.5, 63720.0, SID_B, f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json"),
    _seg(64000.0, 64025.0, SID_B, f"ben_ucpk2_2026-09-02_17-46-40_sid{SID_B}_c0065_off0.0_to25.0_srcwav.json"),
    _seg(64650.0, 64680.0, SID_C, f"ben_ucpk2_2026-09-02_17-57-30_sid{SID_C}_c0001_off0.0_to30.0_srcwav.json"),
    _seg(65170.0, 65200.0, SID_C, f"ben_ucpk2_2026-09-02_18-06-10_sid{SID_C}_c0017_off0.0_to30.0_srcwav.json"),
    _seg(65690.0, 65710.0, SID_C, f"ben_ucpk2_2026-09-02_18-14-50_sid{SID_C}_c0033_off0.0_to20.0_srcwav.json"),
]


# ---- merge_segments ------------------------------------------------------

def test_the_0902_day_merges_into_the_three_blocks_the_design_measured():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    assert [(b["from"], b["to"]) for b in blocks] == [
        ("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    assert [b["minutes"] for b in blocks] == [40, 33, 18]
    assert [b["session_ids"] for b in blocks] == [[SID_A], [SID_B], [SID_C]]
    assert all(b["selectable_as_whole"] for b in blocks)


def test_input_order_does_not_matter():
    shuffled = list(reversed(DAY_0902))
    assert rb.merge_segments(shuffled, 600, 5400) == rb.merge_segments(DAY_0902, 600, 5400)


def test_a_gap_of_exactly_the_threshold_merges_and_one_second_more_splits():
    a = {"start": 0.0, "end": 100.0, "session_id": None, "key": "k1"}
    b_at = {"start": 700.0, "end": 710.0, "session_id": None, "key": "k2"}
    b_after = {"start": 701.0, "end": 710.0, "session_id": None, "key": "k3"}
    assert len(rb.merge_segments([a, b_at], 600, 5400)) == 1
    assert len(rb.merge_segments([a, b_after], 600, 5400)) == 2


def test_the_17_47_to_17_57_silence_is_over_ten_minutes_so_those_stay_apart():
    # 17:47:05 -> 17:57:30 is 625 s. At a 630 s gap the two afternoon blocks join.
    joined = rb.merge_segments(DAY_0902, 630, 5400)
    assert [(b["from"], b["to"]) for b in joined] == [("10:55", "11:35"), ("17:14", "18:15")]
    assert joined[1]["session_ids"] == sorted([SID_B, SID_C])


def test_a_block_longer_than_the_long_threshold_is_not_selectable_as_a_whole():
    blocks = rb.merge_segments(DAY_0902, 600, 1980)
    # 40 min, 33 min (exactly 1980 s), 18 min
    assert [b["selectable_as_whole"] for b in blocks] == [False, True, True]


def test_a_contained_segment_does_not_shorten_the_block():
    long_seg = {"start": 0.0, "end": 1000.0, "session_id": None, "key": "a"}
    inner = {"start": 10.0, "end": 20.0, "session_id": None, "key": "b"}
    [block] = rb.merge_segments([long_seg, inner], 600, 5400)
    assert block["end"] == 1000.0


def test_null_session_ids_are_not_listed_and_duplicates_collapse():
    segs = [{"start": 0.0, "end": 5.0, "session_id": None, "key": "a"},
            {"start": 6.0, "end": 9.0, "session_id": SID_A, "key": "b"},
            {"start": 10.0, "end": 12.0, "session_id": SID_A, "key": "c"}]
    [block] = rb.merge_segments(segs, 600, 5400)
    assert block["session_ids"] == [SID_A]


def test_segments_without_numeric_times_are_ignored_not_guessed():
    segs = [{"start": None, "end": 5.0, "session_id": None, "key": "a"},
            {"start": True, "end": 5.0, "session_id": None, "key": "b"},
            {"start": 60.0, "end": 90.0, "session_id": None, "key": "c"}]
    [block] = rb.merge_segments(segs, 600, 5400)
    assert (block["start"], block["end"]) == (60.0, 90.0)


def test_no_segments_is_no_blocks():
    assert rb.merge_segments([], 600, 5400) == []


def test_a_segment_past_midnight_renders_as_23_59_not_24():
    [block] = rb.merge_segments(
        [{"start": 86390.0, "end": 86420.0, "session_id": None, "key": "a"}], 600, 5400)
    assert (block["from"], block["to"]) == ("23:59", "23:59")
    assert block["end"] == 86420.0


# ---- filter_segments (F1) --------------------------------------------------

def test_a_session_excluded_by_build_day_sessions_is_dropped():
    kept = rb.filter_segments(DAY_0902, {SID_B}, [])
    assert {s["session_id"] for s in kept} == {SID_A, SID_C}


def test_a_deleted_chunk_session_is_dropped_by_its_extraction_tombstone():
    tombstone = f"extractions/Ben_UCPK2/2026-09-02/sid{SID_C}"
    kept = rb.filter_segments(DAY_0902, set(), [tombstone])
    assert {s["session_id"] for s in kept} == {SID_A, SID_B}


def test_a_tombstone_for_another_folder_or_date_does_not_match():
    kept = rb.filter_segments(DAY_0902, set(), [
        f"extractions/Someone_Else/2026-09-02/sid{SID_C}",
        f"extractions/Ben_UCPK2/2026-09-03/sid{SID_C}",
    ])
    assert len(kept) == len(DAY_0902)


def test_a_legacy_whole_file_recording_is_dropped_by_its_base_tombstone():
    legacy = {"start": 45779.8, "end": 46043.8, "session_id": None,
              "key": "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json"}
    other = {"start": 50000.0, "end": 50010.0, "session_id": None,
             "key": "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_13-50-00.json"}
    kept = rb.filter_segments([legacy, other], set(),
                              ["extractions/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34"])
    assert kept == [other]


def test_a_tombstone_on_the_raw_transcript_key_also_matches():
    target = DAY_0902[0]["key"]
    kept = rb.filter_segments(DAY_0902, set(), [target])
    assert target not in {s["key"] for s in kept}
    assert len(kept) == len(DAY_0902) - 1


def test_empty_prefixes_never_match_everything():
    assert len(rb.filter_segments(DAY_0902, None, ["", None])) == len(DAY_0902)


# ---- topic_ids_in_block ------------------------------------------------------

TOPICS = [
    {"id": "t-a1", "time_range": "10:56 – 11:10"},
    {"id": "t-a2", "time_range": "11:30 – 11:36"},
    {"id": "t-b1", "time_range": "17:30 – 17:45"},
    {"id": "t-edge", "time_range": "17:47 – 17:50"},   # starts inside 17:47:05's minute
    {"id": "t-gap", "time_range": "17:50 – 17:55"},    # wholly in the silence between blocks
    {"id": "t-c1", "time_range": "18:00 – 18:14"},
    {"id": "t-empty", "time_range": ""},
    {"id": "t-none", "time_range": None},
    {"id": "t-backwards", "time_range": "11:10 – 10:56"},
]


def test_topics_are_placed_by_overlap_with_minute_precision():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    placed = [rb.topic_ids_in_block(b, TOPICS) for b in blocks]
    assert placed == [["t-a1", "t-a2"], ["t-b1", "t-edge"], ["t-c1"]]


def test_unparseable_and_backwards_topics_are_in_no_block():
    blocks = rb.merge_segments(DAY_0902, 600, 5400)
    everywhere = {tid for b in blocks for tid in rb.topic_ids_in_block(b, TOPICS)}
    assert not everywhere & {"t-empty", "t-none", "t-backwards", "t-gap"}


def test_a_single_minute_topic_is_not_zero_width():
    block = {"start": 43650.0, "end": 43660.0}          # 12:07:30 - 12:07:40
    assert rb.topic_ids_in_block(block, [{"id": 7, "time_range": "12:07 – 12:07"}]) == ["7"]


def test_the_parser_is_photo_bindings_and_accepts_its_dash_forms():
    block = {"start": 0.0, "end": 86399.0}
    rows = [{"id": "hyphen", "time_range": "08:19 - 08:48"},
            {"id": "en", "time_range": "08:19 – 08:48"},
            {"id": "em", "time_range": "08:19 — 08:48"}]
    assert rb.topic_ids_in_block(block, rows) == ["hyphen", "en", "em"]
    assert rb.parse_time_range is __import__("photo_binding").parse_time_range
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_recording_blocks.py -q
```

Expected: `1 error` during collection — `ModuleNotFoundError: No module named 'recording_blocks'`.

- [ ] **Step 3: Write the module**

Create `src/recording_blocks.py`:

```python
"""Recording blocks: a day's recorded segments merged on silence. PURE -- no I/O.

Design: docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md
§5.4/§5.5 as amended by §11 F1 and F9.

lambda_recording_segments stores raw segments; org-api's GET /sessions is the only
caller of this module and merges at read time, so a threshold change reaches every
day, finished or not (F9). Deleted recordings and wholly-excluded sessions are
filtered here at read time, never in storage (F1).

All times are seconds since midnight on the device's wall clock -- the clock
transcript filenames and topics.time_range carry. Nothing here converts timezones.
"""
from photo_binding import parse_time_range

_LAST_SECOND_OF_DAY = 86399


def _hhmm(seconds):
    """Floor to the minute. Clamped to 23:59: a segment whose VAD offset carries it past
    midnight keeps its raw numbers but must not render as "24:03"."""
    s = min(max(int(seconds), 0), _LAST_SECOND_OF_DAY)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_segments(segments, gap_seconds, long_block_seconds):
    """Merge segments whose silence between them is <= gap_seconds.

    Returns blocks ordered by start:
      {"from": "HH:MM", "to": "HH:MM", "start": float, "end": float, "minutes": int,
       "selectable_as_whole": bool, "session_ids": [sorted unique non-null]}

    `selectable_as_whole` is end - start <= long_block_seconds (D1: a long block is
    chosen by its topics, not as a whole). A segment without numeric start/end is
    ignored rather than guessed.
    """
    ordered = sorted(
        (s for s in segments if _is_number(s.get("start")) and _is_number(s.get("end"))),
        key=lambda s: (float(s["start"]), float(s["end"])),
    )
    blocks = []
    current = None
    for seg in ordered:
        start = float(seg["start"])
        end = max(float(seg["end"]), start)
        if current is not None and start - current["end"] <= gap_seconds:
            current["end"] = max(current["end"], end)
        else:
            current = {"start": start, "end": end, "sids": set()}
            blocks.append(current)
        if seg.get("session_id"):
            current["sids"].add(seg["session_id"])
    return [{
        "from": _hhmm(b["start"]),
        "to": _hhmm(b["end"]),
        "start": b["start"],
        "end": b["end"],
        "minutes": int(round((b["end"] - b["start"]) / 60)),
        "selectable_as_whole": (b["end"] - b["start"]) <= long_block_seconds,
        "session_ids": sorted(b["sids"]),
    } for b in blocks]


def _tombstone_candidates(segment):
    """Every key a recording tombstone could name for this segment.

    The live tombstone shape is `extractions/{folder}/{date}/sid{32hex}` (a chunk
    session). A legacy whole-file recording's base is its filename minus `.json` and
    minus the `_off` suffix -- the rule lambda_extract_session.session_base_from_key
    applies -- so that is offered too. The transcript key itself is included so a
    tombstone written against the raw key also matches.
    """
    key = segment.get("key") or ""
    out = [key] if key else []
    parts = key.split("/")
    if len(parts) == 4 and parts[0] == "transcripts":
        folder, date, name = parts[1], parts[2], parts[3]
        sid = segment.get("session_id")
        if sid:
            out.append(f"extractions/{folder}/{date}/sid{sid}")
        elif name.endswith(".json"):
            out.append(f"extractions/{folder}/{date}/{name[:-len('.json')].split('_off')[0]}")
    return out


def filter_segments(segments, excluded_session_ids, deleted_prefixes):
    """Drop what the picker must not advertise (F1), at read time.

    - a segment whose session_id is in `excluded_session_ids` (a session all of whose
      topics were excluded by build_day_sessions);
    - a segment any of whose tombstone candidates starts with a deleted source prefix
      (redactions.deleted_source_prefixes -- prefix match, as DELETED_SOURCE_PREDICATE).
    """
    excluded = set(excluded_session_ids or ())
    prefixes = [p for p in (deleted_prefixes or ()) if p]
    kept = []
    for seg in segments:
        if seg.get("session_id") and seg["session_id"] in excluded:
            continue
        candidates = _tombstone_candidates(seg)
        if any(c.startswith(p) for c in candidates for p in prefixes):
            continue
        kept.append(seg)
    return kept


def topic_ids_in_block(block, topic_rows):
    """Ids (as str) of the topic rows whose time_range overlaps the block.

    time_range is minute precision ("11:30 – 11:36"), so a topic covers from the start
    of its first minute to the end of its last: "12:07 – 12:07" is 12:07:00-12:07:59,
    not a zero-width instant. Overlap, not containment -- the frontend's window rule.
    A topic whose range does not parse, or ends before it starts, is placed in no block.
    """
    ids = []
    for row in topic_rows:
        parsed = parse_time_range(row.get("time_range"))
        if parsed is None:
            continue
        start_min, end_min = parsed
        if end_min < start_min:
            continue
        topic_start = start_min * 60
        topic_end = end_min * 60 + 59
        if topic_start <= block["end"] and topic_end >= block["start"]:
            ids.append(str(row["id"]))
    return ids
```

- [ ] **Step 4: Run the tests to verify they pass**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_recording_blocks.py -q
```

Expected: `20 passed`.

- [ ] **Step 5: Commit**

Stage by path only (never `git add -A` in this repo). The message goes through a file so no BOM or shell quoting reaches git.

```bash
MSG="$(mktemp)"
cat > "$MSG" <<'EOF'
feat(org-api): merge recorded segments into recording blocks

Pure module for the read side of recording blocks (design 2026-09-15
§5.4/§5.5, F1, F9): merge segments on a configurable silence gap, drop
deleted and wholly-excluded sessions at read time, and place topics by
minute-precision overlap using photo_binding.parse_time_range.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M
EOF
git -C C:/Users/camil/Dropbox/fs-blocks add \
  src/recording_blocks.py \
  tests/unit/test_recording_blocks.py
git -C C:/Users/camil/Dropbox/fs-blocks commit -F "$MSG"
git -C C:/Users/camil/Dropbox/fs-blocks show --stat --oneline HEAD
```

Expected: one new commit whose `--stat` lists exactly: `src/recording_blocks.py`, `tests/unit/test_recording_blocks.py`.

### Task 3: The compute function (`lambda_recording_segments.py`)

**Files:**
- Create: `src/lambda_recording_segments.py`
- Test: `tests/unit/test_recording_segments_compute.py`

**Interfaces:**
- Consumes:
  - Task 1: `day_recording_segments.get`, `upsert_monotonic`, `mark_dirty`, `list_dirty` (signatures above).
  - Task 2 (seam test only): `recording_blocks.merge_segments`, `recording_blocks.filter_segments`.
  - `repositories.users.get_by_folder_name_global(conn, folder_name) -> dict | None` (`folder_name` is globally unique, migration 0012).
  - `transcript_utils.extract_base_time_from_filename(filename) -> datetime | None`, `extract_vad_offsets_from_filename(filename) -> tuple[float, float]` (`(0.0, 0.0)` when absent), `extract_session_id_from_filename(filename) -> str | None`.
  - `sweep_state.mark_pending(stage) -> bool`, `clear_pending(stage) -> bool`, `is_pending(stage) -> bool` (fails open). `FLAG_KEY` is passed as `stage`, giving the DynamoDB item `SWEEP_STATE#RECORDING_BLOCKS#{STAGE}`.
  - `db.connection.get_connection(dsn=None, autocommit=False)` (context manager).
- Produces (module `lambda_recording_segments`; the template references `lambda_recording_segments.lambda_handler`):
  - `lambda_handler(event, context) -> dict` — `{"swept": int}`, `{"swept": 0, "skipped": "no-pending"}`, or `{"outcomes": list[str]}`.
  - `segment_from_key(key: str) -> dict | None`; `keys_from_event(event: dict) -> list[str]`; `is_schedule_event(event: dict) -> bool`; `is_safety_tick(now: datetime) -> bool`.
  - `list_day_keys(s3, bucket, folder, date) -> list[str]`; `compute_day(conn, s3, bucket, user_id, folder, date) -> dict` with keys `objects`, `segments`, `skipped`, `written`.
  - `handle_object_key(conn, s3, bucket, key, now) -> str` — one of `"ignored"`, `"skipped-unresolved"`, `"debounced-dirty"`, `"computed"`.
  - `sweep_dirty(conn, s3, bucket, limit=SWEEP_LIMIT) -> int`.
  - Constants: `DEBOUNCE_SECONDS = 30`, `SWEEP_LIMIT = 50`, `STAGE`, `FLAG_KEY = f"RECORDING_BLOCKS#{STAGE}"`, `SAFETY_WINDOW_START_MINUTE = 5`, `S3_BUCKET`.

The test module gates on `psycopg` and then imports the lambda directly. It deliberately does not use `pytest.importorskip("lambda_recording_segments")`, which would turn a missing module into a SKIP instead of the failure the next step expects.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_recording_segments_compute.py`:

```python
"""Unit: lambda_recording_segments -- segment derivation, debounce, trailing pass, handler.

The repository is replaced by an in-memory double (its SQL is exercised for real in
tests/integration/test_day_recording_segments.py); S3 by a paginating double; the
DynamoDB flag by recording stubs. The functions under test are the real ones.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("psycopg", reason="requires psycopg (installed in CI)")
import lambda_recording_segments as rs  # noqa: E402
import recording_blocks  # noqa: E402
import sweep_state  # noqa: E402

SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"
PREFIX = "transcripts/Ben_UCPK2/2026-09-02/"
REAL_KEY = PREFIX + f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json"
NOW = datetime(2026, 9, 2, 6, 0, 0, tzinfo=timezone.utc)       # minute 0: not a safety tick
USER = {"id": "u-ben", "folder_name": "Ben_UCPK2"}

# Real key shapes (device name, date, time, sid, chunk index, VAD offsets, source) for
# the constructed 2026-09-02 day, plus a Transcribe write-check object that is not a
# transcript at all.
DAY_KEYS = [PREFIX + n for n in (
    f"ben_ucpk2_2026-09-02_10-55-18_sid{SID_A}_c0001_off0.0_to5.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-05-02_sid{SID_A}_c0021_off0.0_to28.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-14-40_sid{SID_A}_c0041_off2.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-24-10_sid{SID_A}_c0061_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-33-50_sid{SID_A}_c0079_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_11-35-12_sid{SID_A}_c0081_off0.0_to20.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-14-05_sid{SID_B}_c0001_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-23-40_sid{SID_B}_c0019_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-32-55_sid{SID_B}_c0037_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-46-40_sid{SID_B}_c0065_off0.0_to25.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_17-57-30_sid{SID_C}_c0001_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_18-06-10_sid{SID_C}_c0017_off0.0_to30.0_srcwav.json",
    f"ben_ucpk2_2026-09-02_18-14-50_sid{SID_C}_c0033_off0.0_to20.0_srcwav.json",
    ".write_access_check_file.temp",
)]


class FakePaginator:
    def __init__(self, pages, calls):
        self.pages, self.calls = pages, calls

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class FakeS3:
    """list_objects_v2 over `keys`, `page_size` per page. Records every paginate call."""

    def __init__(self, keys, page_size=4):
        self.pages = [{"Contents": [{"Key": k} for k in keys[i:i + page_size]]}
                      for i in range(0, len(keys), page_size)] or [{"KeyCount": 0}]
        self.paginate_calls = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.pages, self.paginate_calls)


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _dirty_row(date, count=1):
    return {"user_id": "u-ben", "report_date": date, "folder_name": "Ben_UCPK2",
            "segments": [], "source_object_count": count, "dirty": True, "computed_at": NOW}


@pytest.fixture
def store(monkeypatch):
    """In-memory day_recording_segments with the same monotonic rule as the SQL, plus a
    recording stand-in for the sweep_state flag. `events` records flag calls and dirty
    listings in order."""
    state = {"rows": {}, "upserts": [], "dirty_marks": [], "events": [], "pending": True}

    def get(conn, user_id, report_date):
        return state["rows"].get((user_id, str(report_date)))

    def upsert_monotonic(conn, user_id, report_date, folder_name, segments, count):
        state["upserts"].append({"user_id": user_id, "report_date": str(report_date),
                                 "folder_name": folder_name, "segments": segments,
                                 "source_object_count": count})
        key = (user_id, str(report_date))
        old = state["rows"].get(key)
        if old is not None and count < old["source_object_count"]:
            return False
        state["rows"][key] = {"user_id": user_id, "report_date": str(report_date),
                              "folder_name": folder_name, "segments": segments,
                              "source_object_count": count, "dirty": False,
                              "computed_at": NOW}
        return True

    def mark_dirty(conn, user_id, report_date):
        state["dirty_marks"].append((user_id, str(report_date)))
        row = state["rows"].get((user_id, str(report_date)))
        if row is None:
            return False
        row["dirty"] = True
        return True

    def list_dirty(conn, limit=50):
        state["events"].append(("list", None))
        return [{"user_id": r["user_id"], "report_date": r["report_date"],
                 "folder_name": r["folder_name"]}
                for r in state["rows"].values() if r["dirty"]][:limit]

    monkeypatch.setattr(rs.day_recording_segments, "get", get)
    monkeypatch.setattr(rs.day_recording_segments, "upsert_monotonic", upsert_monotonic)
    monkeypatch.setattr(rs.day_recording_segments, "mark_dirty", mark_dirty)
    monkeypatch.setattr(rs.day_recording_segments, "list_dirty", list_dirty)
    monkeypatch.setattr(rs.users, "get_by_folder_name_global",
                        lambda conn, folder: dict(USER) if folder == "Ben_UCPK2" else None)
    monkeypatch.setattr(rs.sweep_state, "mark_pending",
                        lambda key, **kw: state["events"].append(("mark", key)) or True)
    monkeypatch.setattr(rs.sweep_state, "clear_pending",
                        lambda key, **kw: state["events"].append(("clear", key)) or True)
    monkeypatch.setattr(rs.sweep_state, "is_pending", lambda key, **kw: state["pending"])
    return state


# ---- segment_from_key --------------------------------------------------------

def test_a_real_chunk_key_yields_its_wall_clock_span_and_session():
    assert rs.segment_from_key(REAL_KEY) == {
        "start": 39318.0, "end": 39323.0, "session_id": SID_A, "key": REAL_KEY}


def test_the_vad_offset_moves_the_start_and_the_span_is_to_minus_off():
    key = PREFIX + f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_off1.5_to30.0_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["start"] == 63691.5 and seg["end"] == 63720.0


def test_a_batch_key_reads_as_its_first_chunk():
    key = PREFIX + f"ben_ucpk2_2026-09-02_17-41-30_sid{SID_B}_c0055_bn4_off1.5_to114.0_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["session_id"] == SID_B
    assert seg["start"] == 63691.5 and seg["end"] == 63804.0


def test_a_legacy_vad_key_has_no_session_and_keeps_its_offset():
    key = "transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34_off1465.8_to1729.8_srcwav.json"
    seg = rs.segment_from_key(key)
    assert seg["session_id"] is None
    assert seg["start"] == pytest.approx(45779.8) and seg["end"] == pytest.approx(46043.8)


def test_a_whole_file_key_has_no_length_in_its_name_so_end_equals_start():
    seg = rs.segment_from_key("transcripts/Benl1/2026-03-20/Benl1_2026-03-20_12-18-34.json")
    assert seg["start"] == 44314.0 and seg["end"] == 44314.0


def test_the_time_is_read_after_the_date_never_from_it():
    # BUG-01: a device name full of digit-dash pairs must not be read as the time.
    key = PREFIX + "cam26-02-09_2026-09-02_10-55-18_off0.0_to5.0_srcwav.json"
    assert rs.segment_from_key(key)["start"] == 39318.0


def test_names_without_a_base_time_and_non_json_objects_yield_nothing():
    assert rs.segment_from_key(PREFIX + "notes.json") is None
    assert rs.segment_from_key(PREFIX + ".write_access_check_file.temp") is None


# ---- event shapes and the flag -------------------------------------------------

def test_both_trigger_shapes_yield_the_key():
    eventbridge = {"source": "aws.s3", "detail-type": "Object Created",
                   "detail": {"object": {"key": REAL_KEY}}}
    s3_records = {"Records": [{"s3": {"object": {"key": REAL_KEY.replace("_", "%5F")}}}]}
    assert rs.keys_from_event(eventbridge) == [REAL_KEY]
    assert rs.keys_from_event(s3_records) == [REAL_KEY]


def test_the_schedule_event_is_told_apart_from_an_object_event():
    schedule = {"source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
    obj = {"source": "aws.s3", "detail-type": "Object Created",
           "detail": {"object": {"key": REAL_KEY}}}
    assert rs.is_schedule_event(schedule) is True
    assert rs.is_schedule_event(obj) is False
    assert rs.keys_from_event(schedule) == []


def test_the_flag_is_a_separate_item_from_the_finalize_sweeps():
    assert rs.FLAG_KEY == f"RECORDING_BLOCKS#{rs.STAGE}"
    assert sweep_state._pk(rs.FLAG_KEY) != sweep_state._pk(rs.STAGE)


def test_exactly_one_five_minute_tick_per_hour_is_a_safety_tick_whatever_the_phase():
    for phase in range(5):
        ticks = [datetime(2026, 9, 2, 6, m, tzinfo=timezone.utc) for m in range(phase, 60, 5)]
        assert sum(rs.is_safety_tick(t) for t in ticks) == 1, f"phase {phase}"
    # and the window contains the finalize sweep's safety minute (7)
    assert rs.is_safety_tick(datetime(2026, 9, 2, 6, 7, tzinfo=timezone.utc))


# ---- handle_object_key -------------------------------------------------------

def test_first_transcript_of_a_day_lists_every_page_and_writes_sorted_segments(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS, page_size=4)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", REAL_KEY, NOW) == "computed"
    assert s3.paginate_calls == [{"Bucket": "bkt", "Prefix": PREFIX}]
    [write] = store["upserts"]
    assert write["user_id"] == "u-ben" and write["report_date"] == "2026-09-02"
    assert write["folder_name"] == "Ben_UCPK2"
    assert write["source_object_count"] == 15                  # every listed object
    assert len(write["segments"]) == 14                        # the .temp is not a segment
    starts = [s["start"] for s in write["segments"]]
    assert starts == sorted(starts)
    assert "skipped 1 object(s) with no base time" in caplog.text
    assert "recording_segments: computed folder=Ben_UCPK2 date=2026-09-02" in caplog.text
    assert store["events"] == []                               # a computed day raises no flag


def test_an_unresolvable_folder_is_skipped_without_listing_or_guessing(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS)
    key = "transcripts/Nobody/2026-09-02/x_2026-09-02_10-00-00.json"
    assert rs.handle_object_key(FakeConn(), s3, "bkt", key, NOW) == "skipped-unresolved"
    assert s3.paginate_calls == [] and store["upserts"] == []
    assert "skipped-unresolved folder=Nobody" in caplog.text


def test_a_key_outside_the_day_layout_is_ignored_and_says_so(store, caplog):
    caplog.set_level(logging.INFO)
    s3 = FakeS3(DAY_KEYS)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", "transcripts/flat.json", NOW) == "ignored"
    assert s3.paginate_calls == []
    assert "ignored key=transcripts/flat.json" in caplog.text


def test_a_day_computed_moments_ago_is_marked_dirty_and_flagged_not_recomputed(store, caplog):
    caplog.set_level(logging.INFO)
    row = _dirty_row("2026-09-02", count=3)
    row.update(dirty=False, computed_at=NOW - timedelta(seconds=rs.DEBOUNCE_SECONDS - 1))
    store["rows"][("u-ben", "2026-09-02")] = row
    s3 = FakeS3(DAY_KEYS)
    assert rs.handle_object_key(FakeConn(), s3, "bkt", REAL_KEY, NOW) == "debounced-dirty"
    assert s3.paginate_calls == [] and store["upserts"] == []
    assert store["rows"][("u-ben", "2026-09-02")]["dirty"] is True
    assert store["events"] == [("mark", rs.FLAG_KEY)]
    assert "debounced-dirty folder=Ben_UCPK2 date=2026-09-02" in caplog.text


def test_a_day_computed_longer_ago_than_the_debounce_is_recomputed(store):
    row = _dirty_row("2026-09-02", count=3)
    row.update(dirty=False, computed_at=NOW - timedelta(seconds=rs.DEBOUNCE_SECONDS))
    store["rows"][("u-ben", "2026-09-02")] = row
    assert rs.handle_object_key(FakeConn(), FakeS3(DAY_KEYS), "bkt", REAL_KEY, NOW) == "computed"
    assert store["dirty_marks"] == []


def test_the_debounce_constant_is_thirty_seconds():
    assert rs.DEBOUNCE_SECONDS == 30


# ---- sweep_dirty ---------------------------------------------------------------

def test_the_trailing_pass_recomputes_every_dirty_day_and_clears_it(store, caplog):
    caplog.set_level(logging.INFO)
    for date in ("2026-09-02", "2026-09-03"):
        store["rows"][("u-ben", date)] = _dirty_row(date)
    s3 = FakeS3(DAY_KEYS)
    assert rs.sweep_dirty(FakeConn(), s3, "bkt") == 2
    assert [c["Prefix"] for c in s3.paginate_calls] == [
        "transcripts/Ben_UCPK2/2026-09-02/", "transcripts/Ben_UCPK2/2026-09-03/"]
    assert not any(r["dirty"] for r in store["rows"].values())
    assert "swept 2 of 2 dirty day(s)" in caplog.text


def test_one_failing_day_does_not_stop_the_trailing_pass(store, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    for date in ("2026-09-02", "2026-09-03"):
        store["rows"][("u-ben", date)] = _dirty_row(date)
    real = rs.list_day_keys

    def flaky(s3, bucket, folder, date):
        if str(date) == "2026-09-02":
            raise RuntimeError("boom")
        return real(s3, bucket, folder, date)

    monkeypatch.setattr(rs, "list_day_keys", flaky)
    assert rs.sweep_dirty(FakeConn(), FakeS3(DAY_KEYS), "bkt") == 1
    assert "sweep failed folder=Ben_UCPK2 date=2026-09-02" in caplog.text
    assert "swept 1 of 2 dirty day(s)" in caplog.text


def test_a_refused_smaller_listing_is_logged_by_the_trailing_pass(store, caplog):
    caplog.set_level(logging.INFO)
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02", count=99)
    assert rs.sweep_dirty(FakeConn(), FakeS3(DAY_KEYS), "bkt") == 1
    assert "sweep kept the larger stored row folder=Ben_UCPK2" in caplog.text
    assert store["rows"][("u-ben", "2026-09-02")]["dirty"] is True


# ---- lambda_handler --------------------------------------------------------------

SCHEDULE = {"source": "aws.events", "detail-type": "Scheduled Event"}


@pytest.fixture
def wired_handler(monkeypatch, store):
    s3 = FakeS3(DAY_KEYS)
    connections = []

    def connect(*a, **k):
        connections.append(k)
        return FakeConn()

    monkeypatch.setattr("boto3.client", lambda name, *a, **k: s3)
    monkeypatch.setattr("db.connection.get_connection", connect)
    monkeypatch.setattr(rs, "S3_BUCKET", "bkt")
    monkeypatch.setattr(rs, "_utcnow", lambda: NOW)
    return {"s3": s3, "connections": connections, "mp": monkeypatch}


def test_an_idle_tick_outside_the_safety_window_never_connects(wired_handler, store, caplog):
    caplog.set_level(logging.INFO)
    store["pending"] = False
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 0, "skipped": "no-pending"}
    assert wired_handler["connections"] == []
    assert store["events"] == []
    assert "sweep skipped (no dirty days flagged)" in caplog.text


def test_a_flagged_tick_clears_the_flag_before_listing_then_sweeps(wired_handler, store):
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02")
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 1}
    assert store["events"] == [("clear", rs.FLAG_KEY), ("list", None)]
    assert wired_handler["connections"] == [{"autocommit": True}]


def test_the_safety_tick_sweeps_even_when_the_flag_reads_idle(wired_handler, store):
    store["pending"] = False
    store["rows"][("u-ben", "2026-09-02")] = _dirty_row("2026-09-02")
    wired_handler["mp"].setattr(rs, "_utcnow", lambda: NOW.replace(minute=7))
    assert rs.lambda_handler(SCHEDULE, None) == {"swept": 1}


def test_the_handler_computes_an_object_event(wired_handler, store):
    event = {"source": "aws.s3", "detail-type": "Object Created",
             "detail": {"object": {"key": REAL_KEY}}}
    assert rs.lambda_handler(event, None) == {"outcomes": ["computed"]}
    assert len(store["upserts"]) == 1


def test_one_failing_key_does_not_sink_the_rest(wired_handler, store, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    real = rs.handle_object_key

    def flaky(conn, s3, bucket, key, now):
        if "Broken" in key:
            raise RuntimeError("boom")
        return real(conn, s3, bucket, key, now)

    monkeypatch.setattr(rs, "handle_object_key", flaky)
    event = {"Records": [
        {"s3": {"object": {"key": "transcripts/Broken/2026-09-02/a_2026-09-02_10-00-00.json"}}},
        {"s3": {"object": {"key": REAL_KEY}}}]}
    assert rs.lambda_handler(event, None) == {"outcomes": ["failed", "computed"]}
    assert "failed for key=transcripts/Broken/" in caplog.text


def test_an_event_with_no_key_says_so_and_does_not_connect(wired_handler, caplog):
    caplog.set_level(logging.INFO)
    assert rs.lambda_handler({"source": "aws.s3", "detail": {}}, None) == {"outcomes": []}
    assert wired_handler["connections"] == []
    assert "no object key in event; nothing to do" in caplog.text


# ---- seam: what the writer stores is what the reader merges ----------------------

def test_the_stored_payload_merges_into_the_0902_blocks_after_a_jsonb_round_trip(store):
    rs.handle_object_key(FakeConn(), FakeS3(DAY_KEYS), "bkt", REAL_KEY, NOW)
    stored = json.loads(json.dumps(store["upserts"][0]["segments"]))   # what jsonb gives back
    blocks = recording_blocks.merge_segments(stored, 600, 5400)
    assert [(b["from"], b["to"]) for b in blocks] == [
        ("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    assert [b["session_ids"] for b in blocks] == [[SID_A], [SID_B], [SID_C]]
    tombstoned = recording_blocks.filter_segments(
        stored, set(), [f"extractions/Ben_UCPK2/2026-09-02/sid{SID_B}"])
    assert [(b["from"], b["to"]) for b in recording_blocks.merge_segments(tombstoned, 600, 5400)] \
        == [("10:55", "11:35"), ("17:57", "18:15")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_recording_segments_compute.py -q
```

Expected: `1 error` during collection — `ModuleNotFoundError: No module named 'lambda_recording_segments'`.

- [ ] **Step 3: Write the function**

Create `src/lambda_recording_segments.py`:

```python
"""lambda_recording_segments.py -- keep day_recording_segments current, in the background.

Design: docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md
§5.4 as amended by §11 F1, F4, F8, F9.

Two triggers, one function:
  * an EventBridge "Object Created" on `transcripts/` -- recompute that (folder, date);
  * a `rate(5 minutes)` schedule -- the trailing pass: recompute every dirty day.

On a transcript: resolve the folder to a user (never guess), then DEBOUNCE -- a day
computed less than DEBOUNCE_SECONDS ago is only marked dirty, because a recording
lands a transcript roughly every 30 s and each one would otherwise take a LIST and a
slot of account concurrency shared with finalize. The trailing pass picks the mark
up, so the last transcripts of a day are never lost to the debounce (F4).

THE TRAILING PASS MUST NOT KEEP AURORA AWAKE. SecondsUntilAutoPause is 600, and a
scheduled client that connects every 300 s would pin the cluster's floor 24/7
(tests/unit/test_sweep_cadence_vs_autopause.py). So the tick connects only when this
feature's own DynamoDB flag (sweep_state, key FLAG_KEY -- a separate item from the
finalize sweep's) says a day was marked dirty, plus once an hour unconditionally in
the safety window, which heals a flag write that was lost. sweep_state fails open.

Compute is one paginated LIST and arithmetic on names: no GetObject, no model call.
Times come only from transcript_utils (BUG-01/BUG-09) -- never a hand-written regex.

In-VPC (Aurora). Its other calls are S3 ListObjectsV2 and the DynamoDB flag, both
through the VPC's existing gateway endpoints. Anything else black-holes (BUG-36).

NOT on the finalize/email path (D5): SessionActivityFunction is untouched.

Every invocation logs one line saying what happened. A guard that passes silently
cannot be told apart from one that never ran.
"""
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import sweep_state
from repositories import day_recording_segments, users
from transcript_utils import (extract_base_time_from_filename,
                              extract_session_id_from_filename,
                              extract_vad_offsets_from_filename)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# A code constant, not a Parameter: it is a load-shedding interval, not a product
# threshold, and the trailing pass bounds its effect to one schedule period.
DEBOUNCE_SECONDS = 30
SWEEP_LIMIT = 50

S3_BUCKET = os.environ.get("S3_BUCKET", "")
STAGE = os.environ.get("STAGE", "prod")
# Passed to sweep_state as its "stage", so the item is SWEEP_STATE#RECORDING_BLOCKS#{stage}:
# this feature's flag can never wake the finalize sweep, nor be cleared by it.
FLAG_KEY = f"RECORDING_BLOCKS#{STAGE}"
# The five-minute window in which one tick per hour connects regardless of the flag.
# It contains the finalize sweep's SAFETY_SWEEP_MINUTE (7): both stages share one
# cluster, and unaligned hourly passes would shrink its idle window.
SAFETY_WINDOW_START_MINUTE = 5

# Structure of the KEY only (folder, date directory). Time of day is never read here.
_DAY_KEY_RE = re.compile(r"^transcripts/([^/]+)/(\d{4}-\d{2}-\d{2})/[^/]+$")


def _utcnow():
    return datetime.now(timezone.utc)


def keys_from_event(event):
    """Object keys this invocation is about, from either trigger shape.

    EventBridge "Object Created": {detail: {object: {key}}}, key already decoded.
    S3 notification: {Records: [{s3: {object: {key}}}]}, key url-encoded.
    Same contract as session_activity._keys_from_event, copied rather than imported so
    this function does not load a finalize-path module (D5).
    """
    keys = []
    obj = (event.get("detail") or {}).get("object") or {}
    if obj.get("key"):
        keys.append(obj["key"])
    for record in event.get("Records", []) or []:
        keys.append(unquote_plus(record["s3"]["object"]["key"]))
    return keys


def is_schedule_event(event):
    """The rate(5 minutes) trailing pass, as opposed to an object event."""
    return event.get("source") == "aws.events" or event.get("detail-type") == "Scheduled Event"


def is_safety_tick(now):
    """Exactly one tick of a rate(5 minutes) schedule falls in this window each hour,
    whatever minute the schedule happens to be phased on."""
    return SAFETY_WINDOW_START_MINUTE <= now.minute < SAFETY_WINDOW_START_MINUTE + 5


def segment_from_key(key):
    """One transcript object's segment, or None when its name yields no base time.

    start = filename base time (seconds since midnight) + VAD offset start
    end   = start + (offset end - offset start)
    A whole-file transcript has no offsets, so its end equals its start: its length is
    not in its name and is not guessed. A batch (`_bn{K}`) reads as its first chunk,
    which is what batch_stitch.build_batch_name guarantees; its span is the audio kept,
    which can be shorter than the wall clock it covers -- immaterial at a 600 s gap.
    """
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".json"):
        return None
    base = extract_base_time_from_filename(name)
    if base is None:
        return None
    off_start, off_end = extract_vad_offsets_from_filename(name)
    start = base.hour * 3600 + base.minute * 60 + base.second + off_start
    end = start + max(0.0, off_end - off_start)
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "session_id": extract_session_id_from_filename(name),
        "key": key,
    }


def list_day_keys(s3, bucket, folder, date):
    """Every object key under transcripts/{folder}/{date}/, across all pages."""
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"transcripts/{folder}/{date}/"):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    return keys


def compute_day(conn, s3, bucket, user_id, folder, date):
    """LIST the day, derive segments, write monotonically. Returns a summary dict."""
    keys = list_day_keys(s3, bucket, folder, date)
    segments = []
    skipped = 0
    for key in keys:
        seg = segment_from_key(key)
        if seg is None:
            skipped += 1
            continue
        segments.append(seg)
    if skipped:
        logger.info("recording_segments: %s/%s skipped %d object(s) with no base time",
                    folder, date, skipped)
    segments.sort(key=lambda s: (s["start"], s["key"]))
    written = day_recording_segments.upsert_monotonic(
        conn, user_id, date, folder, segments, len(keys))
    return {"objects": len(keys), "segments": len(segments),
            "skipped": skipped, "written": written}


def handle_object_key(conn, s3, bucket, key, now):
    """One transcript landed. Returns the outcome: "ignored", "skipped-unresolved",
    "debounced-dirty" or "computed". `now` is an aware UTC datetime."""
    m = _DAY_KEY_RE.match(key)
    if not m:
        logger.info("recording_segments: ignored key=%s (not transcripts/{folder}/{date}/{file})",
                    key)
        return "ignored"
    folder, date = m.group(1), m.group(2)
    user = users.get_by_folder_name_global(conn, folder)
    if user is None:
        logger.warning("recording_segments: skipped-unresolved folder=%s date=%s "
                       "(no users row; not guessing)", folder, date)
        return "skipped-unresolved"
    row = day_recording_segments.get(conn, user["id"], date)
    if row is not None and (now - row["computed_at"]).total_seconds() < DEBOUNCE_SECONDS:
        day_recording_segments.mark_dirty(conn, user["id"], date)
        # AFTER the row is marked (autocommit), so a sweep that reads the flag always
        # finds the row it was raised for.
        sweep_state.mark_pending(FLAG_KEY)
        logger.info("recording_segments: debounced-dirty folder=%s date=%s", folder, date)
        return "debounced-dirty"
    result = compute_day(conn, s3, bucket, user["id"], folder, date)
    logger.info("recording_segments: computed folder=%s date=%s objects=%d segments=%d "
                "skipped=%d written=%s", folder, date, result["objects"],
                result["segments"], result["skipped"], result["written"])
    return "computed"


def sweep_dirty(conn, s3, bucket, limit=SWEEP_LIMIT):
    """The trailing pass. Recompute every dirty day; one failing day never stops the rest."""
    rows = day_recording_segments.list_dirty(conn, limit)
    done = 0
    for row in rows:
        try:
            result = compute_day(conn, s3, bucket, row["user_id"], row["folder_name"],
                                 row["report_date"])
            done += 1
            if not result["written"]:
                logger.info("recording_segments: sweep kept the larger stored row "
                            "folder=%s date=%s objects=%d", row["folder_name"],
                            row["report_date"], result["objects"])
        except Exception:
            logger.exception("recording_segments: sweep failed folder=%s date=%s",
                             row["folder_name"], row["report_date"])
    logger.info("recording_segments: swept %d of %d dirty day(s)", done, len(rows))
    return done


def lambda_handler(event, context):
    import boto3

    from db.connection import get_connection

    s3 = boto3.client("s3")
    if is_schedule_event(event):
        now = _utcnow()
        if not is_safety_tick(now) and not sweep_state.is_pending(FLAG_KEY):
            # Must be logged: without this line the skip path is unverifiable.
            logger.info("recording_segments: sweep skipped (no dirty days flagged)")
            return {"swept": 0, "skipped": "no-pending"}
        # Cleared BEFORE listing: a day marked while this pass runs raises the flag
        # again and is picked up next tick, instead of being cleared away unseen.
        sweep_state.clear_pending(FLAG_KEY)
        with get_connection(autocommit=True) as conn:
            return {"swept": sweep_dirty(conn, s3, S3_BUCKET)}
    keys = keys_from_event(event)
    if not keys:
        logger.info("recording_segments: no object key in event; nothing to do")
        return {"outcomes": []}
    now = _utcnow()
    outcomes = []
    with get_connection(autocommit=True) as conn:
        for key in keys:
            try:
                outcomes.append(handle_object_key(conn, s3, S3_BUCKET, key, now))
            except Exception:
                logger.exception("recording_segments: failed for key=%s", key)
                outcomes.append("failed")
    return {"outcomes": outcomes}
```

- [ ] **Step 4: Run the tests, then the neighbouring guard suites**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_recording_segments_compute.py -q
python -m pytest tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_template_pgdatabase.py tests/unit/test_sweep_cadence_vs_autopause.py tests/unit/test_template_schedule_state.py tests/unit/test_org_api_sessions.py tests/unit/test_session_activity.py -q
```

Expected: `27 passed`, then `137 passed` (nothing in the template references the function yet).

- [ ] **Step 5: Commit**

Stage by path only (never `git add -A` in this repo). The message goes through a file so no BOM or shell quoting reaches git.

```bash
MSG="$(mktemp)"
cat > "$MSG" <<'EOF'
feat(pipeline): compute a day's recorded segments in the background

lambda_recording_segments LISTs transcripts/{folder}/{date}/ on each
transcript landing, derives one segment per object with transcript_utils,
and upserts monotonically. A day computed under 30 s ago is only marked
dirty; a rate(5 minutes) trailing pass recomputes dirty days and connects
to Aurora only when its own sweep_state flag is set, plus an hourly safety
tick, so scale-to-zero still works (design 2026-09-15 §5.4, F4). Includes
a seam test feeding the stored payload into recording_blocks.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M
EOF
git -C C:/Users/camil/Dropbox/fs-blocks add \
  src/lambda_recording_segments.py \
  tests/unit/test_recording_segments_compute.py
git -C C:/Users/camil/Dropbox/fs-blocks commit -F "$MSG"
git -C C:/Users/camil/Dropbox/fs-blocks show --stat --oneline HEAD
```

Expected: one new commit whose `--stat` lists exactly: `src/lambda_recording_segments.py`, `tests/unit/test_recording_segments_compute.py`.

### Task 4: Template, workflows and the guard tests that move with them

**Files:**
- Test (create): `tests/unit/test_template_recording_segments.py`
- Test (modify): `tests/unit/test_template_workflow_parameter_wiring.py` (append after its last test)
- Test (modify): `tests/unit/test_sweep_cadence_vs_autopause.py`
- Modify: `src/template.yaml` (Parameters before `DeviceAnnouncementPatterns`; Conditions after `ShouldEnableFinalize`; new resource directly after `SessionActivityFunction`; `OrgApiFunction` env after `GRADED_ROLES`)
- Modify: `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml` (after the `EvidenceFloorTokens=` line)
- Modify: `tests/unit/test_template_pgdatabase.py` (count 18 → 19)

**Interfaces:**
- Consumes: Task 3's handler path `lambda_recording_segments.lambda_handler`, and the literal source strings `sweep_state.is_pending(FLAG_KEY)`, `is_safety_tick(now)` and `no-pending` (pinned by the cadence test).
- Produces:
  - Parameters `EnableRecordingBlocks` (`'false'`, boolean), `ReportBlockGapSeconds` (`'600'`), `ReportLongBlockSeconds` (`'5400'`); Condition `ShouldEnableRecordingBlocks`.
  - Resource `RecordingSegmentsFunction` (FunctionName `fieldsight-test-recording-segments` / `fieldsight-prod-recording-segments`) with events `TranscriptLanded` (EventBridgeRule) and `DirtyDaySweep` (Schedule `rate(5 minutes)`), both `State: !If [ShouldEnableRecordingBlocks, ENABLED, DISABLED]`.
  - `OrgApiFunction` env `REPORT_BLOCK_GAP_SECONDS: !Ref ReportBlockGapSeconds`, `REPORT_LONG_BLOCK_SECONDS: !Ref ReportLongBlockSeconds` (consumed by Task 5).
  - Repo variables read by the workflows: `TEST_ENABLE_RECORDING_BLOCKS`, `TEST_REPORT_BLOCK_GAP_SECONDS`, `TEST_REPORT_LONG_BLOCK_SECONDS`, and the `PROD_` equivalents.

The boolean `EnableRecordingBlocks` is picked up automatically by the existing `test_every_boolean_toggle_is_reachable_from_a_repo_variable`. The template is LF, so its multi-line anchors are safe.

- [ ] **Step 1: Write the failing template-shape test**

Create `tests/unit/test_template_recording_segments.py`:

```python
"""Unit: RecordingSegmentsFunction is declared the way design §5.4 and this repo's traps require.

Parses the template text by resource block rather than grepping the whole file:
`State:`, `s3:ListBucket` and `transcripts/` all appear under other resources.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {
    "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml"),
    "test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
}
GATE = "State: !If [ShouldEnableRecordingBlocks, ENABLED, DISABLED]"


def _text():
    with open(TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def _function_block(text, name):
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _event_block(block, event_name):
    """One event's body: from `        {event_name}:` to the next 8-space-indented key."""
    start = block.index(f"\n        {event_name}:\n")
    nxt = re.search(r"\n        [A-Za-z][A-Za-z0-9]*:\n", block[start + 1:])
    return block[start:start + 1 + nxt.start()] if nxt else block[start:]


def test_it_is_an_in_vpc_database_function_with_capped_concurrency():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "Condition: HasDb" in block
    assert "Handler: lambda_recording_segments.lambda_handler" in block
    assert "- !Ref PsycopgLayer" in block
    assert "VpcConfig:" in block and "SubnetIds: !Ref DbSubnetIds" in block
    assert "PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'" in block
    assert "S3_BUCKET: !Ref IngestBucketName" in block
    assert re.search(r"^\s*ReservedConcurrentExecutions: 2\s*$", block, re.M)


def test_the_object_rule_watches_transcripts_and_follows_the_switch():
    event = _event_block(_function_block(_text(), "RecordingSegmentsFunction"), "TranscriptLanded")
    assert "Type: EventBridgeRule" in event
    assert GATE in event
    assert "- Object Created" in event
    assert "- !Ref IngestBucketName" in event
    assert "- prefix: transcripts/" in event


def test_the_trailing_pass_runs_every_five_minutes_on_the_same_switch():
    event = _event_block(_function_block(_text(), "RecordingSegmentsFunction"), "DirtyDaySweep")
    assert "Type: Schedule" in event
    assert "Schedule: rate(5 minutes)" in event
    assert GATE in event
    assert not re.search(r"^\s*Enabled:", event, re.M)


def test_it_may_list_transcripts_and_read_or_write_no_object():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "Action: s3:ListBucket" in block
    assert re.search(r"s3:prefix:\s*\n\s*- transcripts/\*\s*\n", block)
    for forbidden in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                      "lambda:InvokeFunction", "secretsmanager:GetSecretValue"):
        assert forbidden not in block, f"RecordingSegmentsFunction must not be granted {forbidden}"


def test_its_only_dynamodb_access_is_the_sweep_flag_in_the_items_table():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "SWEEP_STATE_TABLE: !Ref ItemsTableName" in block
    assert "STAGE: !Ref Stage" in block
    assert re.search(r"- DynamoDBCrudPolicy:\s*\n\s*TableName: !Ref ItemsTableName", block)
    assert block.count("DynamoDB") == block.count("DynamoDBCrudPolicy") + block.count(
        "DynamoDB is reachable"), "no other DynamoDB grant"


def test_it_is_given_no_threshold():
    block = _function_block(_text(), "RecordingSegmentsFunction")
    assert "REPORT_BLOCK_GAP_SECONDS:" not in block
    assert "REPORT_LONG_BLOCK_SECONDS:" not in block


def test_the_finalize_path_function_is_untouched():
    # Comment lines are dropped: a block runs to the next resource NAME, so the
    # banner comment above RecordingSegmentsFunction falls inside this one.
    block = _function_block(_text(), "SessionActivityFunction")
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "recording" not in code.lower()
    assert "Handler: session_activity.lambda_handler" in block


def test_the_switch_is_a_boolean_parameter_behind_a_condition():
    text = _text()
    assert "ShouldEnableRecordingBlocks: !Equals [!Ref EnableRecordingBlocks, 'true']" in text
    param = re.search(r"\n  EnableRecordingBlocks:\n(.*?)(?=\n  \w+:\n)", text, re.S).group(1)
    assert "Default: 'false'" in param
    assert "AllowedValues: ['true', 'false']" in param
```

- [ ] **Step 2: Append the recording-block wiring assertions to the parameter-wiring test**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_sweep_cadence_vs_autopause.py`, find this exact text (it occurs exactly once):

```python
GATED_EXCEPTIONS = {"FinalizeSweepFunction"}
```

Replace it with:

```python
# RecordingSegmentsFunction: its rate(5 minutes) trailing pass reads its own sweep_state
# flag before connecting, with an hourly safety window (design 2026-09-15 §5.4, F4).
GATED_EXCEPTIONS = {"FinalizeSweepFunction", "RecordingSegmentsFunction"}
```

- [ ] **Step 3: Exempt the gated trailing pass in the auto-pause test**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_sweep_cadence_vs_autopause.py`, find this exact text (it occurs exactly once):

```python
    assert "no-pending" in src, "the skip path must be observable in logs"
```

Replace it with:

```python
    assert "no-pending" in src, "the skip path must be observable in logs"


def test_the_recording_blocks_exception_actually_gates_its_connection():
    """RecordingSegmentsFunction is exempt on the same terms as the finalize sweep: its
    tick reads its own flag before connecting, and connects unconditionally only in an
    hourly safety window. If either goes, the exemption is a lie."""
    src = _read(os.path.join(HERE, "..", "..", "src", "lambda_recording_segments.py"))
    assert "sweep_state.is_pending(FLAG_KEY)" in src
    assert "is_safety_tick(now)" in src
    assert "no-pending" in src, "the skip path must be observable in logs"
```

- [ ] **Step 4: Pin that the exemption is earned (auto-pause test)**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_template_workflow_parameter_wiring.py`, find this exact text (it occurs exactly once):

```python
            "collapse ON for that stage")
```

Replace it with:

```python
            "collapse ON for that stage")


# ---- the recording-block thresholds ------------------------------------
#
# Design 2026-09-15 §5.5 as amended by review finding F9: the compute function
# stores raw segments and org-api merges them at read time, so org-api is the
# ONLY reader. A second reader is the GROUP_MERGE_CAP hazard above one step
# worse -- two functions drawing different blocks would log nothing at all.

_BLOCK_TUNABLES = {
    # env var                   : (template Parameter, functions that read it)
    "REPORT_BLOCK_GAP_SECONDS":  ("ReportBlockGapSeconds", ("OrgApiFunction",)),
    "REPORT_LONG_BLOCK_SECONDS": ("ReportLongBlockSeconds", ("OrgApiFunction",)),
}


def test_the_block_tunables_exist_as_template_parameters():
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, _) in _BLOCK_TUNABLES.items():
        assert re.search(rf"\n  {param}:\n", text), \
            f"{env} has no {param} Parameter — it can only ever hold its code default"


def test_every_function_that_reads_a_block_tunable_is_given_it():
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, fns) in _BLOCK_TUNABLES.items():
        for fn in fns:
            assert f"{env}: !Ref {param}" in _function_block(text, fn), \
                f"{fn} reads {env} but is not given it"


def test_both_workflows_pass_the_block_tunables():
    for env_name in ("prod", "test"):
        for _, (param, _) in _BLOCK_TUNABLES.items():
            assert param in _overrides(WORKFLOWS[env_name]), \
                (f"{env_name} does not pass {param}; the Parameter holds its "
                 f"default forever and a re-tuned threshold cannot be applied")


def test_org_api_is_the_only_function_given_a_block_tunable():
    text = open(TEMPLATE, encoding="utf-8").read()
    for env, (param, _) in _BLOCK_TUNABLES.items():
        assert text.count(f"{env}: !Ref {param}") == 1, (
            f"{env} is given to more than one function; org-api must be the single reader")


def test_recording_blocks_default_off_on_prod_and_on_on_test():
    for env_name, fallback in (("prod", "'false'"), ("test", "'true'")):
        text = open(WORKFLOWS[env_name], encoding="utf-8").read()
        line = [ln for ln in text.splitlines() if "EnableRecordingBlocks=" in ln]
        assert len(line) == 1, f"{env_name}: expected one line, found {len(line)}"
        assert fallback in line[0], (
            f"{env_name} must fall back to {fallback} for EnableRecordingBlocks")
```

- [ ] **Step 5: Run the tests to verify they fail**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_template_recording_segments.py -q
python -m pytest tests/unit/test_template_workflow_parameter_wiring.py -q
python -m pytest tests/unit/test_sweep_cadence_vs_autopause.py -q
```

Expected: `7 failed, 1 passed` (six `ValueError: substring not found` because the resource is absent, and the condition assertion; the finalize-isolation test already passes); `5 failed, 56 passed`; `6 passed` (the gate test reads Task 3's source, which already exists).

- [ ] **Step 6: Add the three Parameters (template)**

In `src/template.yaml`, find this exact text (it occurs exactly once):

```yaml
  DeviceAnnouncementPatterns:
```

Replace it with:

```yaml
  EnableRecordingBlocks:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      Whether RecordingSegmentsFunction keeps day_recording_segments current: its
      transcripts/ Object Created rule and its rate(5 minutes) trailing pass both
      take their State from this. Off means both rules are DISABLED, no row is
      ever written, and GET /api/org/sessions carries no recording_blocks key, so
      the picker renders sessions exactly as before. Never on the finalize/email
      path (design D5): the only coupling to email latency is account
      concurrency, which the function caps at 2.

  ReportBlockGapSeconds:
    Type: String
    Default: '600'
    Description: >-
      Silence, in seconds, that splits a day's recorded segments into separate
      recording blocks in the report picker (design D2). Read by org-api only,
      per request, against the raw segments stored per day, so a change reaches
      every day including finished ones (review finding F9). Separate from
      session_scope.SESSION_GAP_MINUTES, which this does not touch (D4).

  ReportLongBlockSeconds:
    Type: String
    Default: '5400'
    Description: >-
      A recording block longer than this many seconds is not selectable as a
      whole; its topics are the unit instead (design D1/D2, 90 minutes). Read by
      org-api only, for the same single-reader reason as ReportBlockGapSeconds.

  DeviceAnnouncementPatterns:
```

- [ ] **Step 7: Add the Condition (template)**

In `src/template.yaml`, find this exact text (it occurs exactly once):

```yaml
  ShouldEnableFinalize: !Equals [!Ref EnableFinalize, 'true']
```

Replace it with:

```yaml
  ShouldEnableFinalize: !Equals [!Ref EnableFinalize, 'true']
  ShouldEnableRecordingBlocks: !Equals [!Ref EnableRecordingBlocks, 'true']
```

- [ ] **Step 8: Add `RecordingSegmentsFunction` directly after `SessionActivityFunction` (template)**

In `src/template.yaml`, find this exact text (it occurs exactly once):

```yaml
                    - suffix: _vad_metadata.json
```

Replace it with:

```yaml
                    - suffix: _vad_metadata.json

  # ----------------------------------------------------------
  # Recording blocks (design 2026-09-15 §5.4, review F1/F4/F8/F9). Keeps
  # day_recording_segments current from the transcript stream so the report
  # picker can offer "10:55-11:35" as one stretch. In-VPC for Aurora; its other
  # calls are S3 ListObjectsV2 and one DynamoDB flag, both through the VPC's
  # existing gateway endpoints (BUG-36: nothing else is reachable from here).
  # Deliberately NOT folded into SessionActivityFunction above: that function
  # feeds finalize, and this one must never sit on the email path (D5).
  # ----------------------------------------------------------
  RecordingSegmentsFunction:
    Type: AWS::Serverless::Function
    Condition: HasDb
    Properties:
      FunctionName: !Sub ["${P}-recording-segments", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]
      CodeUri: src/
      Handler: lambda_recording_segments.lambda_handler
      Timeout: 60
      MemorySize: 256
      # Every transcript landing invokes this, and account concurrency is shared with
      # finalize. 2 bounds what a burst can take. A throttled async invocation is queued
      # and retried by Lambda and logs NOTHING while it waits -- the Throttles metric is
      # the only signal. The rate(5 minutes) pass below heals any day left dirty.
      ReservedConcurrentExecutions: 2
      Layers:
        - !Ref PsycopgLayer
      VpcConfig:
        SubnetIds: !Ref DbSubnetIds
        SecurityGroupIds:
          - !ImportValue
            Fn::Sub: "${DbStackName}-LambdaSG"
      Environment:
        Variables:
          PGHOST: !ImportValue
            Fn::Sub: "${DbStackName}-ClusterEndpoint"
          PGDATABASE: !If [HasPgDatabaseOverride, !Ref PgDatabase, !ImportValue {"Fn::Sub": "${DbStackName}-DbName"}]
          PGUSER: postgres
          PGPASSWORD: !Sub '{{resolve:secretsmanager:${DbSecretArn}:SecretString:password}}'
          S3_BUCKET: !Ref IngestBucketName
          # The trailing pass's "any dirty day?" flag -- its own item in the items table
          # (sweep_state key RECORDING_BLOCKS#{stage}), so an idle tick skips Aurora and
          # the cluster can still auto-pause (test_sweep_cadence_vs_autopause).
          SWEEP_STATE_TABLE: !Ref ItemsTableName
          STAGE: !Ref Stage
          # No REPORT_BLOCK_* here, on purpose: this function stores raw segments and
          # org-api is the single reader of both thresholds (F9).
      Policies:
        - VPCAccessPolicy: {}
        # In-VPC, but DynamoDB is reachable through the existing gateway endpoint
        # (vpce-01233d5b756ffefcb) -- no NAT, no new paid endpoint. The same grant
        # SessionActivityFunction carries for the finalize flag.
        - DynamoDBCrudPolicy:
            TableName: !Ref ItemsTableName
        # Names only. Without ListBucket a missing key answers 403, not 404; this function
        # never GETs, so it is granted nothing else under transcripts/.
        - Version: '2012-10-17'
          Statement:
            - Effect: Allow
              Action: s3:ListBucket
              Resource: !Sub arn:aws:s3:::${IngestBucketName}
              Condition:
                StringLike:
                  s3:prefix:
                    - transcripts/*
      Events:
        # Template-declared EventBridge rule, the SessionActivityFunction pattern -- not
        # a bucket notification (BUG-33: SAM cannot attach S3 events to an external bucket).
        TranscriptLanded:
          Type: EventBridgeRule
          Properties:
            State: !If [ShouldEnableRecordingBlocks, ENABLED, DISABLED]
            Pattern:
              source:
                - aws.s3
              detail-type:
                - Object Created
              detail:
                bucket:
                  name:
                    - !Ref IngestBucketName
                object:
                  key:
                    - prefix: transcripts/
        # The trailing pass (F4): recomputes every day the debounce marked dirty. Connects
        # to Aurora only when its flag is set, plus one hourly safety tick.
        DirtyDaySweep:
          Type: Schedule
          Properties:
            Schedule: rate(5 minutes)
            Description: Recording blocks trailing pass - recomputes days the debounce marked dirty.
            State: !If [ShouldEnableRecordingBlocks, ENABLED, DISABLED]
```

- [ ] **Step 9: Give the thresholds to `OrgApiFunction` only (template)**

In `src/template.yaml`, find this exact text (it occurs exactly once):

```yaml
          GRADED_ROLES: !Ref GradedRoles
```

Replace it with:

```yaml
          GRADED_ROLES: !Ref GradedRoles
          # Recording-block thresholds (design 2026-09-15 §5.5 as amended by F9). org-api
          # is the ONLY function given these: it merges stored segments at read time.
          REPORT_BLOCK_GAP_SECONDS: !Ref ReportBlockGapSeconds
          REPORT_LONG_BLOCK_SECONDS: !Ref ReportLongBlockSeconds
```

- [ ] **Step 10: Pass the three Parameters on TEST (`deploy.yml`)**

In `.github/workflows/deploy.yml`, find this exact text (it occurs exactly once):

```yaml
              "EvidenceFloorTokens=${{ vars.TEST_EVIDENCE_FLOOR_TOKENS || '5' }}" \
```

Replace it with:

```yaml
              "EvidenceFloorTokens=${{ vars.TEST_EVIDENCE_FLOOR_TOKENS || '5' }}" \
              "EnableRecordingBlocks=${{ vars.TEST_ENABLE_RECORDING_BLOCKS || 'true' }}" \
              "ReportBlockGapSeconds=${{ vars.TEST_REPORT_BLOCK_GAP_SECONDS || '600' }}" \
              "ReportLongBlockSeconds=${{ vars.TEST_REPORT_LONG_BLOCK_SECONDS || '5400' }}" \
```

- [ ] **Step 11: Pass the three Parameters on prod (`deploy-prod.yml`)**

In `.github/workflows/deploy-prod.yml`, find this exact text (it occurs exactly once):

```yaml
              "EvidenceFloorTokens=${{ vars.PROD_EVIDENCE_FLOOR_TOKENS || '5' }}" \
```

Replace it with:

```yaml
              "EvidenceFloorTokens=${{ vars.PROD_EVIDENCE_FLOOR_TOKENS || '5' }}" \
              "EnableRecordingBlocks=${{ vars.PROD_ENABLE_RECORDING_BLOCKS || 'false' }}" \
              "ReportBlockGapSeconds=${{ vars.PROD_REPORT_BLOCK_GAP_SECONDS || '600' }}" \
              "ReportLongBlockSeconds=${{ vars.PROD_REPORT_LONG_BLOCK_SECONDS || '5400' }}" \
```

- [ ] **Step 12: Record why the in-VPC function count moved (pgdatabase test comment)**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_template_pgdatabase.py`, find this exact text (it occurs exactly once):

```python
    # 18 again, and for the opposite reason to last time:
```

Replace it with:

```python
    # 19 with RecordingSegmentsFunction (recording blocks, design 2026-09-15 §5.4).
    # 18 again, and for the opposite reason to last time:
```

- [ ] **Step 13: Move the pinned count to 19 (pgdatabase test)**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_template_pgdatabase.py`, find this exact text (it occurs exactly once):

```python
    assert guarded == 18, f"expected 18 guarded PGDATABASE, found {guarded}"
```

Replace it with:

```python
    assert guarded == 19, f"expected 19 guarded PGDATABASE, found {guarded}"
```

- [ ] **Step 14: Run the tests, the guard suites and the pinned linter**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_template_recording_segments.py -q
python -m pytest tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_template_pgdatabase.py tests/unit/test_sweep_cadence_vs_autopause.py tests/unit/test_template_schedule_state.py tests/unit/test_org_api_sessions.py tests/unit/test_session_activity.py -q
pip install 'cfn-lint==1.53.3'
python -c "import sys; from cfnlint.runner import main; sys.argv=['cfn-lint','src/template.yaml']; main()"; echo "exit=$?"
```

Expected: `8 passed`; `143 passed`; cfn-lint prints nothing and `exit=0` (the same result as on the untouched template). Use the pinned 1.53.3 — CI pins it because 1.54.0 rejects this template.

- [ ] **Step 15: Commit**

Stage by path only (never `git add -A` in this repo). The message goes through a file so no BOM or shell quoting reaches git.

```bash
MSG="$(mktemp)"
cat > "$MSG" <<'EOF'
feat(infra): wire RecordingSegmentsFunction and the block thresholds

New in-VPC RecordingSegmentsFunction: EventBridge rule on transcripts/
Object Created plus a rate(5 minutes) trailing pass, both gated by the new
EnableRecordingBlocks switch (TEST on, prod off by default). ListBucket on
transcripts/* only, the items-table flag for the gated sweep, and
ReservedConcurrentExecutions 2. ReportBlockGapSeconds (600) and
ReportLongBlockSeconds (5400) are wired in all three places and given to
OrgApiFunction only (design 2026-09-15 §5.5, F9). Not on the finalize
path: SessionActivityFunction is unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M
EOF
git -C C:/Users/camil/Dropbox/fs-blocks add \
  src/template.yaml \
  .github/workflows/deploy.yml \
  .github/workflows/deploy-prod.yml \
  tests/unit/test_template_recording_segments.py \
  tests/unit/test_template_workflow_parameter_wiring.py \
  tests/unit/test_template_pgdatabase.py \
  tests/unit/test_sweep_cadence_vs_autopause.py
git -C C:/Users/camil/Dropbox/fs-blocks commit -F "$MSG"
git -C C:/Users/camil/Dropbox/fs-blocks show --stat --oneline HEAD
```

Expected: one new commit whose `--stat` lists exactly: `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`, `tests/unit/test_template_recording_segments.py`, `tests/unit/test_template_workflow_parameter_wiring.py`, `tests/unit/test_template_pgdatabase.py`, `tests/unit/test_sweep_cadence_vs_autopause.py`.

### Task 5: `GET /api/org/sessions` returns `recording_blocks`

**Files:**
- Test (create): `tests/unit/test_org_api_sessions_recording_blocks.py`
- Test (modify): `tests/unit/test_template_workflow_parameter_wiring.py` (append after Task 4's last test)
- Modify: `src/lambda_org_api.py` (imports near lines 135 and 141; two helpers inserted directly above `def get_org_sessions`, ~line 7089; one line in its response)

**Interfaces:**
- Consumes:
  - Task 1: `day_recording_segments.get(conn, user_id, report_date) -> dict | None`.
  - Task 2: `recording_blocks.filter_segments`, `merge_segments`, `topic_ids_in_block`.
  - Task 4: env `REPORT_BLOCK_GAP_SECONDS`, `REPORT_LONG_BLOCK_SECONDS` on `OrgApiFunction`.
  - Existing: `users.get_by_folder_name_global(conn, folder_name)`, `redactions.deleted_source_prefixes(conn, folder=None, date=None) -> list[str]`, `session_scope.session_ref(source_s3_key) -> (session_base, kind)`, `session_scope.device_session_id(session_base) -> str | None`, `session_scope.KIND_EXTRACTION`, and `build_day_sessions(conn, caller, folder, date, rows) -> (sessions, excluded)` whose sessions carry `session_id` (the base) and `topic_row_ids` (list of `str`).
- Produces:
  - Response key `recording_blocks` (ABSENT when the folder has no users row, the day was never computed, or the read failed): a list of `{"from", "to", "start", "end", "minutes", "selectable_as_whole", "session_ids", "topic_row_ids"}`.
  - `_block_thresholds() -> tuple[float, float]` — env read per request, with defaults `"600"` / `"5400"`.
  - `_recording_blocks_entry(conn, folder, date, rows, sessions) -> dict` — `{}` or `{"recording_blocks": [...]}`, spread into the envelope.

The route tests live in a new file and copy `test_org_api_sessions.py`'s conventions (`FakeConn`, `make_event`, `_row`, the `wired` stubs) rather than editing that file, which another branch is changing. Chunk-session keys (`sid{hex}`) resolve start and end through `meeting_session.get`, so the fixture stubs it.

- [ ] **Step 1: Write the failing route tests**

Create `tests/unit/test_org_api_sessions_recording_blocks.py`:

```python
"""GET /api/org/sessions -- the `recording_blocks` key (design 2026-09-15 §5.4, F1, F9).

A separate file from test_org_api_sessions.py on purpose: another branch
(feat/day-scoped-reports) edits that one, and the two must merge without conflict.
The conventions (FakeConn, make_event, _row, the `wired` stubs) are copied from it.

Drives the real route, build_day_sessions, recording_blocks and the threshold read;
only the database-facing repository calls are stubbed.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
DATE = "2026-09-02"
DAY = {"date": DATE, "user": "Ada_L"}
SID_A = "81a64d850bb34d8bbf01ec11fd2af02f"
SID_B = "c3d1e0a2b4f64e5f9a7b8c9d0e1f2a3b"
SID_C = "5e6f7a8b9c0d4e1f8a2b3c4d5e6f7a8b"

# (start, end, sid, HH-MM-SS, chunk, off, to) -- the constructed 2026-09-02 day.
_SPANS = [
    (39318.0, 39323.0, SID_A, "10-55-18", "c0001", "0.0", "5.0"),
    (39902.0, 39930.0, SID_A, "11-05-02", "c0021", "0.0", "28.0"),
    (40482.0, 40510.0, SID_A, "11-14-40", "c0041", "2.0", "30.0"),
    (41050.0, 41080.0, SID_A, "11-24-10", "c0061", "0.0", "30.0"),
    (41630.0, 41660.0, SID_A, "11-33-50", "c0079", "0.0", "30.0"),
    (41712.0, 41732.0, SID_A, "11-35-12", "c0081", "0.0", "20.0"),
    (62045.0, 62075.0, SID_B, "17-14-05", "c0001", "0.0", "30.0"),
    (62620.0, 62650.0, SID_B, "17-23-40", "c0019", "0.0", "30.0"),
    (63175.0, 63205.0, SID_B, "17-32-55", "c0037", "0.0", "30.0"),
    (63691.5, 63720.0, SID_B, "17-41-30", "c0055", "1.5", "30.0"),
    (64000.0, 64025.0, SID_B, "17-46-40", "c0065", "0.0", "25.0"),
    (64650.0, 64680.0, SID_C, "17-57-30", "c0001", "0.0", "30.0"),
    (65170.0, 65200.0, SID_C, "18-06-10", "c0017", "0.0", "30.0"),
    (65690.0, 65710.0, SID_C, "18-14-50", "c0033", "0.0", "20.0"),
]
SEGMENTS = [{
    "start": s, "end": e, "session_id": sid,
    "key": f"transcripts/Ada_L/{DATE}/ada_l_{DATE}_{t}_sid{sid}_{c}_off{o}_to{to}_srcwav.json",
} for s, e, sid, t, c, o, to in _SPANS]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_event(method, path, sub="sub-1", params=None):
    return {
        "httpMethod": method,
        "path": path,
        "queryStringParameters": params,
        "body": None,
        "requestContext": {"authorizer": {"claims": {"sub": sub}}},
    }


def body_of(res):
    return json.loads(res["body"])


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
        "source_s3_key": f"extractions/Ada_L/{DATE}/sid{SID_A}.json", "category": "progress",
        "title": "Slab pour", "summary": "Discussed the pour.", "time_range": "10:56 – 11:10",
        "participants": ["Ben", "Neil"], "work_class": "work",
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def _key(sid):
    return f"extractions/Ada_L/{DATE}/sid{sid}.json"


ROWS = [
    _row(id="t-a1", source_s3_key=_key(SID_A), time_range="10:56 – 11:10"),
    _row(id="t-a2", source_s3_key=_key(SID_A), time_range="11:30 – 11:36"),
    _row(id="t-b1", source_s3_key=_key(SID_B), time_range="17:30 – 17:45"),
    _row(id="t-c1", source_s3_key=_key(SID_C), time_range="18:00 – 18:14"),
]


@pytest.fixture
def wired(monkeypatch):
    """Caller resolved, no redactions, ALL-scope site reach, no stored segments yet."""
    calls = {"owner_lookups": [], "segment_reads": [], "prefix_reads": []}
    state = {"stored": None, "prefixes": [], "redacted": {}}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": f"u-{folder}", "folder_name": folder})

    def owner(conn, folder):
        calls["owner_lookups"].append(folder)
        return {"id": f"u-{folder}", "folder_name": folder}

    def segment_read(conn, user_id, report_date):
        calls["segment_reads"].append((user_id, report_date))
        return state["stored"]

    def prefixes(conn, folder=None, date=None):
        calls["prefix_reads"].append((folder, date))
        return list(state["prefixes"])

    monkeypatch.setattr(org.users, "get_by_folder_name_global", owner)
    monkeypatch.setattr(org.day_recording_segments, "get", segment_read)
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", prefixes)
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: dict(state["redacted"]))
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    # Chunk-session bases (`sid{hex}`) resolve their start/end from meeting_session.
    monkeypatch.setattr(org.meeting_session, "get", lambda conn, sid: None)
    monkeypatch.delenv("REPORT_BLOCK_GAP_SECONDS", raising=False)
    monkeypatch.delenv("REPORT_LONG_BLOCK_SECONDS", raising=False)
    return {"mp": monkeypatch, "calls": calls, "state": state}


def _wire_rows(wired, rows):
    wired["mp"].setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix, **kw: list(rows))


def _store(wired, segments=SEGMENTS):
    wired["state"]["stored"] = {"user_id": "u-Ada_L", "report_date": DATE,
                                "folder_name": "Ada_L", "segments": list(segments),
                                "source_object_count": len(segments), "dirty": False,
                                "computed_at": None}


def _get(params=None):
    return org.lambda_handler(make_event("GET", "/api/org/sessions", params=params), None)


def _spans(body):
    return [(b["from"], b["to"]) for b in body["recording_blocks"]]


def test_a_day_never_computed_has_no_key_at_all_and_gap_minutes_is_unchanged(wired):
    _wire_rows(wired, ROWS)
    res = _get(DAY)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "recording_blocks" not in body
    assert body["gap_minutes"] == 15
    assert len(body["sessions"]) == 3


def test_blocks_carry_times_selectability_sessions_and_their_topics(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47"), ("17:57", "18:15")]
    first = body["recording_blocks"][0]
    assert first["minutes"] == 40 and first["selectable_as_whole"] is True
    assert first["session_ids"] == [SID_A]
    assert [b["topic_row_ids"] for b in body["recording_blocks"]] == [
        ["t-a1", "t-a2"], ["t-b1"], ["t-c1"]]
    assert wired["calls"]["segment_reads"] == [("u-Ada_L", DATE)]
    assert wired["calls"]["prefix_reads"] == [("Ada_L", DATE)]


def test_a_session_whose_topics_are_all_excluded_is_not_advertised(wired):
    rows = [r if r["id"] != "t-b1" else dict(r, work_class="non_work") for r in ROWS]
    _wire_rows(wired, rows)
    _store(wired)
    body = body_of(_get(DAY))
    assert body["excluded"]["non_work"] == 1
    assert _spans(body) == [("10:55", "11:35"), ("17:57", "18:15")]


def test_a_session_with_audio_but_no_topic_rows_still_shows_its_block(wired):
    _wire_rows(wired, [r for r in ROWS if r["id"] != "t-c1"])
    _store(wired)
    body = body_of(_get(DAY))
    assert _spans(body)[-1] == ("17:57", "18:15")
    assert body["recording_blocks"][-1]["topic_row_ids"] == []


def test_a_deleted_recording_is_not_advertised(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["prefixes"] = [f"extractions/Ada_L/{DATE}/sid{SID_C}"]
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "17:47")]


def test_a_redacted_topic_is_never_named_by_a_block(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["state"]["redacted"] = {"t-a2": {"scope": "redacted"}}
    body = body_of(_get(DAY))
    assert body["recording_blocks"][0]["topic_row_ids"] == ["t-a1"]
    assert _spans(body)[0] == ("10:55", "11:35")      # the session still has a visible topic


def test_the_thresholds_are_read_from_the_environment_per_request(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["mp"].setenv("REPORT_BLOCK_GAP_SECONDS", "630")
    wired["mp"].setenv("REPORT_LONG_BLOCK_SECONDS", "1980")
    body = body_of(_get(DAY))
    assert _spans(body) == [("10:55", "11:35"), ("17:14", "18:15")]
    assert [b["selectable_as_whole"] for b in body["recording_blocks"]] == [False, False]


def test_the_owner_is_the_folder_being_read_not_the_caller(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    body_of(_get({"date": DATE, "user": "Bob_K"}))
    assert "Bob_K" in wired["calls"]["owner_lookups"]
    assert wired["calls"]["segment_reads"] == [("u-Bob_K", DATE)]


def test_an_unresolvable_owner_has_no_key(wired):
    _wire_rows(wired, ROWS)
    _store(wired)
    wired["mp"].setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    assert "recording_blocks" not in body_of(_get(DAY))


def test_a_failing_blocks_read_still_serves_the_sessions(wired):
    _wire_rows(wired, ROWS)

    def broken(conn, user_id, report_date):
        raise RuntimeError("relation day_recording_segments does not exist")

    wired["mp"].setattr(org.day_recording_segments, "get", broken)
    res = _get(DAY)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "recording_blocks" not in body and len(body["sessions"]) == 3
```

- [ ] **Step 2: Append the code-default parity test to the parameter-wiring test**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `tests/unit/test_template_workflow_parameter_wiring.py`, find this exact text (it occurs exactly once):

```python
            f"{env_name} must fall back to {fallback} for EnableRecordingBlocks")
```

Replace it with:

```python
            f"{env_name} must fall back to {fallback} for EnableRecordingBlocks")


def test_the_block_code_defaults_match_the_template_defaults():
    """When they disagree the environment wins silently, and the number in the source
    reads like the one in force -- the same hazard as the evidence tunables above."""
    tpl = open(TEMPLATE, encoding="utf-8").read()
    src = open(os.path.join(REPO, "src", "lambda_org_api.py"), encoding="utf-8").read()
    for env, (param, _) in _BLOCK_TUNABLES.items():
        block = re.search(rf"\n  {param}:\n(.*?)(?=\n  \w+:\n)", tpl, re.S).group(1)
        tpl_default = re.search(r"Default:\s*'([^']+)'", block).group(1)
        code_default = re.search(
            rf"os\.environ\.get\([\"']{env}[\"'],\s*[\"']([^\"']+)[\"']\)", src).group(1)
        assert float(tpl_default) == float(code_default), (
            f"{env}: template default {tpl_default!r} != code default {code_default!r}")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_org_api_sessions_recording_blocks.py -q
python -m pytest tests/unit/test_template_workflow_parameter_wiring.py -q
```

Expected: `10 errors` — `AttributeError: module 'lambda_org_api' has no attribute 'day_recording_segments'`; then `1 failed, 61 passed` (the code-default test finds no `os.environ.get("REPORT_BLOCK_GAP_SECONDS", ...)` in org-api).

- [ ] **Step 4: Import the repository**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `src/lambda_org_api.py`, find this exact text (it occurs exactly once):

```python
from repositories import location_markers
```

Replace it with:

```python
from repositories import day_recording_segments, location_markers
```

- [ ] **Step 5: Import the pure module**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `src/lambda_org_api.py`, find this exact text (it occurs exactly once):

```python
import report_sections
```

Replace it with:

```python
import recording_blocks
import report_sections
```

- [ ] **Step 6: Add `_block_thresholds` and `_recording_blocks_entry` directly above `get_org_sessions`**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `src/lambda_org_api.py`, find this exact text (it occurs exactly once):

```python
def get_org_sessions(conn, caller, event):
```

Replace it with:

```python
def _block_thresholds():
    """(gap_seconds, long_block_seconds) for recording blocks.

    Read per request rather than at import: org-api is the single reader of both
    (design 2026-09-15 §5.5 as amended by F9). The defaults must equal the template's,
    which test_template_workflow_parameter_wiring pins.
    """
    return (float(os.environ.get("REPORT_BLOCK_GAP_SECONDS", "600")),
            float(os.environ.get("REPORT_LONG_BLOCK_SECONDS", "5400")))


def _recording_blocks_entry(conn, folder, date, rows, sessions):
    """{"recording_blocks": [...]} for the sessions envelope, or {} when there is no answer.

    {} -- so the key is ABSENT, not empty -- when the folder has no users row or the day
    has never been computed. The UI renders sessions then and never waits.

    The owner is resolved with users.get_by_folder_name_global, the same lookup the
    writer (lambda_recording_segments) keys the row with, so the two always agree. The
    folder is already authorised by _resolve_org_media_folder before this runs.
    _timeline_target_id is NOT used: it falls back to the CALLER on a miss, which here
    would serve the caller's own blocks under someone else's day.

    F1, applied at read time on the stored segments:
      * a session that has topic rows but none survived build_day_sessions (all
        redacted or non_work) is not advertised. A session with audio and no topic rows
        at all -- extraction pending, or nothing extracted -- is kept: that untopic'd
        audio is what a whole-block window exists to cover (design §4.1);
      * a recording tombstoned by source prefix is not advertised.
    topic_row_ids only name topics that survived build_day_sessions.

    Any failure logs and returns {}: blocks are an enhancement to a picker that works
    without them, and must never 500 the sessions read.
    """
    try:
        owner = users.get_by_folder_name_global(conn, folder)
        if owner is None:
            return {}
        stored = day_recording_segments.get(conn, owner["id"], date)
        if stored is None:
            return {}
        listed = {session_scope.device_session_id(s["session_id"]) for s in sessions}
        with_topics = set()
        for r in rows:
            base, kind = session_scope.session_ref(r.get("source_s3_key"))
            if kind == session_scope.KIND_EXTRACTION:
                with_topics.add(session_scope.device_session_id(base))
        excluded = (with_topics - listed) - {None}
        kept = recording_blocks.filter_segments(
            stored["segments"], excluded,
            redactions.deleted_source_prefixes(conn, folder, date))
        gap_seconds, long_block_seconds = _block_thresholds()
        visible_ids = {tid for s in sessions for tid in s["topic_row_ids"]}
        topic_rows = [r for r in rows if str(r["id"]) in visible_ids]
        blocks = recording_blocks.merge_segments(kept, gap_seconds, long_block_seconds)
        for block in blocks:
            block["topic_row_ids"] = recording_blocks.topic_ids_in_block(block, topic_rows)
        return {"recording_blocks": blocks}
    except Exception:
        logger.exception("recording blocks unavailable for %s %s; serving sessions without them",
                         folder, date)
        return {}


def get_org_sessions(conn, caller, event):
```

- [ ] **Step 7: Spread the entry into the sessions envelope (the only line changed inside `get_org_sessions`)**

This file has CRLF line endings in this Windows checkout (`core.autocrlf=true`). The anchor below is ONE line on purpose: use it verbatim as `old_string` with the Edit tool.

In `src/lambda_org_api.py`, find this exact text (it occurs exactly once):

```python
        "excluded": excluded,
```

Replace it with:

```python
        "excluded": excluded,
        **_recording_blocks_entry(conn, folder, date, rows, sessions),
```

- [ ] **Step 8: Run the route tests, the guard suites, then the full unit suite**

Run (from `C:/Users/camil/Dropbox/fs-blocks`):

```bash
python -m pytest tests/unit/test_org_api_sessions_recording_blocks.py -q
python -m pytest tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_template_pgdatabase.py tests/unit/test_sweep_cadence_vs_autopause.py tests/unit/test_template_schedule_state.py tests/unit/test_org_api_sessions.py tests/unit/test_session_activity.py -q
python -m pytest tests/unit -q
```

Expected: `10 passed`; `144 passed` (the existing `test_org_api_sessions.py` is untouched and still green); full suite `4848 passed, 2 skipped` (about 2.5 minutes).

- [ ] **Step 9: Prove the tests guard the change (revert check)**

Temporarily change the spread line back to only `        "excluded": excluded,` (delete the `**_recording_blocks_entry(...)` line), then run:

```bash
python -m pytest tests/unit/test_org_api_sessions_recording_blocks.py -q
```

Expected: `7 failed, 3 passed`. The seven tests that assert blocks (or the owner lookup) fail; the never-computed, unresolvable-owner and failing-read tests still pass, because they correctly expect the key to be absent. Restore the line exactly and re-run: `10 passed`. Do not commit the reverted state.

- [ ] **Step 10: Commit**

Stage by path only (never `git add -A` in this repo). The message goes through a file so no BOM or shell quoting reaches git.

```bash
MSG="$(mktemp)"
cat > "$MSG" <<'EOF'
feat(org-api): GET /sessions returns recording_blocks

When a day's segments are stored, the sessions envelope gains
recording_blocks: segments filtered at read time against source-prefix
tombstones and sessions whose topics were all excluded (design 2026-09-15
F1), merged with the configured thresholds (F9), each block listing the
surviving topics it overlaps. The key is absent when no row exists or the
read fails; gap_minutes and SESSION_GAP_MINUTES are unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M
EOF
git -C C:/Users/camil/Dropbox/fs-blocks add \
  src/lambda_org_api.py \
  tests/unit/test_org_api_sessions_recording_blocks.py \
  tests/unit/test_template_workflow_parameter_wiring.py
git -C C:/Users/camil/Dropbox/fs-blocks commit -F "$MSG"
git -C C:/Users/camil/Dropbox/fs-blocks show --stat --oneline HEAD
```

Expected: one new commit whose `--stat` lists exactly: `src/lambda_org_api.py`, `tests/unit/test_org_api_sessions_recording_blocks.py`, `tests/unit/test_template_workflow_parameter_wiring.py`.

### Task 6: Verify on TEST (human-approved run only)

> **OWNER APPROVAL REQUIRED before Steps 2 onward.** Merging to `develop` deploys the TEST stack (`fieldsight-test`) with `EnableRecordingBlocks` ON by default, and applies migration 0056 to the TEST database (`fieldsight_test`). **Nothing in this task touches `main` or prod.** Pushing the branch and opening the PR also need the owner's go-ahead. If any expected result below is not met, stop and report; do not work around it.

**Files:** none changed. This task produces evidence for the PR description.

**Interfaces:**
- Consumes: everything from Tasks 1–5 as deployed. Account `509194952652`, region `ap-southeast-2`, TEST bucket `fieldsight-data-test-509194952652`, TEST items table `fieldsight-test-items`, functions `fieldsight-test-recording-segments` and `fieldsight-test-org-api`.
- Produces: a filled-in verification record (commands and outputs) pasted into the PR.

Shell setup used by every step (Git Bash):

```bash
export AWS_PROFILE=fieldsight-deployer AWS_REGION=ap-southeast-2 MSYS_NO_PATHCONV=1 AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1
aws sts get-caller-identity --query Account --output text
```

Expected: `509194952652`. If this prints nothing or errors, the profile is wrong. Never fall back to the `default` profile: its expired session makes `aws s3 ls` return empty instead of failing.

- [ ] **Step 1: Before merge — simulate the deploy role WITH resource ARNs (F8)**

The CloudFormation-generated names of the new rule and role are only known after deploy. So simulate against the existing `SessionActivityFunction`'s generated rule and role, which follow the same `fieldsight-test-<LogicalId>...` naming the new ones will get, plus the literal new function ARN.

```bash
ROLE=$(aws iam get-role --role-name github-actions-fieldsight-deploy --query Role.Arn --output text)
FN_ARN=arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-recording-segments
RULE_ARN=$(aws events list-rules --name-prefix fieldsight-test-SessionActivity --query 'Rules[0].Arn' --output text)
FN_ROLE_ARN=$(aws iam list-roles --query "Roles[?starts_with(RoleName, 'fieldsight-test-SessionActivityFunctionRole')].Arn | [0]" --output text)
echo "$ROLE | $RULE_ARN | $FN_ROLE_ARN"
aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names lambda:CreateFunction lambda:GetFunction lambda:UpdateFunctionConfiguration lambda:DeleteFunction \
    lambda:PutFunctionConcurrency lambda:DeleteFunctionConcurrency lambda:AddPermission lambda:RemovePermission lambda:TagResource \
  --resource-arns "$FN_ARN" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names events:PutRule events:DescribeRule events:PutTargets events:RemoveTargets events:DeleteRule events:TagResource \
  --resource-arns "$RULE_ARN" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
aws iam simulate-principal-policy --policy-source-arn "$ROLE" \
  --action-names iam:CreateRole iam:GetRole iam:PutRolePolicy iam:GetRolePolicy iam:AttachRolePolicy iam:DeleteRolePolicy iam:PassRole iam:TagRole \
  --resource-arns "$FN_ROLE_ARN" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
```

Expected: none of the three `echo`ed ARNs is empty or `None`, and every row reads `allowed`. Any `implicitDeny` or `explicitDeny` means **stop — do not merge**: the stack would CREATE_FAILED and roll back. Report the action and ARN.

- [ ] **Step 2: With approval — push, open the PR to `develop`, confirm CI**

```bash
git -C C:/Users/camil/Dropbox/fs-blocks push -u origin feat/recording-blocks
gh pr create --repo "$(git -C C:/Users/camil/Dropbox/fs-blocks remote get-url origin)" --base develop --head feat/recording-blocks --title "Recording blocks (reports over any stretch, step 7)" --body-file <(printf 'Implements docs/superpowers/plans/2026-09-16-recording-blocks.md.\n\nVerification record: to follow on TEST.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n\nhttps://claude.ai/code/session_01YBf9b5himhWTUdTLrgHG6M\n')
gh pr checks --watch
```

Expected: all checks green, and the CI log shows `tests/integration/test_day_recording_segments.py` with `7 passed` (not skipped). The base is `develop`, never `main`.

- [ ] **Step 3: With approval — merge to `develop` and watch the TEST deploy**

Merge in the GitHub UI, then:

```bash
gh run list --branch develop --limit 1
gh run watch "$(gh run list --branch develop --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expected: the deploy job succeeds, and its "Apply DB migrations (invoke MigrateFunction, TEST)" payload lists `0056_day_recording_segments.sql`. The migration step runs AFTER `sam deploy`. If a transcript landed in between, the function logged `failed for key=` with `relation "day_recording_segments" does not exist`. That is expected and heals on the next transcript or dirty pass; confirm it did not continue after the migration step.

- [ ] **Step 4: Read the DEPLOYED configuration (three-place wiring)**

```bash
aws lambda get-function-configuration --function-name fieldsight-test-org-api \
  --query 'Environment.Variables.{gap:REPORT_BLOCK_GAP_SECONDS,long:REPORT_LONG_BLOCK_SECONDS}'
aws lambda get-function-configuration --function-name fieldsight-test-recording-segments \
  --query '{env:Environment.Variables.{bucket:S3_BUCKET,table:SWEEP_STATE_TABLE,stage:STAGE,gap:REPORT_BLOCK_GAP_SECONDS},vpc:VpcConfig.SubnetIds,role:Role}'
aws lambda get-function-concurrency --function-name fieldsight-test-recording-segments
FN_ARN=arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-recording-segments
for r in $(aws events list-rule-names-by-target --target-arn "$FN_ARN" --query 'RuleNames[]' --output text); do
  aws events describe-rule --name "$r" --query '{name:Name,state:State,schedule:ScheduleExpression,pattern:EventPattern}'
done
```

Expected: org-api `gap` = `"600"`, `long` = `"5400"`. The recording-segments env shows `bucket` = `fieldsight-data-test-509194952652`, `table` = `fieldsight-test-items`, `stage` = `test`, `gap` = `null` (not given, by design), and a non-empty `vpc`. `ReservedConcurrentExecutions` is `2`. Exactly two rules, both `ENABLED`: one with `rate(5 minutes)`, one whose pattern contains `"prefix":"transcripts/"`.

- [ ] **Step 5: Simulate the function's own role WITH resource ARNs**

```bash
FN_ROLE=$(aws lambda get-function-configuration --function-name fieldsight-test-recording-segments --query Role --output text)
aws iam simulate-principal-policy --policy-source-arn "$FN_ROLE" --action-names s3:ListBucket \
  --resource-arns arn:aws:s3:::fieldsight-data-test-509194952652 \
  --context-entries "ContextKeyName=s3:prefix,ContextKeyValues=transcripts/Ben_UCPK2/2026-09-02/,ContextKeyType=string" \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
aws iam simulate-principal-policy --policy-source-arn "$FN_ROLE" --action-names s3:GetObject \
  --resource-arns "arn:aws:s3:::fieldsight-data-test-509194952652/transcripts/Ben_UCPK2/2026-09-02/x.json" \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
TABLE_ARN=$(aws dynamodb describe-table --table-name fieldsight-test-items --query Table.TableArn --output text)
aws iam simulate-principal-policy --policy-source-arn "$FN_ROLE" --action-names dynamodb:GetItem dynamodb:PutItem \
  --resource-arns "$TABLE_ARN" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output table
```

Expected: `s3:ListBucket` `allowed`; `s3:GetObject` `implicitDeny` (correct — the function never reads objects); `dynamodb:GetItem` and `dynamodb:PutItem` `allowed`.

- [ ] **Step 6: Compute one real TEST day by direct invoke (no fan-out to other consumers)**

Pick a folder and date that have transcripts on TEST, and a transcript key from that day:

```bash
aws s3 ls s3://fieldsight-data-test-509194952652/transcripts/
FOLDER=Ben_UCPK2        # replace with a folder listed above that has a users row on TEST
aws s3 ls "s3://fieldsight-data-test-509194952652/transcripts/$FOLDER/" | tail -5
DATE=2026-09-02         # replace with a date listed above
KEY=$(aws s3api list-objects-v2 --bucket fieldsight-data-test-509194952652 --prefix "transcripts/$FOLDER/$DATE/" --max-items 1 --query 'Contents[0].Key' --output text)
echo "$KEY"
OUT="$(mktemp)"
aws lambda invoke --function-name fieldsight-test-recording-segments --cli-binary-format raw-in-base64-out \
  --payload "{\"source\":\"aws.s3\",\"detail-type\":\"Object Created\",\"detail\":{\"object\":{\"key\":\"$KEY\"}}}" "$OUT"
cat "$OUT"; echo
aws logs tail /aws/lambda/fieldsight-test-recording-segments --since 5m | grep recording_segments
```

Expected: the payload is `{"outcomes": ["computed"]}`, and the log shows `recording_segments: computed folder=<FOLDER> date=<DATE> objects=N segments=M skipped=K written=True`, with M + K = N. If the outcome is `skipped-unresolved`, the folder has no users row on TEST — pick another folder; never create a row to make it pass.

- [ ] **Step 7: Check the stored row through the RDS Data API (read-only)**

```bash
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=$(aws rds describe-db-clusters --db-cluster-identifier fieldsight-db-test-dbcluster-hywiixu8ihi9 --query 'DBClusters[0].MasterUserSecret.SecretArn' --output text)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test \
  --sql "SELECT u.folder_name, d.report_date::text, d.source_object_count, jsonb_array_length(d.segments) AS n_segments, d.dirty, d.computed_at::text FROM day_recording_segments d JOIN users u ON u.id = d.user_id WHERE u.folder_name = :f AND d.report_date = :d::date" \
  --parameters "[{\"name\":\"f\",\"value\":{\"stringValue\":\"$FOLDER\"}},{\"name\":\"d\",\"value\":{\"stringValue\":\"$DATE\"}}]" \
  --query records
```

Expected: one record whose `source_object_count` = N and `n_segments` = M from Step 6, with `dirty` = `false`. Zero records means the invoke did not write: stop.

- [ ] **Step 8: Read `recording_blocks` through the deployed org-api**

Look up a caller who may view `$FOLDER` (the folder's own login, or an admin in its company), then call the route:

```bash
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight_test \
  --sql "SELECT cognito_sub, global_role, folder_name FROM users WHERE cognito_sub IS NOT NULL AND (folder_name = :f OR (global_role IN ('admin','platform_admin') AND company_id = (SELECT company_id FROM users WHERE folder_name = :f)))" \
  --parameters "[{\"name\":\"f\",\"value\":{\"stringValue\":\"$FOLDER\"}}]" --query records
SUB=replace-with-a-cognito_sub-from-the-query-above
EV="{\"httpMethod\":\"GET\",\"path\":\"/api/org/sessions\",\"resource\":\"/api/org/sessions\",\"rawPath\":\"/api/org/sessions\",\"queryStringParameters\":{\"date\":\"$DATE\",\"user\":\"$FOLDER\"},\"body\":null,\"requestContext\":{\"authorizer\":{\"claims\":{\"sub\":\"$SUB\"}},\"http\":{\"method\":\"GET\"}}}"
OUT="$(mktemp)"
aws lambda invoke --function-name fieldsight-test-org-api --cli-binary-format raw-in-base64-out --payload "$EV" "$OUT"
python -c "import json,sys; r=json.load(open(sys.argv[1])); b=json.loads(r['body']); print(r['statusCode'], 'gap_minutes', b.get('gap_minutes'), 'sessions', len(b['sessions'])); print(json.dumps(b.get('recording_blocks', 'KEY ABSENT'), indent=1)[:3000])" "$OUT"
```

Expected: `200 gap_minutes 15`. `recording_blocks` is a list whose blocks each carry `from`, `to`, `minutes`, `selectable_as_whole`, `session_ids` and `topic_row_ids`, every `topic_row_ids` entry also appears in some session's `topic_row_ids`, and the spans are plausible against the day's recordings. Repeat with a date that has no row (for example the day before the first transcript): `KEY ABSENT`.

- [ ] **Step 9: Confirm the trailing pass is gated and healthy**

Wait at least 10 minutes after Step 6, then:

```bash
aws logs tail /aws/lambda/fieldsight-test-recording-segments --since 15m | grep -E "sweep skipped|swept|debounced-dirty|failed"
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Throttles \
  --dimensions Name=FunctionName,Value=fieldsight-test-recording-segments \
  --start-time "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)" --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 3600 --statistics Sum
```

Expected: a line every 5 minutes, either `recording_segments: sweep skipped (no dirty days flagged)` or `recording_segments: swept N of N dirty day(s)`, and no `failed` lines after the migration step. `Throttles` `Sum` is `0` or absent. Throttled calls log nothing, so this metric is the only place a throttle shows.

- [ ] **Step 10: Optional — prove EventBridge delivery with a harmless probe object**

Only if the owner wants rule delivery proven beyond Step 4's `ENABLED` state. First confirm in the source that each other consumer of `transcripts/` ignores a key with no base time and no `sid`: `lambda_extract_session.session_base_from_key` (returns `None` and logs), `session_activity.process_transcript_key` (returns `None` without a `sid`), and `lambda_rolling_summary`'s handler. If any of them would act on such a key, skip this step.

```bash
PROBE="transcripts/RB_Probe_NoUser/$(date +%Y-%m-%d)/probe.json"
echo '{}' | aws s3 cp - "s3://fieldsight-data-test-509194952652/$PROBE"
sleep 60
aws logs tail /aws/lambda/fieldsight-test-recording-segments --since 3m | grep RB_Probe_NoUser
aws s3 rm "s3://fieldsight-data-test-509194952652/$PROBE"
```

Expected: `recording_segments: skipped-unresolved folder=RB_Probe_NoUser ...`, which proves the rule delivered the event and the function refused to guess; then the probe is deleted.

- [ ] **Step 11: Record the evidence**

Paste the outputs of Steps 1 and 4–9 (and 10 if run) into the PR description under "TEST verification". State explicitly that nothing was merged to `main` and nothing was deployed to prod. Prod enablement is a separate owner decision: it needs `develop` → `main`, the prod migration, and `PROD_ENABLE_RECORDING_BLOCKS` set to `true`.

---

## Spec coverage

| Requirement | Where |
|---|---|
| §5.4 in-VPC function, EventBridge on `transcripts/`, not SessionActivity (D5) | Task 3; Task 4 Steps adding `RecordingSegmentsFunction`; `test_the_finalize_path_function_is_untouched` |
| §5.4 LIST + `transcript_utils` names, no GetObject, no model call | Task 3 `segment_from_key` / `list_day_keys`; Task 4 IAM test |
| §5.4 resolve folder with `get_by_folder_name_global`, skip unresolvable | Task 3 `handle_object_key`; `test_an_unresolvable_folder_is_skipped_without_listing_or_guessing` |
| §5.4 `recording_blocks` absent when no row; `from`/`to`/`minutes`/`selectable_as_whole`/topic ids; `gap_minutes` untouched | Task 5 |
| §5.5 three-place wiring, one reader | Task 4 (`_BLOCK_TUNABLES`), Task 5 code-default parity; Task 6 Step 4 |
| §7 throttles log nothing; new resources roll back; inert threshold | Task 4 `ReservedConcurrentExecutions: 2`; Task 6 Steps 1, 4, 9 |
| §10 thresholds measured on one account | Kept as template Parameters so re-tuning is a repo-variable change, not a code deploy |
| F1 read-time filtering (tombstones, all-excluded sessions) | Task 2 `filter_segments`; Task 5 `_recording_blocks_entry` and its tests |
| F4 debounce + dirty + trailing pass + monotonic + reserved concurrency | Task 1 SQL (+ integration test); Task 3; Task 4 |
| F8 simulate WITH resource ARNs | Task 6 Steps 1 and 5 |
| F9 segments stored, merge at read | Task 1 schema test (`gap_seconds` / `blocks jsonb` absent); Task 5 |
