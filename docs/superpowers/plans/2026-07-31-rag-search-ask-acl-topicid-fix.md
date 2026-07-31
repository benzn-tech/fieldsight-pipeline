# RAG Search + Ask — topic_id linkage, identity, graded ACL parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore prod Search (empty since 2026-07-17) and Ask (400/403 for org accounts), and bring the RAG ACL to per-author parity with the dashboard (site_manager = SELF+WORKERS), without opening any over-share.

**Architecture:** Four workstreams on the legacy report stack + RAG lambdas. WS1 fixes Search's dropped results two ways (a frontend-agnostic aggregation hotfix that recovers all existing data with zero re-ingest, plus a durable ingest-side topic_id relink + backfill). WS2 removes the legacy DynamoDB identity gate on `/ask`+`/search`. WS3 makes `lambda_rag_search` scope through the same `repositories/scope.visible_scope` primitive the dashboard uses (author_ids fail-closed). WS4 makes project-scoped search accept a site UUID and backfills site slugs.

**Tech Stack:** Python 3.12 Lambdas, psycopg3, Aurora PostgreSQL + pgvector, SAM/CloudFormation, pytest (FakeConn/FakeCursor doubles, no live DB).

## Global Constraints

- **Test harness (Windows + uv):** `export UV_LINK_MODE=copy` then
  `export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2` then
  `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`.
- **No live DB in unit tests.** Mirror existing FakeConn / monkeypatch patterns in `tests/unit/test_lambda_rag_search.py` and `tests/unit/test_lambda_ask_agent_search.py`.
- **Deploy:** push `develop` → TEST stack; push `main` → PROD (approval-gated). Test↔prod share the Aurora cluster, different DBs (`fieldsight_test` vs `fieldsight`, BUG-38). Adding CFN resources/params → grant IAM on deploy role `github-actions-fieldsight-deploy` (`simulate-principal-policy`, don't guess) — env-var-only changes need no IAM.
- **Windows git:** autocrlf=true; use single-line Edit anchors; never `git add -A` on `develop`. Commit only named files.
- **SECURITY (binding):** Task 6 (WS2 remove gate) MUST NOT deploy without Tasks 2+3 (WS3 author filter) in the same release. Deploying WS2 alone lets a site_manager search other authors on their sites.
- **Branch:** create `feat/rag-search-ask-acl-fix` off `develop` (current checkout is `feat/qr-login-v2-backend` — do NOT build on it).

---

## Task 0: Branch setup

**Files:** none (git only)

- [ ] **Step 1: Create the feature branch off develop**

```bash
cd /c/Users/camil/Dropbox/fieldsight-pipeline
git fetch origin
git checkout -b feat/rag-search-ask-acl-fix origin/develop
```

- [ ] **Step 2: Confirm baseline tests pass**

```bash
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```
Expected: all green (baseline before changes).

---

## Task 1: WS1 hotfix — `_aggregate_topics` keeps formal-topic chunks without a topic_id

**Why:** On authority-flip days (2026-07-17+) `report_chunks.topic_id` is NULL, so `_aggregate_topics`'s `if not topic_id: continue` drops every chunk → Search returns 0. Keep `chunk_type=='topic'` chunks even without a UUID; still drop raw `transcript_window` chunks. Group/deeplink by title when no UUID. Recovers ALL existing data with zero re-ingest.

**Files:**
- Modify: `src/lambda_ask_agent.py` (`_aggregate_topics`, ~line 514)
- Test: `tests/unit/test_lambda_ask_agent_search.py`

**Interfaces:**
- Consumes: chunk dicts from rag-search with keys `topic_id, chunk_type, topic_title, chunk_text, metadata, report_date, site_id, site_name, site_slug, source_s3_key, distance`.
- Produces: `_aggregate_topics(chunks, question="") -> list[row]`; unchanged row shape `{report_date, site_name, topic_id, title, route, score, lexical}`. `topic_id` may now be `""` (empty) for title-grouped rows.

- [ ] **Step 1: Write failing tests** (append to `tests/unit/test_lambda_ask_agent_search.py`)

```python
def test_search_keeps_topic_chunk_without_topic_id(monkeypatch):
    # authority-flip (2026-07-17+): chunk_type='topic' but topic_id NULL.
    # metadata.title / chunk_text carry the title; the row MUST survive.
    wire(monkeypatch, [chunk(None, "2026-07-23", 0.2, None, chunk_type="topic",
                             text="[13:30] Voice Recording System Demonstration")])
    out = run(ev(question="recording"))
    assert out["count"] == 1
    r = out["results"][0]
    assert "Voice Recording System Demonstration" in r["title"]
    # deep-link falls back to the derived title (no UUID available)
    assert "topicTitle=" in r["route"]

def test_search_still_drops_topicless_transcript_window(monkeypatch):
    # unchanged: raw transcript-window chunks with no topic stay dropped.
    wire(monkeypatch, [chunk(None, "2026-07-23", 0.2, None,
                             chunk_type="transcript_window", text="spk_0: raw line")])
    out = run(ev(question="recording"))
    assert out["count"] == 0

def test_search_groups_topicless_topic_chunks_by_title(monkeypatch):
    # two topic chunks, same title, no UUID -> collapse to ONE row (best dist).
    wire(monkeypatch, [
        chunk(None, "2026-07-23", 0.4, None, chunk_type="topic",
              text="[13:30] Recording App Orientation"),
        chunk(None, "2026-07-23", 0.1, None, chunk_type="topic",
              text="[13:30] Recording App Orientation"),
    ])
    out = run(ev(question="recording"))
    assert out["count"] == 1
    assert out["results"][0]["score"] == 0.1
```

> Note: `chunk(...)` builds `topic_title=title`; passing `title=None` means the row has NO `topic_title` (mirrors the NULL LEFT-JOIN on a NULL topic_id), forcing the title to come from `chunk_text`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_ask_agent_search.py -q -k "topicless_topic or keeps_topic_chunk_without or still_drops_topicless_transcript"`
Expected: FAIL (topic chunks currently dropped by `if not topic_id`).

- [ ] **Step 3: Implement — change the drop rule + title/route derivation**

In `src/lambda_ask_agent.py` `_aggregate_topics`, replace the drop guard and group key:

```python
    for c in chunks:
        topic_id = c.get("topic_id")
        ctype = c.get("chunk_type")
        # user pref 2026-07-10: raw transcript-window chunks with no topic are
        # noise in a "topics" list -> dropped. BUT authority-flip (2026-07-17+)
        # leaves chunk_type='topic' rows with topic_id=None (ingest can't link a
        # deferred report topic) -- those ARE formal topics and MUST survive,
        # grouped/deeplinked by title (BUG-39 / WS1).
        if not topic_id and ctype != "topic":
            continue
        # Title: report_chunks JOINs topics for topic_title; a NULL topic_id
        # yields NULL topic_title, so fall back to metadata.title then chunk_text.
        md = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
        derived_title = (c.get("topic_title") or md.get("title")
                         or (c.get("chunk_text") or "")[:60])
        date = str(c.get("report_date", "") or "")
        site_id = str(c.get("site_id", "") or "")
        # Group key: UUID when present, else the derived title (defer-day rows).
        group_id = str(topic_id) if topic_id else ("title:" + derived_title)
        key = (date, site_id, group_id)
        dist = c.get("distance")
        dist = float(dist) if dist is not None else 1.0
        cur = groups.get(key)
        if cur is not None and dist >= cur["score"]:
            continue
        folder = _folder_from_source(c.get("source_s3_key"))
        title = derived_title
        route = "/timeline?date=" + _q(date)
        if folder:
            route += "&user=" + _q(folder)
        if derived_title:
            route += "&topicTitle=" + _q(derived_title)
        _slug = c.get("site_slug")
        if _slug:
            route += "&site=" + _q(str(_slug))
        hay = derived_title.lower()
        groups[key] = {
            "report_date": date,
            "site_name": c.get("site_name"),
            "topic_id": str(topic_id) if topic_id else "",
            "title": title,
```
(Keep the remainder of the row dict — `route`, `score`, `lexical` — as-is; they already exist below this block. Adjust only the fields shown. The lexical/threshold ranking that follows the loop is unchanged.)

- [ ] **Step 4: Run to verify pass** (new tests + the whole file so existing tests still pass)

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_lambda_ask_agent_search.py -q`
Expected: PASS (new 3 + all pre-existing, incl. `test_search_mode_drops_topicless_chunks`).

- [ ] **Step 5: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_lambda_ask_agent_search.py
git commit -m "fix(search): keep authority-flip topic chunks (topic_id NULL) in the list, group by title (BUG-39 WS1 hotfix)"
```

---

## Task 2: WS3 — `search_chunks` author_ids filter (fail-closed) in the SQL

**Why:** `build_search_sql` filters only by site. Add an optional per-author allow-set. `c.user_id = ANY(author_ids)` is fail-closed by construction: a NULL `user_id` never matches, so unattributed chunks drop out under SELF/SELF+WORKERS.

**Files:**
- Modify: `src/repositories/search_sql.py` (`build_search_sql`)
- Modify: `src/repositories/chunks.py` (`search_chunks`)
- Test: `tests/unit/test_search_sql.py`

**Interfaces:**
- Produces: `build_search_sql() -> str` (now contains the author clause). `search_chunks(conn, query_embedding, accessible_site_ids, k=5, date_from=None, date_to=None, author_ids=None) -> list[dict]` — `author_ids=None` means no per-author filter (ALL/SITE); a list means restrict to those user_ids.

- [ ] **Step 1: Write failing test** (append to `tests/unit/test_search_sql.py`)

```python
def test_search_sql_has_author_filter_with_null_guard():
    sql = build_search_sql()
    # None => no filter (Ask/ALL/SITE); a list => restrict by user_id.
    assert "%(author_ids)s" in sql
    assert "c.user_id = ANY(%(author_ids)s" in sql
    # IS NULL guard so passing author_ids=None is a no-op (byte-identical scope)
    assert "%(author_ids)s::uuid[] IS NULL" in sql
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run ... pytest tests/unit/test_search_sql.py::test_search_sql_has_author_filter_with_null_guard -q`
Expected: FAIL (clause absent).

- [ ] **Step 3: Implement — add the guarded author clause**

In `src/repositories/search_sql.py`, add one line to the WHERE (after the site filter, before the date bounds):

```python
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "AND (%(author_ids)s::uuid[] IS NULL OR c.user_id = ANY(%(author_ids)s::uuid[])) "
        "AND (%(date_from)s::date IS NULL OR c.report_date >= %(date_from)s::date) "
```

In `src/repositories/chunks.py`, thread the param:

```python
def search_chunks(conn, query_embedding, accessible_site_ids, k=5,
                  date_from=None, date_to=None, author_ids=None) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to,
         "author_ids": list(author_ids) if author_ids is not None else None},
    ).fetchall()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run ... pytest tests/unit/test_search_sql.py tests/unit/test_chunk_search_sql.py -q`
Expected: PASS (new + existing SQL-shape tests).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/search_sql.py src/repositories/chunks.py tests/unit/test_search_sql.py
git commit -m "feat(rag): search_chunks optional author_ids filter (fail-closed via = ANY) (WS3)"
```

---

## Task 3: WS3 — `lambda_rag_search` scopes through `scope.visible_scope` (graded ACL parity)

**Why:** rag-search uses the legacy binary `resolve_scope` + `accessible_site_ids` (site-only). Route it through the SAME `repositories/scope.visible_scope` the dashboard uses so Search/Ask apply the identical per-author tier (site_manager → SELF+WORKERS). Gate on the same `GRADED_ROLES` env; when off, keep today's legacy site-only behavior (byte-identical to now).

**Files:**
- Modify: `src/lambda_rag_search.py` (ACL block ~lines 36–103; add `from repositories import scope`, `GRADED_ROLES` env)
- Test: `tests/unit/test_lambda_rag_search.py`

**Interfaces:**
- Consumes: `scope.visible_scope(conn, caller) -> {"site_ids": set[str], "user_scope": str, "author_ids": set|None, ...}` (Task 2's `search_chunks(..., author_ids=...)`).
- Produces: rag-search return unchanged (`{"chunks": [...], "site_count": n}`); now also narrows chunks by author when graded + SELF/SELF+WORKERS.

- [ ] **Step 1: Update existing fake_search signatures** (they must accept the new kwarg or every rag-search test errors)

In `tests/unit/test_lambda_rag_search.py`, change EVERY `fake_search` / `search_chunks` monkeypatch lambda signature to add `author_ids=None`. Example:

```python
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None, author_ids=None:
                      (captured.update(site_ids=site_ids, author_ids=author_ids) or []))
```
(Apply to all 7 occurrences.)

- [ ] **Step 2: Write failing tests** (append)

```python
def test_graded_site_manager_passes_self_workers_author_ids(wired, monkeypatch):
    monkeypatch.setattr(rag, "GRADED_ROLES", True)
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "user_scope": "SELF+WORKERS",
                                              "author_ids": {"me", "worker-1"}})
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None, author_ids=None:
                      (captured.update(site_ids=site_ids, author_ids=author_ids) or []))
    rag.lambda_handler(make_event(), None)
    assert set(captured["site_ids"]) == {"s-1"}
    assert set(captured["author_ids"]) == {"me", "worker-1"}   # per-author narrowed

def test_graded_admin_passes_no_author_filter(wired, monkeypatch):
    monkeypatch.setattr(rag, "GRADED_ROLES", True)
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1", "s-2"}, "user_scope": "ALL",
                                              "author_ids": None})
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None, author_ids=None:
                      (captured.update(author_ids=author_ids) or []))
    rag.lambda_handler(make_event(), None)
    assert captured["author_ids"] is None   # ALL => no per-author filter

def test_graded_off_falls_back_to_site_only(wired, monkeypatch):
    monkeypatch.setattr(rag, "GRADED_ROLES", False)
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None, author_ids=None:
                      (captured.update(author_ids=author_ids, site_ids=site_ids) or []))
    rag.lambda_handler(make_event(), None)
    assert captured["author_ids"] is None            # legacy path, site-only
    assert captured["site_ids"] == ["s-1"]
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run ... pytest tests/unit/test_lambda_rag_search.py -q -k "graded"`
Expected: FAIL (`rag.GRADED_ROLES` / `rag.scope` not present; author_ids not forwarded).

- [ ] **Step 4: Implement — graded branch in rag-search**

In `src/lambda_rag_search.py`: add imports/env near the other repo imports and `resolve_scope`:

```python
from repositories import scope
GRADED_ROLES = os.environ.get("GRADED_ROLES", "").lower() == "true"
```

Replace the site-id resolution block (the `if resolve_scope(...) == "ALL": ... else: accessible_site_ids(...)`) with:

```python
    if GRADED_ROLES:
        sc = scope.visible_scope(conn, caller)          # dashboard's ACL primitive
        site_ids = [str(s) for s in sc["site_ids"]]
        author_ids = ([str(a) for a in sc["author_ids"]]
                      if sc["author_ids"] is not None else None)
    else:
        # legacy site-only (byte-identical to pre-graded behavior)
        if resolve_scope(caller["global_role"]) == "ALL":
            site_ids = [s["id"] for s in sites.list_company_sites(conn, caller["company_id"])]
        else:
            site_ids = memberships.accessible_site_ids(conn, caller["id"], caller["global_role"])
        author_ids = None
```

Keep the existing `site` (project) narrowing block AFTER this (it intersects `site_ids`). Then pass `author_ids` through:

```python
    rows = chunks.search_chunks(conn, qv, site_ids, k=k, author_ids=author_ids,
                                date_from=date_from, date_to=date_to)
```

- [ ] **Step 5: Run to verify pass** (whole rag-search file — existing + new)

Run: `uv run ... pytest tests/unit/test_lambda_rag_search.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_rag_search.py tests/unit/test_lambda_rag_search.py
git commit -m "feat(rag): rag-search scopes through scope.visible_scope for per-author parity with dashboard (WS3)"
```

---

## Task 4: WS4 — project `site` param accepts a UUID or a slug

**Why:** The frontend sends `site` = site **UUID**; rag-search resolves it only by slug (`get_company_site_by_slug`), and 7/11 sites have NULL slug → empty scope → 0. Accept a UUID that is already in the caller's accessible set; fall back to slug.

**Files:**
- Modify: `src/lambda_rag_search.py` (the `site` narrowing block, ~lines 74–83)
- Test: `tests/unit/test_lambda_rag_search.py`

**Interfaces:**
- Consumes: `site_ids` (from Task 3), `sites.get_company_site_by_slug`.
- Produces: unchanged return; `site` matches by UUID first, then slug, else `[]` (deny).

- [ ] **Step 1: Write failing test** (append)

```python
def test_site_filter_accepts_uuid_directly(wired):
    # frontend sends the site UUID; it must narrow without a slug lookup.
    wired.setattr(rag.sites, "list_company_sites",
                  lambda conn, cid: [{"id": "s-1"}, {"id": "s-2"}])
    def boom_slug(*a, **k):
        raise AssertionError("slug lookup must not run when site is an accessible UUID")
    wired.setattr(rag.sites, "get_company_site_by_slug", boom_slug)
    captured = {}
    wired.setattr(rag.chunks, "search_chunks",
                  lambda conn, qv, site_ids, k=5, date_from=None, date_to=None, author_ids=None:
                      (captured.update(site_ids=site_ids) or []))
    ev = make_event(); ev["site"] = "s-2"
    res = rag.lambda_handler(ev, None)
    assert captured["site_ids"] == ["s-2"]
    assert res["site_count"] == 1
```

> Keep `test_site_filter_narrows_to_one_accessible_site` (slug path) passing — it uses a slug that is NOT in `site_ids` as a UUID, so it falls through to the slug lookup.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run ... pytest tests/unit/test_lambda_rag_search.py -q -k "uuid_directly"`
Expected: FAIL (slug lookup fires / boom).

- [ ] **Step 3: Implement — UUID-first narrowing**

In `src/lambda_rag_search.py` `site` block:

```python
    site_filter = event.get("site") or None
    if site_filter:
        sset = {str(s) for s in site_ids}
        if str(site_filter) in sset:                 # UUID already in reach
            site_ids = [str(site_filter)]
        else:                                        # legacy: treat as project slug
            matched = sites.get_company_site_by_slug(conn, caller["company_id"], site_filter)
            matched_id = str(matched["id"]) if matched else None
            site_ids = [s for s in site_ids if str(s) == str(matched_id)]
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run ... pytest tests/unit/test_lambda_rag_search.py -q`
Expected: PASS (UUID + slug + deny cases).

- [ ] **Step 5: Commit**

```bash
git add src/lambda_rag_search.py tests/unit/test_lambda_rag_search.py
git commit -m "fix(rag): project search accepts site UUID (not only slug) (WS4)"
```

---

## Task 5: WS3/WS4 infra — set `GRADED_ROLES` env on `RagSearchFunction`

**Why:** Task 3 gates on `GRADED_ROLES`; the RagSearch Lambda must receive it (org-api already does via repo var `PROD_GRADED_ROLES`/`TEST_GRADED_ROLES`). Env-var only — no IAM change.

**Files:**
- Modify: `template.yaml` (`RagSearchFunction` Environment)
- Modify: `.github/workflows/deploy.yml` and `.github/workflows/deploy-prod.yml` (pass the existing `GradedRoles` parameter through — mirror how `OrgApiFunction` gets it)
- Test: `tests/unit/test_template_*.py` (add a template assertion if a template-shape test exists; else a `grep`-style check)

**Interfaces:**
- Consumes: existing CFN parameter that carries `GRADED_ROLES` to `OrgApiFunction`.
- Produces: `RagSearchFunction` env `GRADED_ROLES` set to the same value.

- [ ] **Step 1: Find how OrgApiFunction receives GRADED_ROLES**

```bash
grep -n "GRADED_ROLES\|GradedRoles" template.yaml .github/workflows/deploy.yml .github/workflows/deploy-prod.yml
```
Record the parameter name and the exact `Environment.Variables.GRADED_ROLES: !Ref <Param>` line on `OrgApiFunction`.

- [ ] **Step 2: Add the same env to RagSearchFunction**

In `template.yaml`, under `RagSearchFunction:` → `Properties:` → `Environment:` → `Variables:`, add:

```yaml
          GRADED_ROLES: !Ref <SameParamAsOrgApi>
```
No workflow change is needed if the parameter is already passed to the stack for OrgApi (it is a stack-level `--parameter-overrides`); confirm both `deploy.yml` and `deploy-prod.yml` already pass it. If a workflow passes it ONLY for org-api via a function-specific override, replicate that override for rag-search.

- [ ] **Step 3: Verify template parses + env present**

```bash
grep -n -A3 "RagSearchFunction" template.yaml | grep GRADED_ROLES   # must show the new line
sam validate --lint 2>&1 | tail -5    # if sam is available; else skip
```
Expected: `GRADED_ROLES` line present under RagSearchFunction.

- [ ] **Step 4: Commit**

```bash
git add template.yaml .github/workflows/deploy.yml .github/workflows/deploy-prod.yml
git commit -m "infra(rag): pass GRADED_ROLES env to RagSearchFunction (WS3 parity gate)"
```

---

## Task 6: WS2 — remove the legacy user/role gate on `/ask` (and confirm `/search`)

**Why:** `fieldsight-prod-api.ask_question` 400s "Missing user" (global Ask) / 403s "Access denied to this user" (org account absent from the 4-row legacy DynamoDB). The RAG ACL is now enforced downstream by `caller_sub` → rag-search (Task 3). Drop the legacy pre-gate; keep forwarding `caller_sub`.

> **DEPLOY CONSTRAINT:** ships in the SAME release as Tasks 2+3. Never alone.

**Files:**
- Modify: `src/lambda_fieldsight_api.py` (`ask_question`, ~lines 966–1004)
- Test: `tests/unit/test_lambda_fieldsight_api_ask.py`

**Interfaces:**
- Consumes: `caller` dict from `get_caller_identity` (has `sub` from Cognito claims).
- Produces: `ask_question` returns the agent envelope for org callers (no 400/403); `caller_sub` still forwarded.

- [ ] **Step 1: Write failing tests** (append; mirror the file's existing wiring for `ask_question`/`lambda_client`)

```python
def test_ask_org_caller_no_dynamo_profile_not_403(monkeypatch, invoke_capture):
    # org account: get_caller_identity yields role='viewer', sites=[], display_name=''
    caller = {"sub": "sub-ucpk", "role": "viewer", "sites": [], "managed_sites": [],
              "display_name": "", "company_id": ""}
    resp = api.ask_question({"question": "what happened on site?"}, caller)
    assert resp["statusCode"] != 403
    assert resp["statusCode"] != 400
    assert invoke_capture["Payload"]["caller_sub"] == "sub-ucpk"

def test_ask_global_no_user_not_400(monkeypatch, invoke_capture):
    caller = {"sub": "sub-ucpk", "role": "viewer", "sites": [], "managed_sites": [],
              "display_name": "", "company_id": ""}
    resp = api.ask_question({"question": "site-wide question"}, caller)  # no 'user'
    assert resp["statusCode"] != 400
```

> `invoke_capture` = a fixture that monkeypatches `api.lambda_client.invoke` to record the `Payload` and return a minimal `{"Payload": BytesIO(b'{"answer":"x","citations":[]}')}`. Mirror the FakeLambdaClient pattern in `test_lambda_ask_agent_search.py`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run ... pytest tests/unit/test_lambda_fieldsight_api_ask.py -q -k "org_caller_no_dynamo or global_no_user"`
Expected: FAIL (current 400/403).

- [ ] **Step 3: Implement — drop the legacy gate**

In `src/lambda_fieldsight_api.py` `ask_question`, remove the user-required + access-check block:

```python
    # REMOVED (BUG-39 WS2): legacy DynamoDB user/role gate. The RAG ACL is
    # enforced downstream by caller_sub -> rag-search (graded scope.visible_scope,
    # WS3). 'user' is optional soft context only.
    #   was: if not user: user = resolve_user_display_name(caller)
    #        if not user: return error('Missing user')
    #        if caller['role'] == 'worker': user = resolve_user_display_name(caller)
    #        elif user and not can_access_user_data(caller, user): return error('Access denied to this user', 403)
    user = body.get('user', '')   # soft context; may be empty for org callers
```
Leave the payload build (forwarding `caller_sub`, optional `user`/`date`/`topic_id`) intact.

- [ ] **Step 4: Run to verify pass** (whole file — ensure no other test relied on the gate)

Run: `uv run ... pytest tests/unit/test_lambda_fieldsight_api_ask.py tests/unit/test_lambda_fieldsight_api_search.py tests/unit/test_lambda_fieldsight_api_acl.py -q`
Expected: PASS. If a pre-existing test asserted the 400/403 for a scoped caller, update it to reflect that ACL now lives in rag-search (note the change in the commit).

- [ ] **Step 5: Commit**

```bash
git add src/lambda_fieldsight_api.py tests/unit/test_lambda_fieldsight_api_ask.py
git commit -m "fix(ask): drop legacy DynamoDB user/role gate; ACL enforced downstream via caller_sub (BUG-39 WS2)"
```

---

## Task 7: WS1 root — ingest links defer-day chunks to extraction topics

**Why:** On authority-flip defer days `topic_seq_to_id` stays `{}` so every chunk is `topic_id=None`. Populate it by matching each report topic to the day's existing Aurora extraction topics (they DO exist — verified) by time overlap, title as tiebreak. Unmatched → None (Task 1 keeps them searchable by title).

**Files:**
- Modify: `src/lambda_ingest.py` (defer branch, ~lines 337–349)
- New helper: `src/lambda_ingest.py` `_match_report_topics_to_extraction(conn, site_id, user_id, date, report_topics) -> dict[int, str]` (seq → extraction topic uuid)
- Reference (read-only): `src/repositories/topics.py` for the extraction-topic lister/columns (`topics` has `id, site_id, user_id, report_date, occurred_at, ...`).
- Test: `tests/unit/test_lambda_ingest.py`

**Interfaces:**
- Consumes: `report.topics[i]` with `topic_id` (seq int), `topic_title`, `time_range`; Aurora extraction topics for `(site_id, user_id, report_date)`.
- Produces: `_match_report_topics_to_extraction(...) -> {seq: extraction_uuid}` merged into `topic_seq_to_id` on defer days.

- [ ] **Step 1: Add a repo lister for a day's extraction topics** (if none exists)

Check `src/repositories/topics.py` for a function returning a day's topics with `id, occurred_at, title` filtered to `source_s3_key LIKE 'extractions/%'`. If absent, add:

```python
def list_extraction_topics_for_day(conn, site_id, user_id, report_date):
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, title, occurred_at FROM topics "
        "WHERE site_id=%s AND user_id=%s AND report_date=%s "
        "AND source_s3_key LIKE 'extractions/%%' ORDER BY occurred_at",
        (site_id, user_id, report_date),
    ).fetchall()
```

- [ ] **Step 2: Write failing test** (append to `tests/unit/test_lambda_ingest.py`, mirror its FakeConn/monkeypatch style)

```python
def test_defer_day_links_chunks_to_extraction_topic_by_time(monkeypatch, ...):
    # AUTHORITY_FLIP on + extraction topics exist for the day.
    # report topic seq 0 @ 13:30-13:31 -> matches extraction topic 'ext-a' @ 13:30.
    # assert insert_chunk received topic_id='ext-a' for that chunk (not None).
    ...
    assert captured_topic_ids[0] == "ext-a"

def test_defer_day_unmatched_report_topic_stays_none(monkeypatch, ...):
    # a report topic with no overlapping extraction topic -> topic_id None
    # (Task 1 keeps it searchable by title; must not crash).
    ...
    assert captured_topic_ids[0] is None
```

> Fill the `...` by mirroring `test_lambda_ingest.py`'s existing defer-branch test setup (it already exercises `AUTHORITY_FLIP` + `has_topics_for_source_prefix`). Capture `topic_id` via a monkeypatched `chunks.insert_chunk`.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run ... pytest tests/unit/test_lambda_ingest.py -q -k "defer_day_links or defer_day_unmatched"`
Expected: FAIL (defer branch currently leaves `topic_seq_to_id` empty).

- [ ] **Step 4: Implement — match + populate on defer days**

Add the matcher helper (time-overlap primary, title-similarity tiebreak; both derive a start-seconds from `time_range` / `occurred_at`):

```python
def _match_report_topics_to_extraction(conn, site_id, user_id, date, report_topics):
    ext = topics.list_extraction_topics_for_day(conn, site_id, user_id, date)
    if not ext:
        return {}
    seq_to_id = {}
    for t in report_topics:
        seq = t.get("topic_id")
        if seq is None:
            continue
        best, best_key = None, None
        for e in ext:
            key = _overlap_or_title_score(t, e)   # (overlap_secs, title_ratio); higher=better
            if key and (best_key is None or key > best_key):
                best, best_key = e, key
        if best is not None:
            seq_to_id[seq] = str(best["id"])
    return seq_to_id
```

In the defer branch (`if defer_to_extraction:`), replace the comment-only body with:

```python
        if defer_to_extraction:
            logger.info("%s: authority flip — linking chunks to extraction topics under %s",
                        report_key, extraction_prefix)
            topic_seq_to_id = _match_report_topics_to_extraction(
                conn, site["id"], user_id, date, report.get("topics", []))
```
(Leave `topic_seq_to_id = {}` initialization; the defer branch now overwrites it. Non-defer branch unchanged.)

Implement `_overlap_or_title_score(report_topic, ext_topic)` as a small pure helper (parse `time_range` "HH:MM–HH:MM"/"HH:MM:SS" to seconds; `occurred_at` to seconds; return `(overlap_seconds, difflib.SequenceMatcher ratio)` or `None` when neither overlaps nor title-similar > 0.5). Unit-test it directly too.

- [ ] **Step 5: Run to verify pass**

Run: `uv run ... pytest tests/unit/test_lambda_ingest.py -q`
Expected: PASS (defer link + unmatched-None + existing).

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ingest.py src/repositories/topics.py tests/unit/test_lambda_ingest.py
git commit -m "feat(ingest): link authority-flip chunks to extraction topics by time overlap (BUG-39 WS1 root)"
```

---

## Task 8: WS1 backfill — re-ingest 2026-07-17 → today (runbook)

**Why:** Tasks 1+7 fix new ingests; existing 2026-07-17→today chunks stay `topic_id=NULL` until re-ingested. Idempotent via source-key delete+reinsert.

**Files:**
- Create: `docs/superpowers/runbooks/2026-07-31-backfill-topic-id.md`

**Interfaces:** operational only (user runs the AWS commands via `!` — CronCreate/deploy approval are permission-gated).

- [ ] **Step 1: Write the runbook** with the exact steps

```markdown
# Backfill report_chunks.topic_id (authority-flip, 2026-07-17 → today)

## 1. Enumerate the (report_date, folder) pairs still NULL (prod DB, Data API)
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT DISTINCT report_date, split_part(source_s3_key,'/',3) folder FROM report_chunks WHERE topic_id IS NULL AND created_at::date >= '2026-07-17' ORDER BY 1" --region ap-southeast-2

## 2. For each (date, folder): re-trigger ingest by re-touching the report object
# ingest fires on reports/<date>/<folder>/daily_report.json ObjectCreated.
aws s3 cp s3://fieldsight-data-509194952652/reports/<date>/<folder>/daily_report.json \
  s3://fieldsight-data-509194952652/reports/<date>/<folder>/daily_report.json \
  --metadata-directive REPLACE --region ap-southeast-2

## 3. Verify topic_id recovered
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT count(*) total, count(topic_id) with_topic FROM report_chunks WHERE created_at::date >= '2026-07-17'" --region ap-southeast-2
# with_topic should now be > 0 (unmatched report topics legitimately stay NULL; Task 1 keeps them searchable).
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/runbooks/2026-07-31-backfill-topic-id.md
git commit -m "docs(backfill): runbook to re-ingest authority-flip days for topic_id (BUG-39 WS1)"
```

---

## Task 9: WS4 slug — auto-generate on site create + backfill NULL sites

**Why:** 7/11 sites have NULL slug. WS4 UUID (Task 4) makes search work without slug, but the `&site=` deep-link selector-sync still uses slug. Give new sites a slug and backfill existing ones. Independent, low risk — do last.

**Files:**
- Modify: `src/lambda_org_api.py` (`create_site` handler — generate a slug from name when none supplied)
- Reference: `src/repositories/sites.py` (`create_site(..., slug=...)`, `set_slug`)
- Create: `scripts/backfill-site-slugs.md` (runbook) or a one-off `aws rds-data` UPDATE
- Test: `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Produces: `create_site` persists a `slug` (kebab of `name`, deduped per company).

- [ ] **Step 1: Write failing test** (append to `tests/unit/test_lambda_org_api.py`, mirror its handler test style)

```python
def test_create_site_generates_slug_from_name(...):
    # POST /api/org/sites {name:"UC PK"} -> stored slug "uc-pk"
    ...
    assert created["slug"] == "uc-pk"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run ... pytest tests/unit/test_lambda_org_api.py -q -k "generates_slug"`
Expected: FAIL (slug currently None).

- [ ] **Step 3: Implement — slugify in create_site**

Add a pure `_slugify(name) -> str` (lowercase, non-alnum → '-', collapse, strip) and, in the `create_site` handler, pass `slug=_slugify(name)` when the body omits slug. Dedup: if `get_company_site_by_slug` hits, append `-2`, `-3`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run ... pytest tests/unit/test_lambda_org_api.py -q`
Expected: PASS.

- [ ] **Step 5: Backfill runbook for existing NULL-slug sites**

```markdown
# For each NULL-slug site (prod): UPDATE sites SET slug=<kebab(name)> WHERE id=<id> AND slug IS NULL;
# enumerate: SELECT id, name FROM sites WHERE slug IS NULL;
```
(Run via `aws rds-data` against `fieldsight`. Verify uniqueness per company_id before applying.)

- [ ] **Step 6: Commit**

```bash
git add src/lambda_org_api.py tests/unit/test_lambda_org_api.py scripts/backfill-site-slugs.md
git commit -m "feat(sites): auto-generate slug on create + backfill runbook (BUG-39 WS4)"
```

---

## Rollout (after all tasks green on the branch)

1. Push `feat/rag-search-ask-acl-fix` → open PR to `develop` → CI green → merge → TEST stack auto-deploy.
2. **Verify on test** (dev front-end repointed to test gateway `wdsgobb7b0`, DB `fieldsight_test`): a `site_manager` account searches → only SELF+WORKERS rows; Ask returns citations; project-scoped search returns rows. (Use a test-DB row set with mixed authors on one site.)
3. Merge `develop` → `main` → prod approval → PROD deploy. **Tasks 2+3+6 land together (security constraint).**
4. Run Task 8 backfill (07-17→today) on prod; verify `with_topic > 0`.
5. Run Task 9 slug backfill on prod.

## Self-review notes (spec coverage)

- S1 Search empty → Task 1 (hotfix, all existing data) + Task 7 (root) + Task 8 (backfill). ✓
- S2 Ask 400/403 → Task 6. ✓
- S3 rag-search per-author ACL → Task 2 (SQL) + Task 3 (scope) + Task 5 (env). ✓
- S4 site UUID-vs-slug → Task 4 + Task 9 (slug). ✓
- Security constraint (WS2 not without WS3) → Global Constraints + Task 6 header + Rollout step 3. ✓
