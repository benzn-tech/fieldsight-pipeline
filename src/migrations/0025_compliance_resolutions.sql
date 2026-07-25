-- src/migrations/0025_compliance_resolutions.sql
-- Durable safety/quality "resolved" state -- retires the unauthenticated
-- DynamoDB check-off overlay (POST /api/actions/toggle) for compliance rows.
-- Spec 2026-07-26 §1.
--
-- Keyed on a RE-EXTRACTION-STABLE natural identity, NOT on any regenerated DB
-- uuid or positional topic index (those `gen_random_uuid()`-regenerate or
-- reorder on the nightly delete+reinsert re-extraction, which is exactly why
-- the legacy overlay's positional key drifts):
--   (company_id, site_id, report_date, domain, user_folder, content_hash)
-- where content_hash = sha256(normalize(displayed row text)); see content_hash.py.
-- user_folder is IN the key (parsed from the preserved source_s3_key): a
-- collision then requires the SAME recorder's identical-text items on one
-- site/day, not any recorder's.
--
-- Additive only: no change to existing tables, no FK from findings/topics into
-- this table or back. The decoupling from the regenerated uuids is the point.
CREATE TABLE compliance_resolutions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id        uuid NOT NULL REFERENCES sites(id)     ON DELETE CASCADE,
  report_date    date NOT NULL,
  user_folder    text NOT NULL,                 -- recorder folder from source_s3_key (stable)
  domain         text NOT NULL CHECK (domain IN ('safety','quality')),
  content_hash   text NOT NULL,                 -- sha256(normalize(row text)); see content_hash.py
  content_sample text NOT NULL,                 -- normalized text: debug/audit + parity fallback
  resolved       boolean NOT NULL DEFAULT true, -- true=resolved/closed, false=reopened (tombstone)
  resolved_by    uuid REFERENCES users(id),     -- who last set state (nullable: legacy/backfill)
  resolved_at    timestamptz NOT NULL DEFAULT now(),
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, site_id, report_date, domain, user_folder, content_hash)
);
CREATE INDEX idx_compres_range
  ON compliance_resolutions (company_id, site_id, domain, report_date);
