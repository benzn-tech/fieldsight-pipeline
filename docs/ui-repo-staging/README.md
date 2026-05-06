# UI repo staging — paste-ready files for `benzn-tech/fieldsight-ui`

These files are written to the **pipeline** repo for convenience. They belong in the **UI repo** and must be committed there manually.

## How to apply

```bash
# 1. INTEGRATION_PLAN.md — verbatim copy (overwrite if exists)
cp /home/user/fieldsight-pipeline/docs/ui-repo-staging/INTEGRATION_PLAN.md \
   /home/user/fieldsight-ui/INTEGRATION_PLAN.md

# 2. UI_BACKEND_REQUESTS.md — new file (do NOT overwrite if it already has entries)
test -f /home/user/fieldsight-ui/UI_BACKEND_REQUESTS.md \
  && echo "⚠️ UI_BACKEND_REQUESTS.md already exists — diff before overwriting" \
  || cp /home/user/fieldsight-pipeline/docs/ui-repo-staging/UI_BACKEND_REQUESTS.md \
        /home/user/fieldsight-ui/UI_BACKEND_REQUESTS.md

# 3. PLAN.md — APPEND Sprint 9 section (do not overwrite)
cat /home/user/fieldsight-pipeline/docs/ui-repo-staging/PLAN.append-sprint9.md \
  >> /home/user/fieldsight-ui/PLAN.md

# 4. BACKEND-CONTEXT.md — APPEND §4.13–§4.21 (do not overwrite)
cat /home/user/fieldsight-pipeline/docs/ui-repo-staging/BACKEND-CONTEXT.append-sec4.13-21.md \
  >> /home/user/fieldsight-ui/BACKEND-CONTEXT.md

# 5. Commit + push to UI repo
cd /home/user/fieldsight-ui
git checkout -b claude/integration-plan-mirror-2026-05-06
git add INTEGRATION_PLAN.md UI_BACKEND_REQUESTS.md PLAN.md BACKEND-CONTEXT.md
git commit -m "docs: integration plan + Sprint 9 + endpoint contracts (mirror from pipeline repo)"
git push -u origin claude/integration-plan-mirror-2026-05-06
```

## What each file is

| File | Disposition |
|---|---|
| `INTEGRATION_PLAN.md` | **Verbatim mirror** of the same file in the pipeline repo. Overwrite. |
| `UI_BACKEND_REQUESTS.md` | **New file**. If a copy already exists in the UI repo with real entries, merge by hand. |
| `PLAN.append-sprint9.md` | **Append-only** section. Do `cat ... >> PLAN.md`, never overwrite. |
| `BACKEND-CONTEXT.append-sec4.13-21.md` | **Append-only** sections §4.13 through §4.21. Do `cat ... >> BACKEND-CONTEXT.md`, never overwrite. |

## Why staging in pipeline repo

This Claude session can only push to `benzn-tech/fieldsight-pipeline`. Staging the UI-repo files here gives you a single reviewable diff in the pipeline PR before applying them to the UI repo. Once the UI repo's mirror is committed, this `docs/ui-repo-staging/` directory can be deleted in a follow-up commit (or kept as a reference of what went out).
