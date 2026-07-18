# Phase 2 — Aurora read consolidation (Today/Timeline/dates/site-users) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Timeline **dates** dots and the **USERS ON SITE** panel off the legacy `lambda_fieldsight_api` (S3 `user_mapping`-based) read paths and onto **Aurora (org-api)**, scoped by the EXISTING `memberships.accessible_site_ids`. This closes two confirmed leaks/bugs: (a) the `/api/dates` **dots leak** — legacy `get_dates` never rejects a `?site=` the caller can't access and, when the caller has no accessible users on that site, falls through to marking EVERY report-date across all users/companies; (b) **USERS ON SITE empty** — the panel reads legacy `/site-users`, which is built from `config/user_mapping.json` and therefore returns `[]` for Aurora-only sites. The legacy `/timeline` + `/dates` fallback is retained but put **behind a D5 flag** so it can be retired without a code change.

**Architecture:** Two new **read-only** org-api endpoints on the in-VPC Aurora Lambda, each ACL'd exactly like `/live-items`/`/programme` (admin/gm → every non-archived company site; everyone else → `memberships.accessible_site_ids`):
- `GET /api/org/dates?months=&site=` → distinct `topics.report_date` values scoped to the caller's accessible sites (∩ `?site` when given; a `?site` outside the accessible set is **403**, which is what kills the dots leak). Backed by one new repo query `topics.list_report_dates` that mirrors `list_topics_for_date`'s `site_id = ANY(...)` shape.
- `GET /api/org/sites/{id}/members` → members of one accessible site, read from `memberships` (not `user_mapping`), company + site guarded. Backed by one new repo query `memberships.members_for_site` that mirrors `list_company_memberships`.

Then the UI repoints its two reads to Aurora when the org backend is live, keeping the legacy report-gateway reads behind the existing `orgBaseUrl` kill switch **and** a new `legacyReadFallback` (D5) flag. No pipeline schema/migration change; no `template.yaml` change (both routes ride the existing `/api/org/{proxy+}` integration). Every task is independently shippable and reversible by a flag (`timelineSource`, `orgBaseUrl`, `legacyReadFallback`).

**Tech Stack:** Pipeline — Python 3.12, psycopg3 (in-VPC Aurora PG16), SAM `fieldsight-test`/`fieldsight-prod`. Tests: pytest with the repo's FakeConn/FakeCursor (unit, validated in **CI** — BUG-29 no local Python) + the `db` fixture (integration, needs `TEST_DATABASE_URL`, set in CI). UI — no-build single-file React (`window.FS.api.*`), gate = `node --check` + grep pre-checks + `?v=N` cache-buster bumps (no test harness exists in this repo).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-visibility-permission-model-design.md` — §1.1 (the leaks), §3.2 (read-path unification), §3.0 (multi-tenancy), §4 Rollout step 2, D5 (flagged fallback). Phase 1 plan (format/house-style reference): `docs/superpowers/plans/2026-07-17-phase1-identity-enrollment.md`.
- **Scope guard on EVERY new read (non-negotiable):** company + membership scoping. Reuse the EXISTING helpers verbatim — `memberships.accessible_site_ids(conn, user_id, global_role)`, `lambda_org_api._allowed_site_ids(conn, caller)` (str set), `lambda_org_api._resolve_site_param(conn, caller, site_param) -> (site_id_str|None, err|None)`. Never leak cross-company or cross-project. Phase 2 uses **today's** company+membership scoping; it does **NOT** depend on Phase 3's graded `visible_scope`.
- **No new ACL semantics.** These endpoints mirror `/live-items` (`list_live_items`, `src/lambda_org_api.py:822`) and `/programme` (`_resolve_site_param`, `src/lambda_org_api.py:893`) EXACTLY. If a caller can't reach a site through one of those, they can't reach its dates/members either.
- **No local Python (BUG-29):** unit tests are asserted to pass in **CI**, not locally. Integration tests SKIP locally without `TEST_DATABASE_URL` and must be green in CI.
- **Pipeline git:** branch off `develop` (`git checkout -b <name> origin/develop`); NEVER check out `develop` (held by another worktree); NEVER `git add -A` — stage named files only; CRLF repo → single-line Edit anchors. New dev content (comments/commits/docs) in ENGLISH.
- **UI git/hygiene:** `node --check` every modified `.js`; bump `?v=N` in the preview HTMLs for any changed loaded file; BUG-19 (never `new Date('YYYY-MM-DD')`), BUG-20 (a `text/html` 200 is the SPA shell, not JSON — `_fetch.js` already guards). No build step.
- No `template.yaml` / SAM change and no DB migration — both routes are pure reads on existing tables through the existing proxy integration. Live/prod smoke is user-gated (Task 5 handoff), not run by the implementer.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `src/repositories/topics.py` | Modify | add `list_report_dates()` — distinct `report_date` for a site-id set since a date |
| `tests/unit/test_topics_repo.py` | Modify | unit: `list_report_dates` SQL shape + empty-set short-circuit (FakeConn) |
| `tests/integration/test_topics_repository.py` | Modify | integration: distinct dates, since-window, cross-site isolation (real DB) |
| `src/repositories/memberships.py` | Modify | add `members_for_site()` — one site's members, company+site scoped |
| `tests/integration/test_memberships_acl.py` | Modify | integration: members returned, cross-company excluded, archived excluded |
| `src/lambda_org_api.py` | Modify | add `get_org_dates` + `list_site_members` handlers + 2 dispatch routes + header doc lines |
| `tests/unit/test_lambda_org_api.py` | Modify | unit: dates ACL/leak-reject/scope + members ACL, via the existing `wired`/`make_event` harness |
| `fieldsight-ui/scripts/api/org.js` | Modify | add `getSiteMembers()` Aurora read + export |
| `fieldsight-ui/scripts/api/sites.js` | Modify | `getSiteUsers` → Aurora `getSiteMembers` when org live (legacy behind D5 flag) |
| `fieldsight-ui/scripts/pages/timeline.js` | Modify | default-site source: `org.getOrgSites()` (Aurora accessible), not legacy `sites.getSites()` |
| `fieldsight-ui/scripts/api/dates.js` | Modify | Aurora `/dates` when `timelineSource==='aurora'`; legacy `/dates` fallback flag-gated (D5) |
| `fieldsight-ui/scripts/api/timeline.js` | Modify | flag-gate the `_accessDenied` legacy `/timeline` fallback (D5) |
| `fieldsight-ui/scripts/api/index.js` | Modify | add `legacyReadFallback` flag (env-driven, default true) |
| `fieldsight-ui/app-shell-preview.html` (+ other preview HTMLs loading these) | Modify | bump `?v=N` cache-busters for the changed `.js` |

---

### Task 1: Aurora `GET /api/org/dates` — membership-scoped report-dates (kills the dots leak)

**Why:** Legacy `get_dates` (`src/lambda_fieldsight_api.py:296`) has NO `?site` access check (unlike `get_site_users` at :1048–1050 which does), and when `?site=` yields no accessible users, `user_folders` is `[]` → falsy at :331 → the `else` at :340–341 marks EVERY date that has any report, leaking the existence of other users'/companies' report-dates. The Aurora endpoint rejects an out-of-scope `?site` (403) and computes dots only from `topics` rows on the caller's accessible sites.

**Files:**
- Modify: `src/repositories/topics.py`, `tests/unit/test_topics_repo.py`, `tests/integration/test_topics_repository.py`
- Modify: `src/lambda_org_api.py`, `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Produces (repo): `topics.list_report_dates(conn, site_ids, since_date) -> list[datetime.date]` — distinct `report_date` for `site_id = ANY(site_ids)` and `report_date >= since_date`, ascending; `[]` on empty `site_ids` without a round-trip. `site_ids` may be str **or** uuid (SQL casts `::uuid[]`); `since_date` is a `datetime.date`.
- Produces (handler): `GET /api/org/dates?months=<int>&site=<uuid|slug>` → `{"dates": {"YYYY-MM-DD": {"hasReport": true}}}` (same envelope shape the UI dots consumer reads).
- Consumes: existing `_allowed_site_ids`, `_resolve_site_param`, `topics.list_report_dates`.

- [ ] **Step 1: Write the failing repo unit test**

Append to `tests/unit/test_topics_repo.py` (the module already defines `FakeConn`/`FakeCursor` recording every `execute()`'s SQL+params, and imports `from repositories import topics`):

```python
import datetime as _dt


def test_list_report_dates_builds_distinct_since_query():
    conn = FakeConn(results=[[{"report_date": _dt.date(2026, 7, 16)},
                              {"report_date": _dt.date(2026, 7, 17)}]])
    out = topics.list_report_dates(conn, ["s-1", "s-2"], _dt.date(2026, 5, 1))
    assert out == [_dt.date(2026, 7, 16), _dt.date(2026, 7, 17)]
    call = conn.calls[0]
    assert "SELECT DISTINCT report_date FROM topics" in call["sql"]
    assert "site_id = ANY(%s::uuid[])" in call["sql"]
    assert "report_date >= %s" in call["sql"]
    assert "ORDER BY report_date" in call["sql"]
    assert call["params"] == (["s-1", "s-2"], _dt.date(2026, 5, 1))


def test_list_report_dates_empty_site_ids_short_circuits():
    conn = FakeConn(results=[])
    assert topics.list_report_dates(conn, [], _dt.date(2026, 5, 1)) == []
    assert conn.calls == []  # no round-trip on empty scope (mirrors list_topics_for_date)
```

- [ ] **Step 2: Run it — verify it fails**

Run (CI, or note as CI-gated locally per BUG-29): `python -m pytest tests/unit/test_topics_repo.py -k report_dates -v`
Expected: FAIL — `AttributeError: module 'repositories.topics' has no attribute 'list_report_dates'`.

- [ ] **Step 3: Implement `list_report_dates`**

In `src/repositories/topics.py`, add after `list_topics_for_date` (ends ~line 220). `report_date` is a first-class DATE column (`_TOPIC_COLS`, line 6); the `::uuid[]` cast lets the handler pass the str ids it already has from `_allowed_site_ids`/`_resolve_site_param` without re-fetching raw uuids:

```python
def list_report_dates(conn, site_ids, since_date) -> list:
    """Distinct report_date values (ascending) for a caller-computed ACL
    site-id set, on or after since_date. Backs org-api GET /api/org/dates —
    the membership-scoped replacement for legacy get_dates' S3 folder scan,
    which had no ?site access check and leaked cross-user/cross-company
    report-dates (visibility spec §1.1 dots leak). site_ids is the SAME
    kind of caller-scoped list list_topics_for_date takes (ALL company
    sites for admin/gm, else memberships.accessible_site_ids); the ::uuid[]
    cast accepts the str ids _allowed_site_ids/_resolve_site_param hand back.
    Empty site_ids -> [] without a round-trip (mirrors list_topics_for_date)."""
    if not site_ids:
        return []
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT report_date FROM topics "
        "WHERE site_id = ANY(%s::uuid[]) AND report_date >= %s "
        "ORDER BY report_date",
        (list(site_ids), since_date),
    ).fetchall()
    return [r["report_date"] for r in rows]
```

- [ ] **Step 4: Run the repo unit test — verify it passes**

Run: `python -m pytest tests/unit/test_topics_repo.py -k report_dates -v` → 2 passed.

- [ ] **Step 5: Write the failing repo integration test (real-DB semantics)**

Append to `tests/integration/test_topics_repository.py` (uses the shared `db` fixture — real Postgres, per-test rollback; `from repositories import topics` is at the top). Seed helpers there insert companies/sites/users/topics; if none matches this shape, add a minimal local one:

```python
import datetime as _dt


@pytest.mark.integration
def test_list_report_dates_distinct_scoped_and_windowed(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    s1 = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S1') RETURNING id", (cid,)).fetchone()[0]
    s2 = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S2') RETURNING id", (cid,)).fetchone()[0]
    other = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S3') RETURNING id", (cid,)).fetchone()[0]

    def topic(site, d):
        db.execute("INSERT INTO topics (site_id, report_date, title) VALUES (%s,%s,'t')", (site, d))

    topic(s1, _dt.date(2026, 7, 16)); topic(s1, _dt.date(2026, 7, 16))  # dup same day -> DISTINCT collapses
    topic(s2, _dt.date(2026, 7, 17))
    topic(other, _dt.date(2026, 7, 18))                                 # NOT in the scoped set
    topic(s1, _dt.date(2026, 1, 1))                                     # before the window

    out = topics.list_report_dates(db, [str(s1), str(s2)], _dt.date(2026, 6, 1))
    assert out == [_dt.date(2026, 7, 16), _dt.date(2026, 7, 17)]        # distinct, ordered, other/old excluded
```

(Match `upsert_topic`'s required columns if a bare INSERT violates NOT NULLs — reuse the module's existing topic-seeding helper instead of the inline INSERT if one exists.)

- [ ] **Step 6: Run the integration test (CI; SKIPs locally without `TEST_DATABASE_URL`)**

Run: `python -m pytest tests/integration/test_topics_repository.py -k report_dates -v`.

- [ ] **Step 7: Write the failing handler unit tests**

Append to `tests/unit/test_lambda_org_api.py` (harness already provides `make_event`, `FakeConn`, `CALLER` (admin), `wired`, `body_of`; `SITE_ID`/`OTHER_SITE_ID` constants exist for the programme tests):

```python
import datetime as _dt


def test_dates_admin_scopes_to_allowed_ids(wired):
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-1", "s-2"})
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since: (seen.update(site_ids=set(site_ids), since=since)
                                                 or [_dt.date(2026, 7, 16)]))
    res = org.lambda_handler(make_event("GET", "/api/org/dates", params={"months": "2"}), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == {"s-1", "s-2"}          # no ?site -> full accessible set
    assert isinstance(seen["since"], _dt.date)          # NZ window is a date (BUG-37, not a bare str)
    assert body_of(res)["dates"] == {"2026-07-16": {"hasReport": True}}


def test_dates_worker_scope_via_allowed_ids(wired):
    wired.setattr(org.users, "get_user_by_sub",
                  lambda conn, sub: {**CALLER, "global_role": "worker"})
    seen = {}
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {"s-3"})
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since: (seen.update(site_ids=set(site_ids)) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates"), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == {"s-3"}                  # membership scope, not all-company


def test_dates_rejects_site_outside_accessible_set_403(wired):
    # the dots-leak fix: an out-of-scope ?site must 403 BEFORE any date read,
    # not fall through to a lake-wide scan (legacy get_dates bug).
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    called = []
    wired.setattr(org.topics, "list_report_dates",
                  lambda *a, **k: called.append(1) or [])
    res = org.lambda_handler(make_event("GET", "/api/org/dates",
                                        params={"site": OTHER_SITE_ID}), None)
    assert res["statusCode"] == 403
    assert called == []                                 # never reached the date query


def test_dates_with_accessible_site_scopes_to_it(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID, OTHER_SITE_ID})
    seen = {}
    wired.setattr(org.topics, "list_report_dates",
                  lambda conn, site_ids, since: (seen.update(site_ids=list(site_ids)) or []))
    res = org.lambda_handler(make_event("GET", "/api/org/dates", params={"site": SITE_ID}), None)
    assert res["statusCode"] == 200
    assert seen["site_ids"] == [SITE_ID]                # scoped to the one accessible ?site
```

- [ ] **Step 8: Run the handler tests — verify they fail**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -k dates -v`
Expected: FAIL — 404 `not found` from `dispatch` (route + handler not implemented).

- [ ] **Step 9: Implement the handler + route**

In `src/lambda_org_api.py`:

(a) Add the route in `dispatch`, next to `/live-items` (after line 212's block):
```python
    if route == "/dates" and method == "GET":
        return get_org_dates(conn, caller, event)
```

(b) Add the handler beside `list_live_items` (after line 832). Reuses `_allowed_site_ids` (str set, company/membership scoped) and `_resolve_site_param` (the SAME UUID-or-slug ACL guard `/programme` uses — a `?site` outside the accessible set returns its 403, which is the leak fix):
```python
def _dates_window_start(months) -> "datetime.date":
    """First day of the dots window, in NZ (BUG-37/BUG-19: never derive a
    'today' date from a bare UTC now). months defaults to 2 and is clamped
    to 1..24 so a hostile ?months can't force a full-table scan."""
    try:
        m = int(months)
    except (TypeError, ValueError):
        m = 2
    m = max(1, min(m, 24))
    now_nz = datetime.now(timezone.utc) + timedelta(hours=13)
    return (now_nz - timedelta(days=m * 30)).date()


def get_org_dates(conn, caller, event):
    """Membership-scoped report-date index for the Timeline dots — the Aurora
    replacement for legacy /api/dates (get_dates), whose missing ?site check
    leaked cross-user/cross-company report-dates (visibility spec §1.1). ACL
    mirrors /live-items and /programme EXACTLY: admin/gm see every company
    site, everyone else only their memberships; an explicit ?site outside
    that set is 403'd here (via _resolve_site_param) before any read."""
    p = event.get("queryStringParameters") or {}
    since = _dates_window_start(p.get("months"))
    site_param = p.get("site")
    if site_param:
        site_id, err = _resolve_site_param(conn, caller, site_param)
        if err is not None:
            return err                                  # 403 (out of scope) / 404 (unknown slug)
        site_ids = [site_id]
    else:
        site_ids = list(_allowed_site_ids(conn, caller))
    rows = topics.list_report_dates(conn, site_ids, since)
    return ok({"dates": {str(d): {"hasReport": True} for d in rows}})
```

(c) Add `GET /api/org/dates?months=&site=` to the route docstring block (lines 27–35), one line matching the existing style.

- [ ] **Step 10: Run the handler tests — verify pass, then full unit suite**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -k dates -v` → 4 passed.
Then: `python -m pytest tests/unit -q` → green (no regressions).

- [ ] **Step 11: Commit**

```bash
git add src/repositories/topics.py tests/unit/test_topics_repo.py tests/integration/test_topics_repository.py src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): GET /api/org/dates — membership-scoped report-dates; kills legacy dots leak (Phase 2)"
```

**Done:** Aurora computes the dots from `topics` scoped to the caller's accessible sites; an out-of-scope `?site` is 403'd, not silently lake-scanned.

---

### Task 2: Aurora `GET /api/org/sites/{id}/members` — site members from `memberships` (fixes USERS ON SITE empty)

**Why:** The panel reads legacy `/site-users` (`get_site_users`, `src/lambda_fieldsight_api.py:1041`), whose members come from `get_accessible_users` → `load_user_mapping()` (S3 `config/user_mapping.json`, :153). A site created via org-api with no `user_mapping` entry returns `[]`. Read membership rows from Aurora instead.

**Files:**
- Modify: `src/repositories/memberships.py`, `tests/integration/test_memberships_acl.py`
- Modify: `src/lambda_org_api.py`, `tests/unit/test_lambda_org_api.py`

**Interfaces:**
- Produces (repo): `memberships.members_for_site(conn, company_id, site_id) -> list[dict]` — user display columns + membership role for one site, joined `memberships→users→sites`, filtered `s.company_id = u.company_id = company_id` and `archived_at IS NULL` on both. Mirrors `list_company_memberships` (`src/repositories/memberships.py:44`) narrowed to one site.
- Produces (handler): `GET /api/org/sites/{id}/members` → `{"members": [...], "site": "<id>"}`.
- Consumes: existing `_allowed_site_ids`.

- [ ] **Step 1: Implement `members_for_site` (repo)**

In `src/repositories/memberships.py`, add to `__all__` and append. Multi-tenant: BOTH `s.company_id` and `u.company_id` are pinned to the caller's company, so even a bug in the handler ACL can't cross tenants; `%s::uuid` matches the DB uuid column against the URL-supplied str id:

```python
def members_for_site(conn, company_id, site_id) -> list[dict]:
    """Members of ONE site (memberships-backed), for org-api GET
    /api/org/sites/{id}/members -- the Aurora replacement for legacy
    /site-users, which read config/user_mapping.json and so returned []
    for Aurora-only sites (visibility spec §1.1 'USERS ON SITE empty').
    Company-pinned on BOTH sides of the join (multi-tenant invariant) and
    excludes archived members/memberships. Returns each user's display
    columns plus the per-site membership role (site_role)."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT u.id, u.cognito_sub, u.first_name, u.last_name, u.folder_name, "
        "u.avatar_s3_key, u.global_role, m.role AS site_role "
        "FROM memberships m "
        "JOIN users u ON u.id = m.user_id "
        "JOIN sites s ON s.id = m.site_id "
        "WHERE m.site_id = %s::uuid AND s.company_id = %s AND u.company_id = %s "
        "AND m.archived_at IS NULL AND u.archived_at IS NULL "
        "ORDER BY u.first_name, u.last_name",
        (site_id, company_id, company_id),
    ).fetchall()
```

Add `members_for_site` to the `__all__` list at the top of the file.

- [ ] **Step 2: Write + run the failing repo integration test**

Append to `tests/integration/test_memberships_acl.py` (real `db` fixture; reuse its existing company/user/site/membership seed helpers — do not duplicate if present):

```python
@pytest.mark.integration
def test_members_for_site_returns_company_members_excludes_cross_company_and_archived(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('A') RETURNING id").fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id", (cid,)).fetchone()[0]

    def user(sub, fn):
        return db.execute("INSERT INTO users (cognito_sub, company_id, email, first_name, last_name, global_role) "
                          "VALUES (%s,%s,%s,%s,'X','worker') RETURNING id",
                          (sub, cid, sub + "@x.nz", fn)).fetchone()[0]
    u1, u2 = user("s-a", "Ada"), user("s-b", "Bea")
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (u1, sid))
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'site_manager')", (u2, sid))

    # cross-company site+member must not appear
    cidB = db.execute("INSERT INTO companies (name) VALUES ('B') RETURNING id").fetchone()[0]
    sidB = db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'SB') RETURNING id", (cidB,)).fetchone()[0]
    uB = db.execute("INSERT INTO users (cognito_sub, company_id, email, first_name, last_name, global_role) "
                    "VALUES ('s-c',%s,'c@x.nz','Cy','X','worker') RETURNING id", (cidB,)).fetchone()[0]
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s,%s,'worker')", (uB, sidB))
    # archived membership must not appear
    db.execute("UPDATE memberships SET archived_at = now() WHERE user_id=%s AND site_id=%s", (u2, sid))

    rows = memberships.members_for_site(db, cid, str(sid))
    names = [r["first_name"] for r in rows]
    assert names == ["Ada"]                              # Bea archived; Cy cross-company; both excluded
    assert rows[0]["site_role"] == "worker"

    # cross-company caller company must never see this site's members
    assert memberships.members_for_site(db, cidB, str(sid)) == []
```

Run (CI): `python -m pytest tests/integration/test_memberships_acl.py -k members_for_site -v`.

- [ ] **Step 3: Write the failing handler unit tests**

Append to `tests/unit/test_lambda_org_api.py`:

```python
def test_site_members_returns_members_for_accessible_site(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    seen = {}
    wired.setattr(org.memberships, "members_for_site",
                  lambda conn, cid, sid: (seen.update(cid=cid, sid=sid)
                                          or [{"id": "u-1", "first_name": "Ada", "site_role": "worker"}]))
    res = org.lambda_handler(make_event("GET", "/api/org/sites/" + SITE_ID + "/members"), None)
    assert res["statusCode"] == 200
    assert seen == {"cid": "c-uuid-1", "sid": SITE_ID}   # company from caller, site from the URL
    body = body_of(res)
    assert body["site"] == SITE_ID
    assert body["members"][0]["first_name"] == "Ada"


def test_site_members_denies_site_outside_accessible_set_403(wired):
    wired.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    called = []
    wired.setattr(org.memberships, "members_for_site", lambda *a, **k: called.append(1) or [])
    res = org.lambda_handler(make_event("GET", "/api/org/sites/" + OTHER_SITE_ID + "/members"), None)
    assert res["statusCode"] == 403
    assert called == []                                  # ACL rejects before the members read
```

- [ ] **Step 4: Run the handler tests — verify they fail**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -k site_members -v` → FAIL (404 not found — route absent).

- [ ] **Step 5: Implement the handler + route**

In `src/lambda_org_api.py`:

(a) Add the route in `dispatch`, in the `/sites/...` group (after the `m_sa` archive matcher, ~line 188). It's GET so it can't collide with the `^/sites/([^/]+)$` PATCH or the archive POST:
```python
    m_sm = re.match(r"^/sites/([^/]+)/members$", route)
    if m_sm and method == "GET":
        return list_site_members(conn, caller, m_sm.group(1))
```

(b) Add the handler near `list_org_sites` (after `patch_org_site`). ACL via `_allowed_site_ids` (str set) — a site outside the caller's company/membership scope is 403'd before the read; `members_for_site`'s own company pin is defence-in-depth:
```python
def list_site_members(conn, caller, site_id):
    """Members of one site, from memberships (NOT user_mapping) -- the Aurora
    replacement for legacy /site-users. ACL mirrors /live-items: the site id
    must be in the caller's accessible set (admin/gm -> company sites,
    else memberships), which also blocks cross-company and archived sites."""
    if str(site_id) not in _allowed_site_ids(conn, caller):
        return error("access denied to this site", 403)
    rows = memberships.members_for_site(conn, caller["company_id"], site_id)
    return ok({"members": rows, "site": str(site_id)})
```

(c) Add `GET /api/org/sites/{id}/members` to the route docstring block (lines 20–22, next to the other `/sites/{id}` routes).

- [ ] **Step 6: Run the handler tests — verify pass, then full unit suite**

Run: `python -m pytest tests/unit/test_lambda_org_api.py -k site_members -v` → 2 passed.
Then: `python -m pytest tests/unit -q` → green.

- [ ] **Step 7: Commit**

```bash
git add src/repositories/memberships.py tests/integration/test_memberships_acl.py src/lambda_org_api.py tests/unit/test_lambda_org_api.py
git commit -m "feat(org-api): GET /api/org/sites/{id}/members from memberships; fixes USERS ON SITE empty for Aurora-only sites (Phase 2)"
```

**Done:** Site members come from Aurora `memberships`, so Aurora-only sites list their people; company + membership guarded.

---

### Task 3: UI — repoint `getSiteUsers` to Aurora + Timeline default-site from `org.getOrgSites()`

**Files:**
- Modify: `fieldsight-ui/scripts/api/org.js` (add `getSiteMembers`)
- Modify: `fieldsight-ui/scripts/api/sites.js` (`getSiteUsers` delegates to Aurora when org live)
- Modify: `fieldsight-ui/scripts/pages/timeline.js` (default-site source)
- Modify: preview HTML cache-busters

**Interfaces:**
- `window.FS.api.org.getSiteMembers(siteId) -> Promise<{users, site}>` — Aurora `/sites/{id}/members` mapped through the existing `_toPageMember` adapter (`first_name`+`last_name`→`name`, `global_role`→`role`, `folderName()`→`folder_name`), so the Sites panel and the getSiteUsers consumers (`compliance-aggregator`, `user-activity-aggregator`, `tasks-aggregator`, `today-adapter`) receive the field names they already expect.
- `getSiteUsers` return shape unchanged (`{users, site}`); only its source changes when org is live.

- [ ] **Step 1: Add `getSiteMembers` to `org.js`**

In `fieldsight-ui/scripts/api/org.js`, add beside `getLiveItems` (after line 267) and export it in the `window.FS.api.org = {...}` object (line 301). Uses `orgLive()`/`_toPageMember` already in this file; mock keeps the "nothing until seeded" posture of `getLiveItems`:

```javascript
  // -------- site members (Phase 2 — Aurora replaces legacy /site-users) --------
  /* GET /api/org/sites/{id}/members → { members:[...] } (from Aurora
     memberships, company+site ACL). Mapped to the page member shape via
     _toPageMember so getSiteUsers consumers get folder_name/name/role.
     Mock returns no members (matches getLiveItems' seed-first posture). */
  async function getSiteMembers(siteId) {
    if (orgLive()) {
      var res = await api.orgRequest('/sites/' + encodeURIComponent(siteId) + '/members');
      if (res && (res._accessDenied || res._notFound)) return res;
      return { users: (res.members || []).map(_toPageMember), site: siteId };
    }
    await api.delay();
    return { users: [], site: siteId };
  }
```

Add `getSiteMembers: getSiteMembers,` to the exports object (next to `getLiveItems`).

- [ ] **Step 2: Repoint `getSiteUsers` in `sites.js`**

In `fieldsight-ui/scripts/api/sites.js`, replace the body of `getSiteUsers` (line 28–36). When the org backend is live, read from Aurora; keep the legacy `/site-users` behind the `orgBaseUrl` kill switch AND the D5 `legacyReadFallback` flag (only used on an Aurora ACL-divergence, mirroring timeline.js):

```javascript
  async function getSiteUsers(site) {
    /* Phase 2 (Aurora read consolidation): the org backend knows Aurora-only
       sites that legacy /site-users (user_mapping-based) does not — that gap
       was the "USERS ON SITE empty" bug. When org is live, read members from
       Aurora; only fall back to legacy on an ACL divergence AND when the D5
       legacyReadFallback flag is still on (so the legacy read path can be
       retired by flipping the flag). */
    if (!window.FS.api.useMocks && window.FS.api.orgBaseUrl && window.FS.api.org) {
      var res = await window.FS.api.org.getSiteMembers(site);
      if (res && res._accessDenied && window.FS.api.legacyReadFallback) {
        return window.FS.api.request('/site-users', { params: { site: site } });
      }
      return res;
    }
    if (!window.FS.api.useMocks) return window.FS.api.request('/site-users', { params: { site: site } });
    await window.FS.api.delay();
    var f = fixtures().sites || { users: [] };
    var users = f.users.filter(function (u) {
      return (u.sites || []).indexOf(site) !== -1;
    });
    return { users: users, site: site };
  }
```

(`legacyReadFallback` is added in Task 4 Step 1; if Task 4 ships first this reads a defined flag, if this ships first the `&&` short-circuits safely on `undefined` → no fallback, which is the fail-closed-to-Aurora default.)

- [ ] **Step 3: Repoint the Timeline default-site source to Aurora**

In `fieldsight-ui/scripts/pages/timeline.js`, the one-shot sites fetch (line 654) currently reads the legacy report `/sites` (`window.FS.api.sites.getSites()`), which is `user_mapping`/company-global and doesn't reflect Aurora memberships. Point it at Aurora's accessible-sites list so the single-site auto-anchor and the "first accessible site" default (line 673–674) come from `GET /api/org/sites`, never the legacy global mapping list:

```javascript
      window.FS.api.org.getOrgSites()
        .then(function (res) {
          if (cancelled) return;
          setSitesList((res && res.sites) || []);
        })
```

`org.getOrgSites()` returns `{sites:[{site_id,...}]}` (via `_toPageSite`), the SAME shape the page reads at lines 674/682/276, so no downstream change. The existing stale-anchor guard (line 681–689) and the single-site auto-anchor (`sitesList.length === 1 ? sitesList[0].site_id : null`) now operate over the caller's Aurora-accessible sites.

- [ ] **Step 4: Syntax-check + grep verification**

```bash
node --check fieldsight-ui/scripts/api/org.js
node --check fieldsight-ui/scripts/api/sites.js
node --check fieldsight-ui/scripts/pages/timeline.js
grep -n "getSiteMembers" fieldsight-ui/scripts/api/org.js          # defined + exported
grep -n "org.getOrgSites" fieldsight-ui/scripts/pages/timeline.js  # timeline now sources Aurora
grep -n "sites.getSites()" fieldsight-ui/scripts/pages/timeline.js # EXPECT: no hit (repointed)
```

- [ ] **Step 5: Bump cache-busters**

Bump `?v=N` for `api/org.js`, `api/sites.js`, `pages/timeline.js` in `app-shell-preview.html` (and any other preview HTML that loads them). Confirm script load order still has `api/index.js` → `_fetch.js` → `api/org.js` → `api/sites.js` before the pages.

- [ ] **Step 6: Manual verification (state done vs deferred)**

With `useMocks=0`, `orgBaseUrl` set, `timelineSource=aurora`: open a site with Aurora-only members → USERS ON SITE lists them (was empty). Open Timeline as a member of exactly one site → it auto-anchors to that (Aurora) site. Real-browser check may be deferred to the user; state which.

- [ ] **Step 7: Commit**

```bash
git add fieldsight-ui/scripts/api/org.js fieldsight-ui/scripts/api/sites.js fieldsight-ui/scripts/pages/timeline.js fieldsight-ui/app-shell-preview.html
git commit -m "feat(ui): site users + timeline default-site read from Aurora org backend (Phase 2)"
```

**Done:** USERS ON SITE reads Aurora members; Timeline's default/auto-anchor site comes from `GET /api/org/sites`, not the legacy global mapping.

---

### Task 4: UI — dates source → Aurora when `timelineSource==='aurora'`; flag-gate the legacy `/timeline` + `/dates` fallback (D5)

**Files:**
- Modify: `fieldsight-ui/scripts/api/index.js` (add `legacyReadFallback`)
- Modify: `fieldsight-ui/scripts/api/dates.js` (Aurora `/dates` + flag-gated fallback)
- Modify: `fieldsight-ui/scripts/api/timeline.js` (flag-gate the `_accessDenied` legacy fallback)
- Modify: preview HTML cache-busters

**Interfaces:**
- `window.FS.api.legacyReadFallback: boolean` — D5 flag, env-driven, default **true** during rollout. When flipped to **false**, the legacy report-gateway read paths (`/timeline`, `/dates`, `/site-users` fallback) are retired: Aurora is authoritative even on `_accessDenied`.
- `dates.js` `/dates` request/response shape unchanged (`{months,site}` → `{dates:{...}}`); only the base (org vs report) and the fallback gate change.

- [ ] **Step 1: Add the `legacyReadFallback` flag**

In `fieldsight-ui/scripts/api/index.js`, add to the `window.FS.api = {...}` object (after `orgWrites`, line 91), matching the existing env-flag pattern:

```javascript
    /* D5 (visibility spec) — legacy report-gateway read fallback (/timeline,
       /dates, /site-users). Default ON during Phase 2 rollout; flip to false
       (env.legacyReadFallback = false) to retire the legacy read paths once
       Aurora reads are trusted — Aurora then stays authoritative even on an
       _accessDenied divergence. */
    legacyReadFallback: env.legacyReadFallback !== false,
```

- [ ] **Step 2: Route dates through Aurora + flag-gate the fallback**

In `fieldsight-ui/scripts/api/dates.js`, replace `fetchDates` (line 14–29) and add a `datesSource` gate mirroring timeline.js's `timelineSource` (line 29–32):

```javascript
  /* Aurora dots only when the item store is the timeline source AND the org
     gateway is live (same kill switch as timeline.js). */
  function datesSource() {
    var api = window.FS.api;
    return (api.timelineSource === 'aurora' && api.orgBaseUrl) ? 'aurora' : 'report';
  }

  async function fetchDates(opts) {
    if (!window.FS.api.useMocks) {
      var params = { months: opts.months, site: opts.site, user: opts.user };
      if (datesSource() === 'aurora') {
        try {
          /* org /api/org/dates is membership-scoped and rejects an
             out-of-scope ?site (kills the legacy dots leak). No ?user: the
             dots are per accessible-site, not per user folder. */
          var r = await window.FS.api.orgRequest('/dates', {
            params: { months: opts.months, site: opts.site },
          });
          if (r && !r._accessDenied) return r;
        } catch (e) { /* org transport failure → flag-gated report fallback */ }
        if (!window.FS.api.legacyReadFallback) return { dates: {} };  // D5: legacy retired
      }
      return window.FS.api.request('/dates', { params: params });      // legacy read path
    }
    await window.FS.api.delay();
    var f = fixtures().dates || { dates: {} };
    return { dates: f.dates, months: opts.months || 3, site: opts.site || null };
  }
```

Also add the source to the cache key so Aurora↔report don't collide (line 39), mirroring timeline.js line 88:

```javascript
    var key = 'dt:' + datesSource() + ':' + (opts.months || '') + ':' + (opts.user || '') + ':' + (opts.site || '');
```

- [ ] **Step 3: Flag-gate the legacy `/timeline` fallback**

In `fieldsight-ui/scripts/api/timeline.js`, update the aurora branch of `fetchTimeline` (lines 41–51) so the legacy report fallback only fires while `legacyReadFallback` is on:

```javascript
      if (timelineSource() === 'aurora') {
        try {
          var r = await window.FS.api.orgRequest('/timeline', { params: params });
          /* _accessDenied → ACL divergence (shim stricter than prod for
             site_manager/pm, plan D10). _notFound is authoritative (the shim
             already fell back to S3 server-side). */
          if (r && !r._accessDenied) return r;
          if (!window.FS.api.legacyReadFallback) return r;  // D5: legacy retired → Aurora authoritative
        } catch (e) {
          if (!window.FS.api.legacyReadFallback) throw e;   // D5: no legacy transport fallback
        }
      }
      return window.FS.api.request('/timeline', { params: params });   // legacy read path (flag-gated above)
```

- [ ] **Step 4: Syntax-check + grep verification**

```bash
node --check fieldsight-ui/scripts/api/index.js
node --check fieldsight-ui/scripts/api/dates.js
node --check fieldsight-ui/scripts/api/timeline.js
grep -n "legacyReadFallback" fieldsight-ui/scripts/api/index.js fieldsight-ui/scripts/api/dates.js fieldsight-ui/scripts/api/timeline.js fieldsight-ui/scripts/api/sites.js
grep -n "orgRequest('/dates'" fieldsight-ui/scripts/api/dates.js   # dates now hits Aurora
```

- [ ] **Step 5: Bump cache-busters + manual verification**

Bump `?v=N` for `api/index.js`, `api/dates.js`, `api/timeline.js`. With `timelineSource=aurora`, `orgBaseUrl` set, `legacyReadFallback` still true: dots come from Aurora and match the per-site timeline; a `?site` the caller can't access shows no dots (Aurora 403, no fallback leak). Then set `legacyReadFallback=false` and confirm no request goes to the report `/timeline` or `/dates` (retirement path). Defer real-browser confirmation to the user if needed.

- [ ] **Step 6: Commit**

```bash
git add fieldsight-ui/scripts/api/index.js fieldsight-ui/scripts/api/dates.js fieldsight-ui/scripts/api/timeline.js fieldsight-ui/app-shell-preview.html
git commit -m "feat(ui): dots read from Aurora /api/org/dates; legacy timeline+dates fallback behind D5 flag (Phase 2)"
```

**Done:** Dots read from Aurora and honour membership scope; the legacy `/timeline`+`/dates`+`/site-users` fallback is retirable by flipping one flag.

---

### Task 5: PR, merge, deploy, live smoke (handoff — user-gated)

**Files:** none (process). Pipeline and UI are separate repos/PRs.

- [ ] **Step 1: Pipeline PR to `develop`** — `gh pr create --base develop` titled `feat(org-api): Phase 2 Aurora read consolidation — /api/org/dates + /sites/{id}/members`. Confirm CI `Tests` green (unit + the two new integration tests). No migration, no `template.yaml` change.
- [ ] **Step 2: Merge to `develop`** → `deploy.yml` deploys `fieldsight-test`. Data-API smoke: `GET /api/org/dates?months=1` and `GET /api/org/sites/{accessible-site-id}/members` with a real idToken return scoped results; a `?site=<not-accessible>` on `/dates` returns 403; an Aurora-only site's `/members` is non-empty.
- [ ] **Step 3: UI PR** on the FieldSight UI repo (its own branch), Amplify preview with `timelineSource=aurora`, `orgBaseUrl` set, `legacyReadFallback=true`: verify USERS ON SITE populated, Timeline default-site correct, dots from Aurora. Then a preview with `legacyReadFallback=false` to confirm the legacy read paths are cleanly retirable.
- [ ] **Step 4: Promote `develop`→`main` (prod)** is a SEPARATE user decision (carries whatever else is on develop). Surface it; don't bundle silently. After prod deploy, flip the prod UI env `timelineSource=aurora` (already set per the authority-flip cutover) and confirm the two fixes on prod; `legacyReadFallback=false` is the final retirement step, user-gated.

**Done:** Phase 2 live on test, verified; prod promotion + legacy-read retirement are the user's call.

---

## Self-Review (author)

- **Spec coverage:** §1.1 dots leak → Task 1 (`_resolve_site_param` 403 on out-of-scope `?site` + `list_report_dates` scoped to `_allowed_site_ids`; `test_dates_rejects_site_outside_accessible_set_403`, `test_dates_worker_scope_via_allowed_ids`). §1.1 USERS ON SITE empty → Task 2 (`members_for_site` from `memberships`, not `user_mapping`; `test_members_for_site_returns_company_members_excludes_cross_company_and_archived`, `test_site_members_returns_members_for_accessible_site`). §3.2 read-path unification → Tasks 3–4 repoint UI reads to Aurora. §3.0 multi-tenancy → both repo queries pin company on both join sides / reuse `_allowed_site_ids`; `test_...excludes_cross_company...` + `test_..._denies_site_outside_accessible_set_403`. §4 Rollout step 2 → Task 5. D5 flagged fallback → Task 4 `legacyReadFallback` gates every legacy read (`/timeline`, `/dates`, `/site-users`).
- **Uses existing scoping, not Phase 3:** every new read routes through `memberships.accessible_site_ids` / `_allowed_site_ids` / `_resolve_site_param` — the SAME helpers `/live-items` and `/programme` use today. No reference to `visible_scope` or any graded/Phase-3 construct.
- **Type consistency:** `list_report_dates(conn, site_ids, since_date) -> list[date]` identical across repo impl, unit test, integration test, and the handler call; handler stringifies dates with `str(d)` (works for both a real `datetime.date` and a str). `since_date` is a `datetime.date` (not str) so `report_date >= %s` type-matches the DATE column. `members_for_site(conn, company_id, site_id) -> list[dict]` identical across impl/handler/tests; `%s::uuid` / `%s::uuid[]` casts let str ids from `_allowed_site_ids`/URL match uuid columns (the same str-vs-uuid pitfall `_allowed_site_ids`' docstring calls out at `src/lambda_org_api.py:880`).
- **Routing safety:** `/dates` (GET) and `/sites/{id}/members` (GET) can't collide with existing routes — `^/sites/([^/]+)$` is PATCH-only and `$`-anchored (won't match `/members`), archive is POST. Both new routes ride the existing `/api/org/{proxy+}` integration → no `template.yaml` change.
- **Leak-fix proof:** the dots leak is a fall-through in legacy `get_dates` (`src/lambda_fieldsight_api.py:296` — empty `user_folders` at :331 → `else` at :340). The Aurora path has no such fall-through: no accessible sites → empty `site_ids` → `list_report_dates` returns `[]` (short-circuit, asserted); an out-of-scope `?site` → 403 before any query (asserted, `called == []`).
- **BUG-29 / no-local-Python:** all pipeline steps note CI as the pass gate; integration tests SKIP locally without `TEST_DATABASE_URL`. **UI no-build:** gates are `node --check` + grep + cache-buster bumps; no test harness invented. **Reversibility:** each task is independently shippable and reverts via `timelineSource`, `orgBaseUrl`, or `legacyReadFallback`. **Windows/CRLF:** single-line Edit anchors specified; commits stage named files only (never `git add -A`).
- **Placeholder scan:** none — every code step carries full code and exact test names; no TBD/"handle edge cases".
- **Doc references:** the spec (`docs/superpowers/specs/2026-07-17-visibility-permission-model-design.md`) and the Phase 1 plan are landed alongside this plan on `develop`; the §/D references (§1.1 leaks, §3.0 multi-tenancy, §3.2 read-path unification, §4 Rollout step 2, D5 flagged fallback) are current. Code findings/line numbers were read from the current `develop` tree.
