# Fast Confirmation Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The confirmation email arrives about a minute after the user stops recording, instead of sixteen.

**Architecture:** The device already asks the backend to close the session on stop, but through a fire-and-forget call that has not succeeded once since 03 August. The end signal moves onto the chunk-upload path, which has a persisted row and a retry loop. The marker *arms* the close; the session actually closes when the number of uploaded chunks reaches a total the device supplies. The 15-minute idle inference stays exactly as it is, as the backstop.

**Tech Stack:** Python 3.11 Lambda (SAM/CloudFormation), Aurora Postgres via psycopg3, S3, Kotlin/Android (GrandTime repo).

**Spec:** `docs/superpowers/specs/2026-08-08-fast-confirmation-email-design.md` (v3)

## Global Constraints

- **The worst case must remain today's behaviour.** Every path that fails to close a session falls through to the unchanged 15-minute idle inference. Nothing here may make any session slower than it is now.
- **`complete` must never fail because of this feature.** A `complete` that 500s strands an uploaded recording as un-uploaded and the mobile retry loop re-sends the whole file (BUG-43's family). Every new code path on that endpoint is wrapped and swallowed.
- **A session that closes early cannot be recovered** — `touch_segment` does not resume a `finalizing` session. Under-counting is the one failure this design must not have.
- **New CFN Parameters need all three legs**: repo variable → `--parameter-overrides` in **both** `deploy.yml` and `deploy-prod.yml` → template `Parameter`. A value set only on a live Lambda is erased by the next reconcile, silently.
- **`export MSYS_NO_PATHCONV=1`** for any AWS CLI call carrying a `/`-prefixed argument.
- **Two repos.** `CaptureManager.kt` is contended — check for other branches touching it before starting Task 6, and work in a worktree off `origin/main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/migrations/00NN_session_end_marker.sql` | `meeting_session.end_marked_at`, `.expected_chunks`; `recordings.session_id` + index |
| `src/repositories/meeting_session.py` | `mark_end_armed`, `uploaded_chunk_count` |
| `src/repositories/recordings.py` | `insert_pending` populates `session_id` |
| `src/lambda_org_api.py` | `complete_recording` accepts the marker; the arm-then-close decision |
| `src/lambda_finalize_claim.py` | sweep-side re-check of armed sessions |
| `src/template.yaml`, both workflows | `IdleCloseMinutes` parameter |
| GrandTime `CaptureManager.kt`, `UploadWorker.kt`, `UploadUrlReq` | persist + send the marker and the count |

---

## Task 0: Find out why the existing close stopped working

**Files:** GrandTime `app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt`

**Interfaces:** none — this is a measurement task.

The close success rate went from roughly half to **zero** on 03 August. That is a step change, not flakiness, and one of the two candidate causes would defeat this whole design: `freshIdToken() ?: return@launch` (`:257`) is a silent exit, and the new marker also needs a token. The other candidate — org-api returning 5XX for 88% of requests under the old concurrency cap — is already fixed.

**Building the durable channel before knowing which it is would move the failure, not remove it.**

- [ ] **Step 1: Make the silent path speak**

```kotlin
    private fun fireSessionClose(sessionId: String, endedAtMillis: Long, intent: String = "idle") {
        scope.launch(Dispatchers.IO) {
            runCatching {
                val app = context.applicationContext as com.benzn.grandtime.GrandTimeApp
                val token = app.authManager.freshIdToken()
                if (token == null) {
                    // The step change on 03 August is unexplained, and a silent
                    // token failure would defeat the upload-borne marker too.
                    probe("session close $sessionId: no id token — not sent")
                    return@launch
                }
                val ok = sessionsApi.close(token, sessionId, endedAtMillis, intent)
                probe("session close $sessionId intent=$intent ok=$ok")
            }
        }
    }
```

- [ ] **Step 2: Install, record 30 seconds, stop, read the log**

```bash
adb logcat -d | grep "session close"
```

Expected, one of:
- `no id token` → token refresh is the cause. **Stop and fix that first**; the rest of this plan is built on a token-bearing call too.
- `ok=false` → the request is reaching the network and being rejected. Capture the status code before continuing.
- `ok=true` → the close works now and the 03 August cause was the concurrency incident. Continue; the marker is still worth doing because a fire-and-forget signal has no durability, but the urgency drops.

- [ ] **Step 3: Write the finding into the spec, then commit**

```bash
git add app/src/main/java/com/benzn/grandtime/capture/CaptureManager.kt
git commit -m "probe: say why a session close was not sent"
```

---

## Task 1: The migration

**Files:**
- Create: `src/migrations/00NN_session_end_marker.sql` (next free number — **re-check `git ls-tree origin/develop src/migrations/` at the time you write it**, parallel sessions add migrations)
- Test: `tests/integration/test_session_end_marker_sql.py`

**Interfaces:**
- Produces: `meeting_session.end_marked_at timestamptz`, `meeting_session.expected_chunks int`, `recordings.session_id text` + index

- [ ] **Step 1: Write the migration**

```sql
-- The deliberate end, carried on the durable upload path instead of a
-- fire-and-forget call that has not succeeded since 03 August.
--
-- end_marked_at is deliberately NOT `status = 'pending_close'`. touch_segment
-- treats any chunk arriving during pending_close as a RESUME and clears
-- close_intent -- so arming that way would be erased by the very backlog chunks
-- the marker is waiting for. These two columns are ones touch_segment does not
-- write.
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS end_marked_at timestamptz;
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS expected_chunks int;

-- The completion test counts a session's uploaded chunks. Matching them by
-- `s3_key LIKE '%_sid...%'` is a leading-wildcard scan on the hottest endpoint
-- in the system (every chunk of every recording), which _group_ended_for's
-- docstring argues at length must stay at one indexed lookup.
--
-- NULL for photos and legacy recordings: neither carries a session token, and
-- neither is ever counted.
ALTER TABLE recordings ADD COLUMN IF NOT EXISTS session_id text;
CREATE INDEX IF NOT EXISTS idx_recordings_session_uploaded
  ON recordings (session_id) WHERE session_id IS NOT NULL;
```

- [ ] **Step 2: Run it against the real test database and roll back**

The unit suite drives `FakeConn`, which does not run SQL. Per CLAUDE.md, assert through the Data API inside one transaction that is rolled back.

```bash
export MSYS_NO_PATHCONV=1
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=$(aws secretsmanager list-secrets --query "SecretList[?contains(Name,'rds!cluster')].ARN" --output text --region ap-southeast-2 | head -1)
TX=$(aws rds-data begin-transaction --resource-arn "$CL" --secret-arn "$SEC" \
      --database fieldsight_test --region ap-southeast-2 --query transactionId --output text)
# apply the three ALTERs + index with --transaction-id "$TX", then:
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight_test --transaction-id "$TX" --region ap-southeast-2 \
  --sql "SELECT count(*) FROM information_schema.columns WHERE table_name='meeting_session' AND column_name IN ('end_marked_at','expected_chunks')"
aws rds-data rollback-transaction --resource-arn "$CL" --secret-arn "$SEC" \
  --transaction-id "$TX" --region ap-southeast-2
```

Expected: `2`.

- [ ] **Step 3: Commit**

```bash
git add src/migrations/ tests/integration/test_session_end_marker_sql.py
git commit -m "feat(session): columns for an end marker the backlog cannot erase"
```

---

## Task 2: `recordings.session_id` is populated

**Files:**
- Modify: `src/repositories/recordings.py` (`insert_pending`), `src/lambda_org_api.py` (`create_recording_upload_url`)
- Test: `tests/unit/test_recordings_session_id.py`

**Interfaces:**
- Consumes: the `_sid{32hex}` filename token
- Produces: `insert_pending(..., session_id=None)`

- [ ] **Step 1: Write the failing test**

```python
"""The completion count needs an indexed column, not a LIKE scan.

Parsed from the ORIGINAL file_name, not from s3_key: _safe_seg happens to
preserve the token today, and a rule that works by luck breaks when the
sanitiser changes."""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

r = pytest.importorskip("repositories.recordings")

SID = "a" * 32
FN = f"ben_2026-08-09_10-00-00_sid{SID}_c0007_off0.0_to30.0.wav"


def test_session_id_is_written_from_the_filename_token():
    conn = FakeConn(results=[[{"id": "r1"}]])
    r.insert_pending(conn, company_id="c", user_id="u", site_id=None, kind="audio",
                     s3_key="users/B/audio/2026-08-09/" + FN, client_uuid=None,
                     started_at=None, session_id=SID)
    assert "session_id" in conn.calls[0]["sql"].split("VALUES")[0]
    assert SID in conn.calls[0]["params"]


def test_a_photo_has_no_session_and_stores_null():
    # Photos carry no _sid token and must never be counted towards a session.
    conn = FakeConn(results=[[{"id": "r1"}]])
    r.insert_pending(conn, company_id="c", user_id="u", site_id=None, kind="photo",
                     s3_key="users/B/photo/2026-08-09/p.jpg", client_uuid=None,
                     started_at=None)
    assert SID not in [p for p in conn.calls[0]["params"] if isinstance(p, str)]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_recordings_session_id.py -v`
Expected: FAIL — `insert_pending() got an unexpected keyword argument 'session_id'`

- [ ] **Step 3: Implement**

In `recordings.insert_pending`: add `session_id=None` to the signature, `session_id` to the INSERT column list, its `%s`, and the value to the params tuple.

In `lambda_org_api.create_recording_upload_url`, beside the existing key construction:

```python
        m_sid = chunk_stitch.CHUNK_TOKENS_RE.search(file_name or "")
        session_id = m_sid.group(1) if m_sid else None
```

and pass `session_id=session_id` to `insert_pending`. Parse `file_name`, **not** `key` — `_safe_seg` preserves the token today, but relying on that couples this to a sanitiser that has no reason to keep it.

- [ ] **Step 4: Run and commit**

```bash
python -m pytest tests/unit -q
git add src/repositories/recordings.py src/lambda_org_api.py tests/unit/test_recordings_session_id.py
git commit -m "feat(recordings): a chunk knows which session it belongs to"
```

---

## Task 3: Arming, and the completion test

**Files:**
- Modify: `src/repositories/meeting_session.py`, `src/lambda_org_api.py` (`complete_recording`)
- Test: `tests/unit/test_end_marker.py`, `tests/integration/test_end_marker_sql.py`

**Interfaces:**
- Consumes: `sessionEnded`, `sessionChunkCount` in the `complete` body
- Produces: `meeting_session.mark_end_armed(conn, session_id, expected)`, `meeting_session.uploaded_chunk_count(conn, session_id)`

- [ ] **Step 1: Write the failing tests**

```python
"""The end marker: arms, then closes when the count is reached.

Every test here is a way the session could close EARLY, which is the one
failure this design must not have -- touch_segment does not resume a
`finalizing` session, so an early close is permanent and the email goes out
short."""
import pytest

from tests.unit.test_meeting_session_repo import FakeConn

api = pytest.importorskip("lambda_org_api")


def test_the_marker_alone_does_not_close_the_session(monkeypatch):
    # The marker chunk can arrive BEFORE earlier chunks: uploads retry
    # independently and an offline queue drains in whatever order it drains.
    closed = []
    monkeypatch.setattr(api.meeting_session, "uploaded_chunk_count", lambda c, s: 3)
    monkeypatch.setattr(api.meeting_session, "mark_pending_close",
                        lambda *a, **k: closed.append(a))
    api._apply_end_marker(FakeConn(results=[[]]), "s1", expected=37)
    assert closed == [], "3 of 37 uploaded is not a finished meeting"


def test_the_last_upload_closes_it(monkeypatch):
    closed = []
    monkeypatch.setattr(api.meeting_session, "uploaded_chunk_count", lambda c, s: 37)
    monkeypatch.setattr(api.meeting_session, "mark_pending_close",
                        lambda conn, sid, at, intent: closed.append(intent))
    monkeypatch.setattr(api.meeting_session, "end_group", lambda *a: None)
    api._apply_end_marker(FakeConn(results=[[]]), "s1", expected=37)
    assert closed == ["end"], "a deliberate end uses grace 0"


def test_end_group_runs_at_close_not_at_arming(monkeypatch):
    # Ending the group when the marker LANDS makes _adopt_group_from_upload
    # refuse a late joiner's backlog, dropping it from the meeting it was in.
    ended = []
    monkeypatch.setattr(api.meeting_session, "uploaded_chunk_count", lambda c, s: 3)
    monkeypatch.setattr(api.meeting_session, "end_group", lambda *a: ended.append(a))
    api._apply_end_marker(FakeConn(results=[[]]), "s1", expected=37)
    assert ended == []


def test_a_failure_in_the_marker_never_fails_the_upload(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(api.meeting_session, "uploaded_chunk_count", boom)
    with caplog.at_level("ERROR"):
        api._apply_end_marker(FakeConn(results=[[]]), "s1", expected=37)   # must not raise
    assert "s1" in caplog.text, "swallowed but never silent (BUG-40)"


def test_a_missing_count_arms_nothing():
    # An estimate is worse than no marker: under-counting closes early and
    # cannot be undone. Absent the count, fall through to the idle backstop.
    assert api._end_marker_fields({"sessionEnded": True}) is None
    assert api._end_marker_fields({"sessionEnded": True, "sessionChunkCount": 0}) is None
    assert api._end_marker_fields({"sessionEnded": True, "sessionChunkCount": 37}) == 37
```

- [ ] **Step 2: Run and watch them fail**

Run: `python -m pytest tests/unit/test_end_marker.py -v`
Expected: FAIL — `module 'lambda_org_api' has no attribute '_apply_end_marker'`

- [ ] **Step 3: Implement the repository half**

```python
def mark_end_armed(conn, session_id, expected_chunks) -> dict | None:
    """Record that the device deliberately ended this session.

    NOT mark_pending_close: touch_segment treats a chunk arriving during
    pending_close as a resume and clears the close, so arming that way is erased
    by the very backlog this is waiting for. These columns are ones
    touch_segment does not write."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE meeting_session SET end_marked_at = COALESCE(end_marked_at, now()), "
        f"expected_chunks = GREATEST(COALESCE(expected_chunks, 0), %s), "
        f"updated_at = now() "
        f"WHERE session_id = %s AND status NOT IN ('finalizing','sent') "
        f"RETURNING {_COLS}",
        (expected_chunks, session_id),
    ).fetchone()


def uploaded_chunk_count(conn, session_id) -> int:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT count(*) AS n FROM recordings "
        "WHERE session_id = %s AND uploaded_at IS NOT NULL",
        (session_id,),
    ).fetchone()["n"]
```

`GREATEST` on `expected_chunks`, not overwrite: a retried `complete` carrying a stale smaller count must never lower the bar.

- [ ] **Step 4: Implement the handler half**

```python
def _end_marker_fields(body):
    """The count, or None. An absent or zero count arms nothing: under-counting
    closes the session early and that cannot be undone, so no marker is strictly
    better than an estimate."""
    b = body or {}
    if not b.get("sessionEnded"):
        return None
    n = b.get("sessionChunkCount")
    return n if isinstance(n, int) and n > 0 else None


def _apply_end_marker(conn, session_id, expected):
    """Arm, then close if every chunk is in. Never raises: a `complete` that
    500s strands an uploaded recording and the mobile retry re-sends the whole
    file (BUG-43's family)."""
    try:
        meeting_session.mark_end_armed(conn, session_id, expected)
        if meeting_session.uploaded_chunk_count(conn, session_id) < expected:
            return
        meeting_session.mark_pending_close(conn, session_id, None, "end")
        # At CLOSE, not at arming: ending the group earlier makes
        # _adopt_group_from_upload refuse a late joiner's backlog.
        meeting_session.end_group(conn, session_id)
    except Exception:
        logger.exception("end marker for session %s could not be applied", session_id)
```

Call it from `complete_recording` after `mark_uploaded`, guarded on `row.get("session_id")` and `_end_marker_fields(b)`.

- [ ] **Step 5: Run, verify the SQL for real, commit**

```bash
python -m pytest tests/unit -q
```

Then run `uploaded_chunk_count`'s query through the Data API against rows you insert and roll back — `FakeConn` proves nothing about a `count(*)` with a NULL filter.

```bash
git commit -m "feat(session): the end marker arms; the last upload closes"
```

---

## Task 4: The sweep re-checks armed sessions

**Files:**
- Modify: `src/lambda_finalize_claim.py`
- Test: `tests/unit/test_finalize_claim.py`

**Interfaces:**
- Consumes: `meeting_session.list_armed_complete(conn)`
- Produces: armed sessions closed even when no further `complete` arrives

- [ ] **Step 1: Write the failing test**

```python
def test_two_racing_completes_are_rescued_by_the_sweep(monkeypatch):
    """Both final completes can see the other outstanding and neither closes.

    The complete-path check alone assumes "a later complete will ask again" --
    and there is no later complete. The sweep is what makes that assumption
    true."""
    seen = []
    monkeypatch.setattr(fc.meeting_session, "list_armed_complete",
                        lambda conn: [{"session_id": "s1"}])
    monkeypatch.setattr(fc.meeting_session, "mark_pending_close",
                        lambda conn, sid, at, intent: seen.append((sid, intent)))
    monkeypatch.setattr(fc.meeting_session, "end_group", lambda *a: None)
    fc.close_armed_sessions("CONN")
    assert seen == [("s1", "end")]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/unit/test_finalize_claim.py -k armed -v`
Expected: FAIL — no `close_armed_sessions`.

- [ ] **Step 3: Implement**

`list_armed_complete` selects sessions where `end_marked_at IS NOT NULL`, `status NOT IN ('finalizing','sent')`, and the uploaded count has reached `expected_chunks` — one query, joined against `recordings` on the new indexed column. Call `close_armed_sessions` from `lambda_handler` **before** `sweep`, so a session that became complete this tick is finalized on the same tick rather than the next.

- [ ] **Step 4: Run and commit**

```bash
python -m pytest tests/unit -q
git commit -m "feat(finalize): the sweep closes an armed session the completes missed"
```

---

## Task 5: The idle threshold becomes its own parameter

**Files:**
- Modify: `src/lambda_finalize_claim.py`, `src/template.yaml`, `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`
- Test: `tests/unit/test_template_workflow_parameter_wiring.py`

**Interfaces:**
- Produces: `IdleCloseMinutes` CFN Parameter, default `15`

One number should not answer both "how long a pause means a different meeting" and "how long until we assume the device is gone". It ships **defaulting to today's value** — this task changes no behaviour.

- [ ] **Step 1: Write the failing test**

```python
def test_the_idle_close_window_is_settable_without_a_code_change():
    text = open(TEMPLATE, encoding="utf-8").read()
    assert "IdleCloseMinutes" in text
    for env in ("prod", "test"):
        assert "IdleCloseMinutes=" in open(WORKFLOWS[env], encoding="utf-8").read(), \
            f"{env} does not pass it — the Parameter would hold its default forever"


def test_it_defaults_to_todays_value():
    # Shipping a different number alongside a new close path would confound the
    # two changes: a session closing sooner would have two possible causes.
    m = re.search(r"\n  IdleCloseMinutes:\n(?:.*\n)*?\s*Default: '?(\d+)'?\n",
                  open(TEMPLATE, encoding="utf-8").read())
    assert m and m.group(1) == "15"
```

- [ ] **Step 2: Run, implement, run**

`IDLE_CLOSE_SECONDS = int(os.environ.get("IDLE_CLOSE_MINUTES", SESSION_GAP_MINUTES)) * 60`, plus the Parameter, the env var on the function, and the override line in **both** workflows.

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(finalize): the idle window is a switch, at today's value"
```

---

## Task 6: The device sends the marker

**Files:** GrandTime — `CaptureManager.kt`, the upload request model, `UploadWorker.kt`

**Interfaces:**
- Consumes: nothing from the backend
- Produces: `sessionEnded` + `sessionChunkCount` on the final `complete`

**Check for contention first.** `CaptureManager.kt` has been the collision point before; confirm no other branch is mid-flight on it, and work in a worktree off `origin/main`.

- [ ] **Step 1: Persist the marker on the capture row**

On the row, not in memory: the upload may run hours after the meeting, after an app restart.

- [ ] **Step 2: Set it from the right place**

For audio, `endAudio` is synchronous and any point after `audio.stop()` is safe. **For video the count must be read inside the stop callback, after `finalizeVideoDbRow()`** — the same place `fireSessionClose(..., "end")` already fires (`:325`). A count taken in the button handler is N−1 and would close the session before the last chunk uploads.

`sessionChunkCount` is the number of persisted capture rows for that session id — a count, never an estimate.

- [ ] **Step 3: Send it with the final chunk's `complete`**

Add both fields to the complete request body. Omit them entirely for a solo/non-final chunk so the request stays byte-identical for the overwhelming majority of uploads.

- [ ] **Step 4: Test on a real device — both shapes**

Unit tests cannot see this. Record audio, stop, confirm the email arrives in about a minute. Then record **video**, stop, and confirm `expected_chunks` equals the true number of chunks — this is the case the count is delicate for.

- [ ] **Step 5: Commit and PR**

---

## Task 7: Prove it end to end, including the case it exists for

**Files:** none.

- [ ] **Step 1: Normal stop**

Record, stop. Confirm in Aurora within ~90 seconds:

```sql
SELECT status, close_intent, closed_at, end_marked_at, expected_chunks
  FROM meeting_session ORDER BY opened_at DESC LIMIT 1;
```

Expected: `close_intent = 'end'`, and the email in the inbox.

- [ ] **Step 2: The offline backlog — the case the arm/count split exists for**

Put the device in aeroplane mode, record several minutes, stop, then restore signal.

Expected: the session stays `open` while the backlog drains and closes **only when the last chunk is up**. If it closes earlier, the count is being under-stated — stop and fix Task 6 before shipping, because that failure sends a short email and cannot be undone.

- [ ] **Step 3: Confirm the backstop still works**

Kill the app mid-recording so no marker is ever sent. Expected: the session closes by idle inference at 15 minutes, exactly as today.

- [ ] **Step 4: Record the numbers in the spec**

Stop-to-email for the normal case, and the backlog case's close time relative to the last upload.

---

## Self-Review

**Spec coverage.** Task 0 → the unexplained step change; 1 → the two `meeting_session` columns and `recordings.session_id`; 2 → populating it; 3 → arm/count/`end_group` deferral, plus the "no estimate" rule; 4 → the racing-completes hole; 5 → the parameter split with full wiring; 6 → the device half including the video timing rule; 7 → the three real-device cases.

**Not covered by any task, deliberately:** stops from a lost camera or a failure path still send only the fire-and-forget idle close and still wait the full timeout. The spec says so; no task pretends otherwise.

**Type consistency.** `mark_end_armed(conn, session_id, expected_chunks)`, `uploaded_chunk_count(conn, session_id) -> int`, `_end_marker_fields(body) -> int | None`, `_apply_end_marker(conn, session_id, expected)` are used with those signatures in Tasks 3 and 4.

**Ordering risk.** Task 2 must land before Task 3 — the count reads the column Task 2 populates, and on a database where old rows have `session_id` NULL the count under-reports, which closes sessions early. **Task 3's close path must therefore not go live until Task 2 has been deployed and new rows are being written.** Since a session only arms when its device sends the marker (Task 6, later still), this ordering is naturally safe — but it is the one sequencing that matters.
