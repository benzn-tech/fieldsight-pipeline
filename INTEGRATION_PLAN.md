# INTEGRATION_PLAN.md — FieldSight UI Integration Runbook

> **Companion to:** ROADMAP.md (product priorities) and CLAUDE.md (technical reference + bug list).
> **Mirror:** `/home/user/fieldsight-ui/INTEGRATION_PLAN.md` is a synced copy — keep them identical.
> **Status:** Plan committed 2026-05-06. Execution gated on per-stage user approval.

---

## 1. Context

FieldSight has two repos that need to converge:

| Repo | Role | Branch |
|---|---|---|
| `benzn-tech/fieldsight-pipeline` | Python Lambda backend + legacy 808-line `frontend/index.html` | `develop` (target) |
| `benzn-tech/fieldsight-ui` | New HTML+CSS+browser-React UI (Babel in-browser, no build step) | `main` / `claude/sprint8` |

**Backend P0/P1/P2 features** are stacked on `claude/review-feature-content-hsaO3` but never merged to `develop`. **New UI** has 8 sprints + 12 pages already shipped, currently running on mock fixtures. The job is to merge the backend, deploy the new UI to a dev environment, wire it to the live API, validate, then cut over prod.

**Two work streams run in parallel** — backend (this repo) and UI integration (the UI repo, driven by a separate Claude Code instance). Cross-repo coordination happens via this document and `UI_BACKEND_REQUESTS.md`.

---

## 2. Decisions on record

| # | Decision | Choice | Why |
|---|---|---|---|
| 0 | Repo structure | Keep dual-repo | UI has 60+ commits of design history; squashing loses traceability. UI's `scripts/api/*.js` already abstracts backend via `useMocks` switch. |
| 1 | `actions` endpoint contract | Backend adds RESTful adapter (`PATCH /api/actions/{id}` + `POST /api/actions`) | Sprint 8.1.1 needs POST anyway; ~20 lines reusing existing toggle logic. Keeps UI contract clean. |
| 2 | Backend merge order | Merge stacked branches first, then start UI integration branch | hsaO3 ⊃ P2 ⊃ P1 ⊃ P0; one merge of hsaO3 into develop pulls everything. |
| 3 | Plan file location | New `INTEGRATION_PLAN.md` (this file) + ROADMAP.md gets a P3 section | Separation of runbook (executable) from roadmap (priorities). |
| 4 | Programme domain backend | Add to existing `fieldsight-pipeline` backend | Avoids spawning a new repo+stack for one domain; reuses Cognito + DynamoDB conventions. |
| 5 | Cross-repo plan sync | Two copies, user manually pushes UI-repo copy | This Claude session can only push to `fieldsight-pipeline`. UI-repo file contents are emitted for user to commit. |
| 6 | Stage A merge style | One PR for hsaO3 → develop (not per-sub-branch) | Branches already stacked; splitting would require cherry-pick rewrite. Single revert restores prior state. |

---

## 3. Existing planning files inventory

| File | Role | Updated by this plan? |
|---|---|---|
| `/home/user/fieldsight-pipeline/ROADMAP.md` | Product roadmap, P0–P3 | ✅ Adds **P3 — UI Integration** section |
| `/home/user/fieldsight-pipeline/CLAUDE.md` | Tech reference + BUG-01..34 ledger | ⏭️ Untouched; new bugs append as BUG-35+ |
| `/home/user/fieldsight-ui/PLAN.md` | UI single-source action ledger | ✅ User pushes Sprint 9 section (text emitted in §10) |
| `/home/user/fieldsight-ui/BACKEND-CONTEXT.md` | Contract UI team reads | ✅ User pushes 9 P1/P2 + 6 Programme endpoint specs (text emitted in §10) |
| `/home/user/fieldsight-ui/INTEGRATION_PLAN.md` | Mirror of this file | ✅ User pushes verbatim copy |

---

## 4. Branch topology (verified by exploration)

```
P0  ──►  P1  ──►  P2  ──►  claude/review-feature-content-hsaO3   (latest 2026-04-08)
                            │
                            └─ P2 + 7 commits: Environment param, QA/QC L2,
                                               CI workflow, test env setup

main    = e7e0f37 + (samconfig.toml + deploy.yml ×2)             ← deploy infra only
develop = e7e0f37                                                ← empty, no features
```

**Rule:** `merge --no-ff origin/claude/review-feature-content-hsaO3` brings in P0+P1+P2+QA/QC+Env+CI in one go because the branches are stacked. Splitting into per-sub-branch PRs requires un-stacking first; not worth the risk.

---

## 5. Stage A — Backend merge into `develop`

### A.1 Diagnostic preflight (read-only, reproducible)

Before any merge, confirm the topology hasn't drifted:

```bash
cd /home/user/fieldsight-pipeline
git fetch origin
git log --oneline origin/develop..origin/claude/review-feature-content-hsaO3 | wc -l  # expect ~30+
git log --oneline origin/develop..origin/main | wc -l                                  # expect 2-3 (deploy infra)
git log --oneline origin/claude/review-feature-content-hsaO3..origin/main              # expect 0 (main has no features)
```

If `origin/main` has feature commits not on hsaO3, escalate before merging.

### A.2 Merge

```bash
git checkout develop
git pull origin develop
git merge --no-ff origin/claude/review-feature-content-hsaO3 \
  -m "Merge P0+P1+P2+QA/QC L2+CI from hsaO3 review branch"
git merge --no-ff origin/main \
  -m "Merge deploy infrastructure (deploy.yml + samconfig.toml) from main"
git push origin develop
```

`develop` is now the integration trunk.

### A.3 Tag a rollback point

```bash
git tag pre-ui-integration-$(date +%Y%m%d) origin/develop
git push origin pre-ui-integration-$(date +%Y%m%d)
```

If anything goes sideways in stage B, `git reset --hard <tag>` restores the trunk.

### A.4 Feature branch cleanup (deferred)

**Do not delete** `feature/p0-*`, `feature/p1-*`, `feature/p2-*`, or `claude/review-feature-content-hsaO3` until stage B is green for ≥1 week. Once develop has been deployed to dev and validated, prune:

```bash
git push origin --delete feature/p0-ask-agent-search
git push origin --delete feature/p1-calendar-priority-onepager
git push origin --delete feature/p2-dashboard-digest-qaqc-realtime
# Keep hsaO3 indefinitely as the "review snapshot."
```

### A.5 RESTful actions adapter (decision #1)

**File:** `src/lambda_fieldsight_api.py` (post-merge contents from hsaO3).

Add two handlers and two route entries. Reuse `toggle_action()`'s DynamoDB mutation logic — do not duplicate it.

```python
def patch_action(action_id, body, caller):
    # action_id format: "{date}_{topic_id}_{action_index}"
    parts = action_id.split('_')
    if len(parts) < 3:
        return _resp(400, {"error": "invalid action id"})
    date, topic_id, idx = parts[0], parts[1], parts[2]
    done = bool(body.get('done', True))
    # Reuse the existing mutation path:
    return _toggle_action_internal(date, topic_id, int(idx), done, caller)

def create_action(body, caller):
    # body: {date, topic_id, action_text, owner?, due_date?}
    # Writes to AUDIT table with new action_index.
    ...
    return _resp(200, {"id": new_id, **action_dict})
```

Routes (insert before the legacy toggle handler so PATCH/POST take precedence):

```python
elif path.startswith('/api/actions/') and method == 'PATCH':
    action_id = path.split('/')[-1]
    return patch_action(action_id, body, caller)
elif path == '/api/actions' and method == 'POST':
    return create_action(body, caller)
```

**Keep** `POST /api/actions/toggle` for one sprint as a compatibility shim, then remove.

### A.6 Stage A verification

```bash
# 1. CloudFormation: ensure SAM build still succeeds
sam build --config-env test

# 2. Lambda smoke (against existing prod deployment, not test):
aws lambda invoke --function-name fieldsight-api \
  --payload '{"path":"/api/timeline","httpMethod":"GET","queryStringParameters":{"date":"2026-04-29"}}' \
  --cli-binary-format raw-in-base64-out /tmp/test.json
cat /tmp/test.json | head -50

# 3. Legacy frontend: load current prod CloudFront URL, log in, verify /today + /calendar.
```

---

## 6. Stage B — Dev environment minimum-viable connection

**Goal:** new UI runs against live (dev-tier) API, all 12 pages render (some may degrade), full Cognito login round-trip works.

> **2026-05-09 update:** B.0 diagnostics revealed the dev backend stack does not exist (no SAM stack ever deployed; naming conventions inconsistent across DynamoDB / Cognito / template). Stage B has been broken into 5 sub-steps with a full runbook in **[`docs/STAGE_B_EXECUTION.md`](docs/STAGE_B_EXECUTION.md)**. The §6 below is the original plan; the runbook supersedes it for execution.

### B.0 Environment diagnostic (user runs these)

This Claude session has **no AWS CLI access**. The user must run these read-only commands and share the output before stage B can proceed:

```bash
# 1. CloudFormation stacks
aws cloudformation list-stacks --region ap-southeast-2 \
  --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName,'fieldsight')].{Name:StackName,Status:StackStatus,Updated:LastUpdatedTime}"

# 2. Lambda functions (test prefix)
aws lambda list-functions --region ap-southeast-2 \
  --query "Functions[?starts_with(FunctionName,'test-fieldsight') || contains(FunctionName,'fieldsight-test')].FunctionName"

# 3. S3 buckets
aws s3 ls | grep -iE "fieldsight|sitesync"

# 4. CloudFront distributions
aws cloudfront list-distributions --query \
  "DistributionList.Items[].{Id:Id,Domain:DomainName,Aliases:Aliases.Items,Origin:Origins.Items[0].DomainName}"

# 5. Cognito user pools
aws cognito-idp list-user-pools --max-results 60 --region ap-southeast-2

# 6. GitHub secrets (web UI only — gh CLI may not be configured)
# Visit: https://github.com/benzn-tech/fieldsight-pipeline/settings/secrets/actions
# Confirm: TEST_CLOUDFRONT_ID, TEST_FRONTEND_BUCKET, PROD_CLOUDFRONT_ID, PROD_FRONTEND_BUCKET, AWS_OIDC_ROLE_ARN
```

Outputs steer the path:

| Result | Path | Next step |
|---|---|---|
| `fieldsight-test` stack exists, test- Lambdas present, test S3+CF set up | **A** (already deployed) | Skip to B.4 |
| Stack exists but no test S3+CF, or secrets missing | **B** (partial) | Bootstrap missing pieces, then B.2 |
| Nothing exists | **C** (greenfield) | `sam deploy --config-env test` first, then B.2 |

### B.1 Working branch (this session has done it)

```bash
cd /home/user/fieldsight-pipeline
git checkout -b claude/review-develop-branch-5gYGo  # already on this branch
```

(Per the task brief, all artifacts in this session land here. The original plan named it `claude/integrate-new-ui`; the actual instruction overrides that.)

### B.2 Cross-repo deploy script

`scripts/deploy_ui_from_external_repo.sh` ships in this PR. See §11 for usage.

### B.3 GitHub Actions wiring (deferred)

Initial cut: deploy manually via the script in B.2. Only once the manual flow is green should we automate by:
1. Adding a `ui-repo-ref` `workflow_dispatch` input to `deploy.yml`.
2. On push to `develop`, clone `fieldsight-ui` at `claude/sprint8` and sync to test bucket.
3. On push to `main`, clone at `main` and sync to prod bucket.

### B.4 UI shell baseUrl + Cognito (already wired — verify only)

`/home/user/fieldsight-ui/app-shell-preview.html:215-223` **already** reads `?baseUrl=` and `?mocks=0` from the URL. No code change needed in the UI shell. Verify-only checklist:

- [ ] `scripts/auth/cognito.js` user pool ID matches `ap-southeast-2_ps7XIQGHB`
- [ ] Same file's app client ID matches `5npb81jbj1hgh9tsck25kan3os`
- [ ] If a separate dev pool exists (per B.0 output #5), parameterize via `?cognitoPool=...&cognitoClient=...` query params (small follow-up — UI work)

### B.5 Local validation

```bash
cd /home/user/fieldsight-ui
python3 -m http.server 8765
# Browser:
# http://localhost:8765/app-shell-preview.html?mocks=0&baseUrl=https://<dev-api-gw>.execute-api.ap-southeast-2.amazonaws.com/prod
```

Log in with an existing Cognito user. Expected: `/today` populates from `/api/timeline`, calendar dots from `/api/dates`.

### B.6 Deploy to dev S3 + CloudFront

```bash
bash scripts/deploy_ui_from_external_repo.sh \
  --env test \
  --ui-repo-path /home/user/fieldsight-ui
```

Browse the test CloudFront URL and walk all 12 pages.

### Stage B sign-off checklist

- [ ] Legacy `frontend/index.html` still serves on prod CloudFront (untouched)
- [ ] Dev CloudFront serves new UI; Cognito login succeeds
- [ ] `/today` calls `/api/timeline` and shows real data (not mock)
- [ ] `/api/dates` populates calendar dots
- [ ] `/api/sites` populates site selector
- [ ] `/programme`, `/safety`, `/quality`, `/team` degrade gracefully (offline banner / empty state) — no white screens, no uncaught fetch errors
- [ ] Browser console shows no uncaught exceptions across the 12-page tour

---

## 7. Stage C — Wire 9 P1/P2 endpoints (UI work, ~2–3 sprints)

Sprint suggestion (drives `UI_BACKEND_REQUESTS.md` and the UI-repo PLAN.md Sprint 9 section):

| Sprint | Endpoints | Effort |
|---|---|---|
| **9.1** (small, ~1 week) | `GET /api/calendar-events`, `GET /api/onepager`, `GET/POST /api/topics/priority` | UI-only, schema already known |
| **9.2** (medium, ~1–2 weeks) | `POST /api/reports/correction` + `GET /api/corrections`, `POST /api/analytics/events` | UI loses read-only assumption; pair with PLAN.md Q-2 |
| **9.3** (medium, ~1–2 weeks) | `GET /api/dashboard`, `GET /api/search`, `POST /api/ask` (global scope) | Pair with PLAN.md Q-4 (cross-day Ask) |
| **deferred** | `GET/POST /api/digest` | Backend Lambda not yet built |

### Conventions handed to the UI Claude Code instance

1. **One commit per endpoint**: `feat(api): wire /api/<name> — Sprint 9.x`.
2. **One module per endpoint** in `scripts/api/`; do not edit `index.js`.
3. **Mock fixture first**: every new endpoint must work in `useMocks=true` before flipping to live; both modes must remain green.
4. **Schema first**: update `BACKEND-CONTEXT.md` before writing client code.
5. **No backend edits from the UI repo.** Backend asks go in `UI_BACKEND_REQUESTS.md`, synced weekly to this repo.
6. **One PR per Sprint sub-bullet** (9.1, 9.2, 9.3 are separate PRs).

---

## 8. Stage D — Programme domain backend (decision #4)

Programme is the largest gap: 6 endpoints the UI already calls, with no backend. This stage adds them to `fieldsight-pipeline`.

### D.1 Data model

DynamoDB table `fieldsight-programmes` (or environment-prefixed `test-fieldsight-programmes`):

```
PK = PROGRAMME#{programme_id}    SK = META          # programme metadata
PK = PROGRAMME#{programme_id}    SK = TASK#{task_id} # task row (owner, depends_on, dates, float, baseline)
```

GSI `byOwner` on `assignees[0]` for owner-scoped queries.

### D.2 Routes in `lambda_fieldsight_api.py`

| Route | Handler | RBAC |
|---|---|---|
| `GET /api/programmes/{id}` | `get_programme(id, caller)` | site_manager+ read |
| `GET /api/programmes/{id}/tasks?from=&to=` | `get_programme_tasks(id, params, caller)` | site_manager+ read |
| `POST /api/programmes/{id}/tasks` | `create_programme_task(id, body, caller)` | pm+ write |
| `PATCH /api/programmes/{id}/tasks/{task_id}` | `update_programme_task(...)` | pm+ write |
| `DELETE /api/programmes/{id}/tasks/{task_id}` | `delete_programme_task(...)` | pm+ write |
| `POST /api/programmes/{id}/tasks/bulk` | `import_programme_tasks(id, body, caller)` | pm+ write |

### D.3 File parsing

UI's `programme-import-modal.js` parses XLSX/CSV/XML in-browser via SheetJS, then POSTs JSON `tasks[]`. Backend **does not** parse spreadsheets — only validates schema and writes DynamoDB.

### D.4 Schema source of truth

`/home/user/fieldsight-ui/scripts/fixtures/programme.fixture.js` defines the contract. Required fields: `task_id`, `parent_id`, `name`, `start`, `end`, `duration_days`, `assignees`, `depends_on`, `is_group`, `progress_pct`, `baseline_start`, `baseline_end`, `float_days`.

### D.5 Estimate

1.5–2 sprints: routes (1 week) + DynamoDB table + IAM (½ day) + RBAC plumbing (1 day) + integration tests (3 days).

---

## 9. Stage E — Other UI-only domains

| Domain | UI status | Backend gap | Recommendation |
|---|---|---|---|
| Meetings list (`GET /api/meetings`) | UI calls it | Today UI uses S3 presigner — no list API | Small Lambda wrapping `s3:ListObjectsV2` on `meeting_minutes/` prefix — ~½ day |
| Safety / Quality writes | UI has forms | No write Lambda yet | New DynamoDB table + 2 POST routes — ~3 days |
| Aggregators (`/api/today/summary`, etc.) | UI fans out client-side | None | No backend work; UI already aggregates `/api/timeline` + `/api/dates` |

---

## 10. Cross-repo file mirror (user pushes these)

This Claude session writes only to `fieldsight-pipeline`. The UI repo needs the following files; each is emitted in §11–§13 as paste-ready text. After this session ends, the user should `cd /home/user/fieldsight-ui && <paste each file> && git commit -m "docs: integration plan + Sprint 9 + endpoint contracts" && git push`.

| Target file | Purpose |
|---|---|
| `/home/user/fieldsight-ui/INTEGRATION_PLAN.md` | Verbatim copy of this file |
| `/home/user/fieldsight-ui/UI_BACKEND_REQUESTS.md` | Append-only log of UI's asks of backend (synced weekly) |
| `/home/user/fieldsight-ui/PLAN.md` | Append Sprint 9 section (do not overwrite) |
| `/home/user/fieldsight-ui/BACKEND-CONTEXT.md` | Append §4.13–§4.20 with the 9 P1/P2 endpoint schemas + 6 Programme schemas |

---

## 11. Verification matrix

| What | When | How |
|---|---|---|
| Stage A merge clean | After `git push origin develop` | `git log --oneline -10`, then `sam build --config-env test` |
| Actions adapter working | After A.5 + redeploy | `curl -X PATCH .../api/actions/2026-04-29_0_2 -d '{"done":true}'` |
| Dev API live | After B.0 path C (or never if path A) | `curl https://<dev-api>/api/timeline?date=2026-04-29 -H 'Authorization: Bearer ...'` |
| Dev UI live | After B.6 | Browse dev CloudFront, log in, walk 12 pages |
| Sprint 9.x endpoint wired | Per sub-PR | Both `useMocks=true` and `useMocks=false` paths exercised |

---

## 12. Rollback strategy

| Scope | Trigger | Action |
|---|---|---|
| Stage A merge breaks Lambdas | Sam deploy fails or `/api/timeline` 500s | `git revert -m 1 <merge-commit>` then push; tag `pre-ui-integration-*` is the absolute fallback |
| Dev UI shows white screen | Smoke fails after B.6 | `aws s3 sync` previous commit's snapshot back; CloudFront invalidate |
| Prod cutover regression | After cutover | DNS / CloudFront origin revert to legacy bucket; legacy `frontend/index.html` is still in the legacy origin |

---

## 13. Pointers to the artifacts emitted this session

1. **This file** — `/home/user/fieldsight-pipeline/INTEGRATION_PLAN.md`
2. **Deploy script** — `/home/user/fieldsight-pipeline/scripts/deploy_ui_from_external_repo.sh`
3. **ROADMAP P3 section** — appended to `/home/user/fieldsight-pipeline/ROADMAP.md`
4. **UI-repo file contents** — emitted in chat as paste-ready text after commit; user pushes to `fieldsight-ui`

Stage A merge, Stage A.5 Lambda code change, Stage D Programme backend — **not yet executed**. Each requires explicit go-ahead from the user in a follow-up session.
