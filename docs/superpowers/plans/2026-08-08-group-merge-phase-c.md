# Multi-Device Merge Phase C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the multi-device merge that Phases A and B left unconnected, so several devices recording one meeting produce one merged record and one identical email each, instead of N partial reports.

**Architecture:** A new `session_group` table holds each group's merge state. The existing in-VPC finalize sweep gains a **standing scan** (not a check hung on a finalize event) that claims settled, unmerged groups and writes one `extraction_requests/group-*.json`. extract-session merges the members as parallel LLM sources into its own `grp{groupId}.json` key; item-writer deletes the members' solo topics, writes the merged set, and enqueues N `updated` email requests carrying one shared summary.

**Tech Stack:** Python 3.11 Lambda, psycopg3 + Aurora Postgres, S3 request artifacts as the in-VPC→non-VPC crossing, SAM/CloudFormation, pytest.

**Spec:** `docs/superpowers/specs/2026-08-08-group-merge-phase-c-design.md` (v3, two adversarial review rounds folded in). Read it before Task 1.

## Global Constraints

- **Feature flag:** every new behaviour sits behind `EnableGroupMerge` → env `ENABLE_GROUP_MERGE`. Workflow fallbacks: `deploy.yml` `true`, `deploy-prod.yml` **`false`**. Template Parameter default `false`.
- **`VAD_THRESHOLD` stays 0.2** and `DROP_SILENT_CHUNKS` stays `true`. Do not touch either.
- **In-VPC lambdas must never call `lambda:InvokeFunction`** (CLAUDE.md BUG-36). Cross-boundary work goes through an S3 request artifact.
- **Every new S3 read or write needs a matching IAM grant, and `ListBucket` must accompany `GetObject`** on any prefix where a *missing* key must read as missing (CLAUDE.md BUG-43 lesson 3; PR #288 is the third recurrence). Confirm with `aws iam simulate-principal-policy`, never by reading the template.
- **Dates are NZ.** Any date derived from `opened_at` uses the same conversion as `lambda_finalize_claim._resolve_context` (`lambda_finalize_claim.py:109`). Naked `datetime.now()` is UTC (BUG-37).
- **Windows dev:** `export MSYS_NO_PATHCONV=1` before any AWS CLI call with a `/`-prefixed argument (BUG-42). Single-line Edit anchors; never `git add -A` (mixed CRLF).
- **Branch from a freshly fetched `origin/develop`** in a clean worktree — several sessions push to these repos concurrently.
- Run `python -m pytest tests/unit -q` before every commit. The suite is ~2025 tests and takes ~15s.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/migrations/0036_session_group.sql` | the `session_group` table + both new indexes | 1 |
| `src/repositories/session_group.py` | **new** — group state: ensure_row, list_due, claim, re-arm, resolve | 2, 6 |
| `src/repositories/meeting_session.py` | `list_group_member_user_ids` for the read union | 7 |
| `src/lambda_org_api.py` | create the `session_group` row when a joiner joins | 3 |
| `src/lambda_finalize_claim.py` | the standing scan + guards + claim + request artifact | 4 |
| `src/lambda_extract_session.py` | `group-` routing, group prompt, top-level `summary` | 5 |
| `src/lambda_item_writer.py` | grp site rung, member deletes, suppression/re-arm, N email requests | 6 |
| `src/repositories/topics.py` | union merged topics into `list_topics_for_date` | 7 |
| `src/lambda_ingest.py` | defer test counts merged topics for members | 8 |
| `src/lambda_session_finalize.py` | `kind="updated"` honours the carried summary | 9 |
| `src/template.yaml` | parameter, env wiring, both IAM grants | 1, 6, 9 |
| `.github/workflows/deploy.yml`, `deploy-prod.yml` | flag plumbing | 1 |

---

## Task 1: `session_group` table, the flag, and its wiring

Ships inert. Nothing reads the table yet; this task exists so the flag and schema land before any behaviour does, and so a rollback is a variable change.

**Files:**
- Create: `src/migrations/0036_session_group.sql`
- Modify: `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Test: `tests/unit/test_template_group_merge_flag.py`

**Interfaces:**
- Consumes: nothing
- Produces: table `session_group(group_id text PK, company_id uuid, merged_at timestamptz, merge_count int, merge_result text, merged_key text, created_at timestamptz)`; env `ENABLE_GROUP_MERGE` on FinalizeSweep, ExtractSession, ItemWriter, Ingest, SessionFinalize.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_template_group_merge_flag.py
"""Phase C ships behind a flag that is OFF on prod. Text-level assertions,
same approach as test_template_org_api_media_iam.py: the template is full of
CFN intrinsics a plain YAML loader cannot resolve."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"


def test_parameter_defaults_off():
    # A manual `sam deploy` that passes no override must not enable it.
    text = TEMPLATE.read_text(encoding="utf-8")
    m = re.search(r"\n  EnableGroupMerge:\n(?:.*\n)*?\s*Default: '?(\w+)'?\n", text)
    assert m, "no EnableGroupMerge parameter"
    assert m.group(1) == "false", f"EnableGroupMerge defaults to {m.group(1)}"


def test_prod_workflow_fallback_is_off_and_test_is_on():
    prod = (ROOT / ".github/workflows/deploy-prod.yml").read_text(encoding="utf-8")
    test = (ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "EnableGroupMerge=${{ vars.PROD_ENABLE_GROUP_MERGE || 'false' }}" in prod
    assert "EnableGroupMerge=${{ vars.TEST_ENABLE_GROUP_MERGE || 'true' }}" in test
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_template_group_merge_flag.py -v`
Expected: FAIL, "no EnableGroupMerge parameter".

- [ ] **Step 3: Write the migration**

```sql
-- src/migrations/0036_session_group.sql
-- One row per multi-device group, created when the first joiner joins.
--
-- The state does NOT live on the lead's meeting_session row, though that was
-- the first design. Three reasons, all found in review:
--   * the lead may never upload and its /open may never land, so there may be
--     no lead row to stamp or to CAS against;
--   * a lead carries no group_id of its own, so a scan keyed on the lead row
--     has to enumerate DISTINCT group_id over every group ever created and
--     then join -- the bounding predicate ends up on the joined row, so the
--     scan never shrinks;
--   * three consumers (the timeline union, item-writer's suppression check,
--     ingest's defer test) each need the merged artifact's key, and any drift
--     between three re-derivations is a silent miss in a different direction
--     each time. merged_key is written once, here.
CREATE TABLE IF NOT EXISTS session_group (
  group_id     text PRIMARY KEY,           -- the LEAD's session_id
  company_id   uuid NOT NULL,
  merged_at    timestamptz,
  merge_count  int NOT NULL DEFAULT 0,
  merge_result text,                       -- NULL | 'merged' | 'rejected' | 'empty'
  merged_key   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- The standing scan reads ONLY unresolved groups, so a resolved group leaves
-- the candidate set permanently and the scan cost does not grow with history.
CREATE INDEX IF NOT EXISTS idx_session_group_pending
  ON session_group (created_at) WHERE merge_result IS NULL;

-- "which groups was this user a member of" -- needed by the timeline union and
-- the ingest defer test. The existing idx_meeting_session_group is keyed on
-- group_id and does not serve a lookup by user.
CREATE INDEX IF NOT EXISTS idx_meeting_session_group_user
  ON meeting_session (user_id) WHERE group_id IS NOT NULL;
```

- [ ] **Step 4: Add the template parameter**

In `src/template.yaml`, beside the other feature Parameters:

```yaml
  EnableGroupMerge:
    Type: String
    AllowedValues: ['true', 'false']
    # Default OFF even though TEST runs it on. Both workflows pass an explicit
    # value, so this default is only reached by a path that does not -- a manual
    # `sam deploy`, which is how NormaliseAudio nearly shipped on by accident.
    Default: 'false'
    Description: Phase C multi-device merge. Off means the standing scan never runs.
```

- [ ] **Step 5: Wire the env var onto the five functions**

Add to the `Environment.Variables` of `FinalizeSweepFunction`, `ExtractSessionFunction`, `ItemWriterFunction`, `IngestFunction` and `SessionFinalizeFunction`:

```yaml
          ENABLE_GROUP_MERGE: !Ref EnableGroupMerge
```

- [ ] **Step 6: Wire both workflows**

In `.github/workflows/deploy.yml`, in the `--parameter-overrides` list:

```
              "EnableGroupMerge=${{ vars.TEST_ENABLE_GROUP_MERGE || 'true' }}" \
```

In `.github/workflows/deploy-prod.yml`:

```
              "EnableGroupMerge=${{ vars.PROD_ENABLE_GROUP_MERGE || 'false' }}" \
```

- [ ] **Step 7: Run the tests**

Run: `python -m pytest tests/unit -q`
Expected: PASS, count up by 2.

- [ ] **Step 8: Commit**

```bash
git add src/migrations/0036_session_group.sql src/template.yaml \
        .github/workflows/deploy.yml .github/workflows/deploy-prod.yml \
        tests/unit/test_template_group_merge_flag.py
git commit -m "feat(group-merge): session_group table and the flag that keeps it inert on prod"
```

---

## Task 2: `session_group` repository — create, scan, claim

The heart of the fix. `list_due` is the standing scan that replaces the broken event-hung check.

**Files:**
- Create: `src/repositories/session_group.py`
- Test: `tests/unit/test_session_group_repo.py`

**Interfaces:**
- Consumes: `session_group` table (Task 1); `meeting_session.group_is_settled(conn, group_id, idle_grace_seconds) -> bool`
- Produces:
  - `ensure_row(conn, group_id, company_id) -> None`
  - `list_due(conn, idle_grace_seconds) -> list[dict]` — rows with `merge_result IS NULL`, some member with `segment_count > 0`, and settled
  - `claim(conn, group_id, merged_key) -> bool` — CAS; True exactly once per merge
  - `mark_result(conn, group_id, result) -> None` — `'merged' | 'rejected' | 'empty'`
  - `rearm(conn, group_id) -> bool` — clears `merged_at` only if set
  - `get(conn, group_id) -> dict | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_session_group_repo.py
"""Phase C group state. FakeConn drives the SQL text, as in
test_meeting_session_repo.py -- these prove the shape of the statements, not
the semantics of the SQL, which the integration tests cover."""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn   # same double

sg = pytest.importorskip("repositories.session_group",
                         reason="requires psycopg (installed in CI)")

GID = "61be49d5" + "0" * 24


def test_claim_is_a_conditional_update_not_a_blind_one():
    # Two sweep ticks can overlap. The CAS is what makes exactly one of them
    # win; a blind UPDATE would merge (and email) twice.
    conn = FakeConn(results=[[{"group_id": GID}]])
    assert sg.claim(conn, GID, "extractions/A/2026-08-07/grp%s.json" % GID) is True
    sql = conn.calls[0]["sql"]
    assert "UPDATE session_group" in sql
    assert "merged_at IS NULL" in sql          # the condition
    assert "merge_count = merge_count + 1" in sql   # counted per MERGE, not per re-arm
    assert "merged_key" in sql


def test_claim_returns_false_when_another_tick_won():
    conn = FakeConn(results=[[]])              # no row updated
    assert sg.claim(conn, GID, "k") is False


def test_list_due_reads_only_unresolved_groups():
    # The bounding predicate must be on session_group itself, or the scan
    # re-reads every group ever created, every minute, forever.
    conn = FakeConn(results=[[]])
    sg.list_due(conn, 900)
    sql = conn.calls[0]["sql"]
    assert "merge_result IS NULL" in sql
    assert "segment_count > 0" in sql          # "some member produced content"
    assert "session_group" in sql


def test_rearm_is_conditional_so_two_late_members_do_not_double_count():
    conn = FakeConn(results=[[{"group_id": GID}]])
    assert sg.rearm(conn, GID) is True
    sql = conn.calls[0]["sql"]
    assert "merged_at IS NOT NULL" in sql
    assert "merge_count" not in sql            # incremented at claim, not here


def test_mark_result_terminates_the_group():
    conn = FakeConn(results=[[]])
    sg.mark_result(conn, GID, "empty")
    assert "merge_result = %s" in conn.calls[0]["sql"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_session_group_repo.py -v`
Expected: FAIL, module `repositories.session_group` not found.

- [ ] **Step 3: Write the repository**

```python
# src/repositories/session_group.py
"""Per-group merge state for the multi-device merge (spec 2026-08-08 Phase C).

One row per group, created when the first joiner joins -- NOT columns on the
lead's meeting_session row. A lead may never upload, a lead carries no group_id
of its own so a scan keyed on it cannot be bounded, and the merged artifact's
key must be written once rather than re-derived by three separate consumers.
"""
from psycopg.rows import dict_row

_COLS = ("group_id, company_id, merged_at, merge_count, merge_result, "
         "merged_key, created_at")


def ensure_row(conn, group_id, company_id) -> None:
    """Idempotent: /open is best-effort and may arrive twice, and several
    joiners create the same group."""
    conn.execute(
        "INSERT INTO session_group (group_id, company_id) VALUES (%s, %s) "
        "ON CONFLICT (group_id) DO NOTHING",
        (group_id, company_id))


def get(conn, group_id) -> dict | None:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM session_group WHERE group_id = %s",
        (group_id,)).fetchone()


def list_due(conn, idle_grace_seconds) -> list[dict]:
    """Groups ready to merge, asked on EVERY sweep tick.

    This is a standing scan, deliberately not a check hung on a finalize event.
    A group becomes mergeable at a moment when nothing is firing: on the tick
    that finalizes the last member, that member is still `finalizing` with a
    fresh last_segment_at, so group_is_settled is necessarily false; it becomes
    true a tick later when reconcile flips it to `sent`, and sweep() only
    iterates DUE sessions, of which there are then none. The first design asked
    at the one instant the answer could not be yes.

    `merge_result IS NULL` is on session_group itself so the partial index
    bounds the scan -- a resolved group leaves the candidate set for good.
    """
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {', '.join('g.' + c.strip() for c in _COLS.split(','))} "
        "FROM session_group g "
        "WHERE g.merge_result IS NULL "
        # Some member actually recorded. segment_count is maintained by
        # touch_segment, so this stays one indexed query and never lists S3.
        "AND EXISTS (SELECT 1 FROM meeting_session m "
        "            WHERE (m.group_id = g.group_id OR m.session_id = g.group_id) "
        "            AND m.segment_count > 0) "
        # Settled: every member terminal, or quiet past the grace. Same
        # judgement a solo session uses -- a group must not outlive its members.
        "AND NOT EXISTS (SELECT 1 FROM meeting_session m "
        "            WHERE (m.group_id = g.group_id OR m.session_id = g.group_id) "
        "            AND m.status NOT IN ('sent','failed') "
        "            AND COALESCE(m.last_segment_at, m.opened_at, m.created_at) "
        "                > now() - make_interval(secs => %s)) "
        "ORDER BY g.created_at",
        (idle_grace_seconds,)).fetchall()


def claim(conn, group_id, merged_key) -> bool:
    """CAS the merge. True for exactly one caller; a second overlapping sweep
    tick gets False and does nothing, which is what stops a group merging --
    and emailing -- twice.

    merge_count increments HERE, not on re-arm: two late members (or one late
    member's live and final passes) landing together would each re-arm, and
    counting there would burn the cap twice for a single merge."""
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE session_group SET merged_at = now(), merged_key = %s, "
        "merge_count = merge_count + 1 "
        "WHERE group_id = %s AND merged_at IS NULL "
        "RETURNING group_id",
        (merged_key, group_id)).fetchone()
    return row is not None


def mark_result(conn, group_id, result) -> None:
    """Terminal state: 'merged', 'rejected' (span/company guard) or 'empty'
    (settled with nothing usable). Any of them removes the group from the
    standing scan. An operator clearing merge_result + merged_at puts it back,
    which is the recovery path for a company mismatch caused by fixable data."""
    conn.execute(
        "UPDATE session_group SET merge_result = %s WHERE group_id = %s",
        (result, group_id))


def rearm(conn, group_id) -> bool:
    """A late device brought genuinely new content: clear merged_at so the next
    standing scan re-merges. Conditional, so two late arrivals landing together
    re-arm once."""
    row = conn.cursor(row_factory=dict_row).execute(
        "UPDATE session_group SET merged_at = NULL "
        "WHERE group_id = %s AND merged_at IS NOT NULL "
        "RETURNING group_id",
        (group_id,)).fetchone()
    return row is not None
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_session_group_repo.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Write the integration test that the unit tests cannot cover**

Unit tests drive `FakeConn` and prove nothing about the SQL (CLAUDE.md: "Run the SQL against a real database before trusting it").

```python
# tests/integration/test_session_group_sql.py
"""The SQL itself, against a real Postgres. Skips cleanly without
TEST_DATABASE_URL, as the other integration tests do."""
import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")

from repositories import session_group as sg   # noqa: E402


def test_claim_is_exactly_once_under_two_connections():
    gid = "g" + uuid.uuid4().hex[:31]
    with psycopg.connect(DSN) as a, psycopg.connect(DSN) as b:
        with a.transaction():
            sg.ensure_row(a, gid, _a_company(a))
        won = [sg.claim(a, gid, "k1"), sg.claim(b, gid, "k2")]
        assert won.count(True) == 1, "both connections claimed the same group"
        assert sg.get(a, gid)["merge_count"] == 1


def test_list_due_excludes_a_group_with_no_recorded_segments():
    # The 'empty' case: a group formed, nobody recorded. It must not sit in the
    # candidate set forever.
    gid = "g" + uuid.uuid4().hex[:31]
    with psycopg.connect(DSN) as c:
        with c.transaction():
            sg.ensure_row(c, gid, _a_company(c))
            assert gid not in [r["group_id"] for r in sg.list_due(c, 900)]


def _a_company(conn):
    return conn.execute("SELECT id FROM companies LIMIT 1").fetchone()[0]
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/unit -q && python -m pytest tests/integration -q`
Expected: unit PASS; integration SKIPPED locally (no `TEST_DATABASE_URL`), runs in CI.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/session_group.py tests/unit/test_session_group_repo.py \
        tests/integration/test_session_group_sql.py
git commit -m "feat(group-merge): group state repository with a bounded standing scan"
```

---

## Task 3: create the group row when a joiner joins

**Files:**
- Modify: `src/lambda_org_api.py` (the `session_open` path that already accepts `groupId`, around `:823-881`)
- Test: `tests/unit/test_session_group_row_created.py`

**Interfaces:**
- Consumes: `session_group.ensure_row(conn, group_id, company_id)` (Task 2)
- Produces: a `session_group` row exists for every group that has at least one joiner, **whether or not the lead ever uploads**.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_session_group_row_created.py
"""The group row must exist independently of the lead. The lead's /open is
best-effort and fires at record-start, which on a site is routinely offline; a
design that waited for the lead row would leave those groups unclaimable."""
import pytest

api = pytest.importorskip("lambda_org_api", reason="requires psycopg")

LEAD = "a" * 32
JOINER = "b" * 32


def test_joining_creates_the_group_row(monkeypatch):
    created = []
    monkeypatch.setattr(api.session_group, "ensure_row",
                        lambda conn, gid, cid: created.append((gid, cid)))
    api._ensure_group_state(object(), group_id=LEAD, company_id="co-1")
    assert created == [(LEAD, "co-1")]


def test_a_solo_recording_creates_no_group_row(monkeypatch):
    created = []
    monkeypatch.setattr(api.session_group, "ensure_row",
                        lambda conn, gid, cid: created.append(gid))
    api._ensure_group_state(object(), group_id=None, company_id="co-1")
    assert created == [], "a solo session must not create group state"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_session_group_row_created.py -v`
Expected: FAIL, `_ensure_group_state` not defined.

- [ ] **Step 3: Implement**

Add the import beside the other repository imports in `src/lambda_org_api.py`:

```python
from repositories import session_group
```

Add the helper near the other session helpers:

```python
def _ensure_group_state(conn, group_id, company_id):
    """A joiner arrived with a group. Create the group's merge-state row.

    Here rather than at merge time because the group must be discoverable even
    if the LEAD never reaches the backend: /open is fire-and-forget at
    record-start and a site is routinely offline then. The scan reads
    session_group, so a group with no lead row is still claimable."""
    if not group_id:
        return                      # solo recording -- no group state
    session_group.ensure_row(conn, group_id, company_id)
```

Call it in `session_open` immediately after the existing `ensure_open` that persists `group_id`, inside the same transaction:

```python
        _ensure_group_state(conn, existing.get("group_id") or body.get("groupId"), company["id"])
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_session_group_row_created.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Run the full suite and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_org_api.py tests/unit/test_session_group_row_created.py
git commit -m "feat(group-merge): a group's state row is created by the joiner, not the lead"
```

---

## Task 4: the standing scan in the sweep

**Files:**
- Modify: `src/lambda_finalize_claim.py`
- Test: `tests/unit/test_group_merge_scan.py`

**Interfaces:**
- Consumes: `session_group.list_due / claim / mark_result` (Task 2); `meeting_session.group_span_ok(conn, group_id, max_span_seconds) -> bool`; `meeting_session.list_group_members(conn, group_id) -> list[dict]`
- Produces:
  - `GROUP_REQUEST_PREFIX = "extraction_requests/"`, key `group-{group_id}.json`
  - artifact `{"groupId", "leadSessionId", "mergedKey", "members": [{"userFolder", "date", "sessionBase"}]}`
  - `sweep_groups(conn, *, list_due, claim, mark_result, span_ok, members_of, resolve_member, enqueue) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_group_merge_scan.py
"""The standing scan. Every test here is a regression guard for a way the
FIRST design failed -- it hung the check on a finalize event, and a group
becomes mergeable when nothing is firing."""
import pytest

fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg")

GID = "a" * 32
JOINER = "b" * 32


def _members():
    return [{"session_id": GID, "user_id": "u1", "opened_at": None},
            {"session_id": JOINER, "user_id": "u2", "opened_at": None}]


def _ctx(sid):
    return {"userFolder": "Ben_UCPK", "date": "2026-08-07", "sessionBase": "sid" + sid}


def test_a_due_group_is_claimed_and_enqueued_with_every_member():
    enqueued = []
    out = fc.sweep_groups(
        object(),
        list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: True,
        mark_result=lambda conn, gid, r: None,
        span_ok=lambda conn, gid: True,
        members_of=lambda conn, gid: _members(),
        resolve_member=lambda conn, sid: _ctx(sid),
        enqueue=lambda art: enqueued.append(art))
    assert len(enqueued) == 1
    art = enqueued[0]
    assert art["groupId"] == GID
    assert {m["sessionBase"] for m in art["members"]} == {"sid" + GID, "sid" + JOINER}
    assert art["mergedKey"].endswith(f"grp{GID}.json")
    assert out and out[0]["status"] == "claimed"


def test_it_runs_when_no_session_is_due():
    # THE regression. The first design asked only while finalizing a session,
    # so a group that settled between ticks was never looked at again.
    enqueued = []
    fc.sweep_groups(
        object(),
        list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: True, mark_result=lambda conn, gid, r: None,
        span_ok=lambda conn, gid: True, members_of=lambda conn, gid: _members(),
        resolve_member=lambda conn, sid: _ctx(sid),
        enqueue=lambda art: enqueued.append(art))
    assert len(enqueued) == 1, "the scan must not depend on a due session"


def test_a_losing_claim_enqueues_nothing():
    enqueued = []
    fc.sweep_groups(
        object(), list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: False,          # another tick won
        mark_result=lambda conn, gid, r: None, span_ok=lambda conn, gid: True,
        members_of=lambda conn, gid: _members(), resolve_member=lambda conn, sid: _ctx(sid),
        enqueue=lambda art: enqueued.append(art))
    assert enqueued == []


def test_a_failed_span_is_rejected_and_never_enqueued():
    enqueued, results = [], []
    fc.sweep_groups(
        object(), list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: True,
        mark_result=lambda conn, gid, r: results.append(r),
        span_ok=lambda conn, gid: False,             # stale group carried overnight
        members_of=lambda conn, gid: _members(), resolve_member=lambda conn, sid: _ctx(sid),
        enqueue=lambda art: enqueued.append(art))
    assert enqueued == [] and results == ["rejected"]


def test_a_cross_company_member_is_rejected():
    # Phase A's rejection fires only when the lead row exists at join time; an
    # unknown lead is deliberately accepted. So a cross-company group IS
    # representable and the server must re-check here.
    enqueued, results = [], []
    mixed = [{"session_id": GID, "user_id": "u1", "company_id": "co-1"},
             {"session_id": JOINER, "user_id": "u2", "company_id": "co-2"}]
    fc.sweep_groups(
        object(), list_due=lambda conn: [{"group_id": GID, "company_id": "co-1"}],
        claim=lambda conn, gid, key: True,
        mark_result=lambda conn, gid, r: results.append(r),
        span_ok=lambda conn, gid: True, members_of=lambda conn, gid: mixed,
        resolve_member=lambda conn, sid: _ctx(sid),
        enqueue=lambda art: enqueued.append(art))
    assert enqueued == [] and results == ["rejected"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_group_merge_scan.py -v`
Expected: FAIL, `sweep_groups` not defined.

- [ ] **Step 3: Implement**

Add near the top of `src/lambda_finalize_claim.py`, beside the other prefixes:

```python
ENABLE_GROUP_MERGE = os.environ.get("ENABLE_GROUP_MERGE", "false").lower() == "true"
# The longest a group may span before it is refused as stale (a device carrying
# yesterday's code into today). Mirrors meeting_session.group_span_ok's contract.
GROUP_MAX_SPAN_SECONDS = int(os.environ.get("GROUP_MAX_SPAN_SECONDS", "43200"))
```

Add the merged-key helper and the scan:

```python
def group_merged_key(user_folder, date, group_id):
    """The merged artifact's key. Deliberately NOT extraction_key(...,
    'sid'+lead): that is byte-identical to the lead's own final-pass key, and
    the final pass writes blind (no supersede read). A lead-solo final landing
    after the merge would overwrite it, item-writer would then delete the
    MERGED topics as that key's previous output, and every joiner's content
    would be gone from Aurora with the members' own topics already deleted."""
    return f"extractions/{user_folder}/{date}/grp{group_id}.json"


def sweep_groups(conn, *, list_due, claim, mark_result, span_ok, members_of,
                 resolve_member, enqueue):
    """Claim every settled, unmerged group and ask for its merged extraction.

    A STANDING scan, run every tick regardless of whether any session was due.
    That is the whole correction: on the tick that finalizes a group's last
    member, that member is `finalizing` with a fresh last_segment_at, so the
    group cannot be settled yet; it settles a tick later when reconcile marks
    it `sent`, and by then sweep() has no due sessions to hang a check on."""
    results = []
    for row in list_due(conn):
        gid = row["group_id"]
        members = members_of(conn, gid)
        # Both guards the parent spec called unconditional and that Phases A/B
        # never wired. A wrong merge mixes two meetings and mails the result to
        # both sets of people -- contamination plus disclosure.
        companies = {m.get("company_id") for m in members if m.get("company_id")}
        if not span_ok(conn, gid) or len(companies) > 1:
            mark_result(conn, gid, "rejected")
            logger.warning("group %s rejected: span_ok=%s companies=%s",
                           gid, span_ok(conn, gid), sorted(companies))
            results.append({"group_id": gid, "status": "rejected"})
            continue
        contexts = [c for c in (resolve_member(conn, m["session_id"]) for m in members) if c]
        if not contexts:
            mark_result(conn, gid, "empty")
            results.append({"group_id": gid, "status": "empty"})
            continue
        lead = next((c for c in contexts if c["sessionBase"] == "sid" + gid), contexts[0])
        merged_key = group_merged_key(lead["userFolder"], lead["date"], gid)
        if not claim(conn, gid, merged_key):
            results.append({"group_id": gid, "status": "lost-claim"})
            continue
        enqueue({"groupId": gid, "leadSessionId": gid, "mergedKey": merged_key,
                 "members": contexts})
        results.append({"group_id": gid, "status": "claimed"})
    return results
```

Add the real enqueue beside `_request_extraction`:

```python
def _enqueue_group(artifact):
    """Same S3-artifact crossing as every other in-VPC -> non-VPC hop: this
    sweep cannot invoke a lambda (BUG-36), but it can write to S3 through the
    gateway endpoint. The `group-` prefix is what routes it."""
    import boto3
    boto3.client("s3").put_object(
        Bucket=S3_BUCKET,
        Key=f"{EXTRACTION_REQUESTS_PREFIX}group-{artifact['groupId']}.json",
        Body=json.dumps(artifact, ensure_ascii=False),
        ContentType="application/json")
```

Call it at the end of `lambda_handler`'s existing `with get_connection() as conn:` block, after `reconcile`:

```python
        if ENABLE_GROUP_MERGE:
            from repositories import session_group
            sweep_groups(
                conn,
                list_due=lambda c: session_group.list_due(c, SESSION_GAP_MINUTES * 60),
                claim=session_group.claim,
                mark_result=session_group.mark_result,
                span_ok=lambda c, gid: meeting_session.group_span_ok(
                    c, gid, GROUP_MAX_SPAN_SECONDS),
                members_of=meeting_session.list_group_members,
                resolve_member=lambda c, sid: _resolve_context(c, sid),
                enqueue=_enqueue_group)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/unit/test_group_merge_scan.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the full suite and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_finalize_claim.py tests/unit/test_group_merge_scan.py
git commit -m "feat(group-merge): a standing scan claims settled groups, not a finalize hook"
```

---

## Task 5: route and produce the merged extraction

**Files:**
- Modify: `src/lambda_extract_session.py`
- Test: `tests/unit/test_group_extraction_routing.py`

**Interfaces:**
- Consumes: the group request artifact (Task 4); `assemble_group_turns(bucket, keys_by_session) -> (sources, source_filenames)` at `:744`
- Produces: `extractions/{leadFolder}/{date}/grp{groupId}.json` containing `mergedMembers: [str]`, `tier: "group"`, and a **top-level `summary`** and `open_todos`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_group_extraction_routing.py
"""A group request must not be mistaken for a solo one, and the merged
artifact must carry what item-writer and the emails need."""
import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")

GID = "a" * 32


def _artifact():
    return {"groupId": GID, "leadSessionId": GID,
            "mergedKey": f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json",
            "members": [
                {"userFolder": "Ben_UCPK", "date": "2026-08-07", "sessionBase": "sid" + GID},
                {"userFolder": "Sam_UCPK", "date": "2026-08-07", "sessionBase": "sid" + "b" * 32},
            ]}


def test_a_group_key_routes_to_the_group_path_not_the_solo_one():
    assert ex.is_group_request(f"extraction_requests/group-{GID}.json") is True
    assert ex.is_group_request(f"extraction_requests/{GID}.json") is False


def test_a_group_artifact_is_not_parseable_as_a_solo_request():
    # Routing order is the guard, but belt and braces: the shapes differ, so a
    # mis-ordered check fails loudly instead of extracting the lead alone.
    assert ex.parse_final_request(_artifact()) is None


def test_the_merged_artifact_names_every_member_key():
    # item-writer deletes exactly these; a missing one leaves a duplicate, a
    # wrong one deletes somebody else's topics.
    keys = ex.merged_member_keys(_artifact())
    assert keys == [
        f"extractions/Ben_UCPK/2026-08-07/sid{GID}.json",
        "extractions/Sam_UCPK/2026-08-07/sid" + "b" * 32 + ".json",
    ]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_group_extraction_routing.py -v`
Expected: FAIL, `is_group_request` not defined.

- [ ] **Step 3: Implement routing and the key helpers**

```python
GROUP_REQUEST_MARKER = 'group-'


def is_group_request(key):
    """Does this extraction_requests/ object ask for a MERGE?

    Checked BEFORE parse_final_request: everything under that prefix currently
    flows into the solo path, and a group artifact reaching it would extract
    the lead alone while the sweep believed the group had merged."""
    return key.split('/')[-1].startswith(GROUP_REQUEST_MARKER)


def merged_member_keys(artifact):
    """Each member's own extraction key -- exactly what item-writer deletes.

    Byte-identity matters: the delete is keyed on source_s3_key, so a key that
    differs by one character removes nothing and returns rowcount 0, leaving
    the duplicate the merge exists to remove. Each member's OWN date is used
    (a group can straddle NZ midnight); only the merged artifact takes the
    lead's."""
    return [extraction_key(m['userFolder'], m['date'], m['sessionBase'])
            for m in artifact.get('members', [])]
```

In `lambda_handler`, before the existing `parse_final_request` branch:

```python
        if key.startswith(FINAL_REQUESTS_PREFIX) and is_group_request(key):
            extract_group(bucket, json.loads(body))
            continue
```

Guard `parse_final_request` so the shapes cannot be confused:

```python
    if 'members' in payload or 'groupId' in payload:
        return None          # a group request -- not a solo one
```

- [ ] **Step 4: Add `extract_group`**

```python
def extract_group(bucket, artifact):
    """One meeting recorded by several devices -> ONE record.

    Members go to the model as labelled PARALLEL sources, never concatenated:
    across devices there is no shared clock to merge on (BUG-37 is a shipped
    case of a device 12 hours out), so alignment has to be content-based, which
    is what the model does natively in a call we were going to make anyway."""
    keys_by_session = {
        m['sessionBase']: gather_session_segments(
            bucket, m['userFolder'], m['date'], m['sessionBase'])
        for m in artifact['members']}
    sources, source_filenames = assemble_group_turns(bucket, keys_by_session)
    if not sources:
        logger.warning("group %s: no usable turns from %d members -- not writing",
                       artifact['groupId'], len(artifact['members']))
        return None
    result = call_extraction_llm(build_group_prompt(sources), thinking=True)
    result.update({
        'tier': 'group',
        'groupId': artifact['groupId'],
        'mergedMembers': merged_member_keys(artifact),
        'source_transcripts': source_filenames,
        'extracted_at': datetime.utcnow().isoformat() + 'Z',
    })
    s3().put_object(Bucket=bucket, Key=artifact['mergedKey'],
                    Body=json.dumps(result, ensure_ascii=False),
                    ContentType='application/json')
    return artifact['mergedKey']
```

- [ ] **Step 5: Give the group schema a top-level summary**

The existing `EXTRACTION_SCHEMA` (`:982`) has per-topic summaries and **no session-level prose**. The N updated emails need one shared summary, and item-writer is in-VPC so it cannot write one itself (BUG-36). Add to the group prompt's schema:

```python
GROUP_EXTRACTION_SCHEMA = dict(EXTRACTION_SCHEMA)
GROUP_EXTRACTION_SCHEMA['properties'] = dict(EXTRACTION_SCHEMA['properties'])
GROUP_EXTRACTION_SCHEMA['properties'].update({
    # The one summary every member's email quotes. Produced here because this
    # is the only step in the merge path that may call an LLM.
    'summary': {'type': 'string',
                'description': 'Two or three sentences covering the whole meeting '
                               'as heard by all devices together.'},
    'open_todos': {'type': 'array', 'items': {'type': 'string'}},
})
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_group_extraction_routing.py tests/unit/test_extract_group_merge.py -v`
Expected: PASS. The pre-existing `test_extract_group_merge.py` must still pass — it pins `assemble_group_turns`' contract.

- [ ] **Step 7: Run the full suite and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_extract_session.py tests/unit/test_group_extraction_routing.py
git commit -m "feat(group-merge): route group requests and write the merged extraction to its own key"
```

---

## Task 6: item-writer — site rung, member deletes, suppression, and the emails

The largest task, kept whole because its parts share one code path and a reviewer cannot usefully accept one without the others.

**Files:**
- Modify: `src/lambda_item_writer.py`, `src/template.yaml`
- Test: `tests/unit/test_item_writer_group.py`

**Interfaces:**
- Consumes: the merged artifact (Task 5); `session_group.get / rearm / mark_result` (Task 2); `topics.delete_topics_for_source(conn, source_s3_key) -> int`
- Produces: `session_finalize_requests/{sessionId}-updated.json` per member, carrying `kind: "updated"` and the shared `summary`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_item_writer_group.py
"""Four failures that would each be silent in production."""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")

GID = "a" * 32
MERGED_KEY = f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json"


def test_a_grp_key_resolves_its_site_from_the_lead_session():
    # Without this rung every merge ends in "identity bridge miss ... zero
    # writes" -- AFTER the members' topics were deleted. site_for_media misses
    # (filename pattern), _site_from_meeting_session misses (_device_session_id
    # only knows `sid` bases), and an admin/gm lead has no recordings row.
    assert iw._device_session_id("grp" + GID) is None      # today's behaviour
    assert iw._group_id_from_base("grp" + GID) == GID


def test_every_member_key_is_deleted_and_a_zero_rowcount_is_logged(caplog):
    deleted = []
    art = {"tier": "group", "groupId": GID,
           "mergedMembers": ["extractions/A/2026-08-07/sid" + "b" * 32 + ".json"]}
    iw._delete_member_topics(object(), art,
                             delete=lambda conn, k: deleted.append(k) or 0)
    assert deleted == art["mergedMembers"]
    assert "removed 0 topics" in caplog.text, \
        "a delete that matched nothing must be loud -- the duplicate survives"


def test_a_solo_artifact_already_covered_by_the_merge_does_not_re_arm():
    # THE cap-preservation case. Without coverage comparison, the lead's own
    # final -- requested by the sweep before the merge even ran -- re-arms
    # every group, so every group merges twice and reaches the cap immediately.
    merged = {"source_transcripts": ["t1.json", "t2.json"]}
    solo = {"source_transcripts": ["t1.json"]}
    assert iw._brings_new_content(solo, merged) is False


def test_a_genuinely_late_member_does_re_arm():
    merged = {"source_transcripts": ["t1.json"]}
    solo = {"source_transcripts": ["t1.json", "t3.json"]}
    assert iw._brings_new_content(solo, merged) is True


def test_one_updated_request_per_member_all_with_the_same_summary():
    written = []
    art = {"tier": "group", "groupId": GID, "summary": "One shared summary.",
           "open_todos": ["x"]}
    iw._enqueue_updated_emails(object(), art, ["s1", "s2"],
                               put=lambda key, body: written.append((key, body)))
    assert [k for k, _ in written] == [
        "session_finalize_requests/s1-updated.json",
        "session_finalize_requests/s2-updated.json"]
    assert {b["summary"] for _, b in written} == {"One shared summary."}
    assert {b["kind"] for _, b in written} == {"updated"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/unit/test_item_writer_group.py -v`
Expected: FAIL, `_group_id_from_base` not defined.

- [ ] **Step 3: Implement the site rung**

```python
def _group_id_from_base(session_base):
    """The group id inside a merged artifact's base, or None."""
    return session_base[3:] if session_base.startswith('grp') else None


def _site_from_group_lead(conn, company_id, session_base):
    """A merged artifact's site comes from the LEAD's session row.

    Needed because the merged key deliberately is not a `sid` base, so every
    existing rung misses: site_for_media matches on the media filename,
    _site_from_meeting_session's _device_session_id only recognises `sid`, and
    an admin/gm lead has no recordings row for the day. The result would be
    "identity bridge miss ... zero writes" -- the merge discarded silently
    after the members' topics were already deleted.

    Company-scoped exactly as _site_from_meeting_session is, so a stale group
    can never attribute across tenants."""
    gid = _group_id_from_base(session_base)
    if not gid:
        return None
    row = meeting_session.get(conn, gid)
    if not row or not row.get('site_id'):
        return None
    site = sites.get_site(conn, row['site_id'])
    return site if site and str(site.get('company_id')) == str(company_id) else None
```

Insert it into the existing ladder at `:344`, before `site_for_day`:

```python
        site = recordings.site_for_media(conn, company["id"], user_folder, date, session_base) \
            or _site_from_meeting_session(conn, company["id"], session_base) \
            or _site_from_group_lead(conn, company["id"], session_base) \
            or recordings.site_for_day(conn, company["id"], user_folder, date) \
            or lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)
```

- [ ] **Step 4: Implement the deletes, the suppression and the emails**

```python
def _delete_member_topics(conn, artifact, delete=None):
    """Remove each member's solo topics so the merged set is the only record.

    A zero rowcount is logged loudly: the delete is keyed on source_s3_key, so
    a key that differs by one character (a date derived in UTC instead of NZ,
    say) silently removes nothing and the duplicate survives -- exactly the
    outcome this whole feature exists to prevent."""
    delete = delete or topics.delete_topics_for_source
    for key in artifact.get('mergedMembers', []):
        n = delete(conn, key)
        if n == 0:
            logger.warning("group %s: %s removed 0 topics -- the member's solo "
                           "items will now duplicate the merged record",
                           artifact.get('groupId'), key)


def _brings_new_content(solo, merged):
    """Does this solo extraction hold a transcript the merge did not see?

    Coverage, not timing. "Anything written after the merge" would fire on the
    lead's own final pass -- which the sweep requested BEFORE the merge ran --
    so every group would re-merge and re-email once in the completely ordinary
    case, and the cap would be spent before a genuinely late device arrived."""
    return not set(solo.get('source_transcripts') or []).issubset(
        set(merged.get('source_transcripts') or []))


def _enqueue_updated_emails(conn, artifact, session_ids, put=None):
    """One request per member, all quoting ONE summary.

    The summary rides in the artifact rather than being rebuilt per member:
    lambda_session_finalize would otherwise re-summarise each member's own solo
    transcripts, producing N different emails at the cost of N LLM calls -- the
    opposite of 'every member gets identical content'.

    Keyed `-updated` so the worker's result cannot be mistaken by reconcile for
    the member's solo finalize outcome."""
    put = put or _put_finalize_request
    for sid in session_ids:
        put(f"session_finalize_requests/{sid}-updated.json",
            {"kind": "updated", "sessionId": sid,
             "groupId": artifact.get('groupId'),
             "summary": artifact.get('summary'),
             "openTodos": artifact.get('open_todos') or []})
```

- [ ] **Step 5: Add both IAM grants**

In `src/template.yaml`, `ItemWriterFunction`'s policy:

```yaml
            - Effect: Allow
              Action: s3:PutObject
              Resource:
                - !Sub arn:aws:s3:::${IngestBucketName}/session_finalize_requests/*
            # ListBucket beside GetObject deliberately: without it S3 answers 403
            # rather than 404 for a key that does not exist, so "has this group
            # merged yet" would read as DENIED where it means NO. That is the
            # defect PR #288 fixed on ExtractSessionFunction, one prefix over.
            - Effect: Allow
              Action: s3:GetObject
              Resource:
                - !Sub arn:aws:s3:::${IngestBucketName}/extractions/*
            - Effect: Allow
              Action: s3:ListBucket
              Resource: !Sub arn:aws:s3:::${IngestBucketName}
              Condition:
                StringLike:
                  s3:prefix:
                    - extractions/*
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_item_writer_group.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 7: Confirm the grants on the deployed role, not in the template**

After the branch deploys to test:

```bash
export MSYS_NO_PATHCONV=1
R=$(aws lambda get-function-configuration --function-name fieldsight-test-item-writer --query Role --output text)
aws iam simulate-principal-policy --policy-source-arn "$R" \
  --action-names s3:ListBucket --resource-arns arn:aws:s3:::fieldsight-data-test-509194952652 \
  --context-entries "ContextKeyName=s3:prefix,ContextKeyValues=extractions/,ContextKeyType=string" \
  --query "EvaluationResults[0].EvalDecision" --output text
```

Expected: `allowed`. Anything else means the feature is inert and will look like a logic bug.

- [ ] **Step 8: Run the full suite and commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_item_writer.py src/template.yaml tests/unit/test_item_writer_group.py
git commit -m "feat(group-merge): item-writer resolves grp sites, deletes members, and mails one summary"
```

---

## Task 7: the timeline union

**Files:**
- Modify: `src/repositories/topics.py`, `src/repositories/meeting_session.py`
- Test: `tests/unit/test_timeline_group_union.py`, `tests/integration/test_timeline_group_union_sql.py`

**Interfaces:**
- Consumes: `session_group.merged_key` (Task 2)
- Produces: `meeting_session.groups_for_user_on_date(conn, user_id, date) -> list[str]`; `list_topics_for_date` returns merged topics to every member.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_timeline_group_union.py
"""A member whose solo topics were deleted must still see the meeting."""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

t = pytest.importorskip("repositories.topics", reason="requires psycopg")


def test_the_union_is_keyed_on_the_merged_source_key_not_the_lead_identity():
    # Keying on the lead's user_id fails three ways: a graded-role member has
    # author_ids active and filters merged topics out; a member without
    # membership on the lead's SITE is excluded; and adding the lead's user_id
    # to author_ids leaks the lead's OTHER solo topics that day to everyone.
    conn = FakeConn(results=[[], [], [], []])
    t.list_topics_for_date(conn, site_ids=["s1"], report_date="2026-08-07",
                           author_ids=["u2"], merged_keys=["extractions/A/d/grpX.json"])
    sql = conn.calls[0]["sql"]
    assert "source_s3_key = ANY(%s)" in sql
    assert "OR" in sql, "merged topics must be unioned, not filtered by author"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_timeline_group_union.py -v`
Expected: FAIL — `list_topics_for_date` has no `merged_keys` parameter.

- [ ] **Step 3: Add the membership lookup**

```python
# src/repositories/meeting_session.py
def groups_for_user_on_date(conn, user_id, report_date) -> list[str]:
    """Group ids this user was a member of on that NZ date.

    Served by idx_meeting_session_group_user; the existing partial index on
    group_id answers the opposite question and does not help here."""
    rows = conn.execute(
        "SELECT DISTINCT COALESCE(group_id, session_id) AS gid "
        "FROM meeting_session "
        "WHERE user_id = %s AND group_id IS NOT NULL "
        "AND (opened_at AT TIME ZONE 'Pacific/Auckland')::date = %s",
        (user_id, report_date)).fetchall()
    return [r[0] for r in rows]
```

- [ ] **Step 4: Widen the topics query**

In `list_topics_for_date`, add the parameter and the union clause:

```python
def list_topics_for_date(conn, site_ids, report_date, author_ids=None,
                         merged_keys=None):
```

and in the WHERE, alongside the existing site/author predicates:

```python
    #   ... existing predicates ...
    #   OR the merged record of a group this caller was in. Keyed on
    #   source_s3_key: it names exactly the merged rows and nothing else, so it
    #   cannot leak the lead's other topics the way a user_id union would.
    if merged_keys:
        where = f"(({where}) OR t.source_s3_key = ANY(%s))"
        params.append(list(merged_keys))
```

- [ ] **Step 5: Write the integration test**

```python
# tests/integration/test_timeline_group_union_sql.py
"""Against a real database: the unit test proves the SQL's shape, this proves
it returns the right rows. Run inside a transaction that is rolled back."""
import os
import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="needs TEST_DATABASE_URL")


def test_a_member_with_no_topics_of_their_own_sees_the_merged_record():
    from repositories import topics
    with psycopg.connect(DSN) as conn:
        with conn.transaction() as tx:
            # ... insert a site, two users, one merged topic keyed grpX ...
            rows = topics.list_topics_for_date(
                conn, site_ids=[site], report_date="2026-08-07",
                author_ids=[member_id],          # graded role: filters by author
                merged_keys=["extractions/A/2026-08-07/grpX.json"])
            assert [r["source_s3_key"] for r in rows] == \
                ["extractions/A/2026-08-07/grpX.json"]
            tx.rollback()
```

- [ ] **Step 6: Run the suite and commit**

```bash
python -m pytest tests/unit -q
git add src/repositories/topics.py src/repositories/meeting_session.py \
        tests/unit/test_timeline_group_union.py tests/integration/test_timeline_group_union_sql.py
git commit -m "feat(group-merge): every member sees the merged record on their timeline"
```

---

## Task 8: stop the nightly ingest resurrecting the duplicates

Without this the feature un-does itself every night at 05:00 NZ.

**Files:**
- Modify: `src/lambda_ingest.py` (the defer test at `:477`)
- Test: `tests/unit/test_ingest_defers_for_group_members.py`

**Interfaces:**
- Consumes: `meeting_session.groups_for_user_on_date` (Task 7); `session_group.get` (Task 2)
- Produces: no signature change — the existing defer branch simply also fires for group members.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_ingest_defers_for_group_members.py
"""Deleting a member's solo topics empties extractions/{member}/{date}/, so
the authority-flip defer test goes false and the nightly ingest writes
report-sourced topics for that member -- the duplicates come back overnight,
and the merge quietly loses every night."""
import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg")


def test_a_group_member_with_no_solo_topics_still_defers(monkeypatch):
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)      # solo topics deleted
    monkeypatch.setattr(ing, "_merged_keys_for", lambda conn, uid, d: ["grpkey"])
    monkeypatch.setattr(ing.topics, "has_topics_for_source",
                        lambda conn, key: True)          # the merged record exists
    assert ing._should_defer(object(), "u1", "Ben_UCPK", "2026-08-07") is True


def test_a_genuinely_empty_day_still_ingests(monkeypatch):
    # The dangerous false positive: defer on a day with nothing would silently
    # drop that day's report topics.
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(ing, "_merged_keys_for", lambda conn, uid, d: [])
    assert ing._should_defer(object(), "u1", "Ben_UCPK", "2026-08-07") is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_ingest_defers_for_group_members.py -v`
Expected: FAIL, `_should_defer` not defined.

- [ ] **Step 3: Implement**

```python
def _merged_keys_for(conn, user_id, report_date):
    """Merged artifact keys for the groups this user was in that day."""
    from repositories import meeting_session, session_group
    keys = []
    for gid in meeting_session.groups_for_user_on_date(conn, user_id, report_date):
        row = session_group.get(conn, gid)
        if row and row.get("merged_key"):
            keys.append(row["merged_key"])
    return keys


def _should_defer(conn, user_id, user_folder, date):
    """Does this (user, date) already have authoritative extraction topics?

    The plain prefix test covers the lead (the merged key lives under the
    lead's folder) but not a joiner, whose own extraction topics were deleted
    by the merge. Without the second clause the nightly branch below DELETES
    the extraction prefix and writes report topics for every joiner, every
    night -- the merge would have to be redone daily and would still lose.

    The date is NZ, derived the same way _resolve_context derives it. A naked
    UTC date here is BUG-37, and a false positive silently drops a genuine
    zero-extraction day's report topics."""
    if topics.has_topics_for_source_prefix(conn, f"extractions/{user_folder}/{date}/"):
        return True
    return any(topics.has_topics_for_source(conn, k)
               for k in _merged_keys_for(conn, user_id, date))
```

Replace the condition at `:477` with `AUTHORITY_FLIP and _should_defer(conn, user_id, user_folder, date)`.

- [ ] **Step 4: Run the tests, then the full suite, then commit**

```bash
python -m pytest tests/unit/test_ingest_defers_for_group_members.py -v
python -m pytest tests/unit -q
git add src/lambda_ingest.py tests/unit/test_ingest_defers_for_group_members.py
git commit -m "fix(group-merge): the nightly ingest defers for group members too"
```

---

## Task 9: the updated email honours the summary it was given

**Files:**
- Modify: `src/lambda_session_finalize.py`
- Test: `tests/unit/test_updated_email.py`

**Interfaces:**
- Consumes: `session_finalize_requests/{sessionId}-updated.json` (Task 6)
- Produces: one email per member, identical body, no extra LLM call.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_updated_email.py
"""Today process_finalize_request PREFERS a freshly re-derived summary over the
artifact's (:158-167). For an updated email that is exactly wrong: it would
re-summarise each member's own SOLO transcripts, so the N members would get N
different bodies -- at the cost of the N LLM calls the design exists to avoid."""
import pytest

sf = pytest.importorskip("lambda_session_finalize", reason="requires the lambda deps")


def test_an_updated_request_uses_the_carried_summary_verbatim():
    called = []
    art = {"kind": "updated", "sessionId": "s1", "summary": "The merged summary.",
           "openTodos": [], "date": "2026-08-07"}
    sent = {}
    sf.process_finalize_request(
        art,
        complete_summary=lambda a: called.append(a) or {"summary": "solo rewrite"},
        send=lambda **kw: sent.update(kw) or {"status": "sent"},
        write_result=lambda *a, **k: None,
        resolve_recipient=lambda a: "x@example.com")
    assert called == [], "an updated email must not re-summarise per member"
    assert "The merged summary." in (sent.get("text") or sent.get("html") or "")


def test_a_normal_request_still_prefers_the_fresh_summary():
    called = []
    art = {"sessionId": "s1", "summary": "stale rolling", "date": "2026-08-07"}
    sf.process_finalize_request(
        art, complete_summary=lambda a: called.append(a) or {"summary": "fresh"},
        send=lambda **kw: {"status": "sent"}, write_result=lambda *a, **k: None,
        resolve_recipient=lambda a: "x@example.com")
    assert len(called) == 1, "the solo path must keep re-deriving"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_updated_email.py -v`
Expected: FAIL — `complete_summary` is called for the updated artifact.

- [ ] **Step 3: Implement**

In `process_finalize_request`, replace the summary block:

```python
    summary, todos = artifact.get("summary"), artifact.get("openTodos")
    # An `updated` request already carries the ONE merged summary every member
    # must receive. Re-deriving would summarise this member's own solo
    # transcripts instead -- N members, N different emails, N LLM calls.
    if artifact.get("kind") != "updated":
        fresh = (complete_summary if complete_summary is not None else _complete_summary)(artifact)
        if fresh:
            summary, todos = fresh.get("summary", summary), fresh.get("open_todos", todos)
```

Give the updated email its own subject in `build_confirmation_email` (pass `kind` through):

```python
    if kind == "updated":
        subject = f"Updated: {subject} (merged from {n_devices} recordings)"
```

- [ ] **Step 4: Run the tests, the full suite, and commit**

```bash
python -m pytest tests/unit/test_updated_email.py -v
python -m pytest tests/unit -q
git add src/lambda_session_finalize.py tests/unit/test_updated_email.py
git commit -m "feat(group-merge): the updated email quotes the merged summary, not a per-device rewrite"
```

---

## Task 10: prove it on test with two real devices

No new code. This is the only evidence the feature delivers the coverage it exists for — prompt-level merge quality cannot be unit-tested.

**Files:** none.

- [ ] **Step 1: Deploy the branch to test and confirm the flag**

```bash
export MSYS_NO_PATHCONV=1
aws lambda get-function-configuration --function-name fieldsight-test-finalize-claim \
  --query "Environment.Variables.ENABLE_GROUP_MERGE" --output text
```

Expected: `true`. On prod it must read `false`.

- [ ] **Step 2: Record one short meeting on two devices, one scan**

Lead starts recording and shows its code; the second device scans it and records. Both stop. Wait out `SESSION_GAP_MINUTES`.

- [ ] **Step 3: Assert the merge happened**

```bash
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-test-finalize-claim \
  --start-time <ms> --filter-pattern '"group"' --region ap-southeast-2
aws s3 ls s3://fieldsight-data-test-509194952652/extractions/ --recursive | grep grp
```

Expected: a `grp{groupId}.json`; `session_group.merge_result = 'merged'`, `merge_count = 1`.

- [ ] **Step 4: Assert the duplicates are gone and both members see the record**

Query Aurora: exactly one topic set carries the grp `source_s3_key`, and neither member's `sid` key has topics. Open the timeline as each member — both show the meeting.

- [ ] **Step 5: Assert two identical emails**

Both members receive one `Updated:` email with the same body.

- [ ] **Step 6: Exercise the re-merge**

Repeat with the second device kept in airplane mode until after the merge, then let it sync. Expected: `merge_count = 2`, a second pair of emails, and the merged record now containing the late device's content.

- [ ] **Step 7: Leave it overnight**

Confirm the nightly ingest did **not** resurrect either member's solo topics — the Task 8 path, and the one that fails silently a day later.

---

## Self-Review

**Spec coverage.** Standing scan → Task 4. `session_group` table → Task 1, 2. Row created by joiner → Task 3. Span + company guards → Task 4. Own S3 key → Task 5. grp site rung → Task 6. Member deletes with loud zero-rowcount → Task 6. Coverage suppression and re-arm cap → Task 6. Top-level summary produced → Task 5, honoured → Task 9. `-updated` result key → Task 6. Read union on `merged_key` → Task 7. Ingest defer → Task 8. Both IAM grants → Task 6. Flag and rollout → Task 1. Live two-device proof → Task 10. Failure-table rows `empty` / `rejected` / operator recovery → Tasks 2, 4.

**Placeholders.** None: every code step carries the actual code, every test step the actual test. The two integration tests elide only row *fixtures* (`# ... insert a site ...`), which depend on the seed data present at execution time.

**Type consistency.** `merged_key` is the name in the table (Task 1), the repository (Task 2), the artifact field `mergedKey` (Task 4, camelCase matching the other artifacts), and `merged_keys` in the topics query (Task 7). `mergedMembers` is the artifact list (Task 5) consumed by `_delete_member_topics` (Task 6). `group_is_settled` and `group_span_ok` keep their existing signatures.

**Known deviation from the spec worth stating:** the spec puts `merge_count` increment "at claim"; Task 2's `claim` does it and Task 6's `rearm` explicitly does not — a reviewer seeing only Task 6 might read that as an omission.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-08-group-merge-phase-c.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
