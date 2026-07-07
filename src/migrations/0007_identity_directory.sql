-- 0007: identity directory consolidation (Phase 1) — sites.slug + users.folder_name/kind,
-- cognito_sub nullable for non-login (field_only) directory entries. Backfill happens in seed.
ALTER TABLE sites ADD COLUMN slug text;
CREATE UNIQUE INDEX idx_sites_company_slug ON sites (company_id, slug);
ALTER TABLE users ALTER COLUMN cognito_sub DROP NOT NULL;
ALTER TABLE users ADD COLUMN folder_name text;
ALTER TABLE users ADD COLUMN kind text NOT NULL DEFAULT 'login';
CREATE UNIQUE INDEX idx_users_company_folder ON users (company_id, folder_name);
