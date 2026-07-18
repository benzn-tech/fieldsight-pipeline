# G5b — pipeline consumes recordings.site_id — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute an extraction's topics to the site the mobile app tagged on the recording (`recordings.site_id`) when present and company-valid, overriding the recorder's membership — so admin-account and cross-site app recordings attribute correctly.

**Architecture:** Add one read-only repo lookup (`recordings.site_for_media`) that matches `recordings.s3_key` by the extraction's `session_base` within `users/{folder}/…/{date}/`, company-scoped and in-company-checked, returning a site row. Wire it into `lambda_item_writer.write_extraction_items` as `site = recordings.site_for_media(...) or lambda_ingest.resolve_site(...)` — the explicit tag first, the existing membership resolver as fallback. No schema, no migration, no template change; report path (`lambda_ingest`) untouched.

**Tech Stack:** Python 3.12, psycopg3 (in-VPC Aurora PG16), SAM `fieldsight-prod`/`fieldsight-test` stacks. Tests: pytest with the repo's FakeConn (unit) + `db` fixture (integration, needs `TEST_DATABASE_URL`, set in CI).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-g5b-recordings-site-attribution-design.md`. Decisions D1–D5 bind every task.
- **D1** explicit tag (`recordings.site_id`) wins over membership when present + company-valid. **D2** `lambda_item_writer` ONLY (report path unchanged). **D3** multi-tenant invariant: matched site must be in the caller's company. **D4** match by `session_base` (LIKE, escaped). **D5** no match / null `site_id` / no `recordings` row → existing `resolve_site` unchanged.
- LIKE wildcards `_`/`%` in `user_folder` and `session_base` MUST be escaped — reuse the single existing `_escape_like` from `src/repositories/topics.py` (do not re-derive/duplicate).
- Branch off `develop` (`git checkout -b <name> origin/develop`); NEVER check out `develop` (held by another worktree); NEVER `git add -A`; CRLF repo → single-line Edit anchors. Windows: `node` for shell JSON (BUG-29). New dev content (comments/commits) in ENGLISH.
- No migration, no `template.yaml` change. Live/prod smoke is user-gated (deferred to Task 3 handoff, not run by the implementer).

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/repositories/recordings.py` | Modify | add `site_for_media()` — the s3_key-by-session_base lookup returning a site row |
| `tests/unit/test_recordings_site.py` | Create | unit: LIKE-pattern escaping + no-row→None (FakeConn) |
| `tests/integration/test_recordings_repo.py` | Modify | integration: real-DB semantics (match / cross-company / null / newest) |
| `src/lambda_item_writer.py` | Modify | import `recordings`; parse `session_base`; site = tag or membership; header-comment update |
| `tests/unit/test_lambda_item_writer.py` | Modify | `wired` fixture stubs `site_for_media→None`; 4 precedence/fallback tests |

---

### Task 1: `recordings.site_for_media` lookup

**Files:**
- Modify: `src/repositories/recordings.py`
- Create: `tests/unit/test_recordings_site.py`
- Modify: `tests/integration/test_recordings_repo.py`

**Interfaces:**
- Consumes: `topics._escape_like(prefix) -> str` (existing); `sites.get_site(conn, site_id) -> dict|None` (existing).
- Produces: `recordings.site_for_media(conn, company_id, user_folder, date, session_base) -> dict | None` — a site row (same shape as `sites.get_site` / `resolve_site`) or `None`. Consumed by Task 2.

- [ ] **Step 1: Write the failing unit test (LIKE-escape + no-row)**

Create `tests/unit/test_recordings_site.py`:

```python
"""
Unit: recordings.site_for_media LIKE-pattern construction — SP-Ask G5b.
user_folder and session_base contain '_' (a SQL LIKE wildcard) and MUST be
escaped, or the match would hit unrelated s3_keys. Real match/company/null
semantics are covered by tests/integration/test_recordings_repo.py (real DB).
FakeConn/FakeCursor record each execute() call; cursor() accepts row_factory.
"""
import pytest

from repositories import recordings


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop()
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def _pop(self):
        return self._results.pop(0) if self._results else []


def test_site_for_media_escapes_like_wildcards_in_pattern(monkeypatch):
    # match row then sites.get_site row; stub get_site so the test isolates the query
    conn = FakeConn(results=[[{"site_id": "site-1"}]])
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: {"id": sid, "company_id": "co-1"})

    site = recordings.site_for_media(
        conn, "co-1", "Ben_Lin", "2026-07-16", "Ben_Lin_2026-07-16_09-50-00")

    assert site == {"id": "site-1", "company_id": "co-1"}
    sql, params = conn.calls[0]["sql"], conn.calls[0]["params"]
    assert "LIKE %s ESCAPE '\\'" in sql
    assert "ORDER BY r.created_at DESC" in sql and "LIMIT 1" in sql
    assert "r.company_id = %s" in sql and "s.company_id = %s" in sql
    assert "r.site_id IS NOT NULL" in sql
    # underscores in folder AND session_base escaped; date is a fixed literal
    assert params == (
        "co-1", "co-1",
        r"users/Ben\_Lin/%/2026-07-16/Ben\_Lin\_2026-07-16\_09-50-00.%",
    )


def test_site_for_media_no_match_returns_none_and_skips_get_site(monkeypatch):
    conn = FakeConn(results=[[]])  # no matching recording
    called = []
    monkeypatch.setattr(recordings.sites, "get_site",
                        lambda c, sid: called.append(sid))

    assert recordings.site_for_media(
        conn, "co-1", "Ben_Lin", "2026-07-16", "Ben_Lin_2026-07-16_09-50-00") is None
    assert called == []
```

- [ ] **Step 2: Run it — verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_recordings_site.py -v`
Expected: FAIL — `AttributeError: module 'repositories.recordings' has no attribute 'site_for_media'` (and no `recordings.sites`).

- [ ] **Step 3: Implement `site_for_media`**

In `src/repositories/recordings.py`, add the import line at the top (after the existing `from psycopg...` lines) and the function at the end:

```python
from repositories import sites
from repositories.topics import _escape_like
```

```python
def site_for_media(conn, company_id, user_folder, date, session_base) -> dict | None:
    """The app-tagged site (recordings.site_id) for the recording whose media
    file this extraction session came from, or None. Matches recordings.s3_key
    by session_base within users/{folder}/.../{date}/ (LIKE, wildcard-escaped),
    scoped to company_id, and only returns a site that is itself in-company
    (multi-tenant invariant — never attribute across tenants). Newest matching
    recording wins. Returns a sites.get_site()-shaped row so it drops in where
    resolve_site's return is used (lambda_item_writer)."""
    pattern = f"users/{_escape_like(user_folder)}/%/{date}/{_escape_like(session_base)}.%"
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT r.site_id FROM recordings r JOIN sites s ON s.id = r.site_id "
        "WHERE r.company_id = %s AND s.company_id = %s AND r.site_id IS NOT NULL "
        "AND r.s3_key LIKE %s ESCAPE '\\' "
        "ORDER BY r.created_at DESC LIMIT 1",
        (company_id, company_id, pattern),
    ).fetchone()
    if row is None:
        return None
    return sites.get_site(conn, row["site_id"])
```

- [ ] **Step 4: Run the unit test — verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_recordings_site.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Write the failing integration test (real-DB semantics)**

Append to `tests/integration/test_recordings_repo.py` (uses the shared `db` fixture — real Postgres, per-test rollback; import `recordings` is already at the top of that file):

```python
def _seed_company_user_site(db, cname):
    cid = db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (cname,)).fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES (%s, %s, %s, 'worker') RETURNING id",
        (f"sub-{cname}", cid, f"{cname}@x.com")).fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'S') RETURNING id", (cid,)).fetchone()[0]
    return cid, uid, sid


def _insert_recording(db, cid, uid, sid, s3_key):
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, %s, 'audio', %s, %s, now())",
        (cid, uid, sid, s3_key, s3_key))  # client_uuid unique enough for the test


@pytest.mark.integration
def test_site_for_media_returns_in_company_tagged_site(db):
    cid, uid, sid = _seed_company_user_site(db, "A")
    _insert_recording(db, cid, uid, sid,
                      "users/Jo_Bloggs/audio/2026-07-16/Jo_Bloggs_2026-07-16_09-50-00.wav")
    site = recordings.site_for_media(db, cid, "Jo_Bloggs", "2026-07-16", "Jo_Bloggs_2026-07-16_09-50-00")
    assert site is not None and site["id"] == sid


@pytest.mark.integration
def test_site_for_media_excludes_cross_company_and_null_and_nonmatch(db):
    cid, uid, sid = _seed_company_user_site(db, "A")
    # (a) a recording in company A but tagged with a site from company B → must be ignored
    _cidB, uidB, sidB = _seed_company_user_site(db, "B")
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, %s, 'audio', %s, 'cu-x', now())",
        (cid, uid, sidB, "users/X/audio/2026-07-16/X_2026-07-16_10-00-00.wav"))
    assert recordings.site_for_media(db, cid, "X", "2026-07-16", "X_2026-07-16_10-00-00") is None
    # (b) null site_id → ignored
    db.execute(
        "INSERT INTO recordings (company_id, user_id, site_id, kind, s3_key, client_uuid, started_at) "
        "VALUES (%s, %s, NULL, 'audio', %s, 'cu-y', now())",
        (cid, uid, "users/Y/audio/2026-07-16/Y_2026-07-16_11-00-00.wav"))
    assert recordings.site_for_media(db, cid, "Y", "2026-07-16", "Y_2026-07-16_11-00-00") is None
    # (c) no recording matches → None
    assert recordings.site_for_media(db, cid, "Nobody", "2026-07-16", "Nobody_2026-07-16_12-00-00") is None
```

- [ ] **Step 6: Run the integration tests — verify pass (needs TEST_DATABASE_URL; CI has it)**

Run: `.venv/Scripts/python.exe -m pytest tests/integration/test_recordings_repo.py -v` (locally SKIPS without `TEST_DATABASE_URL`; must pass in CI). Then full suite: `.venv/Scripts/python.exe -m pytest tests/unit -q` → green.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/recordings.py tests/unit/test_recordings_site.py tests/integration/test_recordings_repo.py
git commit -m "feat(recordings): site_for_media — app-tagged site from recordings.site_id by session_base (G5b)"
```

**Done:** `site_for_media` returns the in-company tagged site (newest) or None; LIKE-escaped; unit + integration covered.

---

### Task 2: wire `site_for_media` into item-writer (tag > membership)

**Files:**
- Modify: `src/lambda_item_writer.py` (import line 57; site resolution line 235; header comment ~17-25)
- Modify: `tests/unit/test_lambda_item_writer.py` (`wired` fixture + 4 tests)

**Interfaces:**
- Consumes: `recordings.site_for_media(conn, company_id, user_folder, date, session_base) -> dict|None` (Task 1); existing `lambda_ingest.resolve_site`, `_parse_extraction_key(key) -> (user_folder, date, session_base)`, `topics.upsert_topic(conn, site_id, ...)`.
- Produces: nothing downstream (terminal behavior change).

- [ ] **Step 1: Update the `wired` fixture so existing tests keep passing**

In `tests/unit/test_lambda_item_writer.py`, inside the `wired` fixture (after the `resolve_site` stub, ~line 118), add a default "no tag" stub so all existing tests fall through to `resolve_site` exactly as before:

```python
    monkeypatch.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
```

- [ ] **Step 2: Write the failing precedence/fallback tests**

Append to `tests/unit/test_lambda_item_writer.py`:

```python
def _capture_topic_site(wired):
    """Capture the site_id positional arg upsert_topic receives; returns the list."""
    captured = []
    wired.setattr(iw.topics, "upsert_topic",
                  lambda conn, site_id, *a, **k: captured.append(site_id) or {"id": "topic-x"})
    return captured


def test_recording_tag_overrides_membership(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: {"id": "site-TAG"})
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: {"id": "site-MEMBER"})
    seen = _capture_topic_site(wired)
    iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert seen and all(s == "site-TAG" for s in seen)


def test_falls_back_to_membership_when_no_tag(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: {"id": "site-MEMBER"})
    seen = _capture_topic_site(wired)
    iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert seen and all(s == "site-MEMBER" for s in seen)


def test_admin_recording_attributes_via_tag_not_skipped(wired):
    # admin: membership resolver returns None (ALL scope), but the app tag exists
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: {"id": "site-TAG"})
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: None)
    seen = _capture_topic_site(wired)
    result = iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert not result.get("skipped")
    assert seen and all(s == "site-TAG" for s in seen)


def test_no_tag_no_membership_still_skips(wired):
    wired.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    wired.setattr(iw.lambda_ingest, "resolve_site", lambda *a, **k: None)
    called = []
    wired.setattr(iw.topics, "upsert_topic",
                  lambda *a, **k: called.append("upsert") or {"id": "x"})
    result = iw.write_extraction_items("2026-07-16", "Ben_Lin", EXTRACTION_KEY)
    assert result.get("skipped") is True
    assert called == []
```

(`EXTRACTION_KEY` and `make_extraction()` already exist in this test module and the `wired` fixture already loads the extraction JSON into FakeS3.)

- [ ] **Step 3: Run the new tests — verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_lambda_item_writer.py -k "tag or membership or admin_recording or still_skips" -v`
Expected: FAIL — `AttributeError: module 'lambda_item_writer' has no attribute 'recordings'` (import + wiring not there yet).

- [ ] **Step 4: Implement the item-writer change**

In `src/lambda_item_writer.py`:

(a) Add `recordings` to the repositories import (line 57):
```python
from repositories import companies, findings, recordings, topics
```

(b) Replace the site-resolution block at line 235 (the current `site = lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)` and its two preceding comment lines 233-234) with:
```python
        # G5b: the app stamps the in-app project pick onto recordings.site_id.
        # That explicit tag is authoritative over the recorder's membership
        # (and is the ONLY way an admin-account recording — resolve_site returns
        # None for ALL scope — attributes to a site). Fall through to the legacy
        # membership resolver only when there is no matching, company-valid tag.
        session_base = _parse_extraction_key(extraction_key)[2]
        site = recordings.site_for_media(conn, company["id"], user_folder, date, session_base) \
            or lambda_ingest.resolve_site(conn, company["id"], {}, user_folder)
```

(c) Update the header docstring note (lines ~17-25) that says `declared_site` is "NOT consumed for site attribution" / "resolve_site is always called with an empty report dict": add one sentence that G5b now consults `recordings.site_for_media` first (the app-tagged site), with `resolve_site` as the fallback. Keep it one or two lines; do not rewrite the block.

- [ ] **Step 5: Run the tests — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/unit/test_lambda_item_writer.py -v` → all pass (existing + 4 new).
Then full suite: `.venv/Scripts/python.exe -m pytest tests/unit -q` → green (no regressions).

- [ ] **Step 6: Commit**

```bash
git add src/lambda_item_writer.py tests/unit/test_lambda_item_writer.py
git commit -m "feat(item-writer): attribute site from recordings.site_id (app tag) over membership (G5b)"
```

**Done:** item-writer attributes via the app tag when present (admin recordings no longer skip), membership fallback intact.

---

### Task 3: PR, merge, deploy, live smoke (handoff — user-gated)

**Files:** none (process).

- [ ] **Step 1: PR to `develop`** — `gh pr create --base develop` titled `feat(pipeline): G5b — attribute site from recordings.site_id (app tag > membership)`, body summarizing D1–D5 + test evidence. Confirm CI `Tests` green (unit + integration incl. the two new integration tests).
- [ ] **Step 2: MATCH check + user merges to `develop`** → `deploy.yml` deploys `fieldsight-test`. No migration to apply (read-only lookup).
- [ ] **Step 3: Live smoke on the shared Aurora (before promoting to prod, user-gated):** re-trigger an extraction for an app-uploaded `(user_folder, date)` whose `recordings.site_id` is set (e.g. an Ellesmere-tagged Ben_Lin recording — the admin acceptance case): `aws s3 cp s3://fieldsight-data-509194952652/<extraction_key> s3://.../<extraction_key> --metadata-directive REPLACE`. Data-API: `SELECT site_id FROM topics WHERE source_s3_key='<extraction_key>'` = the tagged site (not skipped). Confirm a RealPTT (no `recordings` row) extraction still resolves via membership unchanged.
- [ ] **Step 4: Promote `develop`→`main` (prod) is a SEPARATE user decision** (carries whatever else is on develop, e.g. sp-ask). Do not bundle silently; surface it. After prod deploy, repeat the Step 3 smoke against prod.

**Done:** G5b live on test, verified; prod promotion is the user's call.

---

## Self-Review (author)

- **Spec coverage:** D1 (tag>membership) → Task 2 `or` precedence + `test_recording_tag_overrides_membership`; D2 (item-writer only) → no `lambda_ingest` change, stated in File Structure + Task 3; D3 (multi-tenant) → `site_for_media` `s.company_id=%s` + integration `test_...excludes_cross_company...`; D4 (session_base match, escaped) → `site_for_media` pattern + `test_...escapes_like_wildcards`; D5 (fallback unchanged) → `or resolve_site` + `test_falls_back...`, `test_no_tag_no_membership_still_skips`, and RealPTT-no-row covered by integration `nonmatch`. Admin headline fix → `test_admin_recording_attributes_via_tag_not_skipped` + Task 3 smoke.
- **Placeholder scan:** none — every code step carries full code; no "handle edge cases"/TBD.
- **Type consistency:** `site_for_media(conn, company_id, user_folder, date, session_base) -> dict|None` identical in Task 1 produce, Task 2 consume, and both test files; returns a `sites.get_site` row so `site["id"]` (item-writer line 261/278) works unchanged; `upsert_topic(conn, site_id, ...)` positional site_id matches the capture in Task 2 tests.
- **Escaping:** `_escape_like` reused (single definition), applied to both `user_folder` and `session_base`; `date` is a fixed literal (no wildcard) — asserted in the unit test's exact `params`.
