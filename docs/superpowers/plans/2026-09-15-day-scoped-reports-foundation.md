# Day-scoped reports — foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user generate a report over a whole day (every meeting in it, optionally narrowed to chosen topics) from the Timeline's "All day" view, with every deletion path closed, and let a worker do so for their own day.

**Architecture:** One server-side core selects a day's reportable topic rows and applies both deletion arms; the existing per-meeting routes and three new `/days/{date}/report…` routes sit on it. The existing S3-triggered session-report worker learns a `scope: "day"` artifact written under a `day/` key segment, and checks every session in it against the deletion mirror of every folder involved. The frontend's report client and review modal take a `scope`; the All-day button opens the modal with `scope: 'day'`.

**Tech Stack:** Python 3.12 Lambdas (org-api in-VPC; session-report worker non-VPC), Aurora PostgreSQL via psycopg, S3, SAM; vanilla-JS/React-via-Babel frontend with `node --test`.

**Spec:** `docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md` — this plan implements its §9 steps 2–5, including review findings F2, F5, F7 and §11.2. Read §5.1, §5.2, §5.7, §9 and §11 before starting.

**Not in this plan (later plans):** the generation step (§9 step 6; F3, F8 footer, F10), recording blocks (§9 step 7; F1, F4, F9), template storage (§9 step 8), the ordering-timezone wording (F6).

## Global Constraints

- Backend repo `fieldsight-pipeline`, base branch `develop`. Frontend repo `fieldsight-ui`, base branch `dev`.
- Work in a fresh worktree off `origin/develop` / `origin/dev` — other sessions keep uncommitted changes in the main checkouts: `git -C C:/Users/camil/Dropbox/fieldsight-pipeline worktree add C:/Users/camil/fswork/<name> -b <branch> origin/develop`. On this machine `git -C` needs `C:/` paths, not `/c/...`.
- Windows + `core.autocrlf=true`: stage explicit paths only. **Never `git add -A`.**
- Backend tests: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2` then `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit -q` from the worktree root.
- Frontend tests: `node --test tests/*.test.js` from the worktree root.
- Code comments, commit messages and PR bodies in English.
- Topic and window times are the device's wall clock. Nothing here converts time zones, and nothing reads or writes `topics.occurred_at`.
- `topicRowIds`: absent = everything in scope; `[]` = 400; more than 200 = 400 (`_selected_topic_row_ids`, unchanged).
- Day artifacts live under the existing prefixes with a `day/` segment: `session_report_requests/{folder}/{date}/day/{requestId}.json`, `session_report_results/{folder}/{date}/day/{requestId}.json`, `session_reports/{folder}/{date}/day/{requestId}.docx`. No new prefix, IAM statement or S3 trigger.
- **Deploy order is load-bearing:** Task 1 (session routes refuse non-session ids) and Task 2 (worker) must be deployed to TEST before the Task 4 generate route is merged.
- A deletion check in a document-producing path is **strict**: if the tombstone lookup fails, nothing is produced or served.
- After every fix, revert the production change and confirm its test goes red, then restore it.
- End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
  ```

---

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `src/session_scope.py` | `is_session_base` — the set of legal session id spellings | 1 |
| `src/lambda_org_api.py` | router guard; `_report_rows_in_scope` core; day preview/generate/status routes | 1, 3, 4, 5 |
| `src/lambda_session_report.py` | worker: `day/` doc key, per-session per-folder deletion check, scope fields on results | 2 |
| `tests/unit/test_session_routes_refuse_non_session_ids.py` | new | 1 |
| `tests/unit/test_session_report_worker_day_scope.py` | new | 2 |
| `tests/unit/test_report_scope_applies_both_deletion_arms.py` | new | 3 |
| `tests/unit/test_org_api_sessions.py` | default stub for the new repository call; session route gains the source arm | 3 |
| `tests/unit/test_org_api_day_report.py` | new | 4 |
| `tests/unit/test_org_api_day_report_status.py` | new | 5 |
| `scripts/api/org.js` (ui) | `_reportPath` — one place that builds report URLs for both scopes | 7 |
| `scripts/composites/session-report-modal.js` (ui) | modal takes `scope` | 7 |
| `scripts/pages/timeline.js` (ui) | `generateReportScope`; All-day opens a day report | 7 |
| `scripts/fs-globals.js` (ui) | worker `report:create:self` | 8 |
| `scripts/pages/reports.js` (ui) | legacy Generate/Regenerate gated on `crew` | 8 |
| `app-shell-preview.html` (ui) | cache-buster bumps | 7, 8 |
| `tests/day-report-scope.test.js` (ui) | new | 7 |
| `tests/worker-can-report-own-day.test.js` (ui) | new | 8 |

---

### Task 1: Session routes refuse an id that is not a session (F2)

**Why:** `/sessions/{id}/report/status` builds its result key from the path segment and checks deletion only for that literal id. Once day results exist at `…/day/{rid}.json`, `GET /sessions/day/report/status` would serve a day document with no per-session deletion check. This must be deployed before anything writes a `day/` key.

**Files:**
- Modify: `src/session_scope.py` (add after `_CHUNK_SESSION_BASE_RE`)
- Modify: `src/lambda_org_api.py` (module constant near `REPORT_DATE_RE`; guard in `dispatch` immediately before `m_srp = re.match(r"^/sessions/([^/]+)/report/preview$", route)`)
- Test: `tests/unit/test_session_routes_refuse_non_session_ids.py`

**Interfaces:**
- Produces: `session_scope.is_session_base(value: str | None) -> bool`

- [ ] **Step 1: Write the failing test**

```python
"""Every /sessions/{id}/… read route refuses an id that is not a session.

The routes match `([^/]+)` and build S3 keys from that segment. The day-scoped
report writes its result under `session_report_results/{folder}/{date}/day/…`,
so without this guard `GET /sessions/day/report/status` resolves to a DAY
document and checks deletion only for the literal session "day" — which is in
no mirror. Spec 2026-09-15 §11, finding F2.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import session_scope  # noqa: E402

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}

ROUTES = [
    ("POST", "report/preview", "session_report_preview"),
    ("POST", "report", "session_report_generate"),
    ("GET", "report/status", "session_report_status"),
    ("GET", "rolling", "session_rolling"),
    ("GET", "brief", "session_brief_read"),
]

BAD = ["day", "..", "latest", "sid" + "a" * 31, "sid" + "A" * 32, "grp" + "z" * 32, "sidday"]
GOOD = ["sid" + "a" * 32, "a" * 32, "grp" + "b" * 32,
        "Benl1_2026-07-25_13-00-11", "ben_ucpk2_2026-09-02_10-55-11"]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event(method, path):
    return {"httpMethod": method, "path": path, "queryStringParameters": {},
            "body": None, "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


@pytest.fixture
def routed(monkeypatch):
    calls = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    for _, _, name in ROUTES:
        def handler(conn, caller, sid, event, _n=name):
            calls.append((_n, sid))
            return {"statusCode": 200, "body": "{}"}
        monkeypatch.setattr(org, name, handler)
    return calls


@pytest.mark.parametrize("method,suffix,name", ROUTES, ids=[r[1] for r in ROUTES])
@pytest.mark.parametrize("bad", BAD)
def test_a_non_session_id_is_refused_before_any_handler_runs(routed, method, suffix, name, bad):
    res = org.lambda_handler(_event(method, f"/api/org/sessions/{bad}/{suffix}"), None)
    assert res["statusCode"] == 400, f"{name} accepted {bad!r}"
    assert routed == [], f"{name} ran for {bad!r}"


@pytest.mark.parametrize("method,suffix,name", ROUTES, ids=[r[1] for r in ROUTES])
@pytest.mark.parametrize("good", GOOD)
def test_every_real_session_spelling_still_reaches_its_handler(routed, method, suffix, name, good):
    res = org.lambda_handler(_event(method, f"/api/org/sessions/{good}/{suffix}"), None)
    assert res["statusCode"] == 200
    assert routed == [(name, good)]


def test_is_session_base_names_the_four_spellings():
    for good in GOOD:
        assert session_scope.is_session_base(good), good
    for bad in BAD + ["", None]:
        assert not session_scope.is_session_base(bad), bad
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_session_routes_refuse_non_session_ids.py -q`
Expected: FAIL — `AttributeError: module 'session_scope' has no attribute 'is_session_base'`, and the BAD cases return 200.

- [ ] **Step 3: Implement**

In `src/session_scope.py`, directly after `_CHUNK_SESSION_BASE_RE = re.compile(r"^sid([0-9a-f]{32})$")`:

```python
# Every spelling a session is addressed by in the /sessions/{id}/… routes:
#   sid{32hex}                           a chunk session (extract_session's base)
#   {32hex}                              the same session, bare (brief/rolling/status accept it)
#   grp{32hex}                           a multi-device merged meeting (lambda_finalize_claim)
#   {device}_{YYYY-MM-DD}_{HH-MM-SS}     a legacy whole-file base
# Anything else is not a session, and a route that builds an S3 key from it can be
# pointed at a key that is not a session's -- `day/` being the one that matters.
SESSION_BASE_RE = re.compile(
    r"^(?:(?:sid|grp)?[0-9a-f]{32}"
    r"|[A-Za-z0-9][A-Za-z0-9._-]*_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$"
)


def is_session_base(value):
    """True when `value` is one of the spellings a session is addressed by."""
    return bool(value) and bool(SESSION_BASE_RE.match(value))
```

In `src/lambda_org_api.py`, next to `REPORT_DATE_RE`:

```python
# The /sessions/{id}/… routes that build an S3 key or a scope from {id}.
_SESSION_KEYED_ROUTE_RE = re.compile(
    r"^/sessions/([^/]+)/(?:report/preview|report|report/status|rolling|brief)$")
```

In `dispatch`, immediately before `m_srp = re.match(r"^/sessions/([^/]+)/report/preview$", route)`:

```python
    # F2: an id that is not a session never reaches a handler that builds a key from it.
    m_sk = _SESSION_KEYED_ROUTE_RE.match(route)
    if m_sk and not session_scope.is_session_base(m_sk.group(1)):
        return error("not a session id", 400)
```

- [ ] **Step 4: Run the test to verify it passes, then the whole suite**

Run the Step 2 command. Expected: PASS.
Run the full backend suite (Global Constraints). Expected: no new failures.

- [ ] **Step 5: Revert-check**

Comment out the three guard lines in `dispatch`, run the Step 2 command, confirm the BAD cases fail, restore the lines, run again, confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/session_scope.py src/lambda_org_api.py tests/unit/test_session_routes_refuse_non_session_ids.py
git commit -m "Session routes refuse an id that is not a session"   # + attribution lines
```

---

### Task 2: The worker learns a day-scoped request (F7)

**Why:** `_doc_key` reads `artifact['sessionId']` (KeyError for a day), and `_session_was_deleted` returns False when there is no session id — a day artifact would skip the deletion check entirely. A merged meeting's tombstone lands in the **lead's** mirror, so checking only the requester's folder misses it.

**Files:**
- Modify: `src/lambda_session_report.py` (`_doc_key`, `_session_was_deleted`, `process_request`; add `_is_day`, `_scope_ids_and_folders`, `_scope_result_fields`)
- Test: `tests/unit/test_session_report_worker_day_scope.py`

**Interfaces:**
- Consumes (from Task 4, via the S3 artifact): `{"scope": "day", "requestId": str, "date": "YYYY-MM-DD", "folder": str, "sessionIds": [str], "mirrorFolders": [str], "resultKey": str, …}`
- Produces (result JSON read by Task 5): every result payload for a day request carries `"scope": "day"`, `"sessionIds": [str]`, `"mirrorFolders": [str]`; `done` also carries `"docKey"`.

- [ ] **Step 1: Write the failing test**

```python
"""The session-report worker renders a day-scoped request safely.

A day bundles several sessions, possibly including a multi-device meeting whose
tombstone lives in the LEAD's mirror. Every session is checked against the mirror
of every folder involved; the document key uses a `day/` segment; the result
carries the scope so the status route can re-check before presigning.
Spec 2026-09-15 §9 step 3, finding F7.
"""
import io

import pytest

rep = pytest.importorskip("lambda_session_report", reason="requires boto3 (installed in CI)")
import deletion_mirror  # noqa: E402

A = "a" * 32
B = "b" * 32


def _day(**over):
    a = {"scope": "day", "requestId": "r-1", "date": "2026-09-10", "folder": "James_Lamb",
         "sessionIds": ["sid" + A, "grp" + B], "mirrorFolders": ["James_Lamb", "Lead_F"],
         "deliver": "email", "recipients": ["x@example.nz"],
         "content": {"date": "2026-09-10", "topics": []},
         "resultKey": "session_report_results/James_Lamb/2026-09-10/day/r-1.json"}
    a.update(over)
    return a


def _run(monkeypatch, artifact, deleted_by_folder=None):
    sent, written, put, asked = [], [], [], []

    def fake_deleted(s3, bucket, folder, date, strict=False):
        asked.append(folder)
        return set((deleted_by_folder or {}).get(folder, set()))

    monkeypatch.setattr(deletion_mirror, "deleted_sessions", fake_deleted)
    monkeypatch.setattr(rep, "_send_email", lambda a: sent.append(a))
    monkeypatch.setattr(rep, "_write_result", lambda k, p: written.append((k, p)))
    monkeypatch.setattr(rep, "_content_to_minutes", lambda a: ({}, "A day"))
    monkeypatch.setattr(rep, "generate_word_document", lambda m, t: io.BytesIO(b"x"))
    monkeypatch.setattr(rep, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: put.append(kw["Key"]))})())
    rep.process_request(artifact)
    return sent, written, put, asked


def test_a_day_document_is_keyed_under_day():
    assert rep._doc_key(_day()) == "session_reports/James_Lamb/2026-09-10/day/r-1.docx"


def test_a_day_with_no_deletions_renders_and_records_its_scope(monkeypatch):
    sent, written, put, _ = _run(monkeypatch, _day())
    assert put == ["session_reports/James_Lamb/2026-09-10/day/r-1.docx"]
    assert len(sent) == 1
    key, payload = written[-1]
    assert key == "session_report_results/James_Lamb/2026-09-10/day/r-1.json"
    assert payload["status"] == "done"
    assert payload["scope"] == "day"
    assert payload["sessionIds"] == ["sid" + A, "grp" + B]
    assert payload["mirrorFolders"] == ["James_Lamb", "Lead_F"]


def test_one_deleted_session_stops_the_whole_day(monkeypatch):
    sent, written, put, _ = _run(monkeypatch, _day(), {"James_Lamb": {"sid" + A}})
    assert sent == [] and put == []
    assert written[-1][1]["status"] == "skipped"


def test_a_merged_meeting_deleted_in_the_leads_mirror_stops_the_day(monkeypatch):
    sent, written, put, asked = _run(monkeypatch, _day(), {"Lead_F": {"grp" + B}})
    assert "Lead_F" in asked, "the lead's mirror was never read"
    assert sent == [] and put == []
    assert written[-1][1]["status"] == "skipped"


def test_the_bare_spelling_in_a_mirror_still_matches(monkeypatch):
    sent, _, put, _ = _run(monkeypatch, _day(), {"James_Lamb": {A}})
    assert sent == [] and put == []


def test_a_day_request_without_session_ids_is_an_error_not_a_render(monkeypatch):
    sent, written, put, asked = _run(monkeypatch, _day(sessionIds=[]))
    assert sent == [] and put == [] and asked == []
    assert written[-1][1]["status"] == "error"


def test_a_session_request_result_is_unchanged(monkeypatch):
    sid = "c" * 32
    artifact = {"resultKey": "session_report_results/r-2.json", "requestId": "r-2",
                "folder": "Ben_UCPK2", "date": "2026-08-27", "sessionId": sid,
                "deliver": "download", "content": {"date": "2026-08-27", "topics": []}}
    _, written, _, _ = _run(monkeypatch, artifact)
    payload = written[-1][1]
    assert payload["status"] == "done"
    assert "scope" not in payload and "sessionIds" not in payload
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_session_report_worker_day_scope.py -q`
Expected: FAIL — `KeyError: 'sessionId'` from `_doc_key`, and the deletion tests render.

- [ ] **Step 3: Implement**

Replace `_doc_key` in `src/lambda_session_report.py` and add the helpers after it:

```python
def _is_day(artifact):
    return artifact.get("scope") == "day"


def _doc_key(artifact):
    """The Word doc lives under a DEDICATED session_reports/ prefix — NOT the
    nightly meeting_minutes/ path — so this on-demand Delivery-C artifact never
    collides with the report-generator's manifest machinery (BUG-18 sidestepped;
    authority-flip already de-dupes the nightly path). A day report takes the
    literal `day` segment where a session report takes its session id; the session
    routes refuse `day` as an id (spec 2026-09-15 F2)."""
    segment = "day" if _is_day(artifact) else artifact["sessionId"]
    return (f"session_reports/{artifact['folder']}/{artifact['date']}/"
            f"{segment}/{artifact['requestId']}.docx")


def _scope_ids_and_folders(artifact):
    """(session ids in scope, folders whose deletion mirror must be read).

    A day names every session it bundles, and every folder those sessions' topics
    were written under -- a merged meeting's rows live under the LEAD's folder, and
    so does its tombstone."""
    folder = artifact.get("folder")
    if _is_day(artifact):
        ids = [str(s).strip() for s in (artifact.get("sessionIds") or []) if str(s).strip()]
        folders = {f for f in (artifact.get("mirrorFolders") or []) if f}
        if folder:
            folders.add(folder)
        return ids, sorted(folders)
    sid = (artifact.get("sessionId") or "").strip()
    return ([sid] if sid else []), ([folder] if folder else [])


def _scope_result_fields(artifact):
    """What a day result must carry so the status route can re-check it."""
    if not _is_day(artifact):
        return {}
    ids, folders = _scope_ids_and_folders(artifact)
    return {"scope": "day", "sessionIds": ids, "mirrorFolders": folders}
```

Replace the body of `_session_was_deleted` (keep its docstring, adding one line: "A day request is deleted when ANY of its sessions is, in ANY of its folders' mirrors."):

```python
    date = artifact.get("date")
    ids, folders = _scope_ids_and_folders(artifact)
    if not (date and ids and folders):
        return False
    try:
        import boto3

        import deletion_mirror
        client = boto3.client("s3")
        deleted = set()
        for folder in folders:
            deleted |= set(deletion_mirror.deleted_sessions(client, S3_BUCKET, folder, date))
    except Exception:
        logger.exception("report: deletion mirror unreadable for %s on %s -- proceeding as "
                         "if nothing was deleted, which may mail a removed recording",
                         folders, date)
        return False
    for sid in ids:
        bare = sid[3:] if sid.startswith("sid") else sid
        if sid in deleted or bare in deleted or f"sid{bare}" in deleted:
            return True
    return False
```

In `process_request`, directly after `request_id = artifact.get("requestId")`:

```python
    scope_fields = _scope_result_fields(artifact)
    if _is_day(artifact) and not scope_fields["sessionIds"]:
        # A day with no sessions cannot be checked against any mirror, so it is not rendered.
        _write_result(result_key, {"status": "error", "requestId": request_id,
                                   "error": "day request carries no sessionIds"})
        return
```

and add `**scope_fields` to each of the four `_write_result` payload dicts in `process_request` (skipped, the `buf is None` error, done, and the exception error), e.g.:

```python
        _write_result(result_key, {"status": "skipped", "requestId": request_id,
                                   "reason": "recording deleted", **scope_fields})
```

- [ ] **Step 4: Run the new test, the existing worker tests, then the suite**

Run Step 2's command. Expected: PASS.
Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_session_report_worker.py tests/unit/test_session_report_skips_deleted.py -q` Expected: PASS.
Run the full suite. Expected: no new failures.

- [ ] **Step 5: Revert-check**

Change `for folder in folders:` to `for folder in folders[:1]:`; confirm `test_a_merged_meeting_deleted_in_the_leads_mirror_stops_the_day` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_session_report.py tests/unit/test_session_report_worker_day_scope.py
git commit -m "The report worker checks every session and folder of a day"   # + attribution lines
```

---

### Task 3: One scope core, both deletion arms (§11.2)

**Why:** The session route filters deleted content by topic id only. The nightly ingest re-creates a day's topics with new uuids, which no topic tombstone names, so deleted content returns the next morning (`src/deleted_predicates.py`: "Both arms or neither"). One core applies both arms for every folder the rows come from, and both the existing session route and the new day routes use it.

**Files:**
- Modify: `src/lambda_org_api.py` — add `_deleted_prefixes_for_rows` and `_report_rows_in_scope` directly above `_assemble_session_report`; replace the row loop inside `_assemble_session_report`
- Modify: `tests/unit/test_org_api_sessions.py` — default stub in the `wired` fixture; one new test
- Test: `tests/unit/test_report_scope_applies_both_deletion_arms.py`

**Interfaces:**
- Produces:
  - `_deleted_prefixes_for_rows(conn, rows: list[dict], date: str) -> set[str]` — raises if the tombstone lookup raises
  - `_report_rows_in_scope(conn, caller: dict, folder: str, date: str, session_id: str | None = None, selected: set[str] | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_report_scope_applies_both_deletion_arms.py`:

```python
"""A report's scope drops deleted content by BOTH arms of the tombstone.

  topic arm  -- the rows that exist now (redactions keyed by topic id);
  source arm -- the rows ingest re-creates tomorrow under NEW uuids, which only a
                recording tombstone on the source key still names.

The session route applied only the first. Source prefixes are read for every
folder the rows come from, because a merged meeting's rows -- and its tombstone --
sit under the lead's folder. Spec 2026-09-15 §11.2.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-09-10"
SID_A = "sid" + "a" * 32
SID_B = "sid" + "b" * 32
GRP = "grp" + "c" * 32
CALLER = {"id": "u-1", "company_id": "c-1", "folder_name": "James_Lamb", "global_role": "admin"}


def _row(rid, key, work_class="work"):
    return {"id": rid, "source_s3_key": key, "work_class": work_class, "site_name": "Waipuna"}


ROWS = [
    _row("t-1", f"extractions/James_Lamb/{DATE}/{SID_A}.json"),
    _row("t-2", f"extractions/James_Lamb/{DATE}/{SID_B}.json"),
    _row("t-3", f"extractions/Lead_F/{DATE}/{GRP}.json"),
    _row("t-4", f"reports/{DATE}/James_Lamb/daily_report.json"),
    _row("t-5", f"extractions/James_Lamb/{DATE}/{SID_A}.json", work_class="non_work"),
]


@pytest.fixture
def scope(monkeypatch):
    asked = []
    state = {"redacted": {}, "prefixes": {}}
    monkeypatch.setattr(org, "_day_report_rows", lambda conn, caller, folder, date: list(ROWS))
    monkeypatch.setattr(org.redactions, "list_active_for_topics",
                        lambda conn, ids: dict(state["redacted"]))

    def prefixes(conn, folder=None, date=None):
        asked.append(folder)
        return list(state["prefixes"].get(folder, []))

    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", prefixes)
    return state, asked


def _ids(rows):
    return [r["id"] for r in rows]


def test_a_day_keeps_every_session_and_drops_non_extraction_and_non_work(scope):
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)) == ["t-1", "t-2", "t-3"]


def test_a_session_scope_keeps_only_that_session(scope):
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE,
                                          session_id=SID_B)) == ["t-2"]


def test_the_topic_arm_drops_a_tombstoned_topic(scope):
    state, _ = scope
    state["redacted"] = {"t-2": {"id": "r-1"}}
    assert "t-2" not in _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE))


def test_the_source_arm_drops_a_recreated_topic_no_topic_tombstone_names(scope):
    state, _ = scope
    state["prefixes"] = {"James_Lamb": [f"extractions/James_Lamb/{DATE}/{SID_A}"]}
    assert _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)) == ["t-2", "t-3"]


def test_a_merged_meeting_is_dropped_by_the_leads_tombstone(scope):
    state, asked = scope
    state["prefixes"] = {"Lead_F": [f"extractions/Lead_F/{DATE}/{GRP}"]}
    assert "t-3" not in _ids(org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE))
    assert sorted(set(asked)) == ["James_Lamb", "Lead_F"]


def test_a_selection_only_narrows(scope):
    got = org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE, selected={"t-2", "t-999"})
    assert _ids(got) == ["t-2"]


def test_a_failed_tombstone_lookup_raises_rather_than_including_everything(scope, monkeypatch):
    def boom(conn, folder=None, date=None):
        raise RuntimeError("redactions unreadable")
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes", boom)
    with pytest.raises(RuntimeError):
        org._report_rows_in_scope(None, CALLER, "James_Lamb", DATE)
```

In `tests/unit/test_org_api_sessions.py`, add to the `wired` fixture (after the `list_active_for_topics` stub):

```python
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes",
                        lambda conn, folder=None, date=None: [])
```

and add this test after `test_preview_excludes_redacted_and_non_work`:

```python
def test_preview_excludes_a_session_whose_recording_was_deleted(wired):
    """The overnight case: ingest re-created the topics under new uuids, so no topic
    tombstone names them -- only the recording tombstone on the source key does."""
    _wire_rows(wired, [
        _row(id="t-recreated-1", source_s3_key=KEY_1300, title="Recreated one"),
        _row(id="t-recreated-2", source_s3_key=KEY_1300, title="Recreated two"),
    ])
    wired.setattr(org.redactions, "deleted_source_prefixes",
                  lambda conn, folder=None, date=None:
                  ["extractions/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11"]
                  if folder == "Ada_L" else [])
    assert _preview(SESSION_1300, PREVIEW_PARAMS)["statusCode"] == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_report_scope_applies_both_deletion_arms.py tests/unit/test_org_api_sessions.py -q`
Expected: FAIL — `AttributeError: … has no attribute '_report_rows_in_scope'`, and the recreated-topic preview returns 200.

- [ ] **Step 3: Implement**

In `src/lambda_org_api.py`, directly above `def _assemble_session_report`:

```python
def _deleted_prefixes_for_rows(conn, rows, date):
    """Recording tombstones (source arm) for every folder these rows come from.

    Every folder, not only the requester's: a multi-device meeting's rows are written
    under the LEAD's folder, and so is its tombstone. STRICT -- a failed lookup raises,
    because this feeds a document; producing one that includes a deleted recording is
    worse than producing none (the same trade `_session_was_removed` makes)."""
    folders = sorted({parsed[0] for parsed in
                      (session_scope.parse_extraction_key(r.get("source_s3_key")) for r in rows)
                      if parsed})
    prefixes = set()
    for folder in folders:
        for p in redactions.deleted_source_prefixes(conn, folder, date) or []:
            if isinstance(p, str) and p:
                prefixes.add(p)
    return prefixes


def _report_rows_in_scope(conn, caller, folder, date, session_id=None, selected=None):
    """The topic rows a report may contain, for one session or (session_id=None) the day.

    Scope integrity: rows come from `_day_report_rows` (ACL, site clip, joiners), then
    lose extraction-less rows, non_work, both deletion arms (topic tombstones AND
    recording tombstones on the source key -- `deleted_predicates.py`: both arms or
    neither), and finally intersect with the caller's chosen ids. A selection can only
    narrow."""
    rows = _day_report_rows(conn, caller, folder, date)
    if not rows:
        return []
    redacted = redactions.list_active_for_topics(conn, [r["id"] for r in rows])
    deleted = _deleted_prefixes_for_rows(conn, rows, date)
    kept = []
    for r in rows:
        sid, kind = session_scope.session_ref(r.get("source_s3_key"))
        if kind != session_scope.KIND_EXTRACTION:
            continue
        if session_id is not None and sid != session_id:
            continue
        if r["id"] in redacted or r.get("work_class") == "non_work":
            continue
        key = r.get("source_s3_key") or ""
        if any(key.startswith(p) for p in deleted):
            continue
        kept.append(r)
    if selected is not None:
        kept = [r for r in kept if str(r["id"]) in selected]
    return kept
```

In `_assemble_session_report`, replace everything from `rows = _day_report_rows(conn, caller, folder, date)` up to (not including) `if not srows:` with:

```python
    srows = _report_rows_in_scope(conn, caller, folder, date,
                                  session_id=session_id, selected=selected)
```

- [ ] **Step 4: Run the tests, then the full suite**

Run Step 2's command. Expected: PASS.
Run the full suite. Any new failure of the form `AttributeError: 'FakeConn' object has no attribute 'cursor'` inside `deleted_source_prefixes` is a shared fixture that does not stub the new call: add the same `deleted_source_prefixes` default stub to that fixture and re-run. Expected end state: no new failures.

- [ ] **Step 5: Revert-check**

Replace `deleted = _deleted_prefixes_for_rows(conn, rows, date)` with `deleted = set()`; confirm the source-arm, merged-meeting and recreated-topic tests fail; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_report_scope_applies_both_deletion_arms.py tests/unit/test_org_api_sessions.py
git commit -m "A report's scope drops deleted recordings by both tombstone arms"   # + attribution lines
```

---

### Task 4: Day preview and generate routes

**Merge only after Tasks 1 and 2 are deployed to TEST** (Task 6, Step 3).

**Files:**
- Modify: `src/lambda_org_api.py` — add `_delivery_from_body`, `_assemble_day_report`, `day_report_preview`, `day_report_generate` after `session_report_generate`; use `_delivery_from_body` inside `session_report_generate`; router entries next to the session report routes
- Test: `tests/unit/test_org_api_day_report.py`

**Interfaces:**
- Consumes: `_report_rows_in_scope` (Task 3); `build_day_sessions(conn, caller, folder, date, rows) -> (list[dict], dict)` (each session has `session_id`, `started_at`, `topic_row_ids`); `render_report_shape(rows, doc, date, folder, conn) -> dict` with `topics` (each topic carries `topic_row_id`); `parse_time_range(str) -> (start_min, end_min) | None` (already imported from `photo_binding`); `_session_participants(rows) -> list[str]`; `_selected_topic_row_ids(body) -> (set | None, error | None)`.
- Produces:
  - `POST /api/org/days/{date}/report/preview?user=<folder>` → 200 `{scope:"day", date, title, siteNames, startedAt, participants, topics, sessionIds, fieldDefaults}`
  - `POST /api/org/days/{date}/report?user=<folder>` → 202 `{status:"queued", scope:"day", date, requestId, resultKey}`; writes the artifact `session_report_requests/{folder}/{date}/day/{requestId}.json` with `scope`, `sessionIds`, `mirrorFolders`, `resultKey` (consumed by Task 2)
  - `_delivery_from_body(body) -> (deliver, recipients, error)`

- [ ] **Step 1: Write the failing test**

```python
"""POST /days/{date}/report/preview and /days/{date}/report.

A day report is every reportable topic of one person's day, across meetings,
through the same scope core as a meeting report. Spec 2026-09-15 §5.1, §5.2.
"""
import json
import re

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE = "2026-07-25"
S1300 = "Benl1_2026-07-25_13-00-11"
S1405 = "Benl1_2026-07-25_14-05-00"
KEY_1300 = f"extractions/Ada_L/{DATE}/{S1300}.json"
KEY_1405 = f"extractions/Ada_L/{DATE}/{S1405}.json"
CALLER = {"id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
          "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event(method, path, params=None, body=None):
    return {"httpMethod": method, "path": path, "queryStringParameters": params,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _row(**over):
    base = {"id": "t-1", "site_id": SITE_ID, "site_name": "UC PK", "user_name": "Ada L",
            "source_s3_key": KEY_1300, "category": "progress", "title": "Slab pour",
            "summary": "Discussed the pour.", "time_range": "13:00 – 13:40",
            "participants": ["Ben"], "work_class": "work", "action_items": [],
            "safety_observations": [], "findings": [], "photos": []}
    base.update(over)
    return base


@pytest.fixture
def day(monkeypatch):
    puts = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name",
                        lambda conn, cid, folder: {"id": "u-2", "folder_name": folder})
    monkeypatch.setattr(org.redactions, "list_active_for_topics", lambda conn, ids: {})
    monkeypatch.setattr(org.redactions, "deleted_source_prefixes",
                        lambda conn, folder=None, date=None: [])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org.recordings, "duration_for_media",
                        lambda conn, cid, folder, date, sb: None)
    monkeypatch.setattr(org, "s3", lambda: type("S", (), {
        "put_object": staticmethod(lambda **kw: puts.append(kw))})())
    return monkeypatch, puts


def _rows(mp, rows):
    mp.setattr(org.topics, "list_topics_for_source_prefix", lambda conn, prefix, **k: list(rows))


ROWS = [
    _row(id="t-late", source_s3_key=KEY_1300, title="Late in first meeting", time_range="13:30 – 13:40"),
    _row(id="t-second", source_s3_key=KEY_1405, title="Second meeting", time_range="14:05 – 14:20"),
    _row(id="t-early", source_s3_key=KEY_1300, title="Early in first meeting", time_range="13:05 – 13:10"),
    _row(id="t-personal", source_s3_key=KEY_1300, title="Personal", work_class="non_work"),
]


def _preview(params=None, body=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report/preview",
                                     params or {"user": "Ada_L"}, body), None)


def _generate(body, params=None):
    return org.lambda_handler(_event("POST", f"/api/org/days/{DATE}/report",
                                     params or {"user": "Ada_L"}, body), None)


def test_a_day_preview_spans_every_meeting_in_order(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = _preview()
    assert res["statusCode"] == 200
    b = json.loads(res["body"])
    assert b["scope"] == "day"
    assert [t["topic_title"] for t in b["topics"]] == [
        "Early in first meeting", "Late in first meeting", "Second meeting"]
    assert b["sessionIds"] == [S1300, S1405]
    assert b["siteNames"] == ["UC PK"]
    assert b["title"] == f"{DATE} · UC PK"
    assert b["fieldDefaults"]["site"] == "UC PK"


def test_a_day_with_nothing_reportable_says_so(day):
    mp, _ = day
    _rows(mp, [_row(id="t-p", work_class="non_work")])
    res = _preview()
    assert res["statusCode"] == 404
    assert json.loads(res["body"])["error"] == "nothing to report for that day"


def test_a_bad_date_is_a_400(day):
    mp, _ = day
    _rows(mp, ROWS)
    res = org.lambda_handler(_event("POST", "/api/org/days/nope/report/preview", {"user": "Ada_L"}), None)
    assert res["statusCode"] == 400


def test_an_empty_selection_is_refused(day):
    mp, _ = day
    _rows(mp, ROWS)
    assert _preview(body={"topicRowIds": []})["statusCode"] == 400


def test_a_selection_narrows_the_day(day):
    mp, _ = day
    _rows(mp, ROWS)
    b = json.loads(_preview(body={"topicRowIds": ["t-second", "t-personal"]})["body"])
    assert [t["topic_title"] for t in b["topics"]] == ["Second meeting"]
    assert b["sessionIds"] == [S1405]


def test_generate_enqueues_a_day_artifact_under_the_day_segment(day):
    mp, puts = day
    _rows(mp, ROWS)
    res = _generate({"deliver": "download"})
    assert res["statusCode"] == 202
    b = json.loads(res["body"])
    assert b["scope"] == "day" and re.fullmatch(r"[0-9a-f]{32}", b["requestId"])
    assert b["resultKey"] == f"session_report_results/Ada_L/{DATE}/day/{b['requestId']}.json"
    assert len(puts) == 1
    assert puts[0]["Key"] == f"session_report_requests/Ada_L/{DATE}/day/{b['requestId']}.json"
    art = json.loads(puts[0]["Body"])
    assert art["scope"] == "day"
    assert art["sessionIds"] == [S1300, S1405]
    assert art["mirrorFolders"] == ["Ada_L"]
    assert art["resultKey"] == b["resultKey"]
    assert "sessionId" not in art


def test_email_without_recipients_is_refused_and_nothing_is_enqueued(day):
    mp, puts = day
    _rows(mp, ROWS)
    assert _generate({"deliver": "email"})["statusCode"] == 400
    assert puts == []


def test_a_failed_tombstone_lookup_enqueues_nothing(day):
    mp, puts = day
    _rows(mp, ROWS)

    def boom(conn, folder=None, date=None):
        raise RuntimeError("redactions unreadable")
    mp.setattr(org.redactions, "deleted_source_prefixes", boom)
    assert _generate({"deliver": "download"})["statusCode"] == 500
    assert puts == []


def test_the_folder_gate_is_the_media_one(day):
    mp, puts = day
    _rows(mp, ROWS)
    seen = {}

    def gate(conn, caller, user, what="media"):
        seen["what"] = what
        return None, org.error("not permitted to view this user's day report", 403)
    mp.setattr(org, "_resolve_org_media_folder", gate)
    assert _generate({"deliver": "download"})["statusCode"] == 403
    assert seen["what"] == "day report" and puts == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_org_api_day_report.py -q`
Expected: FAIL — the routes return 404 "not found".

- [ ] **Step 3: Implement**

In `src/lambda_org_api.py`, add directly above `def session_report_generate`:

```python
def _delivery_from_body(body):
    """(deliver, recipients, None) or (None, None, error) -- shared by both generate routes."""
    deliver = body.get("deliver", "download")
    if deliver not in ("download", "email"):
        return None, None, error("deliver must be 'download' or 'email'", 400)
    recipients = body.get("recipients") or []
    if deliver == "email" and not recipients:
        return None, None, error("recipients required when deliver='email'", 400)
    return deliver, recipients, None
```

In `session_report_generate`, replace its four `deliver`/`recipients` lines with:

```python
    deliver, recipients, err = _delivery_from_body(body)
    if err is not None:
        return err
```

Add directly below `session_report_generate`:

```python
def _assemble_day_report(conn, caller, date, event, selected=None):
    """Every reportable topic of one (folder, date), across meetings. (content, None) or
    (None, error). Scope comes from `_report_rows_in_scope` with no session, so every
    exclusion a meeting report applies, a day report applies (spec 2026-09-15 §5.2)."""
    if not date or not REPORT_DATE_RE.match(date):
        return None, error("date required (YYYY-MM-DD)", 400)
    p = event.get("queryStringParameters") or {}
    user = (p.get("user") or "").strip()
    folder, err = _resolve_org_media_folder(conn, caller, user, what="day report")
    if err is not None:
        return None, err
    rows = _report_rows_in_scope(conn, caller, folder, date, session_id=None, selected=selected)
    if not rows:
        return None, error("nothing to report for that day", 404)

    # Meetings in their authoritative start order (build_day_sessions), then topics by the
    # parsed start of their time_range, unparseable last -- never the lexical sort of the
    # free text, never created_at (spec §8.4 of the superseded draft).
    sessions, _excluded = build_day_sessions(conn, caller, folder, date, rows)
    rank = {}
    for i, s in enumerate(sessions):
        for tid in s.get("topic_row_ids") or []:
            rank.setdefault(tid, i)

    def order(r):
        rng = parse_time_range(r.get("time_range"))
        return (rank.get(str(r["id"]), len(sessions)), rng is None, rng[0] if rng else 0, str(r["id"]))

    rows = sorted(rows, key=order)
    session_ids = []
    for r in rows:
        sid = session_scope.session_id_from_source_key(r.get("source_s3_key"))
        if sid and sid not in session_ids:
            session_ids.append(sid)
    mirror_folders = sorted({parsed[0] for parsed in
                             (session_scope.parse_extraction_key(r.get("source_s3_key")) for r in rows)
                             if parsed})
    site_names = sorted({r["site_name"] for r in rows if r.get("site_name")})
    starts = [s["started_at"] for s in sessions if s.get("started_at")]
    shaped = render_report_shape(rows, {}, date, folder, conn)
    position = {str(r["id"]): i for i, r in enumerate(rows)}
    topics_out = sorted(shaped["topics"],
                        key=lambda t: position.get(str(t.get("topic_row_id")), len(position)))
    return {
        "scope": "day",
        "date": date,
        "folder": folder,
        "title": f"{date} · {', '.join(site_names)}" if site_names else date,
        "siteNames": site_names,
        "startedAt": min(starts) if starts else None,
        "participants": _session_participants(rows),
        "topics": topics_out,
        "sessionIds": session_ids,
        "mirrorFolders": mirror_folders,
        "topic_row_ids": [str(r["id"]) for r in rows],
    }, None


def day_report_preview(conn, caller, date, event):
    """POST /api/org/days/{date}/report/preview?user= — a whole day, read-only."""
    selected, err = _selected_topic_row_ids(parse_body(event) or {})
    if err is not None:
        return err
    content, err = _assemble_day_report(conn, caller, date, event, selected)
    if err is not None:
        return err
    return ok({
        "scope": "day",
        "date": content["date"],
        "title": content["title"],
        "siteNames": content["siteNames"],
        "startedAt": content["startedAt"],
        "participants": content["participants"],
        "topics": content["topics"],
        "sessionIds": content["sessionIds"],
        "fieldDefaults": {
            "title": content["title"],
            "attendees": content["participants"],
            "date": content["date"],
            "site": ", ".join(content["siteNames"]),
        },
    })


def day_report_generate(conn, caller, date, event):
    """POST /api/org/days/{date}/report?user= — enqueue a day report for the worker.

    Same hand-off as the meeting report (in-VPC org-api writes an S3 request artifact,
    the non-VPC worker renders it), under a `day/` key segment. The artifact names every
    session and every folder in scope so the worker can check each against its deletion
    mirror (Task 2)."""
    body = parse_body(event)
    if body is None:
        return error("malformed JSON body", 400)
    deliver, recipients, err = _delivery_from_body(body)
    if err is not None:
        return err
    selected, err = _selected_topic_row_ids(body)
    if err is not None:
        return err
    content, err = _assemble_day_report(conn, caller, date, event, selected)
    if err is not None:
        return err

    request_id = uuid.uuid4().hex
    folder = content["folder"]
    result_key = f"session_report_results/{folder}/{date}/day/{request_id}.json"
    request_key = f"session_report_requests/{folder}/{date}/day/{request_id}.json"
    artifact = {
        "scope": "day",
        "requestId": request_id,
        "date": date,
        "folder": folder,
        "companyId": str(caller["company_id"]),
        "requestedBy": str(caller["id"]),
        "templateId": body.get("templateId"),
        "requestedTopicRowIds": (sorted(selected) if selected else None),
        "selectedTopicRowIds": content["topic_row_ids"],
        "sessionIds": content["sessionIds"],
        "mirrorFolders": content["mirrorFolders"],
        "title": body.get("title") or content["title"],
        "attendees": body.get("attendees") or content["participants"],
        "fields": body.get("fields") or {},
        "deliver": deliver,
        "recipients": recipients,
        "content": content,
        "resultKey": result_key,
    }
    s3().put_object(Bucket=LAKE_BUCKET, Key=request_key,
                    Body=json.dumps(artifact, default=str),
                    ContentType="application/json")
    return ok({"status": "queued", "scope": "day", "date": date,
               "requestId": request_id, "resultKey": result_key}, 202)
```

In `dispatch`, immediately after the `m_srs` session status block:

```python
    m_drp = re.match(r"^/days/([^/]+)/report/preview$", route)
    if m_drp and method == "POST":
        return day_report_preview(conn, caller, m_drp.group(1), event)
    m_drg = re.match(r"^/days/([^/]+)/report$", route)
    if m_drg and method == "POST":
        return day_report_generate(conn, caller, m_drg.group(1), event)
```

- [ ] **Step 4: Run the test, the session report tests, then the suite**

Run Step 2's command. Expected: PASS.
Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_org_api_sessions.py tests/unit/test_session_report_topic_selection.py -q` Expected: PASS.
Run the full suite. Expected: no new failures.

- [ ] **Step 5: Revert-check**

In `_assemble_day_report`, replace `rows = sorted(rows, key=order)` with a no-op; confirm `test_a_day_preview_spans_every_meeting_in_order` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_org_api_day_report.py
git commit -m "Report on a whole day, across its meetings"   # + attribution lines
```

---

### Task 5: Day report status refuses a document with a deleted session (F7)

**Files:**
- Modify: `src/lambda_org_api.py` — add `_REQUEST_ID_RE`, `_is_removed_spelling`, `_any_session_removed`, `day_report_status`; make `_session_was_removed` use `_is_removed_spelling`; router entry
- Test: `tests/unit/test_org_api_day_report_status.py`

**Interfaces:**
- Consumes: result JSON written by Task 2 (`status`, `docKey`, `sessionIds`, `mirrorFolders`, `emailed`, `error`).
- Produces: `GET /api/org/days/{date}/report/status?user=&requestId=` → `{status: pending|done|removed|error|skipped, docUrl?, emailed?, error?}`

- [ ] **Step 1: Write the failing test**

```python
"""GET /days/{date}/report/status — never presign a day with a deleted session.

A presigned URL outlives the check that produced it, so every session named in
the result is re-checked against every folder's mirror BEFORE a URL is built. A
result that names no sessions cannot be checked and is not served.
Spec 2026-09-15 §9 step 3, finding F7.
"""
import json

import pytest
from botocore.exceptions import ClientError

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import deletion_mirror  # noqa: E402

CALLER = {"id": "u-1", "company_id": "c-1", "global_role": "admin"}
DATE = "2026-09-10"
RID = "d" * 32
A = "sid" + "a" * 32
GRP = "grp" + "b" * 32


class _Presigned(Exception):
    pass


def _event(**extra):
    params = {"user": "James_Lamb", "requestId": RID}
    params.update(extra)
    return {"queryStringParameters": params}


def _wire(monkeypatch, stored=None, removed_by_folder=None, missing=False, presign_raises=False):
    got = []
    payload = json.dumps(stored or {}).encode("utf-8")

    class _Body:
        def read(self):
            return payload

    class _S3:
        def get_object(self, Bucket, Key):
            got.append(Key)
            if missing:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": _Body()}

        def generate_presigned_url(self, *a, **kw):
            if presign_raises:
                raise _Presigned("a day with a deleted session must never reach a presign")
            return "https://signed.example/day.docx"

    monkeypatch.setattr(oa, "s3", lambda: _S3())
    monkeypatch.setattr(oa, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("James_Lamb", None))
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict",
                        lambda s3, bucket, folder, date: set((removed_by_folder or {}).get(folder, set())))
    return got


DONE = {"status": "done", "docKey": "session_reports/James_Lamb/2026-09-10/day/x.docx",
        "sessionIds": [A, GRP], "mirrorFolders": ["James_Lamb", "Lead_F"], "emailed": False}


def test_pending_until_the_worker_writes_a_result(monkeypatch):
    got = _wire(monkeypatch, missing=True)
    res = oa.day_report_status(None, CALLER, DATE, _event())
    assert json.loads(res["body"]) == {"status": "pending"}
    assert got == [f"session_report_results/James_Lamb/{DATE}/day/{RID}.json"]


def test_done_presigns_when_nothing_was_deleted(monkeypatch):
    _wire(monkeypatch, stored=DONE)
    b = json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"])
    assert b["status"] == "done" and b["docUrl"].startswith("https://")


def test_a_deleted_session_is_removed_and_never_presigned(monkeypatch):
    _wire(monkeypatch, stored=DONE, removed_by_folder={"James_Lamb": {A}}, presign_raises=True)
    assert json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"]) == {"status": "removed"}


def test_a_merged_meeting_deleted_in_the_leads_mirror_is_removed(monkeypatch):
    _wire(monkeypatch, stored=DONE, removed_by_folder={"Lead_F": {GRP}}, presign_raises=True)
    assert json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"]) == {"status": "removed"}


def test_a_result_naming_no_sessions_is_not_served(monkeypatch):
    _wire(monkeypatch, stored=dict(DONE, sessionIds=[]), presign_raises=True)
    b = json.loads(oa.day_report_status(None, CALLER, DATE, _event())["body"])
    assert b["status"] == "error"


@pytest.mark.parametrize("bad", ["", "r-1", "../x", "D" * 32, "d" * 31])
def test_a_malformed_request_id_is_a_400(monkeypatch, bad):
    _wire(monkeypatch, stored=DONE)
    assert oa.day_report_status(None, CALLER, DATE, _event(requestId=bad))["statusCode"] == 400


def test_an_unreadable_mirror_raises(monkeypatch):
    _wire(monkeypatch, stored=DONE)

    def boom(s3, bucket, folder, date):
        raise RuntimeError("mirror unreadable")
    monkeypatch.setattr(deletion_mirror, "deleted_sessions_strict", boom)
    with pytest.raises(RuntimeError):
        oa.day_report_status(None, CALLER, DATE, _event())
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_org_api_day_report_status.py -q`
Expected: FAIL — `AttributeError: … has no attribute 'day_report_status'`.

- [ ] **Step 3: Implement**

Next to `REPORT_DATE_RE` in `src/lambda_org_api.py`:

```python
# The worker's request ids are uuid4().hex.
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
```

Directly above `def _session_was_removed`:

```python
def _is_removed_spelling(session_id, removed):
    """Both spellings: the mirror holds whatever base the delete endpoint had."""
    bare = session_id[3:] if session_id.startswith("sid") else session_id
    return session_id in removed or bare in removed or ("sid" + bare) in removed
```

Replace the last two lines of `_session_was_removed` with:

```python
    removed = deletion_mirror.deleted_sessions_strict(s3(), S3_BUCKET, folder, date)
    return _is_removed_spelling(session_id, removed)
```

Directly below `session_report_status`:

```python
def _any_session_removed(session_ids, folders, date):
    """STRICT, like `_session_was_removed`: an unreadable mirror raises."""
    for folder in folders:
        removed = deletion_mirror.deleted_sessions_strict(s3(), S3_BUCKET, folder, date)
        if any(_is_removed_spelling(sid, removed) for sid in session_ids):
            return True
    return False


def day_report_status(conn, caller, date, event):
    """GET /api/org/days/{date}/report/status?user=&requestId= — poll a day report.

    The result names every session and folder in scope (Task 2). Each is re-checked
    against the deletion mirrors before a URL is presigned, because a presign outlives
    the check that produced it."""
    if not date or not REPORT_DATE_RE.match(date):
        return error("date required (YYYY-MM-DD)", 400)
    p = event.get("queryStringParameters") or {}
    user = (p.get("user") or "").strip()
    request_id = (p.get("requestId") or "").strip()
    if not _REQUEST_ID_RE.match(request_id):
        return error("requestId required", 400)
    folder, err = _resolve_org_media_folder(conn, caller, user, what="day report status")
    if err is not None:
        return err

    result_key = f"session_report_results/{folder}/{date}/day/{request_id}.json"
    try:
        obj = s3().get_object(Bucket=LAKE_BUCKET, Key=result_key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return ok({"status": "pending"})
        raise
    result = json.loads(obj["Body"].read().decode("utf-8"))
    status = result.get("status")
    if status == "done" and result.get("docKey"):
        session_ids = [s for s in (result.get("sessionIds") or []) if s]
        folders = [f for f in (result.get("mirrorFolders") or []) if f] or [folder]
        if not session_ids:
            logger.error("day report %s: result names no sessions -- not served", request_id)
            return ok({"status": "error", "error": "report result is incomplete"})
        if _any_session_removed(session_ids, folders, date):
            logger.info("day report %s: a session in it was deleted -- not served", request_id)
            return ok({"status": "removed"})
        url = s3().generate_presigned_url(
            "get_object", Params={"Bucket": LAKE_BUCKET, "Key": result["docKey"]},
            ExpiresIn=PRESIGNED_URL_EXPIRY)
        return ok({"status": "done", "docUrl": url, "emailed": bool(result.get("emailed"))})
    if status == "error":
        return ok({"status": "error", "error": result.get("error")})
    return ok({"status": status or "pending"})
```

In `dispatch`, after the `m_drg` block from Task 4:

```python
    m_drs = re.match(r"^/days/([^/]+)/report/status$", route)
    if m_drs and method == "GET":
        return day_report_status(conn, caller, m_drs.group(1), event)
```

- [ ] **Step 4: Run the test, the existing removal test, then the suite**

Run Step 2's command. Expected: PASS.
Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit/test_removed_session_is_not_served.py -q` Expected: PASS.
Run the full suite. Expected: no new failures.

- [ ] **Step 5: Revert-check**

Change `for folder in folders:` in `_any_session_removed` to `for folder in folders[:1]:`; confirm the merged-meeting test fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_org_api_day_report_status.py
git commit -m "A day report is never served once a meeting in it is deleted"   # + attribution lines
```

---

### Task 6: Backend deploy order and TEST verification

**Files:** none (release procedure). A green unit suite says nothing about what is deployed (CLAUDE.md BUG-22).

- [ ] **Step 1: PR A — Tasks 1 and 2**

```bash
git push -u origin <branch-A>
gh pr create --base develop --title "Session ids are validated and the report worker understands a day" --body "<what + why; tests; revert-checks>"
```
Wait for CI green, merge, then find the deploy run for the merge commit — `gh run list` right after a merge can show the previous commit's run, so match `headSha`:
```bash
gh run list --workflow deploy.yml --limit 5 --json headSha,status,conclusion,databaseId
```
Expected: the run whose `headSha` is the merge commit is `completed` / `success`.

- [ ] **Step 2: Confirm TEST carries the change**

```bash
export AWS_PROFILE=fieldsight-deployer
D="C:/Users/camil/AppData/Local/Temp/claude/verify"; mkdir -p "$D"
aws lambda get-function --function-name fieldsight-test-session-report --query Code.Location --output text | xargs curl -sL -o "$D/sr.zip"
unzip -p "$D/sr.zip" lambda_session_report.py | grep -c "_scope_ids_and_folders"
aws lambda get-function --function-name fieldsight-test-org-api --query Code.Location --output text | xargs curl -sL -o "$D/oa.zip"
unzip -p "$D/oa.zip" lambda_org_api.py | grep -c "_SESSION_KEYED_ROUTE_RE"
```
Expected: both counts ≥ 1.

- [ ] **Step 3: PR B (Task 3) and PR C (Tasks 4 and 5)**

Open PR B, merge, repeat Steps 1–2 grepping `lambda_org_api.py` for `_report_rows_in_scope`. **Do not merge PR C until Step 2 has passed for PR A.** Then open PR C, merge, repeat Steps 1–2 grepping for `day_report_status`.

- [ ] **Step 4: End-to-end on TEST**

Choose a TEST folder and date with at least two meetings, then call the day routes through the TEST gateway with a valid ID token for a TEST admin (the dev site's session supplies one; copy it from the browser's devtools Authorization header):
```bash
aws apigateway get-rest-apis --query "items[?contains(name,'fieldsight-test')].[id,name]" --output text
# use the id of the TEST stack's API below
TOKEN="<id token>"; GW="https://<test-api-id>.execute-api.ap-southeast-2.amazonaws.com/prod/api/org"
curl -s -X POST "$GW/days/<date>/report/preview?user=<folder>" -H "Authorization: $TOKEN" | head -c 600
curl -s -X POST "$GW/days/<date>/report?user=<folder>" -H "Authorization: $TOKEN" -H "Content-Type: application/json" -d '{"deliver":"download"}'
curl -s "$GW/sessions/day/report/status?date=<date>&user=<folder>&requestId=<rid>" -H "Authorization: $TOKEN"
curl -s "$GW/days/<date>/report/status?user=<folder>&requestId=<rid>" -H "Authorization: $TOKEN"
```
Expected: preview lists topics from both meetings; generate returns 202; the `/sessions/day/…` call returns 400 "not a session id"; the day status call reaches `done` with a `docUrl` that downloads a `.docx`.

- [ ] **Step 5: Promote**

Only after Step 4: open `develop → main`, and let the `production` environment gate be approved by a human.

---

### Task 7: Frontend — the report client, modal and All-day button take a scope

**Depends on:** PR C deployed to the gateway the dev site points at. Check before testing in a browser:
`aws amplify get-branch --app-id d2fssznicvuckr --branch-name dev --query 'branch.environmentVariables.FS_ORG_BASEURL'`

**Files:**
- Modify: `scripts/api/org.js` — add `_reportPath`; use it in `getSessionReportPreview`, `generateSessionReport`, `getSessionReportStatus`
- Modify: `scripts/composites/session-report-modal.js` — `buildGeneratePayload` carries a day scope; the component builds scope options from `props.scope`
- Modify: `scripts/pages/timeline.js` — add and export `generateReportScope`; `GenerateReportButton` opens a day report on All day
- Modify: `app-shell-preview.html` — `org.js?v=22`→`?v=23`, `session-report-modal.js?v=5`→`?v=6`, `timeline.js?v=63`→`?v=64`
- Test: `tests/day-report-scope.test.js`

**Interfaces:**
- Consumes: the Task 4/5 routes.
- Produces: `org.getSessionReportPreview/generateSessionReport/getSessionReportStatus({scope?: 'day', sessionId?, date, user, requestId?, …})`; `buildGeneratePayload(ctx)` with `ctx.scope`; `previewErrorMessage(res) -> string`; `generateErrorMessage(res) -> string`; `generateReportScope(session, sessionCount) -> 'session' | 'day' | null`; modal prop `scope`.

- [ ] **Step 1: Write the failing test**

```js
'use strict';
/* A day report travels on /days/{date}/report…; a meeting report is unchanged.
   Spec 2026-09-15 §5.1, §9 step 5. */
const test = require('node:test');
const assert = require('node:assert');

let calls;

function loadOrg() {
  calls = [];
  global.window = {
    FieldSight: { fixtures: {} },
    FS: {
      api: {
        useMocks: false, orgWrites: true, timelineSource: 'aurora',
        orgBaseUrl: 'https://org.example/prod/api',
        delay: function () { return Promise.resolve(); },
        orgRequest: function (path, opts) {
          calls.push({ path: path, method: opts && opts.method, params: opts && opts.params, body: opts && opts.body });
          return Promise.resolve({ ok: true });
        },
      },
    },
  };
  delete require.cache[require.resolve('../scripts/api/org.js')];
  require('../scripts/api/org.js');
  return global.window.FS.api.org;
}

test('a day preview posts to /days/{date}/report/preview with only the user', async () => {
  const org = loadOrg();
  await org.getSessionReportPreview({ scope: 'day', date: '2026-09-10', user: 'James_Lamb' });
  assert.deepStrictEqual(calls, [{ path: '/days/2026-09-10/report/preview', method: 'POST',
    params: { user: 'James_Lamb' }, body: undefined }]);
});

test('a day generate posts to /days/{date}/report and forwards a real selection', async () => {
  const org = loadOrg();
  await org.generateSessionReport({ scope: 'day', date: '2026-09-10', user: 'James_Lamb',
    deliver: 'download', topicRowIds: ['t-1'] });
  assert.equal(calls[0].path, '/days/2026-09-10/report');
  assert.deepStrictEqual(calls[0].params, { user: 'James_Lamb' });
  assert.deepStrictEqual(calls[0].body.topicRowIds, ['t-1']);
});

test('a day status polls /days/{date}/report/status', async () => {
  const org = loadOrg();
  await org.getSessionReportStatus({ scope: 'day', date: '2026-09-10', user: 'James_Lamb', requestId: 'r' });
  assert.deepStrictEqual(calls[0], { path: '/days/2026-09-10/report/status', method: undefined,
    params: { user: 'James_Lamb', requestId: 'r' }, body: undefined });
});

test('a meeting report is unchanged', async () => {
  const org = loadOrg();
  await org.getSessionReportPreview({ sessionId: 'sid1', date: '2026-09-10', user: 'James_Lamb' });
  assert.deepStrictEqual(calls[0], { path: '/sessions/sid1/report/preview', method: 'POST',
    params: { date: '2026-09-10', user: 'James_Lamb' }, body: undefined });
});

test('buildGeneratePayload: a day carries its scope and no session id; a meeting carries no scope', () => {
  global.window = { FieldSight: {} };
  delete require.cache[require.resolve('../scripts/composites/session-report-modal.js')];
  const { buildGeneratePayload } = require('../scripts/composites/session-report-modal.js');
  const day = buildGeneratePayload({ scope: 'day', date: 'd', userFolder: 'u', form: {} });
  assert.equal(day.scope, 'day');
  assert.ok(!('sessionId' in day));
  const meeting = buildGeneratePayload({ session: { session_id: 's' }, date: 'd', userFolder: 'u', form: {} });
  assert.ok(!('scope' in meeting));
  assert.equal(meeting.sessionId, 's');
});

test('a worker with no recording folder is told so, not "unavailable"', () => {
  global.window = { FieldSight: {} };
  delete require.cache[require.resolve('../scripts/composites/session-report-modal.js')];
  const { previewErrorMessage, generateErrorMessage } = require('../scripts/composites/session-report-modal.js');
  assert.match(previewErrorMessage({ _accessDenied: true, error: 'no folder mapping for your account' }),
    /no recording folder/);
  assert.equal(previewErrorMessage({ _notFound: true }), 'Preview is unavailable here.');
  assert.equal(generateErrorMessage({ error: 'topicRowIds: at most 200' }), 'topicRowIds: at most 200');
  assert.equal(generateErrorMessage({}), 'The report did not start.');
});

test('generateReportScope: a meeting, a day, or nothing', () => {
  global.React = { createElement: function () { return {}; }, useState: function (v) { return [v, function () {}]; },
    useEffect: function () {}, useRef: function (v) { return { current: v }; }, Fragment: 'Fragment' };
  global.window = { FS: {}, FieldSight: {}, location: { href: '' } };
  global.document = { addEventListener() {}, removeEventListener() {} };
  delete require.cache[require.resolve('../scripts/pages/timeline.js')];
  const { generateReportScope } = require('../scripts/pages/timeline.js');
  assert.equal(generateReportScope({ session_id: 's' }, 3), 'session');
  assert.equal(generateReportScope(null, 2), 'day');
  assert.equal(generateReportScope(null, 0), null);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/day-report-scope.test.js`
Expected: FAIL — day calls go to `/sessions/undefined/…`; `previewErrorMessage`, `generateErrorMessage` and `generateReportScope` are not exported. (If `require('../scripts/pages/timeline.js')` throws on a missing global, add a stub for exactly that global to the last test and re-run until the failure is the missing export.)

- [ ] **Step 3: Implement**

`scripts/api/org.js` — directly above `async function getSessionReportPreview`:

```js
  /* One place that builds report URLs for both scopes. A day has no session id -- it
     is addressed by its date -- and the two must not drift (spec 2026-09-15 §5.2). */
  function _reportPath(opts, suffix) {
    if (opts.scope === 'day') {
      return '/days/' + encodeURIComponent(opts.date) + '/report' + suffix;
    }
    return '/sessions/' + encodeURIComponent(opts.sessionId) + '/report' + suffix;
  }

  function _reportParams(opts, extra) {
    var base = opts.scope === 'day' ? { user: opts.user } : { date: opts.date, user: opts.user };
    return Object.assign(base, extra || {});
  }
```

In `getSessionReportPreview`, replace the live `api.orgRequest(...)` call with:
```js
      return api.orgRequest(_reportPath(opts, '/preview'),
        { method: 'POST', params: _reportParams(opts) });
```
In `generateSessionReport`, replace the live call's path and params with `_reportPath(opts, '')` and `_reportParams(opts)` (body unchanged).
In `getSessionReportStatus`, replace the live call with:
```js
      return api.orgRequest(_reportPath(opts, '/status'),
        { params: _reportParams(opts, { requestId: opts.requestId }) });
```

`scripts/composites/session-report-modal.js` — replace the whole `buildGeneratePayload` function with:
```js
  function buildGeneratePayload(ctx) {
    // ctx = {scope?, session, date, userFolder, form:{templateId,title,attendees,fields}, deliver, recipients, topicRowIds?}
    var form = (ctx && ctx.form) || {};
    var deliver = ctx && ctx.deliver === 'email' ? 'email' : 'download';
    var payload = {
      date: ctx ? ctx.date : undefined,
      user: ctx ? ctx.userFolder : undefined,
      templateId: form.templateId || null,
      title: (form.title || '').trim(),
      attendees: Array.isArray(form.attendees) ? form.attendees : [],
      fields: form.fields || {},
      deliver: deliver,
      // recipients only travel when emailing (download has no addressees)
      recipients: deliver === 'email' && Array.isArray(ctx.recipients) ? ctx.recipients : [],
    };
    // A day is addressed by its date and has no session id (spec 2026-09-15 §5.1).
    // A meeting payload is exactly what it was before a day scope existed.
    if (ctx && ctx.scope === 'day') {
      payload.scope = 'day';
    } else {
      payload.sessionId = ctx && ctx.session ? ctx.session.session_id : undefined;
    }
    // ABSENT means "everything in scope". Only a real subset travels: the backend
    // rejects an empty list (asking for nothing) and treats a missing one as everything.
    if (ctx && Array.isArray(ctx.topicRowIds) && ctx.topicRowIds.length) {
      payload.topicRowIds = ctx.topicRowIds.slice();
    }
    return payload;
  }
```
Add directly below it, and add both names to the file's `module.exports`:
```js
  /* What to tell the reviewer when the preview could not be built. A worker whose account
     has no recording folder gets a 403 from the server; "unavailable" would hide the one
     thing they can act on (spec 2026-09-15 §5.7). */
  function previewErrorMessage(res) {
    var raw = (res && res.error) || '';
    if (/no folder mapping/i.test(raw)) {
      return 'Your account has no recording folder yet, so there is nothing of yours to report on.';
    }
    return raw || 'Preview is unavailable here.';
  }

  /* The server's reason when a report did not start (spec §5.2), not a generic line. */
  function generateErrorMessage(res) {
    return (res && res.error) || 'The report did not start.';
  }
```
In `SessionReportModal`, directly below `function sid()`:
```js
    function scopeOpts() {
      return props.scope === 'day'
        ? { scope: 'day', date: props.date, user: props.userFolder }
        : { sessionId: sid(), date: props.date, user: props.userFolder };
    }
```
Then: replace the preview call's argument object with `scopeOpts()`; replace `setPreviewErr((res && res.error) || 'Preview is unavailable here.')` with `setPreviewErr(previewErrorMessage(res))`; replace the status call's argument object with `Object.assign(scopeOpts(), { requestId: reqId })`; add `scope: props.scope,` as the first property of the `buildGeneratePayload({...})` call in `onGenerate`; and in `onGenerate` replace `setError('The report did not start.')` with `setError(generateErrorMessage(res))`.

`scripts/pages/timeline.js` — directly above `function generateReportUnavailableReason`:
```js
  /* Which report a Generate click makes, or null when there is nothing to report.
     A selected meeting is a meeting report; "All day" with at least one meeting is a
     day report (spec 2026-09-15 §5.1); a day with no meeting has neither. */
  function generateReportScope(session, sessionCount) {
    if (session) return 'session';
    return sessionCount > 0 ? 'day' : null;
  }
```
In `GenerateReportButton`, replace the `if (!props.session) { … }` block and the final `return` with:
```js
    var scope = generateReportScope(props.session, props.sessionCount);
    if (!scope) {
      return React.createElement('button', {
        type:      'button',
        className: 'fs-btn fs-btn--secondary fs-btn--sm fs-generate-report',
        disabled:  true,
        title:     generateReportUnavailableReason(props.sessionCount),
      }, 'Generate report');
    }
    return React.createElement(React.Fragment, null,
      React.createElement('button', {
        type:      'button',
        className: 'fs-btn fs-btn--primary fs-btn--sm fs-generate-report',
        onClick:   function () { setOpen(true); },
        title:     scope === 'day' ? 'Generate a report for this whole day'
                                   : 'Generate a report for this meeting',
      }, 'Generate report'),
      React.createElement(Modal, {
        open:       open,
        onClose:    function () { setOpen(false); },
        scope:      scope,
        session:    props.session,
        date:       props.date,
        userFolder: props.userFolder,
        siteName:   props.siteName,
        topics:     props.topics,
      }));
```
Add `generateReportScope: generateReportScope,` to the `module.exports` object at the end of the file.

Bump the three `?v=` values in `app-shell-preview.html`.

- [ ] **Step 4: Run the new test and the whole suite**

Run: `node --test tests/day-report-scope.test.js` Expected: PASS.
Run: `node --test tests/*.test.js` Expected: no new failures (in particular `session-report-modal-flow`, `session-report-modal`, `session-report-topic-selection`, `session-report-time-window`).

- [ ] **Step 5: Revert-check**

In `_reportPath`, delete the `if (opts.scope === 'day')` block; confirm the three day tests fail; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/api/org.js scripts/composites/session-report-modal.js scripts/pages/timeline.js app-shell-preview.html tests/day-report-scope.test.js
git commit -m "All day can be reported on"   # + attribution lines
```

---

### Task 8: A worker may report on their own day; the legacy /reports controls stay closed (D7, F5)

**Files:**
- Modify: `scripts/fs-globals.js` — worker permissions
- Modify: `scripts/pages/reports.js` — both `canRegenerate` lines
- Modify: `app-shell-preview.html` — `fs-globals.js?v=10`→`?v=11`, `reports.js?v=5`→`?v=6`
- Test: `tests/worker-can-report-own-day.test.js`

**Interfaces:**
- Produces: `FS.can({role:'worker'}, FS.P('report','create')) === true`; `FS.can({role:'worker'}, FS.P('report','create', FS.SCOPES.CREW)) === false`.

- [ ] **Step 1: Write the failing test**

```js
'use strict';
/* A worker may create a report of their own day (spec 2026-09-15 D7); the server
   already pins them to their own folder. The /reports page's Generate/Regenerate
   controls call a legacy gateway and stay closed to workers (F5), so they ask for a
   crew-scoped create rather than any create. `scripts/roles.js` is loaded by no page;
   fs-globals.js is the authority. */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

global.window = {};
global.document = { addEventListener() {}, removeEventListener() {} };
require('../scripts/fs-globals.js');
const FS = global.window.FS;

test('a worker may create a report', () => {
  assert.equal(FS.can({ role: 'worker' }, FS.P('report', 'create')), true);
});

test('a worker does not pass a crew-scoped create', () => {
  assert.equal(FS.can({ role: 'worker' }, FS.P('report', 'create', FS.SCOPES.CREW)), false);
});

test('foreman and above still pass the crew gate', () => {
  for (const role of ['foreman', 'site_manager', 'project_manager']) {
    assert.equal(FS.can({ role: role }, FS.P('report', 'create', FS.SCOPES.CREW)), true, role);
  }
});

test('both /reports gates ask for a crew-scoped create (wiring pin)', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'scripts', 'pages', 'reports.js'), 'utf8');
  const crew = src.match(/window\.FS\.P\('report','create',window\.FS\.SCOPES\.CREW\)/g) || [];
  const unscoped = src.match(/window\.FS\.P\('report','create'\)/g) || [];
  assert.equal(crew.length, 2);
  assert.equal(unscoped.length, 0);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/worker-can-report-own-day.test.js`
Expected: FAIL — worker create is false; the wiring pin counts 0 crew gates and 2 unscoped.

- [ ] **Step 3: Implement**

`scripts/fs-globals.js`, in `ROLES.worker.permissions`, after `P('report','view',SCOPES.SELF),`:
```js
        P('report','create',SCOPES.SELF),    /* spec 2026-09-15 D7 — own day only; the server pins the folder */
```

`scripts/pages/reports.js`, both occurrences:
```js
    /* Legacy daily/weekly/monthly regenerate (old gateway). Crew-scoped so a worker's
       own-day report grant does not open it (spec 2026-09-15 F5). */
    var canRegenerate = window.FS.can(caller, window.FS.P('report','create',window.FS.SCOPES.CREW));
```

Bump the two `?v=` values in `app-shell-preview.html`.

- [ ] **Step 4: Run the new test and the whole suite**

Run: `node --test tests/worker-can-report-own-day.test.js` Expected: PASS.
Run: `node --test tests/*.test.js` Expected: no new failures.

- [ ] **Step 5: Revert-check**

Remove the new worker line; confirm `a worker may create a report` fails; restore; confirm PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/fs-globals.js scripts/pages/reports.js app-shell-preview.html tests/worker-can-report-own-day.test.js
git commit -m "A worker can report on their own day"   # + attribution lines
```

---

### Task 9: Frontend release and in-browser verification

**Files:** none. Two defects in this repo passed every node test and failed only in the browser (fieldsight-ui `CLAUDE.md`, "How to verify").

- [ ] **Step 1:** Open one PR to `dev` with Tasks 7 and 8; merge after review.
- [ ] **Step 2:** Confirm the Amplify dev build for the merge commit succeeded:
  `aws amplify list-jobs --app-id d2fssznicvuckr --branch-name dev --max-items 3 --query 'jobSummaries[].[commitId,status]'`
- [ ] **Step 3:** In the dev site, as an admin, on a day with at least two meetings: select **All day** → **Generate report** is enabled → the preview lists topics from every meeting, earliest meeting first → Generate → the done step downloads a `.docx`.
- [ ] **Step 4:** Select one meeting → the preview shows only that meeting (unchanged behaviour).
- [ ] **Step 5:** As a worker account: own Timeline shows **Generate report**; `/reports` shows **no** Generate panel and no Regenerate control.
- [ ] **Step 6:** On a day with no meetings: **Generate report** is disabled with the "No meeting was recorded this day" tooltip.
- [ ] **Step 6a:** With a worker account whose `folder_name` is NULL, open Generate report: the preview says the account has no recording folder. If it says "Preview is unavailable here." instead, `api.orgRequest` is not passing the server's `error` text through on a 403 — find where `orgRequest` builds its `_accessDenied` envelope, pass `error` through with a test in the same PR, before promoting.
- [ ] **Step 7:** Promote `dev → main` only after Steps 3–6 pass, and only after the backend is on prod (Task 6 Step 5).
