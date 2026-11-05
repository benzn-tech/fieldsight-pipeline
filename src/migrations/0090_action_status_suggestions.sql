-- 0090: status suggestions from speech (spec 2026-10-27, v1 same-session).
-- docs/superpowers/specs/2026-10-27-action-status-suggestions-design.md §4.2
--
-- The extractor may propose that an action item was reported done / closed /
-- blocked / started in the same session. A proposal is stored here and never
-- written to action_items.status: a person accepts it (the normal audited
-- PATCH) or dismisses it.
--
-- Keyed on the item's stable_id, not action_items.id: rows are superseded on
-- every re-extraction, the identity is what survives. UNIQUE (stable_id,
-- suggested_status) with ON CONFLICT DO NOTHING makes a re-run idempotent and
-- means a dismissed suggestion is never shown again for the same item+status.
-- `run` is the writer's pass id ("<tier>:<extracted_at>"); live and final share
-- one extraction key, so the key alone cannot say which pass proposed it.
CREATE TABLE IF NOT EXISTS action_status_suggestions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id          uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  stable_id        uuid NOT NULL,
  suggested_status text NOT NULL
                   CHECK (suggested_status IN ('done','closed','blocked','in_progress')),
  quote            text,
  segment_key      text,
  offset_sec       double precision,
  run              text NOT NULL,
  state            text NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','accepted','dismissed')),
  resolved_by      uuid REFERENCES users(id),
  resolved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (stable_id, suggested_status)
);

CREATE INDEX IF NOT EXISTS idx_status_suggestions_pending
  ON action_status_suggestions (stable_id) WHERE state = 'pending';
