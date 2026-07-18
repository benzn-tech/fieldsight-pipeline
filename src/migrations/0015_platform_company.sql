-- D6: dedicated platform company so a platform_admin keeps company_id NOT NULL
-- (cross-company visibility lives ONLY in visible_scope's branch, never in a
-- null company pin). Idempotent -- safe to re-run. NOTE: this is a DATA SEED,
-- not a schema change: users.global_role is plain text with no CHECK/enum
-- (0002_core_relational.sql:16), so the new roles regional_manager/
-- platform_admin need NO migration -- only this row does. Promoting the vendor
-- account to platform_admin + reparenting it to this company is a manual/seed
-- op (not code), gated on this row existing.
INSERT INTO companies (name, industry)
SELECT 'FieldSight-platform', 'platform'
WHERE NOT EXISTS (SELECT 1 FROM companies WHERE name = 'FieldSight-platform');
