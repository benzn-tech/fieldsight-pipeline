-- 0092: company offboarding -- companies.status plus its audit trail
-- (spec docs/superpowers/specs/2026-11-01-company-offboarding-design.md §4).
--
-- status: 'active' | 'suspended' | 'exited'. Both non-active states block the company's
-- people (enforced in code by tenancy_status, behind COMPANY_STATUS_ENFORCE); data is kept.
-- exited_at is the FIRST exit and is kept on reactivation; billing_stopped_at is set by
-- the operator on exit (or on request for a suspension).
--
-- Additive only. sam deploy runs BEFORE this migration, so code must read these columns
-- through tenancy_status's savepoint-guarded helpers (missing column -> 'active'), never
-- through users._COLS / get_user_by_sub.
-- Idempotent: safe to run twice. No GRANT (single DB role).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS status_changed_at timestamptz;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS status_reason text;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS billing_stopped_at timestamptz;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS exited_at timestamptz;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'companies_status_check' AND conrelid = 'companies'::regclass
  ) THEN
    ALTER TABLE companies
      ADD CONSTRAINT companies_status_check
      CHECK (status IN ('active','suspended','exited'));
  END IF;
END $$;

-- One row per status change, append-only, written in the same transaction as the change.
-- actor_user_id is a soft reference (no FK): the trail must outlive the people it records
-- (same reasoning as membership_audit, 0086).
CREATE TABLE IF NOT EXISTS company_status_events (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id),
  from_status   text,
  to_status     text NOT NULL,
  reason        text,
  actor_user_id uuid,
  actor_sub     text,
  at            timestamptz NOT NULL DEFAULT now(),
  detail        jsonb
);
CREATE INDEX IF NOT EXISTS idx_company_status_events_company
  ON company_status_events (company_id, at);

-- Cognito side effects of a status change, one row per user per action, so the async
-- worker's retries are idempotent and their outcome is visible.
CREATE TABLE IF NOT EXISTS company_status_cognito (
  event_id   uuid NOT NULL REFERENCES company_status_events(id),
  user_sub   text NOT NULL,
  action     text NOT NULL CHECK (action IN ('disable','signout','enable')),
  state      text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
  attempts   int NOT NULL DEFAULT 0,
  last_error text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, user_sub, action)
);
CREATE INDEX IF NOT EXISTS idx_company_status_cognito_pending
  ON company_status_cognito (state) WHERE state = 'pending';
