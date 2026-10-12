-- Sub-project 4 (design 2026-10-12): who owns a lake object, as data. One row per S3 key
-- under a tenant prefix. company_id/site_id/user_id are SOFT references (no FK, no cascade)
-- on purpose: the record must outlive the people and projects it describes, like
-- membership_audit. company_id NULL = unresolved (never guessed); the backfill lists them.
-- A row is moved between companies by one UPDATE ... WHERE site_id (sites.company_id change);
-- the S3 object is never touched.
-- reason (controller ruling C2): why company_id is NULL when the resolver decided so on
-- purpose, e.g. 'split_day' (a whole-day document of a day spanning several companies).
CREATE TABLE IF NOT EXISTS lake_objects (
  s3_key       text PRIMARY KEY,
  company_id   uuid,
  site_id      uuid,
  user_id      uuid,
  session_base text,
  folder       text,
  day          date,
  kind         text NOT NULL,
  size_bytes   bigint,
  source       text NOT NULL CHECK (source IN ('upload','manifest','backfill')),
  reason       text,
  -- When the S3 object was last written (backfill: S3 LastModified; drain: the manifest line's
  -- t). NULL for rows minted at upload time. Report history shows this, not updated_at (the
  -- time the index learned of the object).
  object_modified_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  deleted_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_lake_objects_company      ON lake_objects (company_id);
CREATE INDEX IF NOT EXISTS idx_lake_objects_site         ON lake_objects (site_id);
CREATE INDEX IF NOT EXISTS idx_lake_objects_company_kind ON lake_objects (company_id, kind);
CREATE INDEX IF NOT EXISTS idx_lake_objects_kind_day     ON lake_objects (kind, day);
CREATE INDEX IF NOT EXISTS idx_lake_objects_unresolved   ON lake_objects (s3_key) WHERE company_id IS NULL;
