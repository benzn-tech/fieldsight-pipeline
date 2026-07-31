# Backfill sites.slug (org-api-created sites, NULL slug — 7/11 in prod)

Context: sites created via `POST /api/org/sites` before Task 9 (WS4) got no
`slug` — only the seed/import path called `set_slug`. WS4's UUID-based search
already works slug-free; this backfill is for the `&site=<slug>` deep-link
selector-sync nicety on the reports side. `idx_sites_company_slug` is a real
`UNIQUE(company_id, slug)` index (Postgres allows unlimited NULLs, but two
non-NULL slugs colliding in the same company will fail the UPDATE) — dedup
per company BEFORE applying.

## 1. Enumerate NULL-slug sites (prod DB, Data API)
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT id, name, company_id FROM sites WHERE slug IS NULL ORDER BY company_id, name" --region ap-southeast-2

## 2. Compute kebab(name) per row (same rule as `_slugify` in src/lambda_org_api.py)
# lowercase; run of non a-z0-9 -> single '-'; strip leading/trailing '-'.
# e.g. "UC PK" -> "uc-pk", "SB1108 Ellesmere College" -> "sb1108-ellesmere-college".

## 3. Per-company uniqueness check BEFORE applying — two things to check:
#   a) collisions among the NULL-slug rows themselves (same company, same kebab name)
#      -> append -2, -3, … to the later row(s), same rule as _unique_site_slug.
#   b) collision against an EXISTING non-NULL slug already in that company
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT company_id, slug FROM sites WHERE company_id = '<company_id>' AND slug = '<candidate-slug>'" --region ap-southeast-2
# if this returns a row, use <candidate-slug>-2 (and re-check) for that site instead.

## 4. Apply, one row at a time (idempotent — WHERE ... AND slug IS NULL guards re-runs)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "UPDATE sites SET slug = '<kebab-name>' WHERE id = '<site-id>' AND slug IS NULL" --region ap-southeast-2

## 5. Verify
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" --database fieldsight \
  --sql "SELECT count(*) total, count(slug) with_slug FROM sites" --region ap-southeast-2
# with_slug should now equal total (or total minus any sites intentionally left slug-less).
