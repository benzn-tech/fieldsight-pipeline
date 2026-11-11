-- 0093: per-site task numbers ("#69") for action items
-- (spec docs/superpowers/specs/2026-11-01-background-action-agent-design.md §14 D1).
--
-- One row per action_items.stable_id, written the first time that stable_id is inserted
-- (lambda_item_writer / lambda_ingest, after carry-forward, through
-- repositories.action_items.assign_task_no) or by scripts/backfill_task_numbers.py.
-- A carried stable_id keeps its row, so the number travels across re-extraction.
--
-- Numbers are NEVER reused. The counter is max(task_no)+1 per site taken under
-- pg_advisory_xact_lock(hashtext('task_no:' || site_id)), and nothing deletes from this
-- table: deleting action_items rows (or the whole topic) leaves the number taken. No FK to
-- action_items (stable_id is not unique there -- every superseded pass shares it) and no FK
-- to sites: a CASCADE would free numbers, and a plain FK would block a site delete.
--
-- Additive only. sam deploy runs BEFORE this migration, so the existing write paths and
-- org-api payloads touch this table only inside a savepoint that treats 42P01 as
-- "not migrated yet". Idempotent: safe to run twice. No GRANT (single DB role).
CREATE TABLE IF NOT EXISTS action_task_numbers (
  stable_id   uuid PRIMARY KEY,
  site_id     uuid NOT NULL,
  task_no     integer NOT NULL CHECK (task_no > 0),
  created_at  timestamptz NOT NULL DEFAULT now(),
  -- Also the index the per-site max(task_no) and the (site_id, task_no) lookup use.
  CONSTRAINT action_task_numbers_site_task_no_key UNIQUE (site_id, task_no)
);
