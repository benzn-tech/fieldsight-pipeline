-- 0012: folder_name becomes GLOBALLY unique (was unique per company, 0007).
-- The shared-lake pipeline routes S3 objects to a tenant by folder_name alone
-- (users/{folder}/..., reports/{date}/{folder}/...) — two companies claiming
-- one folder would silently cross-attribute data. Fail loudly at onboarding
-- instead. Additive-only (shared-Aurora rule); safe on current data (single
-- company today).
CREATE UNIQUE INDEX idx_users_folder_global ON users (folder_name) WHERE folder_name IS NOT NULL;
