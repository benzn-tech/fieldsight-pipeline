# Multi-Device Session Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several devices recording one meeting be merged into a single set of topics, joined by scanning a QR code on the lead device.

**Architecture:** The group's identity is the lead device's own `session_id` — nothing is allocated, so a group forms with no connectivity. `meeting_session` gains a nullable `group_id`; the joiner carries it on `/open` and, for offline safety, persists it in its local Room row. Extraction merges a group by handing every member's transcript to the LLM as labelled parallel sources; there is no shared clock to align on.

**Tech Stack:** Python 3.12 / psycopg3 / SAM (fieldsight-pipeline) · Kotlin / Android / Room / zxing (GrandTime)

**Spec:** `docs/superpowers/specs/2026-08-04-multi-device-session-merge-design.md`

## Global Constraints

- **`group_id` is nullable and defaults to NULL.** Every existing row is a solo recording; no backfill, no behaviour change for single-device sessions.
- **Never merge across companies.** The server validates that every member of a group shares one `company_id` and rejects otherwise — the UI cannot be trusted to enforce it.
- **Failure bias is under-merge.** A merge that cannot be completed degrades to today's separate per-device reports. Never produce a merged report from a group whose membership is uncertain.
- **Do not change the chunk key format.** `..._sid{32hex}_c{NNNN}` is parsed by `chunk_stitch.parse_chunk_key`, `session_scope`, VAD and transcribe. `group_id` travels via the API and the local DB, never the filename.
- **`/open` stays best-effort.** It is an online optimization; correctness comes from the uploaded chunks plus the device's local record. A failed `/open` must never lose the group.
- **GrandTime work happens in a git worktree.** Another session is actively working on `feat/device-identity-phase2` in that repo. See Task 6.
- Backend tests: `python -m pytest tests/unit -q` from the repo root. Mobile tests: `./gradlew test`.

---

## File Structure

**Backend (`fieldsight-pipeline`)**

| File | Responsibility |
|---|---|
| `src/migrations/0031_session_group.sql` | Adds `meeting_session.group_id` + partial index |
| `src/repositories/meeting_session.py` | `ensure_open` accepts `group_id`; new `list_group_members`, `group_is_settled` |
| `src/lambda_org_api.py` | `session_open` accepts + validates `groupId` |
| `src/lambda_extract_session.py` | `assemble_group_turns` — gather every member's turns as labelled sources |
| `tests/unit/test_session_group.py` | Group repository + validation behaviour |
| `tests/unit/test_extract_group_merge.py` | Merge assembly and its degradation paths |

**Mobile (`GrandTime`)**

| File | Responsibility |
|---|---|
| `app/src/main/java/com/benzn/grandtime/capture/SessionGroup.kt` | Parse/format the `fs1:` payload; hold the pending group id |
| `app/src/main/java/com/benzn/grandtime/net/SessionsApiClient.kt` | `open()` gains `groupId` |
| `app/src/main/java/com/benzn/grandtime/db/CaptureRecord.kt` | `groupId` column (offline durability) |
| `app/src/test/java/com/benzn/grandtime/SessionGroupTest.kt` | Payload parsing |

---

## Phase A — Backend (ships independently, backward compatible)

### Task 1: `group_id` column

**Files:**
- Create: `src/migrations/0031_session_group.sql`
- Test: `tests/unit/test_session_group.py`

**Interfaces:**
- Produces: `meeting_session.group_id text NULL`, self-referencing `meeting_session(session_id)`

- [ ] **Step 1: Write the migration**

```sql
-- 0031: multi-device session merge (spec 2026-08-04). The group's identity IS the
-- lead device's session_id, so nothing has to be allocated and a group can form
-- with no connectivity. NULL = a solo recording, which is every existing row.
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS group_id text
  REFERENCES meeting_session(session_id);

-- Partial: only grouped sessions are ever looked up this way, and the vast
-- majority of rows are solo.
CREATE INDEX IF NOT EXISTS idx_meeting_session_group
  ON meeting_session (group_id) WHERE group_id IS NOT NULL;
```

- [ ] **Step 2: Write the failing test**

```python
# tests/unit/test_session_group.py
import os, re

MIG = os.path.join(os.path.dirname(__file__), "..", "..",
                   "src", "migrations", "0031_session_group.sql")


def test_migration_is_additive_and_idempotent():
    """A destructive statement here would run against the prod database on the
    next merge to main. Additive + IF NOT EXISTS is the whole safety story."""
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "add column if not exists group_id" in sql
    assert "create index if not exists" in sql
    for destructive in ("drop ", "truncate", "delete from", "alter column"):
        assert destructive not in sql, f"destructive statement: {destructive}"


def test_group_id_is_nullable_and_self_referencing():
    sql = open(MIG, encoding="utf-8").read().lower()
    assert "references meeting_session(session_id)" in sql
    assert "not null" not in sql.split("add column")[1].split(";")[0]
```

- [ ] **Step 3: Run it**

Run: `python -m pytest tests/unit/test_session_group.py -q`
Expected: PASS (the migration file is the implementation)

- [ ] **Step 4: Commit**

```bash
git add src/migrations/0031_session_group.sql tests/unit/test_session_group.py
git commit -m "feat(session): add nullable group_id to meeting_session"
```

---

### Task 2: Repository accepts and reads groups

**Files:**
- Modify: `src/repositories/meeting_session.py` (`ensure_open`, currently line 24)
- Test: `tests/unit/test_session_group.py`

**Interfaces:**
- Consumes: `meeting_session.group_id` (Task 1)
- Produces:
  - `ensure_open(conn, session_id, company_id, user_id, site_id, kind, opened_at, group_id=None) -> dict`
  - `list_group_members(conn, group_id) -> list[dict]` — rows ordered by `opened_at`
  - `group_is_settled(conn, group_id, idle_grace_seconds) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
def test_ensure_open_stores_group_id():
    conn = FakeConn(results=[[{"session_id": "a"*32, "group_id": "b"*32}]])
    row = meeting_session.ensure_open(
        conn, "a"*32, "co-1", "u-1", None, "audio", None, group_id="b"*32)
    assert row["group_id"] == "b"*32
    assert "b"*32 in conn.calls[0]["params"]


def test_ensure_open_without_group_is_unchanged():
    """Solo recording: the column is simply NULL, no new behaviour."""
    conn = FakeConn(results=[[{"session_id": "a"*32, "group_id": None}]])
    row = meeting_session.ensure_open(conn, "a"*32, "co-1", "u-1", None, "audio", None)
    assert row["group_id"] is None


def test_list_group_members_scopes_to_the_group_and_orders_by_open():
    conn = FakeConn(results=[[{"session_id": "a"*32}, {"session_id": "c"*32}]])
    rows = meeting_session.list_group_members(conn, "b"*32)
    assert len(rows) == 2
    sql = conn.calls[0]["sql"]
    assert "group_id = %s" in sql
    assert "order by opened_at" in sql.lower()


def test_group_is_settled_only_when_every_member_is_terminal_or_idle():
    # one member still open and recently active -> not settled
    conn = FakeConn(results=[[{"unsettled": 1}]])
    assert meeting_session.group_is_settled(conn, "b"*32, 900) is False
    conn = FakeConn(results=[[{"unsettled": 0}]])
    assert meeting_session.group_is_settled(conn, "b"*32, 900) is True
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_session_group.py -q`
Expected: FAIL — `ensure_open() got an unexpected keyword argument 'group_id'`

- [ ] **Step 3: Implement**

In `src/repositories/meeting_session.py`, change `ensure_open` to accept and persist the column, and add the two readers:

```python
def ensure_open(conn, session_id, company_id, user_id, site_id, kind, opened_at,
                group_id=None) -> dict:
    """... (keep the existing docstring, then add:)

    group_id is the LEAD device's session_id (multi-device merge, spec
    2026-08-04). NULL for a solo recording, which is every pre-existing row.
    It is only ever set on the way in — a later call never clears it, so a
    best-effort /open that arrives twice cannot orphan a joined device."""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO meeting_session (session_id, company_id, user_id, site_id, "
        f"kind, opened_at, group_id) "
        f"VALUES (%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),%s) "
        f"ON CONFLICT (session_id) DO UPDATE SET "
        f"  site_id = COALESCE(meeting_session.site_id, EXCLUDED.site_id), "
        f"  kind = COALESCE(meeting_session.kind, EXCLUDED.kind), "
        f"  group_id = COALESCE(meeting_session.group_id, EXCLUDED.group_id), "
        f"  updated_at = now() "
        f"RETURNING {_COLS}",
        (session_id, company_id, user_id, site_id, kind, opened_at, group_id),
    ).fetchone()


def list_group_members(conn, group_id) -> list[dict]:
    """Every session in one group, oldest first. The lead is its own member
    (its group_id equals its session_id), so this returns the whole meeting."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM meeting_session WHERE group_id = %s "
        f"ORDER BY opened_at NULLS FIRST, session_id",
        (group_id,),
    ).fetchall()


def group_is_settled(conn, group_id, idle_grace_seconds) -> bool:
    """True when no member could still be recording: every one is terminal, or
    has gone quiet past the same idle grace a solo session uses.

    Deliberately reuses the solo idle judgement rather than inventing a
    multi-device window — a group must not outlive the sessions inside it, and
    a second timeout concept is a second thing to get wrong. A device whose
    owner forgot to press stop, or that went offline, must not hold the whole
    meeting's report hostage."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT COUNT(*) AS unsettled FROM meeting_session "
        "WHERE group_id = %s "
        "AND status NOT IN ('sent','failed') "
        "AND COALESCE(last_segment_at, opened_at, created_at) "
        "    > now() - make_interval(secs => %s)",
        (group_id, idle_grace_seconds),
    ).fetchone()
    return int((row or {}).get("unsettled") or 0) == 0
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_session_group.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/unit -q`
Expected: PASS — `ensure_open`'s new argument is keyword-only-with-default, so existing callers are unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/meeting_session.py tests/unit/test_session_group.py
git commit -m "feat(session): persist and read multi-device groups"
```

---

### Task 3: `/open` accepts `groupId`, cross-company rejected

**Files:**
- Modify: `src/lambda_org_api.py:647` (`session_open`)
- Test: `tests/unit/test_session_group.py`

**Interfaces:**
- Consumes: `meeting_session.ensure_open(..., group_id=)` (Task 2)
- Produces: `POST /api/org/sessions/{id}/open` accepting `{"groupId": "<32hex>"}`; response gains `groupId`

- [ ] **Step 1: Write the failing tests**

```python
def test_open_accepts_group_id(wired):
    wired.setattr(org.meeting_session, "ensure_open",
                  lambda *a, **k: {"session_id": "a"*32, "status": "open",
                                   "version": 0, "group_id": k.get("group_id")})
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sessions/{'a'*32}/open",
        body={"kind": "audio", "groupId": "b"*32}), None)
    assert res["statusCode"] == 200
    assert body_of(res)["groupId"] == "b"*32


def test_open_rejects_malformed_group_id(wired):
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sessions/{'a'*32}/open",
        body={"groupId": "not-a-session-id"}), None)
    assert res["statusCode"] == 400


def test_open_rejects_a_group_led_by_another_company(wired):
    """The device scanned a code it should never have had. The UI cannot be
    trusted to prevent this; the tenant boundary is enforced here."""
    wired.setattr(org.meeting_session, "get",
                  lambda conn, sid: {"session_id": sid, "company_id": "other-co"})
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sessions/{'a'*32}/open",
        body={"groupId": "b"*32}), None)
    assert res["statusCode"] == 403


def test_open_accepts_an_unknown_group_leader(wired):
    """The joiner may reach the server before the lead does (its /open is
    best-effort and may have failed). Accept the id; the merge step only ever
    acts on members it can actually see."""
    wired.setattr(org.meeting_session, "get", lambda conn, sid: None)
    wired.setattr(org.meeting_session, "ensure_open",
                  lambda *a, **k: {"session_id": "a"*32, "status": "open",
                                   "version": 0, "group_id": k.get("group_id")})
    res = org.lambda_handler(make_event(
        "POST", f"/api/org/sessions/{'a'*32}/open",
        body={"groupId": "b"*32}), None)
    assert res["statusCode"] == 200
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_session_group.py -q`
Expected: FAIL — response has no `groupId`

- [ ] **Step 3: Implement**

In `session_open`, after the existing `site_id` validation and before `ensure_open`:

```python
    group_id = body.get("groupId")
    if group_id is not None:
        if not _SID_RE.match(group_id):
            return error("groupId must be 32 lowercase hex chars", 400)
        # Tenant boundary. The lead may legitimately be unknown here — the
        # joiner can reach us first, since /open is best-effort — but if we DO
        # know it, it must belong to the caller's company. Never merge across
        # tenants on a client's say-so.
        lead = meeting_session.get(conn, group_id)
        if lead is not None and str(lead["company_id"]) != str(caller["company_id"]):
            return error("group not accessible", 403)
```

and pass it through, returning it:

```python
    row = meeting_session.ensure_open(
        conn, session_id, caller["company_id"], caller["id"], site_id, kind,
        body.get("startedAt"), group_id=group_id,
    )
    return ok({"sessionId": row["session_id"], "status": row["status"],
               "version": row["version"], "groupId": row.get("group_id")})
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_session_group.py -q`
Expected: PASS

- [ ] **Step 5: Full suite + commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_org_api.py tests/unit/test_session_group.py
git commit -m "feat(org-api): accept groupId on session open, reject cross-tenant groups"
```

---

### Task 4: Merge a group's transcripts

**Files:**
- Modify: `src/lambda_extract_session.py` (add beside `assemble_deduped_turns`, line 405)
- Test: `tests/unit/test_extract_group_merge.py`

**Interfaces:**
- Consumes: `assemble_deduped_turns(bucket, keys) -> (turns, source_filenames)`
- Produces: `assemble_group_turns(bucket, keys_by_session) -> (labelled_sources, source_filenames)` where `labelled_sources` is `list[dict]` with keys `session_id`, `turns`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_extract_group_merge.py
import lambda_extract_session as ex


def test_single_member_group_is_identical_to_a_solo_session(monkeypatch):
    """A group of one must not take a different code path — that is how the
    common case silently regresses."""
    monkeypatch.setattr(ex, "assemble_deduped_turns",
                        lambda b, k: ([{"text": "hello"}], ["f1.json"]))
    sources, files = ex.assemble_group_turns("bkt", {"a"*32: ["k1"]})
    assert len(sources) == 1
    assert sources[0]["session_id"] == "a"*32
    assert sources[0]["turns"] == [{"text": "hello"}]
    assert files == ["f1.json"]


def test_each_member_stays_a_separate_labelled_source(monkeypatch):
    """The merge is done by the LLM, which needs to see which device heard
    what. Concatenating the turns would destroy exactly that signal."""
    def fake(bucket, keys):
        return ([{"text": keys[0]}], [keys[0] + ".json"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", fake)
    sources, files = ex.assemble_group_turns("bkt", {"a"*32: ["A"], "b"*32: ["B"]})
    assert [s["session_id"] for s in sources] == ["a"*32, "b"*32]
    assert sources[0]["turns"] != sources[1]["turns"]
    assert set(files) == {"A.json", "B.json"}


def test_a_member_with_no_usable_transcript_is_dropped_not_fatal(monkeypatch):
    """One device's audio being corrupt must not lose the whole meeting."""
    def fake(bucket, keys):
        if keys == ["bad"]:
            return ([], [])
        return ([{"text": "ok"}], ["good.json"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", fake)
    sources, files = ex.assemble_group_turns("bkt", {"a"*32: ["bad"], "b"*32: ["good"]})
    assert [s["session_id"] for s in sources] == ["b"*32]
    assert files == ["good.json"]


def test_a_member_that_raises_is_skipped(monkeypatch):
    def fake(bucket, keys):
        if keys == ["boom"]:
            raise RuntimeError("s3 down")
        return ([{"text": "ok"}], ["good.json"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", fake)
    sources, _ = ex.assemble_group_turns("bkt", {"a"*32: ["boom"], "b"*32: ["good"]})
    assert [s["session_id"] for s in sources] == ["b"*32]


def test_group_with_nothing_usable_returns_empty(monkeypatch):
    monkeypatch.setattr(ex, "assemble_deduped_turns", lambda b, k: ([], []))
    sources, files = ex.assemble_group_turns("bkt", {"a"*32: ["x"]})
    assert sources == [] and files == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_extract_group_merge.py -q`
Expected: FAIL — `module has no attribute 'assemble_group_turns'`

- [ ] **Step 3: Implement**

```python
def assemble_group_turns(bucket, keys_by_session):
    """Assemble one multi-device meeting as PARALLEL labelled sources.

    Returns ([{session_id, turns}, ...], source_filenames).

    Each member is assembled with the existing per-session path, then kept
    SEPARATE. They are not concatenated and not time-merged, because across
    devices there is no shared clock to merge on: assemble_deduped_turns
    orders turns on "the single session clock", and BUG-37 is a shipped case
    of a device's wall clock being 12 hours out. Aligning them would have to
    be content-based — which is what the extraction LLM does natively, in a
    call it was going to make anyway. So the merge decision is deferred to the
    prompt, and this function's only job is to hand it clean, attributed
    sources.

    A member that yields nothing usable (corrupt transcript, S3 failure) is
    dropped rather than raised: losing one device must not lose the meeting.
    The caller reports which devices made it in."""
    sources, filenames = [], []
    for session_id in sorted(keys_by_session):
        keys = keys_by_session[session_id]
        try:
            turns, files = assemble_deduped_turns(bucket, keys)
        except Exception:
            logger.exception("group merge: member %s failed to assemble; skipping",
                             session_id)
            continue
        if not turns:
            continue
        sources.append({"session_id": session_id, "turns": turns})
        filenames.extend(files)
    return sources, filenames
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_extract_group_merge.py -q`
Expected: PASS

- [ ] **Step 5: Full suite + commit**

```bash
python -m pytest tests/unit -q
git add src/lambda_extract_session.py tests/unit/test_extract_group_merge.py
git commit -m "feat(extract): assemble a device group as labelled parallel sources"
```

---

### Task 5: Deploy Phase A to test and verify it changed nothing

**Files:** none (verification task)

- [ ] **Step 1: Merge to develop and let the test deploy run**

```bash
gh pr create --base develop --title "feat(session): multi-device group plumbing (inert without a groupId)"
```

- [ ] **Step 2: Confirm the migration applied**

```bash
aws rds-data execute-statement --region ap-southeast-2 \
  --resource-arn arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9 \
  --secret-arn arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey \
  --database fieldsight_test \
  --sql "SELECT count(*) FROM information_schema.columns WHERE table_name='meeting_session' AND column_name='group_id'"
```

Expected: `1`

- [ ] **Step 3: Confirm a solo `/open` is unaffected**

Invoke `fieldsight-test-org-api` with a `POST /api/org/sessions/{32hex}/open` body of `{"kind":"audio"}` (no `groupId`) and assert `200` with `"groupId": null`.

- [ ] **Step 4: Confirm the timeline read is unchanged**

Invoke `fieldsight-test-org-api` `GET /api/org/timeline?date=2026-07-31&user=Ben_UCPK2` and compare topic count against the value before the deploy. Any change means Phase A was not inert.

---

## Phase B — Mobile (GrandTime)

### Task 6: Create the worktree

**Why:** another session is actively committing to `feat/device-identity-phase2` in `C:/Users/camil/Dropbox/GrandTime`. Working in the same checkout would collide on the same files, and Dropbox holds a build lock on that folder.

- [ ] **Step 1: Create an isolated worktree off the current branch**

```bash
cd /c/Users/camil/Dropbox/GrandTime
git worktree add ../GrandTime-multidevice -b feat/multi-device-group
```

- [ ] **Step 2: Verify isolation**

```bash
cd ../GrandTime-multidevice && git rev-parse --abbrev-ref HEAD   # feat/multi-device-group
cd /c/Users/camil/Dropbox/GrandTime && git rev-parse --abbrev-ref HEAD  # unchanged
```

All remaining mobile tasks happen in `GrandTime-multidevice`.

---

### Task 7: Parse the QR payload

**Files:**
- Create: `app/src/main/java/com/benzn/grandtime/capture/SessionGroup.kt`
- Test: `app/src/test/java/com/benzn/grandtime/SessionGroupTest.kt`

**Interfaces:**
- Produces: `SessionGroup.format(sessionId: String): String`, `SessionGroup.parse(raw: String): String?`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.benzn.grandtime

import com.benzn.grandtime.capture.SessionGroup
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SessionGroupTest {
    private val sid = "a".repeat(32)

    @Test fun formatsWithNamespacePrefix() {
        assertEquals("fs1:$sid", SessionGroup.format(sid))
    }

    @Test fun parsesItsOwnFormat() {
        assertEquals(sid, SessionGroup.parse("fs1:$sid"))
    }

    @Test fun rejectsAnotherAppsQrCode() {
        assertNull(SessionGroup.parse("https://example.com"))
        assertNull(SessionGroup.parse(sid))            // no prefix
        assertNull(SessionGroup.parse("fs1:not-hex"))
    }

    @Test fun rejectsTheLoginQrSoScanningTheWrongCodeCannotJoinAGroup() {
        assertNull(SessionGroup.parse("fsqr1:somecode:prod"))
    }

    @Test fun toleratesSurroundingWhitespace() {
        assertEquals(sid, SessionGroup.parse("  fs1:$sid\n"))
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `./gradlew test --tests '*SessionGroupTest*'`
Expected: FAIL — unresolved reference `SessionGroup`

- [ ] **Step 3: Implement**

```kotlin
package com.benzn.grandtime.capture

/**
 * The join payload for a multi-device meeting. The group's identity IS the lead
 * device's session id, so nothing has to be allocated and a group can form with
 * no network at all — that is the whole reason this is a QR code rather than a
 * server-issued token.
 *
 * The `fs1:` prefix is a namespace guard: the app also scans a LOGIN qr
 * (`QrLoginParser`), and scanning the wrong one must fail cleanly instead of
 * producing a nonsense group.
 */
object SessionGroup {
    private const val PREFIX = "fs1:"
    private val SID = Regex("^[0-9a-f]{32}$")

    fun format(sessionId: String): String = PREFIX + sessionId

    fun parse(raw: String?): String? {
        val t = raw?.trim().orEmpty()
        if (!t.startsWith(PREFIX)) return null
        val sid = t.removePrefix(PREFIX).trim()
        return if (SID.matches(sid)) sid else null
    }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `./gradlew test --tests '*SessionGroupTest*'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/capture/SessionGroup.kt \
        app/src/test/java/com/benzn/grandtime/SessionGroupTest.kt
git commit -m "feat(session): parse the multi-device join payload"
```

---

### Task 8: Persist `groupId` locally, then send it

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/db/CaptureRecord.kt`
- Modify: `app/src/main/java/com/benzn/grandtime/net/SessionsApiClient.kt`
- Modify: `app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt:247`

**Interfaces:**
- Consumes: `SessionGroup.parse` (Task 7); backend `/open` accepting `groupId` (Task 3)
- Produces: `SessionsApiClient.open(idToken, sessionId, startedAtMillis, kind, siteId, groupId)`

- [ ] **Step 1: Write the failing test**

```kotlin
@Test fun openSendsGroupIdWhenPresent() {
    var sentBody = ""
    val http = object : HttpFns {
        override fun postJson(url: String, token: String, body: String): HttpResp {
            sentBody = body; return HttpResp(200, "")
        }
    }
    SessionsApiClient("https://x/api", http)
        .open("tok", "a".repeat(32), 0L, "audio", null, "b".repeat(32))
    assertTrue(sentBody.contains("\"groupId\":\"${"b".repeat(32)}\""))
}

@Test fun openOmitsGroupIdForASoloRecording() {
    var sentBody = ""
    val http = object : HttpFns {
        override fun postJson(url: String, token: String, body: String): HttpResp {
            sentBody = body; return HttpResp(200, "")
        }
    }
    SessionsApiClient("https://x/api", http)
        .open("tok", "a".repeat(32), 0L, "audio", null, null)
    assertFalse(sentBody.contains("groupId"))
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `./gradlew test --tests '*SessionsApiClient*'`
Expected: FAIL — `open` takes 5 arguments

- [ ] **Step 3: Add the column (offline durability)**

In `CaptureRecord.kt`, add after `siteId`:

```kotlin
    // Multi-device merge: the lead device's session id. Persisted rather than
    // only sent, because /open is best-effort — if it fails (offline on site,
    // which is normal), the group must survive in the row and go up with the
    // recording. A lost groupId cannot be reconstructed later.
    val groupId: String? = null,
```

Bump the Room schema version and add the migration alongside the existing ones in the database class:

```kotlin
// ALTER TABLE capture_records ADD COLUMN groupId TEXT
```

- [ ] **Step 4: Send it**

In `SessionsApiClient.open`, add the parameter and the conditional field:

```kotlin
    fun open(idToken: String, sessionId: String, startedAtMillis: Long, kind: String,
             siteId: String?, groupId: String? = null): Boolean {
        val body = JSONObject()
            .put("startedAt", iso(startedAtMillis))
            .put("kind", kind)
        siteId?.let { body.put("siteId", it) }
        groupId?.let { body.put("groupId", it) }
        return post("$baseUrl/org/sessions/$sessionId/open", idToken, body)
    }
```

In `CaptureManager.kt:247`, pass the pending group id through:

```kotlin
                sessionsApi.open(token, sessionId, startedAtMillis, kind, siteId, pendingGroupId)
```

- [ ] **Step 5: Run to verify they pass**

Run: `./gradlew test`
Expected: PASS — `groupId` defaults to null, so every existing caller and every existing row is unaffected.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/db/CaptureRecord.kt \
        app/src/main/java/com/benzn/grandtime/net/SessionsApiClient.kt \
        app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt
git commit -m "feat(session): carry the group id from scan to open, durably"
```

---

### Task 9: Show and scan the code

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/ui/QrScanScreen.kt` (reuse the existing scanner)

- [ ] **Step 1: Give the scanner a purpose parameter**

`QrScanScreen` currently parses with `QrLoginParser`. Parameterise which parser runs so the same camera surface serves both flows, rather than copying a second scanner:

```kotlin
enum class ScanPurpose { LOGIN, JOIN_MEETING }
```

On `JOIN_MEETING`, decode with `SessionGroup.parse`; a non-matching code shows "Not a meeting code" and keeps scanning, exactly as the login flow already does for non-login codes.

- [ ] **Step 2: Lead device shows its code**

While recording, a "Invite a device" control renders `SessionGroup.format(currentSessionId)` as a QR bitmap using the already-vendored zxing encoder.

- [ ] **Step 3: Manual verification on two real devices**

This step cannot be unit-tested and is the point of the whole feature:

1. Device A starts recording, opens "Invite a device"
2. Device B scans, then starts recording
3. Both stop
4. Assert in `fieldsight_test`:

```sql
SELECT session_id, group_id FROM meeting_session
WHERE group_id IS NOT NULL ORDER BY opened_at;
```

Expected: two rows sharing one `group_id`, equal to device A's `session_id`.

- [ ] **Step 4: Commit**

```bash
git commit -am "feat(session): show and scan the meeting join code"
```

---

### Task 10: Leaving a group — the two actions

**Files:**
- Create: `app/src/main/java/com/benzn/grandtime/capture/GroupExit.kt`
- Test: `app/src/test/java/com/benzn/grandtime/GroupExitTest.kt`

**Interfaces:**
- Consumes: `SessionGroup` (Task 7), the persisted `pendingGroupId` (Task 8)
- Produces: `GroupExit.Decision` = `MEETING_ENDED | I_AM_LEAVING | NOT_YET`, and
  `GroupExit.resolve(decision, now, lastStopAt): GroupExit.Outcome` with fields
  `clearsGroup: Boolean`, `notifiesOthers: Boolean`, `asksToResume: Boolean`

- [ ] **Step 1: Write the failing test**

```kotlin
package com.benzn.grandtime

import com.benzn.grandtime.capture.GroupExit
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class GroupExitTest {
    @Test fun meetingEndedClearsMineAndTellsTheOthers() {
        val o = GroupExit.resolve(GroupExit.Decision.MEETING_ENDED)
        assertTrue(o.clearsGroup)
        assertTrue(o.notifiesOthers)
        assertTrue(o.asksToResume)
    }

    @Test fun leavingClearsOnlyMine() {
        // An inspector who finishes early must not stop everyone else's
        // recording — that is the whole reason this is a second action.
        val o = GroupExit.resolve(GroupExit.Decision.I_AM_LEAVING)
        assertTrue(o.clearsGroup)
        assertFalse(o.notifiesOthers)
        assertTrue(o.asksToResume)
    }

    @Test fun notYetKeepsTheGroupAndAsksNothing() {
        val o = GroupExit.resolve(GroupExit.Decision.NOT_YET)
        assertFalse(o.clearsGroup)
        assertFalse(o.notifiesOthers)
        assertFalse(o.asksToResume)
    }

    @Test fun bothExitsAskToResume() {
        // Ending a meeting is not finishing work. Same question either way —
        // one behaviour, not a special case per exit.
        for (d in listOf(GroupExit.Decision.MEETING_ENDED, GroupExit.Decision.I_AM_LEAVING)) {
            assertTrue(GroupExit.resolve(d).asksToResume)
        }
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `./gradlew test --tests '*GroupExitTest*'`
Expected: FAIL — unresolved reference `GroupExit`

- [ ] **Step 3: Implement**

```kotlin
package com.benzn.grandtime.capture

/**
 * What happens when a device leaves a meeting group.
 *
 * TWO actions, not one. An inspector who finishes early while the meeting
 * continues needs I_AM_LEAVING; if MEETING_ENDED were the only option, using it
 * would stop everybody else's recording. They are identical for content already
 * captured — both stay in the meeting — and differ only in whether the rest of
 * the group is told to stop.
 *
 * Both exits ask about resuming, because ending a meeting is not the same as
 * finishing work: the person may be walking to the next task or done for the
 * day. There is no safe default, so it is asked, and the same way for both —
 * one behaviour rather than a special case per exit.
 */
object GroupExit {
    enum class Decision { MEETING_ENDED, I_AM_LEAVING, NOT_YET }

    data class Outcome(
        val clearsGroup: Boolean,
        val notifiesOthers: Boolean,
        val asksToResume: Boolean,
    )

    fun resolve(decision: Decision): Outcome = when (decision) {
        Decision.MEETING_ENDED -> Outcome(true, notifiesOthers = true, asksToResume = true)
        Decision.I_AM_LEAVING -> Outcome(true, notifiesOthers = false, asksToResume = true)
        Decision.NOT_YET -> Outcome(false, notifiesOthers = false, asksToResume = false)
    }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `./gradlew test --tests '*GroupExitTest*'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/capture/GroupExit.kt \
        app/src/test/java/com/benzn/grandtime/GroupExitTest.kt
git commit -m "feat(session): the two ways to leave a meeting group"
```

---

### Task 11: The device-side expiry, and the guarantee it carries

**Files:**
- Modify: `app/src/main/java/com/benzn/grandtime/capture/GroupExit.kt`
- Test: `app/src/test/java/com/benzn/grandtime/GroupExitTest.kt`

**Interfaces:**
- Produces: `GroupExit.hasExpired(lastStopAtMillis, nowMillis): Boolean`, and the
  constant `GroupExit.EXPIRY_MILLIS` = 15 minutes

- [ ] **Step 1: Write the failing test**

```kotlin
private val MIN = 60_000L

@Test fun aPauseWithinTheMeetingKeepsTheGroup() {
    // Battery swap, walking to the next building, a phone call. If these
    // dropped the group the meeting would split into two reports and the user
    // would have to re-scan for nothing.
    assertFalse(GroupExit.hasExpired(lastStopAtMillis = 0, nowMillis = 2 * MIN))
    assertFalse(GroupExit.hasExpired(lastStopAtMillis = 0, nowMillis = 14 * MIN))
}

@Test fun theGroupClearsAfterFifteenMinutesOfSilence() {
    assertTrue(GroupExit.hasExpired(lastStopAtMillis = 0, nowMillis = 16 * MIN))
}

@Test fun expiryUsesTheSessionGapNotTheMisTouchWindow() {
    // 30s is STOP_GRACE_SECONDS, the mis-touch window. Using it here would
    // expire the group during an ordinary pause.
    assertFalse(GroupExit.hasExpired(lastStopAtMillis = 0, nowMillis = 31_000))
    assertEquals(15 * MIN, GroupExit.EXPIRY_MILLIS)
}

@Test fun nextDayHasDefinitelyExpired() {
    assertTrue(GroupExit.hasExpired(lastStopAtMillis = 0, nowMillis = 23 * 60 * MIN))
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `./gradlew test --tests '*GroupExitTest*'`
Expected: FAIL — unresolved reference `hasExpired`

- [ ] **Step 3: Implement**

```kotlin
    /**
     * How long a group survives with no activity, when the user never answers
     * the prompt. Matches SESSION_GAP_MINUTES on the backend (15 min), which is
     * that codebase's definition of "one session" — deliberately NOT
     * STOP_GRACE_SECONDS (30 s), which is the mis-touch window. Thirty seconds
     * would expire the group during a battery swap or a walk to the next
     * building, splitting one meeting into two reports.
     *
     * This is a backstop, not the mechanism: the user's explicit answer is the
     * primary path, and it wins immediately. This only covers "the device went
     * into a bag and nobody answered".
     *
     * It is also NOT the safety guarantee. That lives on the server, which
     * refuses to merge a group whose members span beyond the window using its
     * OWN timestamps — because this check trusts the device clock, and these
     * clocks have been observed 12 hours out (BUG-37).
     */
    const val EXPIRY_MILLIS = 15L * 60_000L

    fun hasExpired(lastStopAtMillis: Long, nowMillis: Long): Boolean =
        nowMillis - lastStopAtMillis > EXPIRY_MILLIS
```

- [ ] **Step 4: Run to verify it passes**

Run: `./gradlew test --tests '*GroupExitTest*'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(session): expire a stale group after the session gap, not the mis-touch window"
```

---

### Task 12: The prompt, where the person actually is

**Files:**
- Create: `app/src/main/java/com/benzn/grandtime/capture/MeetingSounds.kt`
- Create: `app/src/main/res/raw/meeting_confirm_end.wav`, `meeting_ended.wav`
- Modify: `app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt`

**Interfaces:**
- Consumes: `GroupExit` (Tasks 10–11)
- Produces: `MeetingSounds.confirmEnd()`, `MeetingSounds.ended()`

- [ ] **Step 1: Copy the existing cue player**

`AskSounds` already does exactly this and its comment states the rule: cues are
"committed in the APK — NOT downloaded". Mirror it rather than inventing a
second audio path:

```kotlin
package com.benzn.grandtime.capture

import android.content.Context
import android.media.AudioAttributes
import android.media.SoundPool
import com.benzn.grandtime.R

/**
 * Bundled cues for the end-of-meeting prompt.
 *
 * Bundled, not TTS: the copy is fixed, and the moment this matters most is
 * offline on a site — a cloud TTS call that fails without network is worse than
 * useless. Same rule and same mechanism as [com.benzn.grandtime.ask.AskSounds].
 */
class MeetingSounds(context: Context) {
    private val pool = SoundPool.Builder()
        .setMaxStreams(1)
        .setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build(),
        )
        .build()

    private val confirmEnd = pool.load(context, R.raw.meeting_confirm_end, 1)
    private val ended = pool.load(context, R.raw.meeting_ended, 1)

    /** "Recording stopped — please confirm whether the meeting has ended." */
    fun confirmEnd() { pool.play(confirmEnd, 1f, 1f, 1, 0, 1f) }

    /** "The meeting has ended." — played on the OTHER devices when told. */
    fun ended() { pool.play(ended, 1f, 1f, 1, 0, 1f) }

    fun release() = pool.release()
}
```

- [ ] **Step 2: Fire it 20s after a grouped stop**

In `CaptureManager`, when a recording that carried a `groupId` stops, schedule
the cue at 20 s. Guard it: **only when the session was in a group** — a solo
recording must never be interrupted by this.

- [ ] **Step 3: Manual verification**

Unit tests cannot prove a sound was audible. On a real device: start a grouped
recording, stop it, confirm the cue plays at ~20 s and that a solo recording
produces no cue at all.

- [ ] **Step 4: Commit**

```bash
git add app/src/main/java/com/benzn/grandtime/capture/MeetingSounds.kt \
        app/src/main/res/raw/meeting_confirm_end.wav \
        app/src/main/res/raw/meeting_ended.wav \
        app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt
git commit -m "feat(session): ask about the meeting where the person is, not on a screen they are not looking at"
```

---

### Task 13: Telling the other devices, over the channel that already exists

**Files:**
- Modify: `src/lambda_org_api.py` (backend — `/sessions/{id}/close`, group fan-out)
- Modify: `app/src/main/java/com/benzn/grandtime/net/RecordingsApiClient.kt`
- Test: `tests/unit/test_session_group.py`, `app/src/test/.../RecordingsApiClientTest.kt`

**Interfaces:**
- Produces: upload-complete response gains `{"groupEnded": true}`; Kotlin
  `completeStatus` returns `CompleteResult(code: Int, groupEnded: Boolean)`

- [ ] **Step 1: Backend — mark the group ended**

When a close carries `intent: "end"` **and** the session has a `group_id`, mark
every member of that group. A group with one member is a no-op, so the solo path
is untouched.

- [ ] **Step 2: Backend — report it on upload**

The upload-complete handler returns `groupEnded: true` when the recording's
session belongs to a group that has been ended.

- [ ] **Step 3: Mobile — stop reading only the status code**

`completeStatus` currently returns `result.code` and **discards `result.body`**.
Parse the body for `groupEnded`; keep returning the code so every existing
caller and the whole `isTransient` retry logic is unaffected.

- [ ] **Step 4: Mobile — act on it**

On `groupEnded`, play `MeetingSounds.ended()`, stop recording, then ask about
resuming — a resumption starts a **fresh solo session with no group**, so
post-meeting audio can never land in the meeting.

- [ ] **Step 5: Tests**

Backend: ending a group marks every member; a solo close marks nothing.
Mobile: `groupEnded` parsed when present, absent field is `false`, and a
malformed body does not break the existing status-code contract.

- [ ] **Step 6: Commit**

```bash
git commit -am "feat(session): carry the group-ended signal on the upload that was already happening"
```

---

### Task 14: The server-side guard that does not trust the device

**Files:**
- Modify: `src/lambda_org_api.py` (`session_open`)
- Modify: `src/repositories/meeting_session.py`
- Test: `tests/unit/test_session_group.py`

**Interfaces:**
- Produces: `meeting_session.group_span_ok(conn, group_id, max_span_seconds) -> bool`;
  `/open` rejects a join against a long-ended lead

- [ ] **Step 1: Write the failing tests**

```python
def test_join_is_refused_against_a_long_ended_lead():
    """The device kept a stale group overnight. Whatever it believes, the
    server refuses — the merge must not depend on the device being correct."""
    # lead ended well outside the window
    ...


def test_group_span_rejects_members_from_different_days():
    """THE guarantee: yesterday never merges into today. Asserted on the
    SERVER's opened_at, never the device's — these clocks have been seen 12
    hours out (BUG-37), so a device-clock test would prove nothing."""
    ...


def test_group_span_accepts_an_ordinary_meeting_with_a_pause():
    """A battery swap mid-meeting must still merge."""
    ...
```

- [ ] **Step 2–4:** implement `group_span_ok` against `opened_at` (server time),
call it from the merge path, and reject the stale join in `session_open`
alongside the existing cross-tenant check.

- [ ] **Step 5: Commit**

```bash
git commit -am "fix(session): refuse a stale group on the server's own clock"
```

---

## Self-Review Notes

**Spec coverage:** §1 grouping → Tasks 1–2; §2 QR form → Tasks 7, 9; §4 storage → Task 2 (`list_group_members` is what the timeline read will union on); §5 merge → Task 4; §6 tenant guard → Task 3; §7 failure bias → Tasks 2 (`group_is_settled`), 4 (member drops); **leaving a group → Tasks 10–14** (two actions, expiry, the audible prompt, cross-device notification, server guard).

**The one guarantee to not lose while implementing:** after a group is left, the
*next* recording must carry no `group_id` and produce its own separate minutes.
Task 14 is what makes that unconditional — Tasks 10–11 make it pleasant, but
they trust the device.

**Deliberately deferred, not forgotten:**
- The **finalize sweep's group-level trigger** and the **`updated` email** ride on `group_is_settled` (Task 2) but are not wired here — they need Phase A on test with real two-device data first, otherwise we would be tuning a timeout against imagined traffic.
- The **timeline read union** (`my topics` ∪ `groups I was in`) is a read-path change that only matters once merged topics exist. It is the natural first task of Phase C.

Both are listed so the next planner does not think they were missed.
