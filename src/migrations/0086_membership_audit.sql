-- Project-owned tenancy, final review F8: who put whom on whose project.
-- One row per external-member add / revive / re-role / archive, written in the same
-- transaction as the membership change. Nothing reads it in-product; it exists so the
-- question "who gave this person access to our project, and when" has an answer.
-- target_user_id / actor_user_id / site_id carry no ON DELETE cascade on purpose: the
-- trail must outlive the people and projects it records, so they are soft references.
CREATE TABLE IF NOT EXISTS membership_audit (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  at              timestamptz NOT NULL DEFAULT clock_timestamp(),
  action          text NOT NULL,
  actor_user_id   uuid,
  actor_role      text,
  target_user_id  uuid NOT NULL,
  site_id         uuid NOT NULL,
  site_company_id uuid,
  role            text,
  membership_id   uuid
);
CREATE INDEX IF NOT EXISTS idx_membership_audit_site ON membership_audit (site_id, at);
CREATE INDEX IF NOT EXISTS idx_membership_audit_target ON membership_audit (target_user_id, at);
