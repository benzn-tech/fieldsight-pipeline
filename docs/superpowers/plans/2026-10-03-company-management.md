# Company Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner creates and renames tenant companies from the web app, no code path finds a company by its name, one name is held by at most one tenant, and the dead tenancy-collapsing seed is gone.

**Architecture:** Backend (`fieldsight-pipeline`): delete the `COMPANY_NAME` pin so company resolution is directory-only; gate the lake-wide summary on `is_cross_company`; delete `lambda_org_seed` and `companies.get_company_by_name`; add a `lower(btrim(name))` unique index and map its violation to a structured 409; add `PATCH /api/org/companies/{id}`. Frontend (`fieldsight-ui`): named-field api functions in `scripts/api/org.js`, and `+ New company` / `Manage companies` on the Sites page for `platform_admin` only.

**Tech Stack:** Python 3.11 Lambdas, psycopg 3, PostgreSQL 16 (Aurora), AWS SAM; vanilla-JS React-without-JSX frontend, `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-03-company-management-design.md` (fieldsight-pipeline). Read it first — every decision below is argued there.

## Global Constraints

- Code comments, commit messages and docs in **English**.
- The cross-tenant gate is **`is_cross_company(caller["global_role"])`** on the backend (`platform_admin` only, `src/repositories/acl.py`) and **`orgApi.isCrossCompany(user)`** on the frontend. Never `resolve_scope(...) == "ALL"` — a company admin passes that.
- Company responses are the projection **`{"id": str, "name": str}`** and nothing else.
- A name collision answers **409** with body `{"error": "...", "existing": {"id", "name"}}` — never 500.
- Never `git add -A`. Stage named files.
- Backend unit suite: `python -m pytest tests/unit -q` — report passed **and** skipped **and** failed. Integration: `bash /c/Users/camil/fswork/run-tests.sh tests/integration -q` from the worktree root.
- Frontend suite: `node --test tests/*.test.js`. The run rewrites `tests/fixtures/editor-to-prompt.body.json` with line-ending-only changes; `git checkout -- tests/fixtures/` before committing.
- Every new test is **red-proofed**: revert the production change, watch it fail, restore. A test that stays green against the old code is labelled a regression pin in its docstring, not presented as a proof.
- Migration number: the next free one when you write it (**0080** as of 2026-10-03). Re-run `ls src/migrations | tail -3` immediately before committing — parallel sessions add migrations.
- Before each task: `git fetch origin && git rebase origin/develop` (pipeline) / `origin/dev` (ui). Parallel sessions merge continuously.

## Review Focus

1. **A name padded with a tab or a non-breaking space** (`"Frequency\t"`, `"Frequency\u00a0"`) must be stored stripped and must collide with `"Frequency"`. Python `str.strip()` removes both; Postgres `btrim()` removes spaces only — so the handler must strip before the lookup and before the insert, and a test must pin it. → Task 4.
2. **Renaming a company to its own name with only the case changed** (`frequency` → `Frequency`) must succeed with 200 — on the backend (the clash is itself) and in the UI (no client-side pre-check may block it). → Tasks 5 and 7.
3. **Two creates of the same name racing** must leave one tenant and answer the loser 409 with `existing`, not 500 — the index fires after the endpoint's own check passed. → Task 4.
4. **A recording folder no directory row claims**, after the pin is gone, must raise an error naming the folder, write nothing to any company, and never mention the seed — in ingest **and** item_writer; `session_activity` must keep returning `None` for it. → Task 1.
5. **The operator company's gm asking Timeline for a date with no `?user=`** must get the disambiguation list, not the lake-wide summary. → Task 2.

---

## Part A — fieldsight-pipeline

Worktree: `git worktree add "C:\Users\camil\fswork\company-mgmt" -b feat/company-management origin/develop` (use the Windows path form; the `/c/...` form lands at `C:/c/...`).

### Task 1: Company resolution is directory-only — remove the `COMPANY_NAME` pin

**Why:** `COMPANY_NAME` defaults to `"FieldSight"`, a company that exists in neither database, so the pin and the name fallback already return `None` everywhere. This task deletes a branch that cannot succeed. Observable behaviour is unchanged on both stacks (both deploy `MultiTenantResolution=true`) except the error text.

**Files:**
- Modify: `src/lambda_ingest.py` — `MULTI_TENANT` (line ~87), `COMPANY_NAME` (~86), `resolve_company` (~139), the `company is None` raise (~650)
- Modify: `src/lambda_item_writer.py` — docstring line ~57, `COMPANY_NAME` (~106), the `company is None` raise (~1058)
- Modify: `src/template.yaml` — the `MultiTenantResolution:` parameter (~1165–1178) and the three `MULTI_TENANT_RESOLUTION: !Ref MultiTenantResolution` lines (SessionActivityFunction, IngestFunction, ItemWriterFunction)
- Modify: `.github/workflows/deploy-prod.yml` — delete the line `"MultiTenantResolution=true" \`
- Modify: `.github/workflows/deploy.yml` — delete the line `"MultiTenantResolution=${{ vars.TEST_MULTI_TENANT_RESOLUTION || 'true' }}" \`
- Modify (tests): see Step 5
- Test: `tests/unit/test_company_resolution_is_directory_only.py` (new)

**Interfaces:**
- Produces: `lambda_ingest.resolve_company(conn, user_folder) -> dict | None` — directory-only. `lambda_ingest.unknown_folder_error(user_folder) -> RuntimeError`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_company_resolution_is_directory_only.py`:

```python
"""Company resolution reads the identity directory and nothing else.

`COMPANY_NAME` defaulted to "FieldSight", a company that exists in neither
database (the operator company is "FieldSight-platform"), so the name pin and
the name fallback had been returning None on both stacks for as long as anyone
could tell. A folder no directory row claims has no company: say so, name the
folder, and never guess a tenant. (Spec 2026-10-03, decision 4.)
"""
import os

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")


def test_a_folder_the_directory_knows_resolves_to_its_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                        lambda conn, folder: {"company_id": "co-1"} if folder == "Ada_L" else None)
    monkeypatch.setattr(ing.companies, "get_company_by_id",
                        lambda conn, cid: {"id": cid, "name": "Acme"})
    assert ing.resolve_company(object(), "Ada_L") == {"id": "co-1", "name": "Acme"}


def test_a_folder_nobody_claims_has_no_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global", lambda conn, folder: None)
    asked = []
    monkeypatch.setattr(ing.companies, "get_company_by_id",
                        lambda conn, cid: asked.append(cid))
    assert ing.resolve_company(object(), "Ben_Lin") is None
    assert asked == [], "no company may be looked up for a folder nobody claims"


def test_a_directory_row_without_a_company_has_no_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                        lambda conn, folder: {"company_id": None})
    assert ing.resolve_company(object(), "Ghost") is None


def test_the_unknown_folder_error_names_the_folder_and_not_the_seed():
    err = ing.unknown_folder_error("Ben_Lin")
    assert isinstance(err, RuntimeError)
    msg = str(err)
    assert "'Ben_Lin'" in msg
    assert "directory" in msg
    assert "seed" not in msg.lower(), (
        "the seed does not fix an unclaimed folder, and running it collapses every "
        "tenant into one company -- the error must never send anyone to it")


def test_the_pin_is_gone():
    assert not hasattr(ing, "COMPANY_NAME")
    assert not hasattr(ing, "MULTI_TENANT")


def test_the_pipeline_lambdas_and_the_stack_no_longer_carry_the_pin():
    # lambda_org_api's COMPANY_NAME is Task 2's; this checks only what Task 1 owns.
    hits = []
    for name in ("lambda_ingest.py", "lambda_item_writer.py"):
        text = open(os.path.join(SRC, name), encoding="utf-8").read()
        for token in ("COMPANY_NAME", "MULTI_TENANT_RESOLUTION"):
            if token in text:
                hits.append(f"{name}: {token}")
    template = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    for token in ("MultiTenantResolution", "MULTI_TENANT_RESOLUTION"):
        if token in template:
            hits.append(f"template.yaml: {token}")
    assert hits == [], hits
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_company_resolution_is_directory_only.py -v`
Expected: FAIL — `unknown_folder_error` does not exist; `COMPANY_NAME`/`MULTI_TENANT` still present; a folder nobody claims hits `get_company_by_name`.

- [ ] **Step 3: Implement — `src/lambda_ingest.py`**

Delete these two module-level lines:

```python
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")
MULTI_TENANT = os.environ.get("MULTI_TENANT_RESOLUTION", "false") == "true"
```

Replace the whole `resolve_company` function with:

```python
def resolve_company(conn, user_folder):
    """Owning company for a lake object, read from the identity directory:
    the globally-unique users.folder_name -> users.company_id.

    A folder no directory row claims has NO company, and None is returned
    rather than a guess. There used to be a fallback to a company found by a
    configured name, and a stack switch that pinned every write to it -- but
    the name defaulted to "FieldSight", which exists in neither database, so
    both had returned None for as long as anyone could tell. No code may find
    a company by its name: names are renamed (spec 2026-10-03)."""
    row = users.get_by_folder_name_global(conn, user_folder)
    if row and row.get("company_id"):
        return companies.get_company_by_id(conn, row["company_id"])
    return None


def unknown_folder_error(user_folder):
    """The error for a lake object whose folder no directory row claims.
    Shared with lambda_item_writer, which reuses this module by import.

    It used to say "run the org seed". The seed does not fix this -- and
    running it moved every person of every tenant into one company."""
    return RuntimeError(
        f"folder {user_folder!r} has no directory row; not ingesting rather "
        "than guessing a tenant")
```

At the `company is None` check inside `ingest_report` (~line 650), replace the `raise RuntimeError(...)` block — the comment line above it and the two-line message — with:

```python
        if company is None:
            raise unknown_folder_error(user_folder)
```

- [ ] **Step 4: Implement — `src/lambda_item_writer.py`, template, workflows**

In `src/lambda_item_writer.py`: delete the docstring line `    COMPANY_NAME  - default: FieldSight (mirrors lambda_ingest's default)`, delete the module line `COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")`, and replace the `if company is None:` block after `company = lambda_ingest.resolve_company(conn, user_folder)` (~line 1058) with:

```python
        if company is None:
            raise lambda_ingest.unknown_folder_error(user_folder)
```

In `src/template.yaml`: delete the `MultiTenantResolution:` parameter block (from `  MultiTenantResolution:` through the last line of its `Description:`), and delete the line `          MULTI_TENANT_RESOLUTION: !Ref MultiTenantResolution` in each of `SessionActivityFunction`, `IngestFunction`, `ItemWriterFunction`.

In both workflows delete the single `"MultiTenantResolution=..." \` line. Each sits mid-way through a backslash-continued `--parameter-overrides` list, so deleting the whole line keeps the list intact. **The template and both workflows must change in the same commit**: `sam deploy` refuses an override for a parameter the template no longer declares.

- [ ] **Step 5: Move the tests that leaned on the pin**

Tests run without `MULTI_TENANT_RESOLUTION`, so they ran in pin mode and stubbed `companies.get_company_by_name` to supply a company. Apply exactly this:

*Unit fixtures that only need "a company"* — replace

```python
    monkeypatch.setattr(ing.companies, "get_company_by_name",
                        lambda conn, name: {"id": "co-1", "name": name})
```

with

```python
    monkeypatch.setattr(ing, "resolve_company",
                        lambda conn, folder: {"id": "co-1", "name": "Co"})
```

at `tests/unit/test_lambda_ingest.py:121`. In the item-writer files the module is reached as `iw.lambda_ingest`, so the replacement is `monkeypatch.setattr(iw.lambda_ingest, "resolve_company", lambda conn, folder: {"id": "co-1", "name": "Co"})` (keep each fixture's own id if it used one other than `co-1`) at `tests/unit/test_lambda_item_writer.py:134`, `tests/unit/test_a_photo_belongs_to_the_day.py:382`, `tests/unit/test_the_writer_stores_introductions.py:122`.

*`tests/unit/test_lambda_item_writer.py:514`* stubs `get_company_by_name` to `None` to drive the not-found path. Replace with `wired.setattr(iw.lambda_ingest, "resolve_company", lambda conn, folder: None)` and change that test's message assertion to `assert "has no directory row" in str(exc.value)` (and assert `"seed" not in`).

*`tests/unit/test_lambda_ingest.py` ~1020–1045* — the tests that set `MULTI_TENANT` / `COMPANY_NAME` and stub `get_company_by_name` exercise the pin, which no longer exists. Delete them; the new file's tests replace them. Remove every other `monkeypatch.setattr(ing, "COMPANY_NAME", ...)` / `"MULTI_TENANT"` in that file (`monkeypatch.setattr` raises `AttributeError` on a missing attribute).

*Integration* — each of these already creates the folder's directory row with `users.upsert_field_only_user(seed, co["id"], folder, ...)`, so directory resolution finds the company. Delete the single line `monkeypatch.setattr(lambda_ingest, "COMPANY_NAME", company_name)` from: `tests/integration/test_carry_forward_pass0.py`, `test_continuity_seam.py`, `test_defer_day_topic_chunk_deletable_after_supersede.py`, `test_ingest_carries_forward.py`, `test_item_id_inserts.py`, `test_question_answered_survives.py`, `test_supersede_two_passes.py`.

Find any remainder: `grep -rn "COMPANY_NAME\|MULTI_TENANT" tests/` must print nothing that touches `lambda_ingest` or `lambda_item_writer` (`lambda_org_api` hits are Task 2's).

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/unit/test_company_resolution_is_directory_only.py -v` → all pass.
Run: `python -m pytest tests/unit -q` → 0 failed. **Every** failure seen while getting there must be in a file named in Step 5; any other failure is a real regression — stop and investigate. `tests/unit/test_session_activity.py::test_unresolvable_company_skips_without_opening` already pins the third consumer (`session_activity` returns `None` when `resolve_company` does) — confirm it is among the passes.
Run: `bash /c/Users/camil/fswork/run-tests.sh tests/integration -q` → 0 failed.

- [ ] **Step 7: Red-proof**

`git stash push src/lambda_ingest.py`, run the new file: the resolution, error and pin tests fail. `git stash pop`. Record which went red.

- [ ] **Step 8: Commit**

```bash
git add src/lambda_ingest.py src/lambda_item_writer.py src/template.yaml \
  .github/workflows/deploy-prod.yml .github/workflows/deploy.yml \
  tests/unit/test_company_resolution_is_directory_only.py \
  tests/unit/test_lambda_ingest.py tests/unit/test_lambda_item_writer.py \
  tests/unit/test_a_photo_belongs_to_the_day.py tests/unit/test_the_writer_stores_introductions.py \
  tests/integration/test_carry_forward_pass0.py tests/integration/test_continuity_seam.py \
  tests/integration/test_defer_day_topic_chunk_deletable_after_supersede.py \
  tests/integration/test_ingest_carries_forward.py tests/integration/test_item_id_inserts.py \
  tests/integration/test_question_answered_survives.py tests/integration/test_supersede_two_passes.py
git commit -m "fix(ingest): company resolution is directory-only; the COMPANY_NAME pin is gone"
```

The repo variable `TEST_MULTI_TENANT_RESOLUTION` becomes unused — list it in the PR body for the owner to delete; it is harmless until then.

---

### Task 2: The lake-wide summary is for `platform_admin` only

**Why:** `summary_report.json` aggregates **every tenant's** daily reports. `admin_disambiguation` served it to any ALL-scope caller of the operator company — on prod, its gm. The lookup that decided "operator company" has been returning `None`, so the branch was off for everyone; repairing the lookup as written would have turned the leak on. Gate on the role that is already the only cross-tenant one.

**Files:**
- Modify: `src/lambda_org_api.py` — `COMPANY_NAME` and its comment block (~186–193), the comment line mentioning `COMPANY_NAME` (~8200), `admin_disambiguation` (~8905)
- Modify: `tests/unit/test_lambda_org_api.py` — the three summary tests (~3405–3452) and the `presign_wired` default stub (~1084)
- Test: `tests/unit/test_the_lake_summary_is_for_platform_admin.py` (new)

**Interfaces:**
- Consumes: `is_cross_company` (already imported in `lambda_org_api`).

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_the_lake_summary_is_for_platform_admin.py`:

```python
"""summary_report.json is every tenant's daily reports in one document.

Only a caller allowed across tenants may have it -- is_cross_company, i.e.
platform_admin. It used to be gated on "the caller's company is the operator
company", which handed it to that company's gm; the lookup behind that gate
had been returning None, so the leak was closed only by accident. These tests
call admin_disambiguation directly: that is where the decision lives.
(Spec 2026-10-03, decision 3.)
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-07-14"
SUMMARY_KEY = f"reports/{DATE}/summary_report.json"
OPERATOR = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
SUMMARY = {"date": DATE, "company_summary": "every tenant's day"}


class FakeConn:
    def execute(self, *a, **k):
        class _C:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None
        return _C()


class FakeS3:
    def __init__(self, objects):
        self.objects, self.get_object_calls = objects, []

    def get_object(self, Bucket, Key):
        self.get_object_calls.append(Key)
        if Key not in self.objects:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        import io
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}

    def list_objects_v2(self, **kw):
        return {"CommonPrefixes": [], "IsTruncated": False}


@pytest.fixture
def lake(monkeypatch):
    s3 = FakeS3({SUMMARY_KEY: SUMMARY})
    monkeypatch.setattr(org, "_s3_client", s3)
    monkeypatch.setattr(org, "_day_has_deleted_sources", lambda conn, folder, date: False)
    monkeypatch.setattr(org.topics, "list_extraction_folder_names_for_date",
                        lambda conn, cid, date: [])
    monkeypatch.setattr(org.recordings, "folders_with_uploads_for_date",
                        lambda conn, cid, date: set())
    return s3


def _caller(role, company):
    return {"id": "u-1", "cognito_sub": "sub-1", "company_id": company,
            "global_role": role, "archived_at": None}


def test_platform_admin_gets_the_summary(lake):
    res = org.admin_disambiguation(FakeConn(), _caller("platform_admin", OPERATOR), DATE)
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == SUMMARY


@pytest.mark.parametrize("role", ["gm", "admin"])
def test_the_operator_companys_own_officers_do_not(lake, role):
    """The people the old gate would have served: ALL-scope members of the
    operator company. This is the assertion that fails against the leak."""
    org.admin_disambiguation(FakeConn(), _caller(role, OPERATOR), DATE)
    assert SUMMARY_KEY not in lake.get_object_calls


@pytest.mark.parametrize("role", ["gm", "admin"])
def test_a_customer_companys_officers_do_not(lake, role):
    org.admin_disambiguation(FakeConn(), _caller(role, "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92"), DATE)
    assert SUMMARY_KEY not in lake.get_object_calls


def test_the_gate_no_longer_looks_a_company_up_by_name():
    assert not hasattr(org, "COMPANY_NAME")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_the_lake_summary_is_for_platform_admin.py -v`
Expected: FAIL — `platform_admin` does not get the summary (the name lookup returns the real stub's answer, not the caller's company); `COMPANY_NAME` exists. If `_day_has_deleted_sources` or `list_objects_v2` shapes differ from the fakes above, adjust the **fakes** to what `admin_disambiguation` actually calls — never the assertions.

- [ ] **Step 3: Implement**

In `src/lambda_org_api.py`, delete `COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")` and the comment block immediately above it that begins `# Fix wave 1 (review finding 1): the lake-owner/internal company name`. In the comment block above `_LAKE_NOT_FOUND_CODES`, replace the sentence that says the branch is gated on the caller's company with: `the whole branch is gated on is_cross_company (platform_admin) -- the only role allowed across tenants (spec 2026-10-03).`

In `admin_disambiguation`, replace

```python
    owner = companies.get_company_by_name(conn, COMPANY_NAME)
    if owner is not None and str(caller["company_id"]) == str(owner["id"]):
```

with

```python
    if is_cross_company(caller["global_role"]):
```

and in its docstring replace the sentences from `Try the day's aggregate summary_report.json verbatim first` through `skipped for everyone, not just non-owners.` with:

```
    Serve the day's aggregate summary_report.json verbatim first -- but ONLY
    to is_cross_company (platform_admin): the report generator builds it
    from every tenant's folders. It used to be gated on "the caller's company
    is the operator company", which would have handed it to that company's gm;
    the name lookup behind that gate returned None, so the leak was closed only
    by accident (spec 2026-10-03).
```

- [ ] **Step 4: Update the old tests**

In `tests/unit/test_lambda_org_api.py`:
- `presign_wired`: delete the `wired.setattr(org.companies, "get_company_by_name", ...)` stub and the comment above it.
- `test_admin_summary_report_verbatim`: make the caller `platform_admin` by adding, before the request, `wired.setattr(org.users, "get_user_by_sub", lambda conn, sub: {**CALLER, "global_role": "platform_admin"})` and `wired.setattr(org, "GRADED_ROLES", True)`; replace its leading comment with `# The lake-wide summary is for platform_admin (is_cross_company) only.` If the routed request does not reach `admin_disambiguation` for a platform_admin under the test harness, delete this test — the new file covers the gate directly — and say so in the commit message.
- `test_admin_summary_report_gated_for_non_owner_company`: delete the `get_company_by_name` stub; the default `admin` CALLER is now refused by role. Rename it `test_admin_summary_report_refused_to_a_company_admin`.
- `test_admin_summary_report_skipped_when_owner_unresolved`: delete — its premise (an unresolvable owner company) no longer exists.
- `tests/unit/test_lambda_org_api.py:7966` and `tests/unit/test_timeline_404_says_what_arrived.py:323,337`: delete the `get_company_by_name` stubs (the function is no longer consulted here; Task 3 deletes it outright).

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/unit/test_the_lake_summary_is_for_platform_admin.py tests/unit/test_lambda_org_api.py tests/unit/test_timeline_404_says_what_arrived.py -q` → 0 failed.
Run: `python -m pytest tests/unit -q` → 0 failed.

- [ ] **Step 6: Red-proof — the mutation that matters**

Change the new gate to the leak's shape: `if caller["global_role"] in ("admin", "gm", "platform_admin"):`. Run the new file: `test_the_operator_companys_own_officers_do_not[gm]` and `[admin]` must fail. Restore. Record it.

- [ ] **Step 7: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_the_lake_summary_is_for_platform_admin.py \
  tests/unit/test_lambda_org_api.py tests/unit/test_timeline_404_says_what_arrived.py
git commit -m "fix(org-api): the lake-wide summary is for platform_admin only"
```

---

### Task 3: Retire `lambda_org_seed` and `companies.get_company_by_name`

**Why:** One default invoke of the seed upserts every Cognito user into a single new company — `upsert_user` overwrites `company_id` on conflict — collapsing multi-tenancy, rolling back roles, and re-pointing folders from the frozen JSON. Both stacks deploy it against the same shared Cognito pool. After Tasks 1–2 its lookup is the last caller of `get_company_by_name`.

**Files:**
- Delete: `src/lambda_org_seed.py`, `tests/unit/test_lambda_org_seed.py`
- Modify: `src/template.yaml` — the `# Lambda 11: Org Seed (Phase 3)` banner and the `OrgSeedFunction:` resource through the end of its `Policies:` block (the line before the `# QR passwordless login` banner); the `OrgSeedLogGroup:` resource
- Modify: `src/repositories/companies.py` — delete `get_company_by_name`
- Modify: `tests/integration/test_memberships_acl.py` — delete the `get_company_by_name` import and `test_get_company_by_name`
- Modify: `tests/unit/test_the_starter_templates_are_usable.py` — retarget `test_the_bodies_live_in_one_place_and_both_callers_call_it`
- Modify: `tests/unit/test_lambda_org_api.py` — comments at ~93 and ~5295 mention the seed; reword
- Test: `tests/unit/test_the_seed_is_retired.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_the_seed_is_retired.py`:

```python
"""lambda_org_seed is gone, and nothing finds a company by its name.

The seed was the one-shot Phase 3 move of identity from user_mapping.json into
Aurora. Long finished, still deployed on both stacks, and read as code: one
default invoke upserted every Cognito user into a single company. Removed, not
guarded (spec 2026-10-03, decision 6).
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(ROOT, "src")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_seed_module_is_gone():
    assert not os.path.exists(os.path.join(SRC, "lambda_org_seed.py"))


def test_no_stack_deploys_it():
    t = _read("src", "template.yaml")
    assert "OrgSeedFunction" not in t
    assert "OrgSeedLogGroup" not in t
    assert "lambda_org_seed" not in t


def test_nothing_finds_a_company_by_its_name():
    hits = []
    for dirpath, _, files in os.walk(SRC):
        for f in files:
            if f.endswith(".py"):
                text = open(os.path.join(dirpath, f), encoding="utf-8").read()
                if "get_company_by_name" in text:
                    hits.append(os.path.relpath(os.path.join(dirpath, f), SRC))
    assert hits == [], (
        "a company found by its name breaks the day it is renamed -- one rename "
        f"silently switched two features off. Found in: {hits}")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_the_seed_is_retired.py -v` → 3 FAIL.

- [ ] **Step 3: Implement**

`git rm src/lambda_org_seed.py tests/unit/test_lambda_org_seed.py`. Delete the template blocks named above. Delete `get_company_by_name` from `src/repositories/companies.py`. Delete the import and `test_get_company_by_name` from `tests/integration/test_memberships_acl.py`.

In `tests/unit/test_the_starter_templates_are_usable.py`, replace the body of `test_the_bodies_live_in_one_place_and_both_callers_call_it` after its `sql` assertions — the part that opens `lambda_org_seed.py` — with:

```python
    # A company made from now on has no members when POST /companies creates
    # it, so its starters are seeded on the first invitation: create_member.
    # (lambda_org_seed, the old second caller, was retired 2026-10-03.)
    api = io.open(os.path.join(os.path.dirname(__file__), "..", "..", "src",
                               "lambda_org_api.py"), encoding="utf-8").read()
    assert "report_templates.seed_starters(" in api, \
        "a company created after this migration would get nothing"
```

and change its docstring's `lambda_org_seed, for every company made from now on` to `create_member, for every company made from now on (on its first invitation)`.

In `tests/unit/test_lambda_org_api.py`, reword the comments at ~93 and ~5295 so they no longer describe the seed as current (e.g. `lambda_org_seed (retired 2026-10-03) used to seed on a manual backfill`).

- [ ] **Step 4: Run the tests**

Run: `grep -rn "get_company_by_name\|lambda_org_seed\|OrgSeed" src/ tests/ .github/` → only the new test file's own strings and the reworded comments.
Run: `python -m pytest tests/unit -q` → 0 failed. Integration → 0 failed.

- [ ] **Step 5: Red-proof**

`git stash` the deletions of `src/lambda_org_seed.py` and the template blocks only (keep the test), run the new file → the module and template tests fail. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/template.yaml src/repositories/companies.py \
  tests/unit/test_the_seed_is_retired.py tests/unit/test_the_starter_templates_are_usable.py \
  tests/unit/test_lambda_org_api.py tests/integration/test_memberships_acl.py
git commit -m "chore: retire lambda_org_seed -- one default invoke collapsed every tenant into one"
```

(`git rm` already staged the two deletions.)

---

### Task 4: One name per tenant — the index, and a structured 409

**Why:** `companies.name` has never been unique. The create endpoint's own check cannot stop a race or a path that bypasses it. The index can.

**Files:**
- Create: `src/migrations/0080_companies_name_unique.sql` (renumber if 0080 is taken)
- Modify: `src/lambda_org_api.py` — new `_company_name_taken`, `create_org_company`
- Modify: `tests/unit/test_org_api_creates_a_company.py`
- Test: `tests/integration/test_company_name_is_unique.py` (new)

**Interfaces:**
- Produces: `_company_name_taken(clash: dict | None) -> response` — the 409, reused by Task 5.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_company_name_is_unique.py`:

```python
"""idx_companies_name_ci: one company per name, ignoring case and spaces."""
import pytest
from psycopg.errors import UniqueViolation

from repositories import companies

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("variant", ["uniqco", "UNIQCO", " UniqCo", "UniqCo  "])
def test_a_case_or_space_variant_cannot_be_inserted(db, variant):
    companies.create_company(db, "UniqCo")
    with pytest.raises(UniqueViolation):
        companies.create_company(db, variant)


def test_different_names_coexist(db):
    companies.create_company(db, "UniqA")
    companies.create_company(db, "UniqB")
```

Append to `tests/unit/test_org_api_creates_a_company.py`:

```python
def test_the_refusal_carries_the_existing_company_as_data(monkeypatch):
    code, body, _ = call(monkeypatch, "platform_admin", {"name": "frequency"},
                         existing=EXISTING)
    assert code == 409
    assert body["existing"] == {"id": "c-old", "name": "Frequency"}


@pytest.mark.parametrize("name", ["Frequency\t", "Frequency\u00a0", "\u00a0Frequency"])
def test_tabs_and_nonbreaking_spaces_are_stripped_before_the_check(monkeypatch, name):
    """REGRESSION PIN, not a proof: the handler already strips with str.strip(),
    so this passes against the code before this task. It pins the order that
    matters -- Postgres btrim() strips spaces only, so if the strip ever moved
    after the lookup, "Frequency\\t" would miss "Frequency" and become a tenant."""
    seen = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, n: seen.append(n) or dict(EXISTING))
    org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps({"name": name}), "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    assert seen == ["Frequency"]


def test_a_race_that_reaches_the_index_is_a_409_not_a_500(monkeypatch):
    """The endpoint's check passed, then a concurrent create took the name."""
    from psycopg.errors import UniqueViolation
    lookups = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": "platform_admin", "archived_at": None}

    class RollbackConn(FakeConn):
        rolled_back = False

        def rollback(self):
            RollbackConn.rolled_back = True

    monkeypatch.setattr(org, "get_connection", lambda *a, **k: RollbackConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)

    def lookup(conn, n):
        lookups.append(n)
        return None if len(lookups) == 1 else dict(EXISTING)

    def insert(conn, n, industry=None):
        raise UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(org.companies, "find_company_by_name_ci", lookup)
    monkeypatch.setattr(org.companies, "create_company", insert)
    res = org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/companies", "queryStringParameters": None,
        "body": json.dumps({"name": "Frequency"}), "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    assert res["statusCode"] == 409, res["body"]
    assert json.loads(res["body"])["existing"]["id"] == "c-old"
    assert RollbackConn.rolled_back, "the aborted transaction must be rolled back before the re-read"
```

Change the existing `test_the_refusal_names_the_company_that_already_exists` to assert `"Frequency" in body["error"]` only (the id moved into `existing`).

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/unit/test_org_api_creates_a_company.py -q` → `test_the_refusal_carries_the_existing_company_as_data` FAILS (no `existing`) and `test_a_race_that_reaches_the_index_is_a_409_not_a_500` FAILS (500). The whitespace test PASSES — it is labelled a regression pin.
Run integration → `test_a_case_or_space_variant_cannot_be_inserted` FAILS (no index).

- [ ] **Step 3: Implement**

Create `src/migrations/0080_companies_name_unique.sql`:

```sql
-- One company per name, ignoring case and surrounding space (spec 2026-10-03).
-- companies.name has had no uniqueness since 0002; the create endpoint guarded
-- it alone, which a race or any path bypassing the API could defeat. If two
-- rows already share a name this statement FAILS and the deploy stops: a
-- duplicate tenant has to be resolved by a person, never silently.
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_ci
    ON companies (lower(btrim(name)));
```

In `src/lambda_org_api.py`, add above `create_org_company`:

```python
def _company_name_taken(clash):
    """409 for a company name another tenant holds. Carries that company as
    data, because the caller who retried after a timeout wants the company
    that exists, not an error message to parse."""
    if clash is None:   # raced, and the winner is not visible to this read
        return error("a company with that name already exists", 409)
    return error(f"a company named '{clash['name']}' already exists", 409,
                 {"existing": {"id": str(clash["id"]), "name": clash["name"]}})
```

In `create_org_company`, replace

```python
    clash = companies.find_company_by_name_ci(conn, name)
    if clash is not None:
        return error(f"a company named '{clash['name']}' already exists "
                     f"({clash['id']})", 409)
    row = companies.create_company(conn, name, industry)
```

with

```python
    clash = companies.find_company_by_name_ci(conn, name)
    if clash is not None:
        return _company_name_taken(clash)
    try:
        row = companies.create_company(conn, name, industry)
    except UniqueViolation:
        # idx_companies_name_ci: a concurrent create took the name after the
        # check above. The transaction is aborted -- roll back before reading.
        conn.rollback()
        return _company_name_taken(companies.find_company_by_name_ci(conn, name))
```

- [ ] **Step 4: Run the tests**

Unit file → pass. Integration file → pass. If the local migration itself fails with a duplicate-key error, the local database holds duplicate company names committed by earlier runs: rebuild the local schema as the local-Postgres notes describe and re-run; **do not** weaken the migration.
Full unit suite → 0 failed. Full integration → 0 failed.

- [ ] **Step 5: Red-proof**

Remove the `try/except` (call `create_company` bare): the race test fails with 500. Restore. Delete the migration file: the integration variant tests fail. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/migrations/0080_companies_name_unique.sql src/lambda_org_api.py \
  tests/unit/test_org_api_creates_a_company.py tests/integration/test_company_name_is_unique.py
git commit -m "feat(org-api): one name per tenant -- a unique index, and a 409 that carries the company"
```

---

### Task 5: `PATCH /api/org/companies/{id}` — rename

**Files:**
- Modify: `src/repositories/companies.py` — add `update_company`
- Modify: `src/lambda_org_api.py` — add `patch_org_company`, dispatch line
- Test: `tests/unit/test_org_api_renames_a_company.py` (new); `tests/integration/test_company_name_is_unique.py` (append)

**Interfaces:**
- Consumes: `_company_name_taken` (Task 4), `find_company_by_name_ci`, `get_company_by_id`.
- Produces: `PATCH /api/org/companies/{id}` body `{name?, industry?}` → `200 {"company": {"id","name"}}`; `update_company(conn, company_id, **fields) -> dict | None` where `fields` ⊆ `{"name", "industry"}`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_org_api_renames_a_company.py`:

```python
"""PATCH /api/org/companies/{id} -- rename a tenant. platform_admin only.

Safe because nothing finds a company by its name any more (spec 2026-10-03,
section 1); the guard excludes the company itself, so changing only the case of
its own name is allowed.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "2104bcd3-bbdf-421f-bfb2-b451fb8eba54"
OTHER = {"id": "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92", "name": "Southbase"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def rollback(self):
        pass


def call(monkeypatch, role, body, *, cid=CO, exists=True, clash=None):
    writes = []
    caller = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-ops",
              "global_role": role, "archived_at": None}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org.device_heartbeat, "record", lambda *a, **k: None)
    monkeypatch.setattr(org.companies, "get_company_by_id",
                        lambda conn, i: {"id": i, "name": "Frequency"} if exists else None)
    monkeypatch.setattr(org.companies, "find_company_by_name_ci",
                        lambda conn, n: dict(clash) if clash else None)
    monkeypatch.setattr(org.companies, "update_company",
                        lambda conn, i, **f: writes.append((i, f)) or {
                            "id": i, "name": f.get("name", "Frequency"),
                            "industry": f.get("industry"), "created_at": "x"})
    res = org.lambda_handler({
        "httpMethod": "PATCH", "path": f"/api/org/companies/{cid}",
        "queryStringParameters": None,
        "body": json.dumps(body) if body is not None else None, "headers": {},
        "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    return res["statusCode"], json.loads(res["body"]), writes


def test_platform_admin_renames(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "  Frequency NZ "})
    assert code == 200, body
    assert body == {"company": {"id": CO, "name": "Frequency NZ"}}
    assert writes == [(CO, {"name": "Frequency NZ"})]


@pytest.mark.parametrize("role", ["admin", "gm", "pm", "site_manager", "worker",
                                  "regional_manager", "member", "", "nonsense"])
def test_only_platform_admin_may_rename(monkeypatch, role):
    code, _, writes = call(monkeypatch, role, {"name": "X"})
    assert code == 403 and writes == []


def test_a_name_another_tenant_holds_is_409_with_that_tenant(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "southbase"}, clash=OTHER)
    assert code == 409 and writes == []
    assert body["existing"] == OTHER


def test_changing_only_the_case_of_its_own_name_is_allowed(monkeypatch):
    code, body, writes = call(monkeypatch, "platform_admin", {"name": "FREQUENCY"},
                              clash={"id": CO, "name": "Frequency"})
    assert code == 200, body
    assert writes == [(CO, {"name": "FREQUENCY"})]


def test_an_unknown_company_is_404(monkeypatch):
    code, _, writes = call(monkeypatch, "platform_admin", {"name": "X"}, exists=False)
    assert code == 404 and writes == []


@pytest.mark.parametrize("cid", ["not-a-uuid", "123"])
def test_a_malformed_id_is_400(monkeypatch, cid):
    code, _, writes = call(monkeypatch, "platform_admin", {"name": "X"}, cid=cid)
    assert code == 400 and writes == []


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "   "}, {"name": 7},
                                  {"industry": 7}, {"other": "x"}])
def test_nothing_valid_to_change_is_400(monkeypatch, body):
    code, _, writes = call(monkeypatch, "platform_admin", body)
    assert code == 400 and writes == []


def test_a_blank_industry_clears_it(monkeypatch):
    code, _, writes = call(monkeypatch, "platform_admin", {"industry": "   "})
    assert code == 200
    assert writes == [(CO, {"industry": None})]


def test_the_response_is_only_id_and_name(monkeypatch):
    code, body, _ = call(monkeypatch, "platform_admin", {"name": "N", "industry": "construction"})
    assert set(body["company"]) == {"id", "name"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/unit/test_org_api_renames_a_company.py -q` → FAIL (route returns 404 / not found).

- [ ] **Step 3: Implement**

Append to `src/repositories/companies.py`:

```python
def update_company(conn, company_id, **fields) -> dict | None:
    """Set any of name / industry. A key that is absent is left unchanged; a
    key present with None stores NULL (that is how industry is cleared).
    Returns the row, or None for an unknown id."""
    allowed = {"name", "industry"}
    unknown = set(fields) - allowed
    if unknown or not fields:
        raise ValueError(f"update_company: fields must be a non-empty subset of {allowed}")
    cols = sorted(fields)
    sets = ", ".join(f"{c}=%s" for c in cols)
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE companies SET {sets} WHERE id=%s "
        "RETURNING id, name, industry, created_at",
        [fields[c] for c in cols] + [company_id],
    ).fetchone()
```

(Column names come from the fixed `allowed` set, never from the request.)

In `src/lambda_org_api.py`, add after `create_org_company`:

```python
def patch_org_company(conn, caller, company_id, body):
    """PATCH /companies/{id} -- rename a tenant (and/or set its industry).
    platform_admin only, the same gate as create: a company admin renaming
    their own tenant would have to be told a name is taken without being told
    by whom, and that alone discloses that the tenant exists.

    Safe because no code finds a company by its name any more. The duplicate
    guard EXCLUDES this company, so changing only the case of its own name
    ("frequency" -> "Frequency") is allowed."""
    if not is_cross_company(caller["global_role"]):
        return error("platform_admin role required", 403)
    if body is None:
        return error("malformed JSON body", 400)
    try:
        company_id = str(uuid.UUID(str(company_id)))
    except (ValueError, AttributeError, TypeError):
        return error("company id must be a uuid", 400)
    changes = {}
    if "name" in body:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return error("name must be a non-empty string", 400)
        changes["name"] = name.strip()
    if "industry" in body:
        industry = body.get("industry")
        if industry is not None and not isinstance(industry, str):
            return error("industry must be a string", 400)
        changes["industry"] = (industry or "").strip() or None
    if not changes:
        return error("nothing to change: send name and/or industry", 400)
    if companies.get_company_by_id(conn, company_id) is None:
        return error("company not found", 404)
    if "name" in changes:
        clash = companies.find_company_by_name_ci(conn, changes["name"])
        if clash is not None and str(clash["id"]) != company_id:
            return _company_name_taken(clash)
    try:
        row = companies.update_company(conn, company_id, **changes)
    except UniqueViolation:
        conn.rollback()
        return _company_name_taken(
            companies.find_company_by_name_ci(conn, changes.get("name", "")))
    logger.info("company updated: %s -> %s by %s", company_id, sorted(changes),
                caller.get("cognito_sub"))
    return ok({"company": {"id": str(row["id"]), "name": row["name"]}})
```

In `dispatch`, directly after the `if route == "/companies":` block, add:

```python
    m_co = re.match(r"^/companies/([^/]+)$", route)
    if m_co and method == "PATCH":
        return patch_org_company(conn, caller, m_co.group(1), parse_body(event))
```

Append to `tests/integration/test_company_name_is_unique.py`:

```python
def test_a_company_may_change_only_the_case_of_its_own_name(db):
    co = companies.create_company(db, "CaseCo")
    row = companies.update_company(db, co["id"], name="CASECO")
    assert row["name"] == "CASECO"


def test_renaming_onto_another_tenants_name_hits_the_index(db):
    companies.create_company(db, "TakenCo")
    other = companies.create_company(db, "FreeCo")
    with pytest.raises(UniqueViolation):
        companies.update_company(db, other["id"], name="takenco")
```

- [ ] **Step 4: Run the tests**

New unit file → pass. Integration → pass. Full unit suite → 0 failed.

- [ ] **Step 5: Red-proof**

Remove the `and str(clash["id"]) != company_id` exclusion: `test_changing_only_the_case_of_its_own_name_is_allowed` fails. Restore. Swap the gate for `resolve_scope(caller["global_role"]) != "ALL"`: the `admin` and `gm` refusal cases fail. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/companies.py src/lambda_org_api.py \
  tests/unit/test_org_api_renames_a_company.py tests/integration/test_company_name_is_unique.py
git commit -m "feat(org-api): PATCH /companies/{id} renames a tenant, platform_admin only"
```

### Part A wrap-up

- [ ] Rebase on `origin/develop`, run the full unit suite and the full integration suite, push, open a PR to `develop` whose body lists: the five commits; that `TEST_MULTI_TENANT_RESOLUTION` is now an unused repo variable; and the one behaviour change on prod (the owner starts receiving the lake-wide summary). Merge when CI is green.
- [ ] **Verify on TEST after the develop deploy** (these are TEST reads/writes — allowed): `SERVER_BUILD` on `fieldsight-test-org-api` equals the merge sha; `idx_companies_name_ci` exists (apply log of `fieldsight-test-migrate`); `POST /api/org/companies {"name":"frequency"}` as the owner's sub → 409 with `existing` (Frequency exists on TEST); `PATCH` Frequency's own id with `{"name":"Frequency"}` → 200; `GET /api/org/timeline?date=<a date with a summary>` with no `user` as the owner → the summary; as a TEST gm → not the summary. `fieldsight-test-org-seed` no longer exists.

---

## Part B — fieldsight-ui

Worktree: `git worktree add "C:\Users\camil\fswork\ui-company-mgmt" -b feat/company-management origin/dev`.

### Task 6: The api layer — `listCompanies`, `createCompany`, `renameCompany`

**Files:**
- Modify: `scripts/api/org.js` — add the three functions and a shared normaliser; export them
- Test: `tests/company-management-api.test.js` (new)

**Interfaces:**
- Produces:
  - `listCompanies() -> Promise<[{id, name}]>` — the directory, sorted by name; `[]` on any failure. No fallback (unlike `getCompanyChoices`).
  - `createCompany({name, industry?}) -> Promise<{company} | {conflict: true, existing, error} | {_accessDenied…}>`
  - `renameCompany(id, {name?, industry?}) -> same shape`
  - Mock mode (`!orgWrite()`): `createCompany`/`renameCompany` reject with `Error('company management needs the live backend')`.

- [ ] **Step 1: Write the failing test**

Create `tests/company-management-api.test.js`:

```js
'use strict';

/*
 * Company management through the api layer. The body is built HERE from
 * named fields: this layer drops anything it does not name, which has
 * silently lost fields in this repo before -- so every assertion is on the
 * body handed to orgRequest, not on an intermediate object.
 */
const test = require('node:test');
const assert = require('node:assert');

let calls;
let reply;   // (path, opts) -> value | Error

function loadOrg() {
  calls = [];
  global.window = {
    FieldSight: { fixtures: {} },
    FS: {
      api: {
        useMocks: false, orgWrites: true, timelineSource: 'aurora',
        orgBaseUrl: 'https://org.example/prod/api',
        delay: function () { return Promise.resolve(); },
        orgRequest: function (p, opts) {
          calls.push({ path: p, method: opts && opts.method, body: opts && opts.body });
          var v = reply(p, opts || {});
          return v instanceof Error ? Promise.reject(v) : Promise.resolve(v);
        },
      },
    },
  };
  delete require.cache[require.resolve('../scripts/api/org.js')];
  require('../scripts/api/org.js');
  return global.window.FS.api.org;
}

function conflict(existing) {
  var e = new Error('a company named \'' + existing.name + '\' already exists');
  e.status = 409;
  e.body = { error: e.message, existing: existing };
  return e;
}

test('createCompany posts only the named fields', async () => {
  reply = function () { return { company: { id: 'c-1', name: 'Frequency' } }; };
  const org = loadOrg();
  const res = await org.createCompany({ name: 'Frequency', bogus: 1, company_id: 'x' });
  assert.deepStrictEqual(calls, [{ path: '/companies', method: 'POST', body: { name: 'Frequency' } }]);
  assert.deepStrictEqual(res, { company: { id: 'c-1', name: 'Frequency' } });
});

test('createCompany sends industry only when given', async () => {
  reply = function () { return { company: { id: 'c-1', name: 'F' } }; };
  const org = loadOrg();
  await org.createCompany({ name: 'F', industry: 'construction' });
  assert.deepStrictEqual(calls[0].body, { name: 'F', industry: 'construction' });
});

test('a 409 resolves to a conflict carrying the existing company, it does not throw', async () => {
  reply = function () { return conflict({ id: 'c-old', name: 'Frequency' }); };
  const org = loadOrg();
  const res = await org.createCompany({ name: 'frequency' });
  assert.strictEqual(res.conflict, true);
  assert.deepStrictEqual(res.existing, { id: 'c-old', name: 'Frequency' });
});

test('any other failure still rejects', async () => {
  reply = function () { var e = new Error('boom'); e.status = 500; return e; };
  const org = loadOrg();
  await assert.rejects(org.createCompany({ name: 'X' }));
});

test('renameCompany PATCHes the id with only the named fields', async () => {
  reply = function () { return { company: { id: 'c-1', name: 'New' } }; };
  const org = loadOrg();
  await org.renameCompany('c-1', { name: 'New', bogus: true });
  assert.deepStrictEqual(calls, [{ path: '/companies/c-1', method: 'PATCH', body: { name: 'New' } }]);
});

test('renameCompany passes a case-only change through untouched', async () => {
  reply = function () { return { company: { id: 'c-1', name: 'FREQUENCY' } }; };
  const org = loadOrg();
  const res = await org.renameCompany('c-1', { name: 'FREQUENCY' });
  assert.strictEqual(res.company.name, 'FREQUENCY');
  assert.strictEqual(calls.length, 1, 'no client-side pre-check may block it');
});

test('listCompanies reads the directory, sorted, and never falls back to the sites', async () => {
  reply = function (p) {
    if (p === '/companies') return { companies: [{ id: 'b', name: 'Zed' }, { id: 'a', name: 'Alpha' }] };
    throw new Error('unexpected ' + p);
  };
  const org = loadOrg();
  assert.deepStrictEqual(await org.listCompanies(), [{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Zed' }]);
  assert.ok(!calls.some(function (c) { return c.path === '/sites'; }));
});

test('listCompanies is [] on denial, absence, junk or a throw', async () => {
  for (const r of [{ _accessDenied: true }, { _notFound: true }, { companies: 'x' }, new Error('x')]) {
    reply = function () { return r; };
    const org = loadOrg();
    assert.deepStrictEqual(await org.listCompanies(), [], 'for ' + JSON.stringify(r));
  }
});

test('in mock mode the writes refuse rather than pretend', async () => {
  reply = function () { return {}; };
  const org = loadOrg();
  window.FS.api.useMocks = true;
  await assert.rejects(org.createCompany({ name: 'X' }));
  await assert.rejects(org.renameCompany('c-1', { name: 'X' }));
  assert.strictEqual(calls.length, 0);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/company-management-api.test.js` → FAIL (`createCompany` is not a function).

- [ ] **Step 3: Implement**

In `scripts/api/org.js`, next to `getCompanyChoices`, add a shared normaliser and make `getCompanyChoices` use it (its behaviour is unchanged — keep its own fallback logic):

```js
  /* [{ id, name }] from a directory payload: rows without an id dropped, a
     nameless row shown by its id, sorted by name. Shared by the picker and
     the management list so the two can never disagree about a company. */
  function normaliseCompanies(rows) {
    return (rows || [])
      .filter(function (c) { return c && c.id; })
      .map(function (c) { return { id: String(c.id), name: c.name || String(c.id) }; })
      .sort(byName);
  }
```

Replace the `.filter(...).map(...).sort(byName)` chain inside `getCompanyChoices` with `return normaliseCompanies(res.companies);`. Then add:

```js
  /* The directory, for Manage companies. platform_admin only (GET /companies
     answers everyone else 403). No fallback to the sites-derived list, unlike
     getCompanyChoices: a management screen that silently showed a partial list
     would let someone conclude a company does not exist. [] on any failure. */
  async function listCompanies() {
    try {
      var res = await api.orgRequest('/companies');
      if (!res || res._accessDenied || res._notFound || !Array.isArray(res.companies)) return [];
      return normaliseCompanies(res.companies);
    } catch (e) {
      return [];
    }
  }

  /* Company writes. Bodies are built from NAMED fields. A 409 -- the name is
     another tenant's -- resolves to { conflict: true, existing, error } rather
     than throwing, because it is an answer the caller acts on, not a failure.
     Everything else behaves exactly as orgRequest does. */
  async function _companyWrite(path, method, body) {
    if (!orgWrite()) throw new Error('company management needs the live backend');
    try {
      return await api.orgRequest(path, { method: method, body: body });
    } catch (e) {
      if (e && e.status === 409) {
        return { conflict: true, error: (e.body && e.body.error) || e.message,
                 existing: (e.body && e.body.existing) || null };
      }
      throw e;
    }
  }

  async function createCompany(input) {
    input = input || {};
    var body = { name: input.name };
    if (input.industry) body.industry = input.industry;
    return _companyWrite('/companies', 'POST', body);
  }

  async function renameCompany(id, input) {
    input = input || {};
    var body = {};
    if (input.name !== undefined) body.name = input.name;
    if (input.industry !== undefined) body.industry = input.industry;
    return _companyWrite('/companies/' + encodeURIComponent(id), 'PATCH', body);
  }
```

Add to the exported object: `listCompanies: listCompanies, createCompany: createCompany, renameCompany: renameCompany,`.

- [ ] **Step 4: Run the tests**

`node --test tests/company-management-api.test.js` → pass. `node --test tests/*.test.js` → 0 fail (the existing `the-company-list-comes-from-the-directory` tests must still pass — they pin `getCompanyChoices`).

- [ ] **Step 5: Red-proof**

Change `createCompany` to `var body = Object.assign({}, input);` → the named-fields test fails. Restore. Remove the 409 branch in `_companyWrite` → the conflict test fails. Restore.

- [ ] **Step 6: Commit**

```bash
git checkout -- tests/fixtures/
git add scripts/api/org.js tests/company-management-api.test.js
git commit -m "feat(api): createCompany / renameCompany / listCompanies, bodies from named fields"
```

---

### Task 7: The Sites page — `+ New company` and `Manage companies`

**Files:**
- Modify: `scripts/pages/sites.js` — two new components (`NewCompanyModal`, `ManageCompaniesModal`), an `initialCompanyId` prop on `NewProjectModal`, header buttons and modal state in the Sites middle column
- Test: `tests/company-management-page.test.js` (new)

**Interfaces:**
- Consumes: `orgApi.createCompany`, `orgApi.renameCompany`, `orgApi.listCompanies`, `orgApi.isCrossCompany` (Task 6 / #383).

- [ ] **Step 1: Write the failing test**

`sites.js` is an IIFE with no export and this repo has no DOM harness, so this test pins **wiring** by reading the source. The behaviour is pinned in Task 6.

Create `tests/company-management-page.test.js`:

```js
'use strict';

/*
 * WIRING ONLY -- sites.js is an IIFE with no export and this repo has no DOM
 * harness. The api behaviour is pinned in company-management-api.test.js.
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'scripts', 'pages', 'sites.js'), 'utf8');

test('the company controls are gated on isCrossCompany, not isAdmin or gm', () => {
  assert.match(SRC, /var mayManageCompanies = !!\(orgApi && orgApi\.isCrossCompany && orgApi\.isCrossCompany\(ctx\.caller\)\) && orgLive\(\);/);
  assert.match(SRC, /mayManageCompanies \? React\.createElement\('button'[\s\S]{0,200}'\+ New company'/);
  assert.match(SRC, /mayManageCompanies \? React\.createElement\('button'[\s\S]{0,200}'Manage companies'/);
});

test('creating a company goes through the api layer', () => {
  assert.match(SRC, /orgApi\.createCompany\(\{ name: name \}\)/);
});

test('renaming goes through the api layer', () => {
  assert.match(SRC, /orgApi\.renameCompany\(row\.id, \{ name: draft \}\)/);
});

test('the management list reads the directory, not the sites', () => {
  assert.match(SRC, /orgApi\.listCompanies\(\)/);
});

test('a taken name offers the existing company instead of a dead end', () => {
  assert.match(SRC, /'Create a project in ' \+ taken\.name/);
});

test('a new project can open with a company already chosen', () => {
  assert.match(SRC, /company_id: props\.initialCompanyId \|\| ''/);
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `node --test tests/company-management-page.test.js` → FAIL.

- [ ] **Step 3: Implement — `NewProjectModal` accepts a preselected company**

In `NewProjectModal`, add `company_id: props.initialCompanyId || ''` to the object passed to `React.useState` for `refForm` (after `longitude: null`).

- [ ] **Step 4: Implement — the two components**

Add directly after `NewProjectModal`:

```js
  /* ---------- NewCompanyModal (company management, spec 2026-10-03) ------ */
  /* platform_admin only -- the caller gates rendering. A taken name is an
     answer, not a failure: offer the company that exists, because whoever
     typed the name wanted somewhere to put a project. */
  function NewCompanyModal(props) {
    var Modal = window.FieldSight && window.FieldSight.ModalOverlay;
    var orgApi = window.FS.api.org;
    var refName = React.useState(''); var name = refName[0], setName = refName[1];
    var refBusy = React.useState(false); var busy = refBusy[0], setBusy = refBusy[1];
    var refTaken = React.useState(null); var taken = refTaken[0], setTaken = refTaken[1];
    function fail() {
      setBusy(false);
      if (window.FS.toast) window.FS.toast.show({ message: 'Could not create company', tone: 'error' });
    }
    function submit() {
      if (!name.trim() || busy) return;
      setBusy(true); setTaken(null);
      orgApi.createCompany({ name: name }).then(function (res) {
        if (res && res.conflict) { setBusy(false); setTaken(res.existing || { id: null, name: name.trim() }); return; }
        if (!res || !res.company) { fail(); return; }
        setBusy(false);
        if (window.FS.toast) window.FS.toast.show({ message: 'Company "' + res.company.name + '" created', tone: 'success' });
        props.onCreated(res.company);
      }).catch(fail);
    }
    if (!Modal) return null;
    return React.createElement(Modal, { open: true, size: 'md', title: 'New company', onClose: props.onClose },
      React.createElement('div', { className: 'fs-settings__pw-form' },
        fFieldRow('Company name *', fText(name, function (v) { setName(v); setTaken(null); })),
        taken ? React.createElement('div', { className: 'fs-settings__field-hint', role: 'alert' },
          'A company called "' + taken.name + '" already exists. ',
          taken.id ? React.createElement('button', {
            type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm',
            onClick: function () { props.onUseExisting(taken); },
          }, 'Create a project in ' + taken.name) : null) : null,
        React.createElement('div', { className: 'fs-settings__actions' },
          React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--md', onClick: props.onClose }, 'Cancel'),
          React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--primary fs-btn--md', disabled: busy || !name.trim(), onClick: submit }, busy ? 'Creating…' : 'Create company'))));
  }

  /* ---------- ManageCompaniesModal --------------------------------------- */
  /* Every tenant from the directory, renamed in place. No client-side name
     check: the server excludes the company itself, so a case-only change of
     its own name must reach it. */
  function ManageCompaniesModal(props) {
    var Modal = window.FieldSight && window.FieldSight.ModalOverlay;
    var orgApi = window.FS.api.org;
    var refRows = React.useState(null); var rows = refRows[0], setRows = refRows[1];
    var refEdit = React.useState(null); var editing = refEdit[0], setEditing = refEdit[1];
    var refDraft = React.useState(''); var draft = refDraft[0], setDraft = refDraft[1];
    var refMsg = React.useState(null); var msg = refMsg[0], setMsg = refMsg[1];
    var refBusy = React.useState(false); var busy = refBusy[0], setBusy = refBusy[1];
    function load() {
      return orgApi.listCompanies().then(function (list) { setRows(list || []); });
    }
    React.useEffect(function () { load(); }, []);
    function save(row) {
      if (!draft.trim() || busy) return;
      setBusy(true); setMsg(null);
      orgApi.renameCompany(row.id, { name: draft }).then(function (res) {
        setBusy(false);
        if (res && res.conflict) {
          setMsg('A company called "' + ((res.existing && res.existing.name) || draft.trim()) + '" already exists.');
          return;
        }
        if (!res || !res.company) { setMsg('Could not rename the company.'); return; }
        setEditing(null);
        if (window.FS.toast) window.FS.toast.show({ message: 'Renamed to "' + res.company.name + '"', tone: 'success' });
        load();
      }).catch(function () { setBusy(false); setMsg('Could not rename the company.'); });
    }
    if (!Modal) return null;
    var body;
    if (rows === null) {
      body = React.createElement('div', { className: 'fs-settings__field-hint' }, 'Loading…');
    } else if (rows.length === 0) {
      body = React.createElement('div', { className: 'fs-settings__field-hint' }, 'No companies could be loaded.');
    } else {
      body = rows.map(function (row) {
        var isEditing = editing === row.id;
        return React.createElement('div', { key: row.id, className: 'fs-settings__field-row' },
          isEditing
            ? fText(draft, function (v) { setDraft(v); setMsg(null); })
            : React.createElement('span', null, row.name),
          isEditing
            ? React.createElement('span', null,
                React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--primary fs-btn--sm', disabled: busy || !draft.trim(), onClick: function () { save(row); } }, busy ? 'Saving…' : 'Save'),
                ' ',
                React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm', onClick: function () { setEditing(null); setMsg(null); } }, 'Cancel'))
            : React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm', onClick: function () { setEditing(row.id); setDraft(row.name); setMsg(null); } }, 'Rename'));
      });
    }
    return React.createElement(Modal, { open: true, size: 'md', title: 'Companies', onClose: props.onClose },
      React.createElement('div', { className: 'fs-settings__pw-form' },
        body,
        msg ? React.createElement('div', { className: 'fs-settings__field-hint', role: 'alert' }, msg) : null,
        React.createElement('div', { className: 'fs-settings__actions' },
          React.createElement('button', { type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--md', onClick: props.onClose }, 'Close'))));
  }
```

- [ ] **Step 5: Implement — header and state in the Sites middle column**

Next to `var nmRef = React.useState(false);` (before the `if (!ctx)` early return — hooks must not be conditional) add:

```js
    var ncRef = React.useState(false);
    var newCompanyOpen = ncRef[0], setNewCompanyOpen = ncRef[1];
    var mcRef = React.useState(false);
    var manageOpen = mcRef[0], setManageOpen = mcRef[1];
    var pcRef = React.useState(null);           /* company preselected for New project */
    var projectCompanyId = pcRef[0], setProjectCompanyId = pcRef[1];
```

After `var canCreate = ...`, add:

```js
    /* Company management is platform_admin only -- the backend gate is
       is_cross_company, which a company admin does not pass, so isAdmin is
       not the signal. Live mode only: there are no companies in the fixtures. */
    var orgApi = window.FS.api.org;
    var mayManageCompanies = !!(orgApi && orgApi.isCrossCompany && orgApi.isCrossCompany(ctx.caller)) && orgLive();
```

In the header's button container, before the `canCreate ? ... '+ New project'` element, add:

```js
        mayManageCompanies ? React.createElement('button', {
          type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm',
          onClick: function () { setManageOpen(true); },
        }, 'Manage companies') : null,
        mayManageCompanies ? React.createElement('button', {
          type: 'button', className: 'fs-btn fs-btn--secondary fs-btn--sm',
          onClick: function () { setNewCompanyOpen(true); },
        }, '+ New company') : null,
```

Change the `+ New project` button's `onClick` to `function () { setProjectCompanyId(null); setNewOpen(true); }`, and pass `initialCompanyId: projectCompanyId` in the `NewProjectModal` props object. Then replace `var modal = newOpen ? ... : null;` with:

```js
    var modal = newOpen ? React.createElement(NewProjectModal, {
      initialCompanyId: projectCompanyId,
      onClose:   function () { setNewOpen(false); },
      onCreated: function (site) { ctx.addSite(site); },
    }) : newCompanyOpen ? React.createElement(NewCompanyModal, {
      onClose:       function () { setNewCompanyOpen(false); },
      onCreated:     function (co) { setNewCompanyOpen(false); setProjectCompanyId(co.id); setNewOpen(true); },
      onUseExisting: function (co) { setNewCompanyOpen(false); setProjectCompanyId(co.id); setNewOpen(true); },
    }) : manageOpen ? React.createElement(ManageCompaniesModal, {
      onClose: function () { setManageOpen(false); },
    }) : null;
```

(A new company goes straight to its first project — the flow the owner was blocked on.)

- [ ] **Step 6: Run the tests**

`node --test tests/company-management-page.test.js` → pass. `node --test tests/*.test.js` → 0 fail.

- [ ] **Step 7: Red-proof**

Change the gate to `ctx.caller.isAdmin` → the gating test fails. Restore.

- [ ] **Step 8: Commit**

```bash
git checkout -- tests/fixtures/
git add scripts/pages/sites.js tests/company-management-page.test.js
git commit -m "feat(sites): + New company and Manage companies, platform_admin only"
```

### Part B wrap-up

- [ ] Rebase on `origin/dev`, full suite, push, PR to `dev`. Note in the body that the rename call needs the Part A backend on the same environment, and that a fresh company has an empty Library until its first invitation. Not exercised in a browser unless the owner has a TEST session open — say which.

---

## Rollout (owner actions marked ✋)

1. Part A merged to `develop` → TEST deploys → the TEST verification in Part A's wrap-up passes.
2. ✋ Release branch from `origin/main` carrying **only** Part A's squash commit (`git worktree add … -b release/company-management origin/main && git cherry-pick <sha> && git push && gh pr create --base main`). The classifier blocks me from this step; I supply the exact command.
3. ✋ Merge it; approve the `production` environment. The deploy applies migration 0080 — **if prod has gained a duplicate company name since 2026-10-03, the migration fails and the deploy stops.** That is intended.
4. After prod is on the build: verify `SERVER_BUILD`, that `fieldsight-prod-org-seed` is gone, and that the owner's Timeline with no `?user=` now shows the lake-wide summary.
5. Part B merged to `dev` → ✋ `dev`→`main` promotion. **`fieldsight-ui` `main` is production with no gate** — only after step 4.
6. ✋ Delete the now-unused repo variable `TEST_MULTI_TENANT_RESOLUTION`.
